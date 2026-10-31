"""Request an upload ticket and register one original. Does not commit."""

import os
import re
import uuid
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session
from sqlalchemy.orm.util import identity_key

from sinuo.application.assets.gc import (
    install_reserved_original,
    place_upload_pin,
    reserve_generation,
)
from sinuo.application.assets.preview import preview_disposition
from sinuo.application.auth.authorizer import SessionAuthorizer
from sinuo.domain.auth.principal import Principal
from sinuo.domain.auth.resource import ResourceRef
from sinuo.domain.public_contract import SENSITIVITY_LEVELS, Action
from sinuo.domain.schema_types import canonical_audit_payload
from sinuo.infrastructure.assets.generations import ObjectPinRow, StoredObjectRow
from sinuo.infrastructure.assets.objects import object_relative, original_key
from sinuo.infrastructure.assets.paths import is_storage_segment, resolve_contained
from sinuo.infrastructure.assets.records import UploadTicketRow
from sinuo.infrastructure.assets.staging import digest_file
from sinuo.infrastructure.domain_models import (
    Account,
    Artifact,
    ArtifactVersion,
    AuditEvent,
    Project,
)
from sinuo.infrastructure.models import OutboxEvent

EVENT_TYPE = "artifact.version_registered"
EVENT_SCHEMA_VERSION = 1
_UPLOAD_PIN_TTL = 3600.0
_EVENT_FIELDS = (
    "project_id",
    "artifact_id",
    "artifact_version_id",
    "content_hash",
    "generation_id",
    "byte_size",
)
_HASH = re.compile(r"^[0-9a-f]{64}$")


class AssetRejected(Exception):
    """The ticket cannot be recorded."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


class AttemptMissing(Exception):
    """The attempt path is not a file. The ticket is left open."""


class RegistrationConflict(Exception):
    """Project revision moved before the original could be registered."""


@dataclass(frozen=True)
class UploadTicket:
    upload_id: str
    project_id: str
    artifact_id: str
    expected_size: int
    declared_hash: str


@dataclass(frozen=True)
class RegistrationOutcome:
    registered: bool
    upload_id: str
    reason: str | None
    artifact_version_id: str | None
    content_hash: str | None
    generation_id: str | None
    byte_size: int | None
    object_key: str | None
    outbox_event_id: str | None
    preview_state: str | None
    preview_reason: str | None
    preview_generated: bool


def request_upload_ticket(
    session: Session,
    *,
    project_id: str,
    artifact_id: str,
    expected_size: int,
    declared_hash: str,
    media_type: str,
    license_name: str,
    sensitivity: str,
    ai_processing_allowed: int,
    account_id: str,
    now: float,
    byte_limit: int | None = None,
    expires_at: float | None = None,
) -> UploadTicket:
    """Insert one open ticket. Does not commit and does not write bytes.

    expires_at is optional. Null means the ticket does not expire.
    """

    if not is_storage_segment(project_id) or not is_storage_segment(artifact_id):
        raise AssetRejected("identifier")
    if type(expected_size) is not int or expected_size < 0:
        raise AssetRejected("expected_size")
    if byte_limit is not None:
        if type(byte_limit) is not int or byte_limit < 0:
            raise AssetRejected("byte_limit")
        if expected_size > byte_limit:
            raise AssetRejected("byte_limit")
    if not isinstance(declared_hash, str) or _HASH.fullmatch(declared_hash) is None:
        raise AssetRejected("declared_hash")
    if sensitivity not in SENSITIVITY_LEVELS:
        raise AssetRejected("sensitivity")
    if type(ai_processing_allowed) is not int or ai_processing_allowed not in (0, 1):
        raise AssetRejected("ai_processing_allowed")
    if type(now) is not float:
        raise AssetRejected("now")
    if expires_at is not None and type(expires_at) is not float:
        raise AssetRejected("expires_at")
    stored_media = _required_text(media_type, limit=255)
    stored_license = _required_text(license_name, limit=200)
    if ".." in stored_media or ".." in stored_license:
        raise AssetRejected("text")
    project = session.get(Project, project_id)
    artifact = session.get(Artifact, artifact_id)
    account = session.get(Account, account_id)
    if project is None or artifact is None or artifact.project_id != project_id or account is None:
        raise AssetRejected("owner")
    upload_id = str(uuid.uuid4())
    session.add(
        UploadTicketRow(
            upload_id=upload_id,
            project_id=project_id,
            artifact_id=artifact_id,
            expected_size=expected_size,
            declared_hash=declared_hash,
            media_type=stored_media,
            license=stored_license,
            sensitivity=sensitivity,
            ai_processing_allowed=ai_processing_allowed,
            created_by_account_id=account_id,
            status="open",
            failure_reason=None,
            artifact_version_id=None,
            generation_id=None,
            created_at=now,
            closed_at=None,
            expires_at=expires_at,
        )
    )
    session.flush()
    return UploadTicket(
        upload_id=upload_id,
        project_id=project_id,
        artifact_id=artifact_id,
        expected_size=expected_size,
        declared_hash=declared_hash,
    )


def register_original(
    session: Session,
    *,
    upload_id: str,
    temp_root: Path,
    relative_path: str,
    objects_root: Path,
    now: float,
    converter_installed: bool | None = None,
    principal: Principal | None = None,
    authorizer: SessionAuthorizer | None = None,
    byte_limit: int | None = None,
) -> RegistrationOutcome:
    """Re-read the ticket, check the server hash, then pin, install, and register.

    Does not commit. The attempt is hashed once before any reservation. Hash
    mismatch, a declared length other than the byte count, a size above
    byte_limit, or an expires_at that is set and is not later than now marks
    the ticket rejected and does not insert a referable original or a stored
    object. When authorizer is set, artifact.upload is
    checked before that insert. When principal is set, the ticket author must
    be that account; a mismatch leaves the ticket open. The same project may
    reuse one ready object for the same bytes. Reuse consumes that upload pin
    only while its fencing token is still the token captured from the ready
    object when the pin was placed. A mismatch raises and the caller rolls
    the transaction back. A different project always gets its own object. The
    version id is always new. On success, one audit_events row is added in
    this same session before returning. Hash mismatch, size mismatch,
    authorization failure, and an expired ticket do not. expires_at null
    does not expire. A value equal to now is expired, the same boundary as
    an upload pin. The audit payload has no filesystem path and no original
    bytes.
    """

    if type(now) is not float:
        raise AssetRejected("now")
    if (principal is None) != (authorizer is None):
        raise AssetRejected("authorizer")
    attempt = resolve_contained(temp_root, relative_path)
    try:
        server_hash, server_size = digest_file(attempt)
    except FileNotFoundError as error:
        raise AttemptMissing(relative_path) from error
    ticket = session.get(UploadTicketRow, upload_id)
    if ticket is None:
        return _unregistered(upload_id, "missing_ticket")
    if authorizer is not None and principal is not None:
        authorizer.authorize(
            Action.ARTIFACT_UPLOAD,
            ResourceRef(
                project_id=ticket.project_id,
                scope_type="artifact",
                artifact_id=ticket.artifact_id,
            ),
            principal,
        )
    if ticket.status != "open":
        return _unregistered(ticket.upload_id, "ticket_closed")
    if principal is not None and ticket.created_by_account_id != principal.account_id:
        return _unregistered(ticket.upload_id, "ticket_owner")
    if ticket.expires_at is not None and ticket.expires_at <= now:
        return _reject(session, ticket, "ticket_expired", now)
    if byte_limit is not None:
        if type(byte_limit) is not int or byte_limit < 0:
            raise AssetRejected("byte_limit")
        if server_size > byte_limit:
            return _reject(session, ticket, "byte_limit", now)
    if server_size != ticket.expected_size:
        return _reject(session, ticket, "expected_size_mismatch", now)
    if server_hash != ticket.declared_hash:
        return _reject(session, ticket, "declared_hash_mismatch", now)
    project_id = ticket.project_id
    artifact_id = ticket.artifact_id
    actor_id = ticket.created_by_account_id
    ready = _ready_original(
        session,
        project_id=project_id,
        content_hash=server_hash,
        byte_size=server_size,
    )
    if ready is not None:
        ready_object_id = ready.object_id
        ready_generation_id = ready.generation_id
        ready_fencing_token = ready.fencing_token
        pinned = place_upload_pin(
            session,
            object_id=ready_object_id,
            upload_id=upload_id,
            now=now,
            expires_at=now + _UPLOAD_PIN_TTL,
        )
        pin_id = pinned.pin_id
        if pinned.placed and pin_id is not None:
            object_key = original_key(project_id, server_hash, ready_generation_id)
            version_id, outbox_event_id, project_revision = _write_version(
                session,
                upload_id=upload_id,
                project_id=project_id,
                server_hash=server_hash,
                server_size=server_size,
                generation_id=ready_generation_id,
                object_key=object_key,
                now=now,
            )
            _consume_upload_pin(
                session,
                pin_id=pin_id,
                object_id=ready_object_id,
                project_id=project_id,
                now=now,
                fencing_token=ready_fencing_token,
            )
            media_type = _close_ticket(
                session,
                upload_id=upload_id,
                project_id=project_id,
                version_id=version_id,
                generation_id=ready_generation_id,
                now=now,
            )
            _release_attempt(
                attempt,
                objects_root,
                project_id,
                server_hash,
                ready_generation_id,
            )
            _record_registration_audit(
                session,
                actor_id=actor_id,
                project_id=project_id,
                artifact_id=artifact_id,
                artifact_version_id=version_id,
                content_hash=server_hash,
                generation_id=ready_generation_id,
                byte_size=server_size,
                project_revision=project_revision,
                now=now,
            )
            return _registered_outcome(
                upload_id=upload_id,
                version_id=version_id,
                server_hash=server_hash,
                server_size=server_size,
                generation_id=ready_generation_id,
                object_key=object_key,
                outbox_event_id=outbox_event_id,
                media_type=media_type,
                converter_installed=converter_installed,
            )
    reserved = reserve_generation(
        session,
        project_id=project_id,
        sha256=server_hash,
        byte_size=server_size,
        now=now,
    )
    pinned = place_upload_pin(
        session,
        object_id=reserved.object_id,
        upload_id=upload_id,
        now=now,
        expires_at=now + _UPLOAD_PIN_TTL,
    )
    pin_id = pinned.pin_id
    if not pinned.placed or pin_id is None:
        raise RegistrationConflict(project_id)
    installed = install_reserved_original(
        session,
        object_id=reserved.object_id,
        pin_id=pin_id,
        source=attempt,
        objects_root=objects_root,
        now=now,
    )
    object_key = original_key(project_id, server_hash, reserved.generation_id)
    if not installed.installed or installed.object_key != object_key:
        raise RegistrationConflict(project_id)
    version_id, outbox_event_id, project_revision = _write_version(
        session,
        upload_id=upload_id,
        project_id=project_id,
        server_hash=server_hash,
        server_size=server_size,
        generation_id=reserved.generation_id,
        object_key=object_key,
        now=now,
    )
    media_type = _close_ticket(
        session,
        upload_id=upload_id,
        project_id=project_id,
        version_id=version_id,
        generation_id=reserved.generation_id,
        now=now,
    )
    _record_registration_audit(
        session,
        actor_id=actor_id,
        project_id=project_id,
        artifact_id=artifact_id,
        artifact_version_id=version_id,
        content_hash=server_hash,
        generation_id=reserved.generation_id,
        byte_size=server_size,
        project_revision=project_revision,
        now=now,
    )
    return _registered_outcome(
        upload_id=upload_id,
        version_id=version_id,
        server_hash=server_hash,
        server_size=server_size,
        generation_id=reserved.generation_id,
        object_key=object_key,
        outbox_event_id=outbox_event_id,
        media_type=media_type,
        converter_installed=converter_installed,
    )


def _reject(
    session: Session,
    ticket: UploadTicketRow,
    reason: str,
    now: float,
) -> RegistrationOutcome:
    ticket.status = "rejected"
    ticket.failure_reason = reason
    ticket.closed_at = now
    session.flush()
    return _unregistered(ticket.upload_id, reason)


def _unregistered(upload_id: str, reason: str) -> RegistrationOutcome:
    return RegistrationOutcome(
        registered=False,
        upload_id=upload_id,
        reason=reason,
        artifact_version_id=None,
        content_hash=None,
        generation_id=None,
        byte_size=None,
        object_key=None,
        outbox_event_id=None,
        preview_state=None,
        preview_reason=None,
        preview_generated=False,
    )


def _required_text(value: str, *, limit: int) -> str:
    if not isinstance(value, str):
        raise AssetRejected("text")
    stripped = value.strip()
    if not stripped or len(stripped) > limit or any(ord(char) < 32 for char in stripped):
        raise AssetRejected("text")
    return stripped


def _advance_project_revision(session: Session, project_id: str) -> int:
    current = session.scalar(select(Project.revision).where(Project.id == project_id))
    if type(current) is not int:
        raise RegistrationConflict(project_id)
    result = session.execute(
        update(Project)
        .where(Project.id == project_id, Project.revision == current)
        .values(revision=current + 1),
        execution_options={"synchronize_session": False},
    )
    if result.rowcount != 1:
        raise RegistrationConflict(project_id)
    cached = session.identity_map.get(identity_key(Project, (project_id,)))
    if cached is not None:
        session.expire(cached)
    return current + 1


def _next_version_no(session: Session, project_id: str, artifact_id: str) -> int:
    highest = session.scalar(
        select(func.max(ArtifactVersion.version_no)).where(
            ArtifactVersion.project_id == project_id,
            ArtifactVersion.artifact_id == artifact_id,
        )
    )
    if highest is None:
        return 1
    if type(highest) is not int:
        raise RegistrationConflict(artifact_id)
    return highest + 1


def _event_payload(
    *,
    project_id: str,
    artifact_id: str,
    artifact_version_id: str,
    content_hash: str,
    generation_id: str,
    byte_size: int,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "project_id": project_id,
        "artifact_id": artifact_id,
        "artifact_version_id": artifact_version_id,
        "content_hash": content_hash,
        "generation_id": generation_id,
        "byte_size": byte_size,
    }
    if tuple(payload) != _EVENT_FIELDS:
        raise RuntimeError("artifact.version_registered payload drifted")
    return payload


def _ready_original(
    session: Session,
    *,
    project_id: str,
    content_hash: str,
    byte_size: int,
) -> StoredObjectRow | None:
    """Return one same-project stored object that is already ready.

    Reserved, claimed, deleted, and other projects are not candidates. The
    caller allocates a new generation when this returns none.
    """

    return session.scalars(
        select(StoredObjectRow)
        .where(
            StoredObjectRow.project_id == project_id,
            StoredObjectRow.sha256 == content_hash,
            StoredObjectRow.byte_size == byte_size,
            StoredObjectRow.state == "ready",
        )
        .order_by(StoredObjectRow.created_at)
    ).first()


def _write_version(
    session: Session,
    *,
    upload_id: str,
    project_id: str,
    server_hash: str,
    server_size: int,
    generation_id: str,
    object_key: str,
    now: float,
) -> tuple[str, str, int]:
    live = session.get(UploadTicketRow, upload_id)
    if live is None or live.status != "open":
        raise RegistrationConflict(project_id)
    version_id = str(uuid.uuid4())
    outbox_event_id = str(uuid.uuid4())
    project_revision = _advance_project_revision(session, live.project_id)
    version_no = _next_version_no(session, live.project_id, live.artifact_id)
    session.add(
        ArtifactVersion(
            id=version_id,
            project_id=live.project_id,
            artifact_id=live.artifact_id,
            version_no=version_no,
            content_hash=server_hash,
            size_bytes=server_size,
            media_type=live.media_type,
            object_key=object_key,
            license=live.license,
            sensitivity=live.sensitivity,
            ai_processing_allowed=live.ai_processing_allowed,
            project_revision=project_revision,
            created_by_account_id=live.created_by_account_id,
            created_at=now,
        )
    )
    session.add(
        OutboxEvent(
            event_id=outbox_event_id,
            project_id=live.project_id,
            event_type=EVENT_TYPE,
            schema_version=EVENT_SCHEMA_VERSION,
            payload=_event_payload(
                project_id=live.project_id,
                artifact_id=live.artifact_id,
                artifact_version_id=version_id,
                content_hash=server_hash,
                generation_id=generation_id,
                byte_size=server_size,
            ),
            created_at=now,
        )
    )
    session.flush()
    return version_id, outbox_event_id, project_revision


def _record_registration_audit(
    session: Session,
    *,
    actor_id: str,
    project_id: str,
    artifact_id: str,
    artifact_version_id: str,
    content_hash: str,
    generation_id: str,
    byte_size: int,
    project_revision: int,
    now: float,
) -> None:
    """Insert one registration audit row. Does not commit."""

    session.add(
        AuditEvent(
            id=str(uuid.uuid4()),
            project_id=project_id,
            actor_id=actor_id,
            command=EVENT_TYPE,
            subject_type="artifact_version",
            subject_id=artifact_version_id,
            project_revision=project_revision,
            payload=canonical_audit_payload(
                _event_payload(
                    project_id=project_id,
                    artifact_id=artifact_id,
                    artifact_version_id=artifact_version_id,
                    content_hash=content_hash,
                    generation_id=generation_id,
                    byte_size=byte_size,
                )
            ),
            created_at=now,
        )
    )
    session.flush()


def _consume_upload_pin(
    session: Session,
    *,
    pin_id: str,
    object_id: str,
    project_id: str,
    now: float,
    fencing_token: int,
) -> None:
    if type(fencing_token) is not int:
        raise RegistrationConflict(project_id)
    consumed = session.execute(
        update(ObjectPinRow)
        .where(
            ObjectPinRow.pin_id == pin_id,
            ObjectPinRow.object_id == object_id,
            ObjectPinRow.purpose == "upload",
            ObjectPinRow.consumed_at.is_(None),
            ObjectPinRow.expires_at > now,
            ObjectPinRow.fencing_token == fencing_token,
        )
        .values(consumed_at=now),
        execution_options={"synchronize_session": False},
    )
    if consumed.rowcount != 1:
        raise RegistrationConflict(project_id)
    cached = session.get(ObjectPinRow, pin_id)
    if cached is not None:
        session.expire(cached)


def _close_ticket(
    session: Session,
    *,
    upload_id: str,
    project_id: str,
    version_id: str,
    generation_id: str,
    now: float,
) -> str:
    live = session.get(UploadTicketRow, upload_id)
    if live is None or live.status != "open":
        raise RegistrationConflict(project_id)
    media_type = live.media_type
    live.status = "registered"
    live.artifact_version_id = version_id
    live.generation_id = generation_id
    live.closed_at = now
    session.flush()
    return media_type


def _registered_outcome(
    *,
    upload_id: str,
    version_id: str,
    server_hash: str,
    server_size: int,
    generation_id: str,
    object_key: str,
    outbox_event_id: str,
    media_type: str,
    converter_installed: bool | None,
) -> RegistrationOutcome:
    preview = preview_disposition(media_type, installed=converter_installed)
    return RegistrationOutcome(
        registered=True,
        upload_id=upload_id,
        reason=None,
        artifact_version_id=version_id,
        content_hash=server_hash,
        generation_id=generation_id,
        byte_size=server_size,
        object_key=object_key,
        outbox_event_id=outbox_event_id,
        preview_state=preview.state,
        preview_reason=preview.reason,
        preview_generated=preview.generated,
    )


def _release_attempt(
    attempt: Path,
    objects_root: Path,
    project_id: str,
    content_hash: str,
    generation_id: str,
) -> None:
    stored = resolve_contained(
        objects_root,
        object_relative(project_id, content_hash, generation_id),
    )
    if attempt.resolve() == stored.resolve():
        return
    try:
        os.unlink(attempt)
    except OSError:
        if not stored.is_file():
            raise
