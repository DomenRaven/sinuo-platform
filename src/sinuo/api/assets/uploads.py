"""Upload ticket, staged bytes, and original registration.

``create_app`` includes this router. Identity, Origin, CSRF,
and ``Idempotency-Key`` follow the version routes. Permission is the existing
``artifact.upload`` grant. The same project and name reuse one artifact; new
bytes become a new version and do not rewrite an older content hash.
"""

import hashlib
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from sinuo.api.auth.dependencies import get_engine
from sinuo.api.auth.errors import _MESSAGES, error_response
from sinuo.api.auth.routes import _idempotency_key, _success
from sinuo.api.auth.schemas import parse_json
from sinuo.api.versions.routes import _guard
from sinuo.application.assets.artifacts import ArtifactRejected, ensure_artifact
from sinuo.application.assets.uploads import (
    AssetRejected,
    AttemptMissing,
    RegistrationConflict,
    register_original,
    request_upload_ticket,
)
from sinuo.application.auth.authorizer import SessionAuthorizer
from sinuo.application.idempotency import (
    CommandResponse,
    CommandScope,
    canonical_hash,
    execute_idempotent,
)
from sinuo.domain.auth.errors import InvalidSchema, NotFoundOrHidden
from sinuo.domain.auth.principal import Principal
from sinuo.domain.auth.resource import ResourceRef
from sinuo.domain.errors import IdempotencyConflict
from sinuo.domain.public_contract import Action, ErrorCode
from sinuo.infrastructure.assets.paths import PathEscape, is_storage_segment, resolve_contained
from sinuo.infrastructure.assets.records import UploadTicketRow
from sinuo.infrastructure.assets.staging import AttemptTooLarge, digest_file, write_attempt_bytes
from sinuo.infrastructure.database import write_uow
from sinuo.infrastructure.domain_models import ArtifactVersion, AuditEvent
from sinuo.infrastructure.models import IdempotencyRecord

router = APIRouter(prefix="/api/v1")

UPLOAD_BYTE_LIMIT = 8 * 1024 * 1024
_TICKET_TTL_SECONDS = 3600.0
_OPEN = Action.ARTIFACT_UPLOAD.value
_CONTENT = "artifact.upload.content"
_COMPLETE = "artifact.upload.complete"
_TEXT_LIMITS = {"name": 200, "media_type": 255, "license": 200}
_REJECT_STATUS = {
    "byte_limit": 413,
    "ticket_expired": 409,
    "ticket_closed": 409,
    "expected_size_mismatch": 422,
    "declared_hash_mismatch": 422,
    "ticket_owner": 404,
    "missing_ticket": 404,
}

_MESSAGES.setdefault(ErrorCode.UPLOAD_TOO_LARGE, "Upload too large.")
_MESSAGES.setdefault(ErrorCode.STORAGE_BUSY, "Storage busy.")

_OPENED = {
    "description": "An isolated upload ticket. A new name inserts an artifact first.",
    "content": {
        "application/json": {
            "example": {
                "data": {
                    "ticket_id": "ticket-id",
                    "upload_id": "ticket-id",
                    "project_id": "project-id",
                    "artifact_id": "artifact-id",
                    "artifact_created": True,
                    "status": "open",
                    "expected_size": 4,
                    "declared_hash": "a" * 64,
                    "expires_at": "2026-11-01T01:00:00Z",
                },
                "meta": {
                    "request_id": "request-id",
                    "version": "0.1.0",
                    "idempotency_replayed": False,
                },
            }
        }
    },
}
_FORBIDDEN = {
    "description": "The caller does not currently hold artifact.upload.",
    "content": {
        "application/json": {
            "example": {
                "error": {
                    "code": "ACTION_FORBIDDEN",
                    "message": "Action not allowed.",
                    "details": {"action": "artifact.upload"},
                    "retryable": False,
                    "request_id": "request-id",
                }
            }
        }
    },
}
_STAGED = {
    "description": "Bytes are in isolated staging. The ticket is still open.",
    "content": {
        "application/json": {
            "example": {
                "data": {
                    "ticket_id": "ticket-id",
                    "upload_id": "ticket-id",
                    "project_id": "project-id",
                    "status": "staged",
                    "byte_size": 4,
                },
                "meta": {
                    "request_id": "request-id",
                    "version": "0.1.0",
                    "idempotency_replayed": False,
                },
            }
        }
    },
}
_TOO_LARGE = {
    "description": "The declared length or the body is above the server byte limit.",
    "content": {
        "application/json": {
            "example": {
                "error": {
                    "code": "UPLOAD_TOO_LARGE",
                    "message": "Upload too large.",
                    "details": {"category": "byte_limit"},
                    "retryable": False,
                    "request_id": "request-id",
                }
            }
        }
    },
}
_REGISTERED = {
    "description": "A new artifact version. The derivative is pending, not a finished preview.",
    "content": {
        "application/json": {
            "example": {
                "data": {
                    "ticket_id": "ticket-id",
                    "upload_id": "ticket-id",
                    "artifact_id": "artifact-id",
                    "artifact_version_id": "version-id",
                    "version_no": 1,
                    "content_hash": "a" * 64,
                    "byte_size": 4,
                    "project_revision": 2,
                    "audit_event_id": "audit-id",
                    "preview_state": "unavailable",
                    "preview_reason": "converter_not_installed",
                    "preview_generated": False,
                    "derivative_status": "pending",
                },
                "meta": {
                    "request_id": "request-id",
                    "version": "0.1.0",
                    "project_revision": 2,
                    "audit_event_id": "audit-id",
                    "idempotency_replayed": False,
                },
            }
        }
    },
}
_HASH_MISMATCH = {
    "description": "The staged bytes do not match the declared hash. No version is registered.",
    "content": {
        "application/json": {
            "example": {
                "error": {
                    "code": "INVALID_SCHEMA",
                    "message": "Invalid schema.",
                    "details": {"category": "declared_hash_mismatch"},
                    "retryable": False,
                    "request_id": "request-id",
                }
            }
        }
    },
}


class _UploadFailure(Exception):
    def __init__(self, status: int, category: str) -> None:
        self.status = status
        self.category = category
        super().__init__(category)


class UploadTicketRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    artifact_id: str | None = Field(default=None, min_length=1, max_length=64)
    name: str = Field(min_length=1, max_length=200)
    media_type: str = Field(min_length=1, max_length=255)
    size: int = Field(ge=0)
    declared_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    license: str = Field(min_length=1, max_length=200)
    sensitivity: str
    ai_processing_allowed: int

    @field_validator("size", "ai_processing_allowed", mode="before")
    @classmethod
    def _strict_int(cls, value: object) -> object:
        if type(value) is not int:
            raise ValueError("int")
        return value

    @field_validator("ai_processing_allowed")
    @classmethod
    def _flag(cls, value: int) -> int:
        if value not in (0, 1):
            raise ValueError("ai_processing_allowed")
        return value

    @field_validator("sensitivity")
    @classmethod
    def _sensitivity(cls, value: str) -> str:
        if value not in {"project", "sensitive", "released"}:
            raise ValueError("sensitivity")
        return value

    @field_validator("name", "media_type", "license")
    @classmethod
    def _plain(cls, value: str, info) -> str:
        limit = _TEXT_LIMITS[info.field_name]
        stripped = value.strip()
        if not stripped or len(stripped) > limit or any(ord(char) < 32 for char in stripped):
            raise ValueError("text")
        if info.field_name != "name" and ".." in stripped:
            raise ValueError("text")
        return stripped

    @field_validator("artifact_id")
    @classmethod
    def _artifact_id(cls, value: str | None) -> str | None:
        if value is not None and not is_storage_segment(value):
            raise ValueError("artifact_id")
        return value


class UploadCompleteRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    declared_hash: str = Field(pattern=r"^[0-9a-f]{64}$")


def _parse(model: type[BaseModel], raw: bytes) -> BaseModel:
    payload = parse_json(raw)
    if not isinstance(payload, dict):
        raise InvalidSchema()
    try:
        return model.model_validate(payload)
    except ValidationError as error:
        raise InvalidSchema() from error


def _rfc3339(value: float) -> str:
    moment = datetime.fromtimestamp(value, timezone.utc)
    seconds = moment.strftime("%Y-%m-%dT%H:%M:%S")
    fraction = f"{moment.microsecond:06d}".rstrip("0")
    if fraction:
        return f"{seconds}.{fraction}Z"
    return f"{seconds}Z"


def _directory(request: Request, name: str) -> Path | None:
    value = getattr(request.app.state, name, None)
    if isinstance(value, Path) and value.is_dir():
        return value
    return None


def _attempt_relative(ticket_id: str) -> str:
    return f"{ticket_id}/attempt.part"


def _failure_response(request: Request, failure: _UploadFailure) -> JSONResponse:
    details = {"category": failure.category}
    if failure.status == 413:
        code = ErrorCode.UPLOAD_TOO_LARGE
    elif failure.status == 503:
        return error_response(
            request,
            status=503,
            code=ErrorCode.STORAGE_BUSY,
            details=details,
            retryable=True,
        )
    elif failure.status == 409:
        code = ErrorCode.HEAD_CHANGED
    elif failure.status == 404:
        code = ErrorCode.NOT_FOUND_OR_HIDDEN
    else:
        code = ErrorCode.INVALID_SCHEMA
    return error_response(
        request,
        status=failure.status,
        code=code,
        details=details,
        retryable=False,
    )


def _from_command(request: Request, response: CommandResponse, replayed: bool) -> JSONResponse:
    if response.status >= 400:
        category = response.body.get("error_category")
        text = category if isinstance(category, str) and category else "rejected"
        return _failure_response(request, _UploadFailure(response.status, text))
    revision = response.body.get("project_revision")
    audit_event_id = response.body.get("audit_event_id")
    return _success(
        request,
        response.body,
        status=response.status,
        replayed=replayed,
        project_revision=revision if type(revision) is int else None,
        audit_event_id=audit_event_id if isinstance(audit_event_id, str) else None,
    )


def _authorize_project(session: Session, principal: Principal, project_id: str) -> None:
    if not is_storage_segment(project_id):
        raise NotFoundOrHidden()
    SessionAuthorizer(session).authorize(
        Action.ARTIFACT_UPLOAD,
        ResourceRef(project_id=project_id, scope_type="project"),
        principal,
    )


def _prepare_ticket(session: Session, principal: Principal, ticket_id: str) -> UploadTicketRow:
    if not is_storage_segment(ticket_id):
        raise NotFoundOrHidden()
    ticket = session.get(UploadTicketRow, ticket_id)
    if ticket is None:
        raise NotFoundOrHidden()
    SessionAuthorizer(session).authorize(
        Action.ARTIFACT_UPLOAD,
        ResourceRef(project_id=ticket.project_id, scope_type="project"),
        principal,
    )
    if ticket.created_by_account_id != principal.account_id:
        raise NotFoundOrHidden()
    return ticket


def _scope(
    ticket: UploadTicketRow,
    principal: Principal,
    command: str,
    request_key: str,
) -> CommandScope:
    return CommandScope(
        project_id=ticket.project_id,
        actor_id=principal.account_id,
        command=command,
        request_key=request_key,
    )


def _already_stored(session: Session, scope: CommandScope) -> bool:
    key = (scope.project_id, scope.actor_id, scope.command, scope.request_key)
    return session.get(IdempotencyRecord, key) is not None


def _announced_length(request: Request) -> int | None:
    raw = request.headers.get("content-length")
    if raw is None:
        return None
    if not raw.isdigit():
        raise InvalidSchema()
    return int(raw)


def _open_ticket(
    session: Session,
    principal: Principal,
    project_id: str,
    body: UploadTicketRequest,
    now: float,
) -> CommandResponse:
    if body.size > UPLOAD_BYTE_LIMIT:
        raise _UploadFailure(413, "byte_limit")
    try:
        artifact_id, created = ensure_artifact(
            session,
            project_id=project_id,
            name=body.name,
            artifact_id=body.artifact_id,
            now=now,
        )
    except ArtifactRejected as exc:
        raise InvalidSchema() from exc
    try:
        ticket = request_upload_ticket(
            session,
            project_id=project_id,
            artifact_id=artifact_id,
            expected_size=body.size,
            declared_hash=body.declared_hash,
            media_type=body.media_type,
            license_name=body.license,
            sensitivity=body.sensitivity,
            ai_processing_allowed=body.ai_processing_allowed,
            account_id=principal.account_id,
            now=now,
            byte_limit=UPLOAD_BYTE_LIMIT,
            expires_at=now + _TICKET_TTL_SECONDS,
        )
    except AssetRejected as exc:
        if exc.reason == "byte_limit":
            raise _UploadFailure(413, "byte_limit") from exc
        if exc.reason == "owner":
            raise NotFoundOrHidden() from exc
        raise InvalidSchema() from exc
    return CommandResponse(
        201,
        {
            "ticket_id": ticket.upload_id,
            "upload_id": ticket.upload_id,
            "project_id": ticket.project_id,
            "artifact_id": ticket.artifact_id,
            "artifact_created": created,
            "status": "open",
            "expected_size": ticket.expected_size,
            "declared_hash": ticket.declared_hash,
            "expires_at": _rfc3339(now + _TICKET_TTL_SECONDS),
        },
    )


def _receipt(
    session: Session,
    scope: CommandScope,
    document: dict[str, Any],
) -> dict[str, Any] | None:
    existing = session.get(
        IdempotencyRecord,
        (scope.project_id, scope.actor_id, scope.command, scope.request_key),
    )
    if existing is None:
        return None
    if existing.request_hash != canonical_hash(document):
        raise IdempotencyConflict(scope.command)
    stored = existing.response_json
    if not isinstance(stored, dict):
        raise IdempotencyConflict(scope.command)
    return dict(stored)


def _write_staged(root: Path, ticket_id: str, payload: bytes) -> None:
    relative = _attempt_relative(ticket_id)
    try:
        write_attempt_bytes(root, relative, payload, byte_limit=UPLOAD_BYTE_LIMIT)
    except AttemptTooLarge as exc:
        raise _UploadFailure(413, "byte_limit") from exc
    except FileExistsError:
        current_hash, current_size = digest_file(resolve_contained(root, relative))
        digest = hashlib.sha256(payload).hexdigest()
        if current_size != len(payload) or current_hash != digest:
            raise _UploadFailure(409, "staged_bytes") from None


def _staged_body(ticket: UploadTicketRow, byte_size: int) -> dict[str, Any]:
    return {
        "ticket_id": ticket.upload_id,
        "upload_id": ticket.upload_id,
        "project_id": ticket.project_id,
        "status": "staged",
        "byte_size": byte_size,
    }


def _registration_body(session: Session, outcome, version: ArtifactVersion) -> dict[str, Any]:
    audit_event_id = session.scalars(
        select(AuditEvent.id).where(
            AuditEvent.project_id == version.project_id,
            AuditEvent.subject_type == "artifact_version",
            AuditEvent.subject_id == version.id,
            AuditEvent.command == "artifact.version_registered",
        )
    ).first()
    if not isinstance(audit_event_id, str) or version.content_hash != outcome.content_hash:
        raise RegistrationConflict(version.project_id)
    pending = outcome.preview_generated is False
    return {
        "ticket_id": outcome.upload_id,
        "upload_id": outcome.upload_id,
        "artifact_id": version.artifact_id,
        "artifact_version_id": version.id,
        "version_no": version.version_no,
        "content_hash": version.content_hash,
        "byte_size": version.size_bytes,
        "project_revision": version.project_revision,
        "audit_event_id": audit_event_id,
        "preview_state": outcome.preview_state,
        "preview_reason": outcome.preview_reason,
        "preview_generated": outcome.preview_generated,
        "derivative_status": "pending" if pending else outcome.preview_state,
    }


@router.post(
    "/projects/{project_id}/artifacts/uploads",
    status_code=201,
    responses={201: _OPENED, 403: _FORBIDDEN},
)
async def post_artifact_upload(project_id: str, request: Request) -> JSONResponse:
    body = _parse(UploadTicketRequest, await request.body())
    assert isinstance(body, UploadTicketRequest)
    now = time.time()
    try:
        with write_uow(get_engine(request)) as session:
            principal = _guard(request, session, now)
            scope = CommandScope(
                project_id=project_id,
                actor_id=principal.account_id,
                command=_OPEN,
                request_key=_idempotency_key(request),
            )
            replayed = _already_stored(session, scope)
            document = {"project_id": project_id, **body.model_dump(mode="json")}

            def authorize(active: Session) -> None:
                _authorize_project(active, principal, project_id)

            def handler(active: Session) -> CommandResponse:
                return _open_ticket(active, principal, project_id, body, now)

            response = execute_idempotent(session, scope, document, authorize, handler)
    except _UploadFailure as failure:
        return _failure_response(request, failure)
    return _from_command(request, response, replayed)


@router.put(
    "/uploads/{ticket_id}/content",
    responses={200: _STAGED, 413: _TOO_LARGE},
)
async def put_upload_content(ticket_id: str, request: Request) -> JSONResponse:
    announced = _announced_length(request)
    now = time.time()
    if announced is not None and announced > UPLOAD_BYTE_LIMIT:
        try:
            with write_uow(get_engine(request)) as session:
                principal = _guard(request, session, now)
                _prepare_ticket(session, principal, ticket_id)
                raise _UploadFailure(413, "byte_limit")
        except _UploadFailure as failure:
            return _failure_response(request, failure)
    payload = await request.body()
    document = {
        "upload_id": ticket_id,
        "byte_size": len(payload),
        "content_sha256": hashlib.sha256(payload).hexdigest(),
    }
    try:
        with write_uow(get_engine(request)) as session:
            replay = _claim_content(session, request, ticket_id, document, announced, now)
    except _UploadFailure as failure:
        return _failure_response(request, failure)
    if replay is not None:
        return _success(request, replay, replayed=True)
    root = _directory(request, "uploads_root")
    if root is None:
        return _failure_response(request, _UploadFailure(503, "storage_not_configured"))
    try:
        _write_staged(root, ticket_id, payload)
    except _UploadFailure as failure:
        return _failure_response(request, failure)
    except PathEscape:
        return _failure_response(request, _UploadFailure(503, "storage_not_configured"))
    try:
        with write_uow(get_engine(request)) as session:
            response, replayed = _record_content(session, request, ticket_id, document, now)
    except _UploadFailure as failure:
        return _failure_response(request, failure)
    return _from_command(request, response, replayed)


def _claim_content(
    session: Session,
    request: Request,
    ticket_id: str,
    document: dict[str, Any],
    announced: int | None,
    now: float,
) -> dict[str, Any] | None:
    principal = _guard(request, session, now)
    ticket = _prepare_ticket(session, principal, ticket_id)
    request_key = _idempotency_key(request)
    byte_size = document["byte_size"]
    if type(byte_size) is not int:
        raise InvalidSchema()
    if byte_size > UPLOAD_BYTE_LIMIT:
        raise _UploadFailure(413, "byte_limit")
    if announced is not None and announced != byte_size:
        raise InvalidSchema()
    if announced is not None and announced != ticket.expected_size:
        raise _UploadFailure(422, "expected_size")
    if byte_size != ticket.expected_size:
        raise _UploadFailure(422, "expected_size")
    scope = _scope(ticket, principal, _CONTENT, request_key)
    stored = _receipt(session, scope, document)
    if stored is not None:
        return stored
    if _directory(request, "uploads_root") is None:
        raise _UploadFailure(503, "storage_not_configured")
    if ticket.status != "open":
        raise _UploadFailure(409, "ticket_closed")
    return None


def _record_content(
    session: Session,
    request: Request,
    ticket_id: str,
    document: dict[str, Any],
    now: float,
) -> tuple[CommandResponse, bool]:
    principal = _guard(request, session, now)
    ticket = _prepare_ticket(session, principal, ticket_id)
    scope = _scope(ticket, principal, _CONTENT, _idempotency_key(request))
    replayed = _already_stored(session, scope)

    def authorize(active: Session) -> None:
        _prepare_ticket(active, principal, ticket_id)

    def handler(active: Session) -> CommandResponse:
        current = _prepare_ticket(active, principal, ticket_id)
        if current.status != "open":
            raise _UploadFailure(409, "ticket_closed")
        return CommandResponse(200, _staged_body(current, int(document["byte_size"])))

    response = execute_idempotent(session, scope, document, authorize, handler)
    return response, replayed


@router.post(
    "/uploads/{ticket_id}/complete",
    status_code=201,
    responses={201: _REGISTERED, 422: _HASH_MISMATCH},
)
async def post_upload_complete(ticket_id: str, request: Request) -> JSONResponse:
    parsed = _parse(UploadCompleteRequest, await request.body())
    assert isinstance(parsed, UploadCompleteRequest)
    now = time.time()
    document = {"ticket_id": ticket_id, "declared_hash": parsed.declared_hash}
    try:
        with write_uow(get_engine(request)) as session:
            response, replayed = _complete(session, request, ticket_id, parsed, document, now)
    except _UploadFailure as failure:
        return _failure_response(request, failure)
    except AttemptMissing:
        return _failure_response(request, _UploadFailure(422, "content_missing"))
    except RegistrationConflict:
        return _failure_response(request, _UploadFailure(409, "registration_conflict"))
    except PathEscape:
        return _failure_response(request, _UploadFailure(503, "storage_not_configured"))
    return _from_command(request, response, replayed)


def _complete(
    session: Session,
    request: Request,
    ticket_id: str,
    body: UploadCompleteRequest,
    document: dict[str, Any],
    now: float,
) -> tuple[CommandResponse, bool]:
    principal = _guard(request, session, now)
    scope_ticket = _prepare_ticket(session, principal, ticket_id)
    scope = _scope(scope_ticket, principal, _COMPLETE, _idempotency_key(request))
    replayed = _already_stored(session, scope)

    def authorize(active: Session) -> None:
        _prepare_ticket(active, principal, ticket_id)

    def handler(active: Session) -> CommandResponse:
        ticket = _prepare_ticket(active, principal, ticket_id)
        if ticket.declared_hash != body.declared_hash:
            raise InvalidSchema()
        if ticket.status != "open":
            raise _UploadFailure(409, "ticket_closed")
        uploads = _directory(request, "uploads_root")
        objects = _directory(request, "objects_root")
        if uploads is None or objects is None:
            raise _UploadFailure(503, "storage_not_configured")
        outcome = register_original(
            active,
            upload_id=ticket.upload_id,
            temp_root=uploads,
            relative_path=_attempt_relative(ticket.upload_id),
            objects_root=objects,
            now=now,
            converter_installed=False,
            principal=principal,
            authorizer=SessionAuthorizer(active),
            byte_limit=UPLOAD_BYTE_LIMIT,
        )
        if not outcome.registered or outcome.artifact_version_id is None:
            reason = outcome.reason or "rejected"
            return CommandResponse(
                _REJECT_STATUS.get(reason, 422),
                {"error_category": reason},
            )
        version = active.get(ArtifactVersion, outcome.artifact_version_id)
        if version is None:
            raise RegistrationConflict(ticket.project_id)
        return CommandResponse(201, _registration_body(active, outcome, version))

    response = execute_idempotent(session, scope, document, authorize, handler)
    return response, replayed
