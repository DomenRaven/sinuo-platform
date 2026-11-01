"""Map review conflicts onto error codes that 04 already lists."""

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from sinuo.api.auth.errors import _MESSAGES, error_response
from sinuo.application.reviews.opinions import OpinionAnchorInvalid, OpinionRevisionConflict
from sinuo.domain.public_contract import ErrorCode


def register_review_exception_handlers(app: FastAPI) -> None:
    """Register opinion failures. Identity, idempotency, and head conflicts stay global."""

    _MESSAGES.setdefault(ErrorCode.INVALID_ANCHOR, "Invalid anchor.")

    @app.exception_handler(OpinionRevisionConflict)
    async def opinion_revision(request: Request, _exc: OpinionRevisionConflict) -> JSONResponse:
        return error_response(
            request,
            status=409,
            code=ErrorCode.DRAFT_REVISION_CONFLICT,
            details={"category": "opinion_revision"},
            retryable=False,
        )

    @app.exception_handler(OpinionAnchorInvalid)
    async def opinion_anchor(request: Request, _exc: OpinionAnchorInvalid) -> JSONResponse:
        return error_response(
            request,
            status=422,
            code=ErrorCode.INVALID_ANCHOR,
            details={"category": "node_id"},
            retryable=False,
        )
