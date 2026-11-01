"""Register, read, and download one snapshot export.

Identity, Origin, CSRF, and the idempotency key follow the version routes.
The project id comes from the path. The actor comes from the session.
Download rebuilds the package from current rights and does not stream a stale file.
"""

import time
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response
from sqlalchemy.orm import Session

from sinuo import __version__
from sinuo.api.auth.dependencies import get_engine, get_settings, session_token
from sinuo.api.auth.errors import request_id_for
from sinuo.api.auth.routes import _idempotency_key, _nostore, _success
from sinuo.api.exports.schemas import parse_export_create
from sinuo.api.versions.routes import _guard
from sinuo.application.assets.download import download_response_headers
from sinuo.application.auth.authorizer import SessionAuthorizer
from sinuo.application.auth.sessions import resolve_principal
from sinuo.application.exports.package import (
    compose_job,
    export_view,
    open_export,
    require_export,
    zip_files,
)
from sinuo.application.idempotency import (
    CommandResponse,
    CommandScope,
    canonical_hash,
    execute_idempotent,
)
from sinuo.domain.auth.errors import NotFoundOrHidden
from sinuo.domain.auth.resource import ResourceRef
from sinuo.domain.public_contract import Action
from sinuo.infrastructure.database import write_uow
from sinuo.infrastructure.models import IdempotencyRecord

router = APIRouter(prefix="/api/v1")

_ACCEPTED = {
    "description": "One queued export. latest is false; the row points at its input snapshot.",
    "content": {
        "application/json": {
            "example": {
                "data": {
                    "export_job_id": "export-id",
                    "input_snapshot_id": "snapshot-id",
                    "status": "queued",
                    "latest": False,
                    "format": "markdown",
                    "include_originals": False,
                    "access_epoch_at_input": 1,
                    "source_manifest": {
                        "page_version_ids": ["pv-1"],
                        "artifact_version_ids": [],
                        "evidence_anchor_ids": [],
                    },
                    "audit_event_id": "audit-id",
                },
                "meta": {
                    "request_id": "request-id",
                    "version": "0.1.0",
                    "audit_event_id": "audit-id",
                    "idempotency_replayed": False,
                },
            }
        }
    },
}
_FORBIDDEN = {
    "description": "The caller does not currently hold report.export.",
    "content": {
        "application/json": {
            "example": {
                "error": {
                    "code": "ACTION_FORBIDDEN",
                    "message": "Action not allowed.",
                    "details": {"action": "report.export"},
                    "retryable": False,
                    "request_id": "request-id",
                }
            }
        }
    },
}
_HIDDEN = {
    "description": "No such export inside the caller's current authorization.",
    "content": {
        "application/json": {
            "example": {
                "error": {
                    "code": "NOT_FOUND_OR_HIDDEN",
                    "message": "Not found.",
                    "details": {},
                    "retryable": False,
                    "request_id": "request-id",
                }
            }
        }
    },
}
_READ = {
    "description": "Current job status and the frozen input snapshot. latest is false.",
    "content": {
        "application/json": {
            "example": {
                "data": {
                    "export_job_id": "export-id",
                    "input_snapshot_id": "snapshot-id",
                    "status": "succeeded",
                    "latest": False,
                    "format": "markdown",
                    "include_originals": True,
                    "access_epoch_at_input": 1,
                    "source_manifest": {
                        "page_version_ids": ["pv-1"],
                        "artifact_version_ids": ["av-1"],
                        "evidence_anchor_ids": ["ea-1"],
                    },
                },
                "meta": {"request_id": "request-id", "version": "0.1.0"},
            }
        }
    },
}


def _already_stored(session: Session, scope: CommandScope) -> bool:
    key = (scope.project_id, scope.actor_id, scope.command, scope.request_key)
    return session.get(IdempotencyRecord, key) is not None


def _committed(request: Request, response: CommandResponse, replayed: bool) -> JSONResponse:
    audit_event_id = response.body.get("audit_event_id")
    return _success(
        request,
        response.body,
        status=response.status,
        replayed=replayed,
        audit_event_id=audit_event_id if isinstance(audit_event_id, str) else None,
    )


def _root(request: Request, name: str) -> Path | None:
    value = getattr(request.app.state, name, None)
    if isinstance(value, Path) and value.is_dir():
        return value
    return None


def _authorize_export(session: Session, job, principal) -> None:
    SessionAuthorizer(session).authorize(
        Action.REPORT_EXPORT,
        ResourceRef(project_id=job.project_id, scope_type="project"),
        principal,
    )


@router.post(
    "/projects/{project_id}/exports",
    status_code=202,
    responses={202: _ACCEPTED, 403: _FORBIDDEN},
)
async def post_export(project_id: str, request: Request) -> JSONResponse:
    body = parse_export_create(await request.body())
    now = time.time()
    with write_uow(get_engine(request)) as session:
        principal = _guard(request, session, now)
        scope = CommandScope(
            project_id=project_id,
            actor_id=principal.account_id,
            command=Action.REPORT_EXPORT.value,
            request_key=_idempotency_key(request),
        )
        replayed = _already_stored(session, scope)
        request_document = {"project_id": project_id, **body.model_dump(mode="json")}

        def authorize(active: Session) -> None:
            SessionAuthorizer(active).authorize(
                Action.REPORT_EXPORT,
                ResourceRef(project_id=project_id, scope_type="project"),
                principal,
            )

        def handler(active: Session) -> CommandResponse:
            created = open_export(
                active,
                principal=principal,
                project_id=project_id,
                source_manifest=body.source_manifest.model_dump(mode="json"),
                include_originals=body.include_originals,
                export_format=body.format,
                request_hash=canonical_hash(request_document),
                now=now,
            )
            return CommandResponse(202, created)

        response = execute_idempotent(session, scope, request_document, authorize, handler)
    return _committed(request, response, replayed)


@router.get(
    "/exports/{export_id}",
    responses={200: _READ, 403: _FORBIDDEN, 404: _HIDDEN},
)
def get_export(export_id: str, request: Request) -> JSONResponse:
    settings = get_settings(request)
    with Session(get_engine(request)) as session:
        principal = resolve_principal(session, session_token(request, settings), now=time.time())
        job = require_export(session, export_id)
        _authorize_export(session, job, principal)
        payload: dict[str, Any] = export_view(job, command=False)
    body = {
        "data": payload,
        "meta": {"request_id": request_id_for(request), "version": __version__},
    }
    return _nostore(JSONResponse(body))


@router.get(
    "/exports/{export_id}/content",
    responses={
        200: {
            "description": "Zip rebuilt with current rights. File hashes match manifest.json.",
            "content": {"application/zip": {}},
        },
        403: _FORBIDDEN,
        404: _HIDDEN,
    },
)
def get_export_content(export_id: str, request: Request) -> Response:
    settings = get_settings(request)
    with Session(get_engine(request)) as session:
        principal = resolve_principal(session, session_token(request, settings), now=time.time())
        job = require_export(session, export_id)
        _authorize_export(session, job, principal)
        if job.status != "succeeded":
            raise NotFoundOrHidden()
        files = compose_job(
            session,
            job,
            principal,
            objects_root=_root(request, "objects_root"),
            derived_root=_root(request, "derived_root"),
        )
    return Response(
        content=zip_files(files),
        headers=download_response_headers("export.zip", "application/zip"),
    )
