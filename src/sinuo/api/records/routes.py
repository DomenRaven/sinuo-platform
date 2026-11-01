"""Create one test record on an exact artifact version.

Identity, Origin, CSRF, and the idempotency key follow the version routes.
The project id comes from the path. The actor comes from the session.
``create_app`` does not mount this router.
"""

import time
from datetime import datetime, timezone
from typing import Any

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
from sinuo.application.idempotency import CommandResponse, CommandScope, execute_idempotent
from sinuo.application.records.tests import TestRecordInvalid, TestRecordView, create_test_record
from sinuo.domain.auth.errors import InvalidSchema
from sinuo.domain.auth.resource import ResourceRef
from sinuo.domain.public_contract import Action, ErrorCode
from sinuo.infrastructure.database import write_uow
from sinuo.infrastructure.models import IdempotencyRecord

router = APIRouter(prefix="/api/v1")

_MESSAGES.setdefault(ErrorCode.INVALID_ANCHOR, "Invalid anchor.")

_CREATED = {
    "description": "A test record bound to one artifact version.",
    "content": {
        "application/json": {
            "example": {
                "data": {
                    "test_record_id": "record-id",
                    "project_id": "project-id",
                    "account_id": "account-id",
                    "artifact_version_id": "version-id",
                    "conditions": {"task": "synthetic task", "limitation": "synthetic limit"},
                    "observation": "synthetic observation",
                    "result": "synthetic result",
                    "source": "synthetic source",
                    "supersedes_id": None,
                    "project_revision": 2,
                    "created_at": "2026-11-01T00:00:00Z",
                    "audit_event_id": "audit-id",
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
    "description": "The version is an alias, or a correction names a different artifact version.",
    "content": {
        "application/json": {
            "example": {
                "error": {
                    "code": "INVALID_ANCHOR",
                    "message": "Invalid anchor.",
                    "details": {"category": "artifact_version_id"},
                    "retryable": False,
                    "request_id": "request-id",
                }
            }
        }
    },
}
_FORBIDDEN = {
    "description": "The caller does not hold test.record on this project.",
    "content": {
        "application/json": {
            "example": {
                "error": {
                    "code": "ACTION_FORBIDDEN",
                    "message": "Action not allowed.",
                    "details": {"action": "test.record"},
                    "retryable": False,
                    "request_id": "request-id",
                }
            }
        }
    },
}


class TestRecordCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    artifact_version_id: str = Field(min_length=1, max_length=64)
    conditions: dict[str, Any] = Field(min_length=1)
    observation: str = Field(min_length=1, max_length=8000)
    result: str = Field(min_length=1, max_length=8000)
    source: str | None = Field(default=None, min_length=1, max_length=2000)
    supersedes_id: str | None = Field(default=None, min_length=1, max_length=64)


def parse_test_record(raw: bytes) -> TestRecordCreate:
    payload = parse_json(raw)
    if not isinstance(payload, dict):
        raise InvalidSchema()
    try:
        return TestRecordCreate.model_validate(payload)
    except ValidationError as error:
        raise InvalidSchema() from error


def _rfc3339(value: float) -> str:
    moment = datetime.fromtimestamp(float(value), timezone.utc)
    seconds = moment.strftime("%Y-%m-%dT%H:%M:%S")
    fraction = f"{moment.microsecond:06d}".rstrip("0")
    if fraction:
        return f"{seconds}.{fraction}Z"
    return f"{seconds}Z"


def _payload(view: TestRecordView) -> dict[str, Any]:
    return {
        "test_record_id": view.test_record_id,
        "project_id": view.project_id,
        "account_id": view.account_id,
        "artifact_version_id": view.artifact_version_id,
        "conditions": view.conditions,
        "observation": view.observation,
        "result": view.result,
        "source": view.source,
        "supersedes_id": view.supersedes_id,
        "project_revision": view.project_revision,
        "created_at": _rfc3339(view.created_at),
        "audit_event_id": view.audit_event_id,
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


def _invalid_record(request: Request, category: str) -> JSONResponse:
    return error_response(
        request,
        status=422,
        code=ErrorCode.INVALID_ANCHOR,
        details={"category": category},
        retryable=False,
    )


@router.post(
    "/projects/{project_id}/test-records",
    status_code=201,
    responses={201: _CREATED, 403: _FORBIDDEN, 422: _REJECTED},
)
async def post_test_record(project_id: str, request: Request) -> JSONResponse:
    body = parse_test_record(await request.body())
    now = time.time()
    try:
        with write_uow(get_engine(request)) as session:
            principal = _guard(request, session, now)
            authorizer = SessionAuthorizer(session)
            scope = CommandScope(
                project_id=project_id,
                actor_id=principal.account_id,
                command=Action.TEST_RECORD.value,
                request_key=_idempotency_key(request),
            )
            replayed = _already_stored(session, scope)
            request_document = {"project_id": project_id, **body.model_dump(mode="json")}

            def authorize(active: Session) -> None:
                del active
                authorizer.authorize(
                    Action.TEST_RECORD,
                    ResourceRef(project_id=project_id, scope_type="project"),
                    principal,
                )

            def handler(active: Session) -> CommandResponse:
                created = create_test_record(
                    active,
                    project_id=project_id,
                    actor_id=principal.account_id,
                    artifact_version_id=body.artifact_version_id,
                    conditions=body.conditions,
                    observation=body.observation,
                    result=body.result,
                    source=body.source,
                    supersedes_id=body.supersedes_id,
                    now=now,
                )
                return CommandResponse(201, _payload(created))

            response = execute_idempotent(session, scope, request_document, authorize, handler)
    except TestRecordInvalid as exc:
        return _invalid_record(request, exc.category)
    return _committed(request, response, replayed)
