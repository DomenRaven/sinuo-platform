"""HTTP adapters for candidate preview, commit, and reject.

The caller holds ``write_uow``. Preview inserts a new row and does not replay.
Commit and reject call the existing functions, which own their idempotency.
``project_id`` comes from the candidate row. The actor comes from the session.
"""

import time
from datetime import datetime, timezone

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from sinuo.api.auth.dependencies import get_engine
from sinuo.api.auth.routes import _idempotency_key, _success
from sinuo.api.candidates.schemas import parse_commit, parse_preview, parse_reject
from sinuo.api.versions.routes import _guard
from sinuo.application.candidates import (
    CandidatePreviewView,
    commit_candidate,
    preview_candidate,
    reject_candidate,
)
from sinuo.application.idempotency import CommandResponse
from sinuo.domain.auth.errors import NotFoundOrHidden
from sinuo.infrastructure.database import write_uow
from sinuo.infrastructure.domain_models import Candidate

router = APIRouter(prefix="/api/v1")

_PREVIEW_OK = {
    "description": "A new preview row. No business commit and no project revision.",
    "content": {
        "application/json": {
            "example": {
                "data": {
                    "preview_id": "preview-id",
                    "preview_hash": "ab" * 32,
                    "expires_at": "2026-10-29T10:10:00Z",
                    "effective_proposal": {
                        "base_version_id": "pv-3",
                        "node_id": "synthetic-decision-node",
                        "page_id": "page-id",
                        "schema_version": 1,
                    },
                    "preconditions": [
                        {
                            "artifact_version_id": None,
                            "draft_content_hash": None,
                            "draft_content_revision": None,
                            "draft_page_id": None,
                            "page_version_id": "pv-3",
                            "source_kind": "page_version",
                        }
                    ],
                    "affected_objects": [{"page_id": "page-id"}, {"page_version_id": "pv-3"}],
                },
                "meta": {"request_id": "request-id", "version": "0.1.0"},
            }
        }
    },
}
_AUTH_REQUIRED = {
    "description": "No signed-in actor.",
    "content": {
        "application/json": {
            "example": {
                "error": {
                    "code": "AUTH_REQUIRED",
                    "message": "Authentication required.",
                    "details": {},
                    "retryable": False,
                    "request_id": "request-id",
                }
            }
        }
    },
}
_COMMIT_OK = {
    "description": (
        "First commit. meta includes audit_event_id and idempotency_replayed. "
        "project_revision is included only when the service returned one."
    ),
    "content": {
        "application/json": {
            "example": {
                "data": {
                    "audit_event_id": "audit-id",
                    "candidate_id": "candidate-id",
                    "content_hash": "cd" * 32,
                    "content_revision": 2,
                    "disposition_id": "disposition-id",
                    "page_id": "page-id",
                    "preview_id": "preview-id",
                    "status": "committed",
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
_COMMIT_REPLAY = {
    "description": "Same idempotency key and same body.",
    "content": {
        "application/json": {
            "example": {
                "data": {
                    "audit_event_id": "audit-id",
                    "candidate_id": "candidate-id",
                    "disposition_id": "disposition-id",
                    "preview_id": "preview-id",
                    "status": "committed",
                },
                "meta": {
                    "request_id": "request-id",
                    "version": "0.1.0",
                    "audit_event_id": "audit-id",
                    "idempotency_replayed": True,
                },
            }
        }
    },
}
_PREVIEW_CHANGED = {
    "description": "Saved preview hash does not match.",
    "content": {
        "application/json": {
            "example": {
                "error": {
                    "code": "PREVIEW_CHANGED",
                    "message": "Preview changed.",
                    "details": {},
                    "retryable": False,
                    "request_id": "request-id",
                }
            }
        }
    },
}
_REJECT_OK = {
    "description": "Candidate rejected. The business graph and project revision stay put.",
    "content": {
        "application/json": {
            "example": {
                "data": {
                    "audit_event_id": "audit-id",
                    "candidate_id": "candidate-id",
                    "disposition_id": "disposition-id",
                    "status": "rejected",
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


def _rfc3339(value: float) -> str:
    moment = datetime.fromtimestamp(float(value), timezone.utc)
    seconds = moment.strftime("%Y-%m-%dT%H:%M:%S")
    fraction = f"{moment.microsecond:06d}".rstrip("0")
    if fraction:
        return f"{seconds}.{fraction}Z"
    return f"{seconds}Z"


def _project_id(session: Session, candidate_id: str) -> str:
    candidate = session.get(Candidate, candidate_id)
    if candidate is None:
        raise NotFoundOrHidden()
    return candidate.project_id


def _preview_data(view: CandidatePreviewView) -> dict:
    return {
        "preview_id": view.preview_id,
        "preview_hash": view.preview_hash,
        "expires_at": _rfc3339(view.expires_at),
        "effective_proposal": view.effective_proposal,
        "preconditions": list(view.preconditions),
        "affected_objects": list(view.affected_objects),
    }


def _confirmed(request: Request, response: CommandResponse, replayed: bool) -> JSONResponse:
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


@router.post(
    "/candidates/{candidate_id}/preview",
    responses={200: _PREVIEW_OK, 401: _AUTH_REQUIRED},
)
async def post_preview(candidate_id: str, request: Request) -> JSONResponse:
    body = parse_preview(await request.body())
    now = time.time()
    with write_uow(get_engine(request)) as session:
        principal = _guard(request, session, now)
        project_id = _project_id(session, candidate_id)
        view = preview_candidate(
            session,
            principal=principal,
            project_id=project_id,
            candidate_id=candidate_id,
            now=now,
            user_patch=body.user_patch,
            expected_page_version_id=body.expected_page_version_id,
            expected_draft_content_revision=body.expected_draft_content_revision,
        )
        data = _preview_data(view)
    return _success(request, data)


@router.post(
    "/candidates/{candidate_id}/commit",
    status_code=201,
    responses={201: _COMMIT_OK, 200: _COMMIT_REPLAY, 409: _PREVIEW_CHANGED},
)
async def post_commit(candidate_id: str, request: Request) -> JSONResponse:
    body = parse_commit(await request.body())
    now = time.time()
    with write_uow(get_engine(request)) as session:
        principal = _guard(request, session, now)
        request_key = _idempotency_key(request)
        project_id = _project_id(session, candidate_id)
        response, replayed = commit_candidate(
            session,
            principal=principal,
            project_id=project_id,
            candidate_id=candidate_id,
            preview_id=body.preview_id,
            preview_hash=body.preview_hash,
            expected_page_version_id=body.expected_page_version_id,
            expected_draft_content_revision=body.expected_draft_content_revision,
            request_key=request_key,
            now=now,
            user_patch=body.user_patch,
        )
    return _confirmed(request, response, replayed)


@router.post(
    "/candidates/{candidate_id}/reject",
    status_code=201,
    responses={201: _REJECT_OK, 401: _AUTH_REQUIRED},
)
async def post_reject(candidate_id: str, request: Request) -> JSONResponse:
    body = parse_reject(await request.body())
    now = time.time()
    with write_uow(get_engine(request)) as session:
        principal = _guard(request, session, now)
        request_key = _idempotency_key(request)
        project_id = _project_id(session, candidate_id)
        response, replayed = reject_candidate(
            session,
            principal=principal,
            project_id=project_id,
            candidate_id=candidate_id,
            request_key=request_key,
            now=now,
            reason=body.reason,
        )
    return _confirmed(request, response, replayed)
