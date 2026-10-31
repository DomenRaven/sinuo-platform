"""SSE replay for one persisted agent run.

``create_app`` includes this router. The cookie is resolved the same way as
the version history read. ``replay_events`` performs ``run.read`` through
``SessionAuthorizer`` before it keeps a payload. Each response frame is the
next persisted row only. The frame after that is loaded in a new session once
the previous frame has been produced, so a revoked grant fails before that
later payload is written.
"""

import json
import time
from collections.abc import Iterator

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from sqlalchemy.orm import Session

from sinuo.api.auth.dependencies import get_engine, get_settings, session_token
from sinuo.api.auth.errors import forbidden_details, request_id_for
from sinuo.api.events.replay import (
    RESET_EVENT_TYPE,
    EventCursorRejected,
    ReplayEvent,
    replay_events,
)
from sinuo.application.auth.sessions import resolve_principal
from sinuo.domain.auth.errors import ActionForbidden, AuthRequired, NotFoundOrHidden
from sinuo.domain.public_contract import ErrorCode
from sinuo.infrastructure.domain_models import AgentRun, RunEvent

router = APIRouter(prefix="/api/v1")

_CURSOR_MESSAGE = "Last-Event-ID is outside this run."


def _failure(request: Request, exc: Exception) -> tuple[int, dict[str, object]]:
    if isinstance(exc, AuthRequired):
        status = 401
        code = ErrorCode.AUTH_REQUIRED
        message = "Authentication required."
        details: dict[str, str] = {}
    elif isinstance(exc, ActionForbidden):
        status = 403
        code = ErrorCode.ACTION_FORBIDDEN
        message = "Action not allowed."
        details = forbidden_details(exc)
    elif isinstance(exc, NotFoundOrHidden):
        status = 404
        code = ErrorCode.NOT_FOUND_OR_HIDDEN
        message = "Not found."
        details = {}
    elif isinstance(exc, EventCursorRejected):
        status = 422
        code = ErrorCode.INVALID_SCHEMA
        message = _CURSOR_MESSAGE
        details = {"category": "last_event_id"}
    else:
        raise exc
    body = {
        "error": {
            "code": code.value,
            "message": message,
            "details": details,
            "retryable": False,
            "request_id": request_id_for(request),
        }
    }
    return status, body


def _json_error(request: Request, exc: Exception) -> JSONResponse:
    status, body = _failure(request, exc)
    response = JSONResponse(body, status_code=status)
    response.headers["Cache-Control"] = "no-store"
    return response


def _error_frame(request: Request, exc: Exception) -> str:
    body = _failure(request, exc)[1]
    encoded = json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return f"event: error\ndata: {encoded}\n\n"


def _data_frame(event: ReplayEvent) -> str:
    data = {
        "schema_version": event.schema_version,
        "event_id": event.event_id,
        "seq": event.seq,
        "type": event.event_type,
        "project_id": event.project_id,
        "run_id": event.run_id,
        "task_id": event.task_id,
        "trace_id": event.trace_id,
        "payload": event.payload,
        "progress": event.progress,
    }
    encoded = json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return f"id: {event.event_id}\nevent: {event.event_type}\ndata: {encoded}\n\n"


def _detach(event: ReplayEvent) -> ReplayEvent:
    return ReplayEvent(
        event_id=event.event_id,
        seq=event.seq,
        event_type=event.event_type,
        project_id=event.project_id,
        run_id=event.run_id,
        schema_version=event.schema_version,
        payload=dict(event.payload),
        progress=event.progress,
        task_id=event.task_id,
        trace_id=event.trace_id,
        created_at=event.created_at,
    )


def _next_event(request: Request, run_id: str, last_event_id: str | None) -> ReplayEvent | None:
    """Authorize, then keep only the next persisted frame from this read."""

    settings = get_settings(request)
    with Session(get_engine(request)) as session:
        principal = resolve_principal(session, session_token(request, settings), now=time.time())
        run = session.get(AgentRun, run_id)
        if run is None:
            raise NotFoundOrHidden()
        result = replay_events(
            session,
            RunEvent,
            principal=principal,
            project_id=run.project_id,
            run_id=run.id,
            last_event_id=last_event_id,
        )
        if not result.events:
            return None
        return _detach(result.events[0])


def _frames(request: Request, run_id: str, opened: ReplayEvent | None) -> Iterator[str]:
    if opened is None:
        return
    yield _data_frame(opened)
    if opened.event_type == RESET_EVENT_TYPE:
        return
    seen = opened.event_id
    while True:
        try:
            nxt = _next_event(request, run_id, seen)
        except (AuthRequired, ActionForbidden, NotFoundOrHidden, EventCursorRejected) as exc:
            yield _error_frame(request, exc)
            return
        if nxt is None or nxt.event_id == seen:
            return
        yield _data_frame(nxt)
        if nxt.event_type == RESET_EVENT_TYPE:
            return
        seen = nxt.event_id


@router.get("/agent-runs/{run_id}/events")
def get_agent_run_events(run_id: str, request: Request) -> Response:
    cursor = request.headers.get("last-event-id")
    try:
        opened = _next_event(request, run_id, cursor)
    except (AuthRequired, ActionForbidden, NotFoundOrHidden, EventCursorRejected) as exc:
        return _json_error(request, exc)
    return StreamingResponse(
        _frames(request, run_id, opened),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-store"},
    )
