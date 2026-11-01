"""Create one evidence anchor on an exact artifact version.

Identity, Origin, CSRF, and the idempotency key follow the version routes.
The project id comes from the path. The author comes from the session.
``create_app`` does not mount this router.
"""

import time
from datetime import datetime, timezone
from typing import Any, Literal

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy.orm import Session

from sinuo.api.auth.dependencies import get_engine
from sinuo.api.auth.errors import _MESSAGES, error_response
from sinuo.api.auth.routes import _idempotency_key, _success
from sinuo.api.auth.schemas import parse_json
from sinuo.api.versions.routes import _guard
from sinuo.application.auth.authorizer import SessionAuthorizer
from sinuo.application.evidence.anchors import AnchorInvalid, AnchorView, create_anchor
from sinuo.application.idempotency import CommandResponse, CommandScope, execute_idempotent
from sinuo.domain.auth.errors import InvalidSchema
from sinuo.domain.auth.resource import ResourceRef
from sinuo.domain.public_contract import Action, ErrorCode
from sinuo.infrastructure.database import write_uow
from sinuo.infrastructure.models import IdempotencyRecord

router = APIRouter(prefix="/api/v1")

_MESSAGES.setdefault(ErrorCode.INVALID_ANCHOR, "Invalid anchor.")

_CREATED = {
    "description": "An anchor on one artifact version, with the excerpt and a place.",
    "content": {
        "application/json": {
            "example": {
                "data": {
                    "evidence_anchor_id": "anchor-id",
                    "project_id": "project-id",
                    "artifact_version_id": "version-id",
                    "locator_type": "page",
                    "locator": {"excerpt": "synthetic excerpt", "page": 4},
                    "excerpt_hash": "a" * 64,
                    "extractor_version": "manual-1",
                    "visibility": "project",
                    "sensitivity": "released",
                    "supersedes_id": None,
                    "schema_version": 1,
                    "created_by_account_id": "account-id",
                    "created_at": "2026-11-01T00:00:00Z",
                    "audit_event_id": "audit-id",
                    "project_revision": 2,
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
_REJECTED = {
    "description": "No page, region, or timecode, or a correction retargets the version.",
    "content": {
        "application/json": {
            "example": {
                "error": {
                    "code": "INVALID_ANCHOR",
                    "message": "Invalid anchor.",
                    "details": {"category": "locator"},
                    "retryable": False,
                    "request_id": "request-id",
                }
            }
        }
    },
}
_FORBIDDEN = {
    "description": "The caller does not hold evidence.create on this project.",
    "content": {
        "application/json": {
            "example": {
                "error": {
                    "code": "ACTION_FORBIDDEN",
                    "message": "Action not allowed.",
                    "details": {"action": "evidence.create"},
                    "retryable": False,
                    "request_id": "request-id",
                }
            }
        }
    },
}


class EvidenceCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    artifact_version_id: str = Field(min_length=1, max_length=64)
    excerpt: str = Field(min_length=1, max_length=4000)
    locator: dict[str, Any]
    supersedes_id: str | None = Field(default=None, min_length=1, max_length=64)
    visibility: Literal["project", "restricted"] | None = None
    extractor_version: str | None = Field(default=None, min_length=1, max_length=64)


def parse_evidence_create(raw: bytes) -> EvidenceCreate:
    payload = parse_json(raw)
    if not isinstance(payload, dict):
        raise InvalidSchema()
    try:
        return EvidenceCreate.model_validate(payload)
    except ValidationError as error:
        raise InvalidSchema() from error


def _rfc3339(value: float) -> str:
    moment = datetime.fromtimestamp(float(value), timezone.utc)
    seconds = moment.strftime("%Y-%m-%dT%H:%M:%S")
    fraction = f"{moment.microsecond:06d}".rstrip("0")
    if fraction:
        return f"{seconds}.{fraction}Z"
    return f"{seconds}Z"


def _payload(view: AnchorView) -> dict[str, Any]:
    return {
        "evidence_anchor_id": view.evidence_anchor_id,
        "project_id": view.project_id,
        "artifact_version_id": view.artifact_version_id,
        "locator_type": view.locator_type,
        "locator": view.locator,
        "excerpt_hash": view.excerpt_hash,
        "extractor_version": view.extractor_version,
        "visibility": view.visibility,
        "sensitivity": view.sensitivity,
        "supersedes_id": view.supersedes_id,
        "schema_version": view.schema_version,
        "created_by_account_id": view.created_by_account_id,
        "created_at": _rfc3339(view.created_at),
        "audit_event_id": view.audit_event_id,
        "project_revision": view.project_revision,
    }


def _committed(request: Request, response: CommandResponse, replayed: bool) -> JSONResponse:
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


def _already_stored(session: Session, scope: CommandScope) -> bool:
    key = (scope.project_id, scope.actor_id, scope.command, scope.request_key)
    return session.get(IdempotencyRecord, key) is not None


def _invalid_anchor(request: Request, category: str) -> JSONResponse:
    return error_response(
        request,
        status=422,
        code=ErrorCode.INVALID_ANCHOR,
        details={"category": category},
        retryable=False,
    )


@router.post(
    "/projects/{project_id}/evidence-anchors",
    status_code=201,
    responses={201: _CREATED, 403: _FORBIDDEN, 422: _REJECTED},
)
async def post_evidence_anchor(project_id: str, request: Request) -> JSONResponse:
    body = parse_evidence_create(await request.body())
    now = time.time()
    try:
        with write_uow(get_engine(request)) as session:
            principal = _guard(request, session, now)
            authorizer = SessionAuthorizer(session)
            scope = CommandScope(
                project_id=project_id,
                actor_id=principal.account_id,
                command=Action.EVIDENCE_CREATE.value,
                request_key=_idempotency_key(request),
            )
            replayed = _already_stored(session, scope)
            request_document = {"project_id": project_id, **body.model_dump(mode="json")}

            def authorize(active: Session) -> None:
                del active
                authorizer.authorize(
                    Action.EVIDENCE_CREATE,
                    ResourceRef(project_id=project_id, scope_type="project"),
                    principal,
                )

            def handler(active: Session) -> CommandResponse:
                created = create_anchor(
                    active,
                    project_id=project_id,
                    actor_id=principal.account_id,
                    artifact_version_id=body.artifact_version_id,
                    excerpt=body.excerpt,
                    locator=body.locator,
                    supersedes_id=body.supersedes_id,
                    visibility=body.visibility,
                    extractor_version=body.extractor_version,
                    now=now,
                )
                return CommandResponse(201, _payload(created))

            response = execute_idempotent(session, scope, request_document, authorize, handler)
    except AnchorInvalid as exc:
        return _invalid_anchor(request, exc.category)
    return _committed(request, response, replayed)
