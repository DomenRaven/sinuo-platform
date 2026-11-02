"""Read one project's requirement trace.

``create_app`` mounts this router as ``trace_router``.
The read checks the session cookie and does not require a CSRF token.
"""

import time
from dataclasses import asdict

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from sinuo import __version__
from sinuo.api.auth.dependencies import get_engine, get_settings, session_token
from sinuo.api.auth.errors import request_id_for
from sinuo.api.auth.routes import _nostore
from sinuo.application.auth.authorizer import SessionAuthorizer
from sinuo.application.auth.sessions import resolve_principal
from sinuo.application.trace.requirements import RequirementTrace, trace_requirements

router = APIRouter(prefix="/api/v1")

_PATH = "/projects/{project_id}/trace"
_OK = {
    "description": "Evidence places and tests pinned to the cited work versions.",
    "content": {
        "application/json": {
            "example": {
                "data": {
                    "project_id": "project-id",
                    "page_version_id": "page-version-id",
                    "node_id": None,
                    "requirements": [],
                    "anchors": [
                        {
                            "evidence_anchor_id": "anchor-id",
                            "artifact_version_id": "version-id",
                            "locator_type": "page",
                            "page": 4,
                            "region": None,
                            "timecode": None,
                            "excerpt": "synthetic excerpt",
                            "excerpt_visible": True,
                            "supersedes_id": None,
                            "requirement_node_ids": [],
                        }
                    ],
                    "tests": [
                        {
                            "test_record_id": "record-id",
                            "artifact_version_id": "version-id",
                            "supersedes_id": None,
                            "observation": "synthetic observation",
                            "result": "synthetic result",
                            "source": None,
                            "conditions": {"task": "synthetic task"},
                            "body_visible": True,
                        }
                    ],
                    "gaps": [
                        {
                            "field": "node.kind",
                            "detail": "page content has no requirement, evidence, or test kind",
                        }
                    ],
                    "pinned_artifact_version_ids": ["version-id"],
                },
                "meta": {"request_id": "request-id", "version": "0.1.0"},
            }
        }
    },
}
_REJECTED = {
    "description": "The query names both targets, neither target, or an alias such as latest.",
    "content": {
        "application/json": {
            "example": {
                "error": {
                    "code": "INVALID_SCHEMA",
                    "message": "Invalid schema.",
                    "details": {},
                    "retryable": False,
                    "request_id": "request-id",
                }
            }
        }
    },
}


def _payload(trace: RequirementTrace) -> dict[str, object]:
    return asdict(trace)


@router.get(_PATH, responses={200: _OK, 422: _REJECTED}, tags=["trace"])
def get_requirement_trace(
    project_id: str,
    request: Request,
    page_version_id: str | None = None,
    node_id: str | None = None,
) -> JSONResponse:
    """Return anchors and pinned tests for one page version or one requirement node."""

    settings = get_settings(request)
    with Session(get_engine(request)) as session:
        principal = resolve_principal(session, session_token(request, settings), now=time.time())
        traced = trace_requirements(
            session,
            SessionAuthorizer(session),
            principal,
            project_id,
            page_version_id=page_version_id,
            node_id=node_id,
        )
    body = {
        "data": _payload(traced),
        "meta": {"request_id": request_id_for(request), "version": __version__},
    }
    return _nostore(JSONResponse(body))
