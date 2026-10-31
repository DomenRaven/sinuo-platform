"""Upload pins and generation GC. Does not write a preview or hash inside the lock."""

import os
import re
import uuid
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import exists, select, update
from sqlalchemy.orm import Session

from sinuo.infrastructure.assets.generations import ObjectPinRow, StoredObjectRow
from sinuo.infrastructure.assets.objects import install_original, object_relative, original_key
from sinuo.infrastructure.assets.paths import PathEscape, is_storage_segment, resolve_contained
from sinuo.infrastructure.domain_models import ArtifactVersion

_HASH = re.compile(r"^[0-9a-f]{64}$")
_OPENABLE = frozenset({"reserved", "ready"})
_TERMINAL = frozenset({"gc_claimed", "deleted"})


class GenerationRejected(Exception):
    """The generation arguments cannot be stored."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


@dataclass(frozen=True)
class GenerationReservation:
    object_id: str
    project_id: str
    generation_id: str
    sha256: str
    fencing_token: int
    byte_size: int


@dataclass(frozen=True)
class UploadPin:
    placed: bool
    object_id: str
    pin_id: str | None
    reason: str | None


@dataclass(frozen=True)
class GcClaim:
    claimed: bool
    object_id: str
    generation_id: str | None
    fencing_token: int | None
    reason: str | None


@dataclass(frozen=True)
class InstallResult:
    installed: bool
    object_id: str
    object_key: str | None
    reason: str | None


@dataclass(frozen=True)
class DeleteResult:
    deleted: bool
    object_id: str
    generation_id: str | None
    reason: str | None


def reserve_generation(
    session: Session,
    *,
    project_id: str,
    sha256: str,
    byte_size: int,
    now: float,
    generation_id: str | None = None,
) -> GenerationReservation:
    """Insert one reserved generation. Does not write bytes or commit."""

    if not is_storage_segment(project_id):
        raise GenerationRejected("project_id")
    if not isinstance(sha256, str) or _HASH.fullmatch(sha256) is None:
        raise GenerationRejected("sha256")
    if type(byte_size) is not int or byte_size < 0:
        raise GenerationRejected("byte_size")
    _require_time(now)
    chosen = str(uuid.uuid4()) if generation_id is None else generation_id
    if not is_storage_segment(chosen):
        raise GenerationRejected("generation_id")
    object_id = str(uuid.uuid4())
    session.add(
        StoredObjectRow(
            object_id=object_id,
            project_id=project_id,
            sha256=sha256,
            generation_id=chosen,
            state="reserved",
            fencing_token=0,
            byte_size=byte_size,
            created_at=now,
        )
    )
    session.flush()
    return GenerationReservation(
        object_id=object_id,
        project_id=project_id,
        generation_id=chosen,
        sha256=sha256,
        fencing_token=0,
        byte_size=byte_size,
    )


def place_upload_pin(
    session: Session,
    *,
    object_id: str,
    upload_id: str,
    now: float,
    expires_at: float,
) -> UploadPin:
    """Pin a reserved or ready generation. A claimed generation is left unchanged."""

    _require_time(now)
    _require_time(expires_at)
    if not isinstance(upload_id, str) or not is_storage_segment(upload_id):
        raise GenerationRejected("upload_id")
    if expires_at <= now:
        return UploadPin(False, object_id, None, "expiry")
    row = session.get(StoredObjectRow, object_id)
    if row is None or row.state not in _OPENABLE:
        return UploadPin(False, object_id, None, "generation_unavailable")
    pin_id = str(uuid.uuid4())
    session.add(
        ObjectPinRow(
            pin_id=pin_id,
            object_id=row.object_id,
            project_id=row.project_id,
            upload_id=upload_id,
            purpose="upload",
            expires_at=expires_at,
            consumed_at=None,
            fencing_token=row.fencing_token,
            created_at=now,
        )
    )
    session.flush()
    return UploadPin(True, row.object_id, pin_id, None)


def claim_generation(
    session: Session,
    *,
    object_id: str,
    now: float,
    retention_seconds: int,
) -> GcClaim:
    """CAS reserved/ready to gc_claimed when nothing live still holds the generation."""

    _require_time(now)
    if type(retention_seconds) is not int or retention_seconds < 0:
        raise GenerationRejected("retention_seconds")
    row = session.get(StoredObjectRow, object_id)
    if row is None:
        return GcClaim(False, object_id, None, None, "missing")
    token = row.fencing_token
    generation_id = row.generation_id
    project_id = row.project_id
    object_key = original_key(project_id, row.sha256, generation_id)
    cutoff = now - retention_seconds
    live_pin = exists(
        select(ObjectPinRow.pin_id).where(
            ObjectPinRow.object_id == object_id,
            ObjectPinRow.consumed_at.is_(None),
            ObjectPinRow.expires_at > now,
        )
    )
    referenced = exists(
        select(ArtifactVersion.id).where(
            ArtifactVersion.project_id == project_id,
            ArtifactVersion.object_key == object_key,
        )
    )
    result = session.execute(
        update(StoredObjectRow)
        .where(
            StoredObjectRow.object_id == object_id,
            StoredObjectRow.project_id == project_id,
            StoredObjectRow.state.in_(("reserved", "ready")),
            StoredObjectRow.fencing_token == token,
            StoredObjectRow.created_at <= cutoff,
            ~live_pin,
            ~referenced,
        )
        .values(state="gc_claimed", fencing_token=token + 1),
        execution_options={"synchronize_session": False},
    )
    session.expire(row)
    if result.rowcount == 1:
        return GcClaim(True, object_id, generation_id, token + 1, None)
    current = session.get(StoredObjectRow, object_id)
    if current is None:
        return GcClaim(False, object_id, None, None, "missing")
    return GcClaim(
        False,
        object_id,
        None,
        None,
        _claim_block(session, current, now=now, cutoff=cutoff, object_key=object_key),
    )


def install_reserved_original(
    session: Session,
    *,
    object_id: str,
    pin_id: str,
    source: Path,
    objects_root: Path,
    now: float,
) -> InstallResult:
    """Link a checked attempt only while its upload pin and reservation are live.

    A gc_claimed or deleted generation is not linked. The attempt file stays
    where the caller left it.
    """

    _require_time(now)
    row = session.get(StoredObjectRow, object_id)
    if row is None or row.state != "reserved":
        return InstallResult(False, object_id, None, "generation_unavailable")
    pin = session.get(ObjectPinRow, pin_id)
    if not _pin_matches(pin, row, now):
        return InstallResult(False, object_id, None, "generation_unavailable")
    if not source.is_file():
        return InstallResult(False, object_id, None, "attempt_missing")
    token = row.fencing_token
    key = install_original(source, objects_root, row.project_id, row.sha256, row.generation_id)
    state_update = session.execute(
        update(StoredObjectRow)
        .where(
            StoredObjectRow.object_id == object_id,
            StoredObjectRow.state == "reserved",
            StoredObjectRow.fencing_token == token,
        )
        .values(state="ready", fencing_token=token + 1),
        execution_options={"synchronize_session": False},
    )
    if state_update.rowcount != 1:
        raise GenerationRejected("install_conflict")
    consumed = session.execute(
        update(ObjectPinRow)
        .where(
            ObjectPinRow.pin_id == pin_id,
            ObjectPinRow.object_id == object_id,
            ObjectPinRow.purpose == "upload",
            ObjectPinRow.consumed_at.is_(None),
            ObjectPinRow.expires_at > now,
            ObjectPinRow.fencing_token == token,
        )
        .values(consumed_at=now),
        execution_options={"synchronize_session": False},
    )
    if consumed.rowcount != 1:
        raise GenerationRejected("pin_conflict")
    session.expire_all()
    return InstallResult(True, object_id, key, None)


def delete_claimed_generation(
    session: Session,
    *,
    object_id: str,
    fencing_token: int,
    objects_root: Path,
) -> DeleteResult:
    """Unlink only the generation captured by this claim, then mark it deleted.

    A stale token returns before any unlink. A newer generation is a different
    row and a different path, so this call cannot remove it.
    """

    if type(fencing_token) is not int:
        return DeleteResult(False, object_id, None, "stale_token")
    row = session.get(StoredObjectRow, object_id)
    if row is None:
        return DeleteResult(False, object_id, None, "missing")
    if row.fencing_token != fencing_token:
        return DeleteResult(False, object_id, None, "stale_token")
    if row.state not in _TERMINAL:
        return DeleteResult(False, object_id, None, "not_claimed")
    generation_id = row.generation_id
    project_id = row.project_id
    content_hash = row.sha256
    try:
        _unlink_generation(objects_root, project_id, content_hash, generation_id)
    except (OSError, PathEscape):
        return DeleteResult(False, object_id, generation_id, "unlink_failed")
    if row.state == "deleted":
        return DeleteResult(True, object_id, generation_id, None)
    result = session.execute(
        update(StoredObjectRow)
        .where(
            StoredObjectRow.object_id == object_id,
            StoredObjectRow.state == "gc_claimed",
            StoredObjectRow.fencing_token == fencing_token,
        )
        .values(state="deleted"),
        execution_options={"synchronize_session": False},
    )
    session.expire(row)
    if result.rowcount != 1:
        return DeleteResult(False, object_id, generation_id, "stale_token")
    return DeleteResult(True, object_id, generation_id, None)


def _claim_block(
    session: Session,
    row: StoredObjectRow,
    *,
    now: float,
    cutoff: float,
    object_key: str,
) -> str:
    if row.state not in _OPENABLE:
        return "state"
    if row.created_at > cutoff:
        return "retained"
    if _has_live_pin(session, row.object_id, now):
        return "pinned"
    referenced = session.scalar(
        select(ArtifactVersion.id).where(
            ArtifactVersion.project_id == row.project_id,
            ArtifactVersion.object_key == object_key,
        )
    )
    if referenced is not None:
        return "referenced"
    return "conflict"


def _has_live_pin(session: Session, object_id: str, now: float) -> bool:
    found = session.scalar(
        select(ObjectPinRow.pin_id).where(
            ObjectPinRow.object_id == object_id,
            ObjectPinRow.consumed_at.is_(None),
            ObjectPinRow.expires_at > now,
        )
    )
    return found is not None


def _pin_matches(pin: ObjectPinRow | None, row: StoredObjectRow, now: float) -> bool:
    if pin is None:
        return False
    return (
        pin.object_id == row.object_id
        and pin.purpose == "upload"
        and pin.consumed_at is None
        and pin.expires_at > now
        and pin.fencing_token == row.fencing_token
    )


def _unlink_generation(
    objects_root: Path,
    project_id: str,
    content_hash: str,
    generation_id: str,
) -> None:
    path = resolve_contained(objects_root, object_relative(project_id, content_hash, generation_id))
    if path.is_file():
        os.unlink(path)
    elif path.exists():
        raise OSError(generation_id)
    try:
        path.parent.rmdir()
    except OSError:
        return


def _require_time(value: float) -> None:
    if type(value) is not float:
        raise GenerationRejected("now")
