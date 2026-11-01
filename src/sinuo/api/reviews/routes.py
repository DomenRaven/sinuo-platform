"""Create, edit, and read one authorized review opinion.

Identity, Origin, CSRF, and the idempotency key follow the version routes.
The project id comes from the path or the opinion row. The author comes from
the session. Team comments and handoff are not this surface.
"""

import time
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from sinuo import __version__
from sinuo.api.auth.dependencies import get_engine, get_settings, session_token
from sinuo.api.auth.errors import request_id_for
from sinuo.api.auth.routes import _idempotency_key, _nostore, _success
from sinuo.api.reviews.schemas import parse_opinion_create, parse_opinion_patch
from sinuo.api.versions.routes import _guard
from sinuo.application.auth.authorizer import SessionAuthorizer
from sinuo.application.auth.sessions import resolve_principal
from sinuo.application.idempotency import CommandResponse, CommandScope, execute_idempotent
from sinuo.application.reviews.opinions import (
    OpinionView,
    create_opinion,
    edit_opinion,
    load_opinion,
    read_opinion,
)
from sinuo.domain.auth.errors import SourceRedacted
from sinuo.domain.auth.resource import ResourceRef
from sinuo.domain.public_contract import Action
from sinuo.infrastructure.database import write_uow
from sinuo.infrastructure.models import IdempotencyRecord

router = APIRouter(prefix="/api/v1")

_CREATED = {
    "description": "The signed-in actor's opinion, bound to the given page version.",
    "content": {
        "application/json": {
            "example": {
                "data": {
                    "opinion_id": "opinion-id",
                    "project_id": "project-id",
                    "author_account_id": "account-id",
                    "page_version_id": "pv-1",
                    "node_id": "node-1",
                    "body": "synthetic opinion",
                    "revision": 1,
                    "visibility": "project",
                    "created_at": "2026-11-01T00:00:00Z",
                    "updated_at": "2026-11-01T00:00:00Z",
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
_ANCHOR_REJECTED = {
    "description": "node_id is not in the anchored page version.",
    "content": {
        "application/json": {
            "example": {
                "error": {
                    "code": "INVALID_ANCHOR",
                    "message": "Invalid anchor.",
                    "details": {"category": "node_id"},
                    "retryable": False,
                    "request_id": "request-id",
                }
            }
        }
    },
}
_EDITED = {
    "description": "The author replaced the text. The page version id is unchanged.",
    "content": {
        "application/json": {
            "example": {
                "data": {
                    "opinion_id": "opinion-id",
                    "project_id": "project-id",
                    "author_account_id": "account-id",
                    "page_version_id": "pv-1",
                    "node_id": "node-1",
                    "body": "synthetic edited opinion",
                    "revision": 2,
                    "visibility": "project",
                    "created_at": "2026-11-01T00:00:00Z",
                    "updated_at": "2026-11-01T00:00:01Z",
                    "audit_event_id": "audit-id",
                    "project_revision": 3,
                },
                "meta": {
                    "request_id": "request-id",
                    "version": "0.1.0",
                    "project_revision": 3,
                    "audit_event_id": "audit-id",
                    "idempotency_replayed": False,
                },
            }
        }
    },
}
_EDIT_FORBIDDEN = {
    "description": "The caller is not the author, or no longer holds opinion.edit_own.",
    "content": {
        "application/json": {
            "example": {
                "error": {
                    "code": "ACTION_FORBIDDEN",
                    "message": "Action not allowed.",
                    "details": {"action": "opinion.edit_own"},
                    "retryable": False,
                    "request_id": "request-id",
                }
            }
        }
    },
}
_READ = {
    "description": "An opinion visible to someone who can read the anchored page version.",
    "content": {
        "application/json": {
            "example": {
                "data": {
                    "opinion_id": "opinion-id",
                    "project_id": "project-id",
                    "author_account_id": "account-id",
                    "page_version_id": "pv-1",
                    "node_id": "node-1",
                    "body": "synthetic opinion",
                    "revision": 1,
                    "visibility": "project",
                    "created_at": "2026-11-01T00:00:00Z",
                    "updated_at": "2026-11-01T00:00:00Z",
                },
                "meta": {"request_id": "request-id", "version": "0.1.0"},
            }
        }
    },
}
_HIDDEN = {
    "description": "No such opinion inside the caller's current authorization.",
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


def _rfc3339(value: float) -> str:
    moment = datetime.fromtimestamp(float(value), timezone.utc)
    seconds = moment.strftime("%Y-%m-%dT%H:%M:%S")
    fraction = f"{moment.microsecond:06d}".rstrip("0")
    if fraction:
        return f"{seconds}.{fraction}Z"
    return f"{seconds}Z"


def _payload(view: OpinionView, *, command: bool) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "opinion_id": view.opinion_id,
        "project_id": view.project_id,
        "author_account_id": view.author_account_id,
        "page_version_id": view.page_version_id,
        "node_id": view.node_id,
        "body": view.body,
        "revision": view.revision,
        "visibility": view.visibility,
        "created_at": _rfc3339(view.created_at),
        "updated_at": _rfc3339(view.updated_at),
    }
    if command:
        payload["audit_event_id"] = view.audit_event_id
        payload["project_revision"] = view.project_revision
    return payload


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


@router.post(
    "/projects/{project_id}/review-opinions",
    status_code=201,
    responses={201: _CREATED, 422: _ANCHOR_REJECTED},
)
async def post_review_opinion(project_id: str, request: Request) -> JSONResponse:
    body = parse_opinion_create(await request.body())
    now = time.time()
    with write_uow(get_engine(request)) as session:
        principal = _guard(request, session, now)
        authorizer = SessionAuthorizer(session)
        scope = CommandScope(
            project_id=project_id,
            actor_id=principal.account_id,
            command=Action.OPINION_CREATE.value,
            request_key=_idempotency_key(request),
        )
        replayed = _already_stored(session, scope)
        request_document = {"project_id": project_id, **body.model_dump(mode="json")}

        def authorize(active: Session) -> None:
            authorizer.authorize(
                Action.OPINION_CREATE,
                ResourceRef(
                    project_id=project_id,
                    scope_type="page_version",
                    page_version_id=body.page_version_id,
                ),
                principal,
            )

        def handler(active: Session) -> CommandResponse:
            created = create_opinion(
                active,
                project_id=project_id,
                actor_id=principal.account_id,
                page_version_id=body.page_version_id,
                node_id=body.node_id,
                body=body.body,
                visibility=body.visibility,
                now=now,
            )
            return CommandResponse(201, _payload(created, command=True))

        response = execute_idempotent(session, scope, request_document, authorize, handler)
    return _committed(request, response, replayed)


@router.patch(
    "/review-opinions/{opinion_id}",
    responses={200: _EDITED, 403: _EDIT_FORBIDDEN},
)
async def patch_review_opinion(opinion_id: str, request: Request) -> JSONResponse:
    body = parse_opinion_patch(await request.body())
    now = time.time()
    with write_uow(get_engine(request)) as session:
        principal = _guard(request, session, now)
        anchor = load_opinion(session, opinion_id)
        authorizer = SessionAuthorizer(session)
        scope = CommandScope(
            project_id=anchor.project_id,
            actor_id=principal.account_id,
            command=Action.OPINION_EDIT_OWN.value,
            request_key=_idempotency_key(request),
        )
        replayed = _already_stored(session, scope)
        request_document = {
            "opinion_id": opinion_id,
            "project_id": anchor.project_id,
            **body.model_dump(mode="json"),
        }

        def authorize(active: Session) -> None:
            current = load_opinion(active, opinion_id)
            authorizer.authorize(
                Action.OPINION_EDIT_OWN,
                ResourceRef(
                    project_id=current.project_id,
                    scope_type="page_version",
                    page_version_id=current.page_version_id,
                    author_account_id=current.author_account_id,
                ),
                principal,
            )
            if current.tombstoned:
                raise SourceRedacted()

        def handler(active: Session) -> CommandResponse:
            edited = edit_opinion(
                active,
                opinion_id=opinion_id,
                project_id=anchor.project_id,
                actor_id=principal.account_id,
                expected_revision=body.expected_revision,
                body=body.body,
                now=now,
            )
            return CommandResponse(200, _payload(edited, command=True))

        response = execute_idempotent(session, scope, request_document, authorize, handler)
    return _committed(request, response, replayed)


@router.get(
    "/review-opinions/{opinion_id}",
    responses={200: _READ, 404: _HIDDEN},
)
def get_review_opinion(opinion_id: str, request: Request) -> JSONResponse:
    settings = get_settings(request)
    with Session(get_engine(request)) as session:
        principal = resolve_principal(session, session_token(request, settings), now=time.time())
        anchor = load_opinion(session, opinion_id)
        SessionAuthorizer(session).authorize(
            Action.PAGE_READ,
            ResourceRef(
                project_id=anchor.project_id,
                scope_type="page_version",
                page_version_id=anchor.page_version_id,
            ),
            principal,
        )
        if anchor.tombstoned:
            raise SourceRedacted()
        payload = _payload(read_opinion(session, opinion_id), command=False)
    body = {
        "data": payload,
        "meta": {"request_id": request_id_for(request), "version": __version__},
    }
    return _nostore(JSONResponse(body))
