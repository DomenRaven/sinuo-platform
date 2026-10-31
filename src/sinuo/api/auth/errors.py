"""Map identity failures onto the shared error envelope."""

import re
import uuid

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from sinuo.application.auth.sessions import RateLimited
from sinuo.domain.auth.errors import (
    AccessEpochConflict,
    ActionForbidden,
    AuthRequired,
    InvalidSchema,
    NotFoundOrHidden,
    SourceRedacted,
)
from sinuo.domain.errors import IdempotencyConflict
from sinuo.domain.public_contract import ErrorCode

_REQUEST_ID = re.compile(r"^[A-Za-z0-9_-]{8,64}$")

_MESSAGES = {
    ErrorCode.AUTH_REQUIRED: "Authentication required.",
    ErrorCode.ACTION_FORBIDDEN: "Action not allowed.",
    ErrorCode.NOT_FOUND_OR_HIDDEN: "Not found.",
    ErrorCode.SOURCE_REDACTED: "Source redacted.",
    ErrorCode.INVALID_SCHEMA: "Invalid schema.",
    ErrorCode.HEAD_CHANGED: "Head changed.",
    ErrorCode.DRAFT_REVISION_CONFLICT: "Draft revision conflict.",
    ErrorCode.IDEMPOTENCY_KEY_REUSED: "Idempotency key reused.",
    ErrorCode.PREFERENCE_REVISION_CONFLICT: "Preference revision conflict.",
    ErrorCode.REQUEST_BUDGET_EXCEEDED: "Request budget exceeded.",
}


def request_id_for(request: Request) -> str:
    current = getattr(request.state, "request_id", None)
    if isinstance(current, str):
        return current
    header = request.headers.get("x-request-id", "")
    if _REQUEST_ID.fullmatch(header):
        request.state.request_id = header
        return header
    generated = str(uuid.uuid4())
    request.state.request_id = generated
    return generated


def error_response(
    request: Request,
    *,
    status: int,
    code: ErrorCode,
    details: dict,
    retryable: bool,
    retry_after: int | None = None,
) -> JSONResponse:
    body = {
        "error": {
            "code": code.value,
            "message": _MESSAGES[code],
            "details": details,
            "retryable": retryable,
            "request_id": request_id_for(request),
        }
    }
    response = JSONResponse(body, status_code=status)
    response.headers["Cache-Control"] = "no-store"
    if retry_after is not None:
        response.headers["Retry-After"] = str(retry_after)
    return response


def forbidden_details(exc: ActionForbidden) -> dict[str, str]:
    if exc.category in {"csrf", "origin"}:
        return {"category": exc.category}
    if exc.action:
        return {"action": exc.action}
    return {}


def register_auth_exception_handlers(app: FastAPI) -> None:
    """Register identity and idempotency errors. Does not install dependency overrides."""

    @app.exception_handler(AuthRequired)
    async def auth_required(request: Request, _exc: AuthRequired) -> JSONResponse:
        return error_response(
            request,
            status=401,
            code=ErrorCode.AUTH_REQUIRED,
            details={},
            retryable=False,
        )

    @app.exception_handler(ActionForbidden)
    async def action_forbidden(request: Request, exc: ActionForbidden) -> JSONResponse:
        return error_response(
            request,
            status=403,
            code=ErrorCode.ACTION_FORBIDDEN,
            details=forbidden_details(exc),
            retryable=False,
        )

    @app.exception_handler(NotFoundOrHidden)
    async def hidden(request: Request, _exc: NotFoundOrHidden) -> JSONResponse:
        return error_response(
            request,
            status=404,
            code=ErrorCode.NOT_FOUND_OR_HIDDEN,
            details={},
            retryable=False,
        )

    @app.exception_handler(SourceRedacted)
    async def redacted(request: Request, _exc: SourceRedacted) -> JSONResponse:
        return error_response(
            request,
            status=410,
            code=ErrorCode.SOURCE_REDACTED,
            details={},
            retryable=False,
        )

    @app.exception_handler(InvalidSchema)
    async def invalid_schema(request: Request, _exc: InvalidSchema) -> JSONResponse:
        return error_response(
            request,
            status=422,
            code=ErrorCode.INVALID_SCHEMA,
            details={},
            retryable=False,
        )

    @app.exception_handler(AccessEpochConflict)
    async def access_epoch(request: Request, _exc: AccessEpochConflict) -> JSONResponse:
        return error_response(
            request,
            status=409,
            code=ErrorCode.HEAD_CHANGED,
            details={"category": "access_epoch"},
            retryable=False,
        )

    @app.exception_handler(IdempotencyConflict)
    async def idempotency(request: Request, _exc: IdempotencyConflict) -> JSONResponse:
        return error_response(
            request,
            status=409,
            code=ErrorCode.IDEMPOTENCY_KEY_REUSED,
            details={},
            retryable=False,
        )

    @app.exception_handler(RateLimited)
    async def rate_limited(request: Request, exc: RateLimited) -> JSONResponse:
        return error_response(
            request,
            status=429,
            code=ErrorCode.REQUEST_BUDGET_EXCEEDED,
            details={},
            retryable=True,
            retry_after=exc.retry_after,
        )
