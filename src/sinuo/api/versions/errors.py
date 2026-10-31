"""Map version conflicts onto the existing error codes."""

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from sinuo.api.auth.errors import error_response
from sinuo.application.versions.branches import BranchConflict
from sinuo.application.versions.checkpoints import CheckpointConflict
from sinuo.application.versions.drafts import DraftConflict
from sinuo.application.versions.layouts import LayoutConflict
from sinuo.application.versions.pages import ProjectRevisionConflict
from sinuo.domain.public_contract import ErrorCode


def register_version_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(DraftConflict)
    async def draft_conflict(request: Request, _exc: DraftConflict) -> JSONResponse:
        return error_response(
            request,
            status=409,
            code=ErrorCode.DRAFT_REVISION_CONFLICT,
            details={},
            retryable=False,
        )

    @app.exception_handler(ProjectRevisionConflict)
    @app.exception_handler(CheckpointConflict)
    @app.exception_handler(BranchConflict)
    @app.exception_handler(LayoutConflict)
    async def head_conflict(request: Request, _exc: Exception) -> JSONResponse:
        return error_response(
            request,
            status=409,
            code=ErrorCode.HEAD_CHANGED,
            details={},
            retryable=False,
        )
