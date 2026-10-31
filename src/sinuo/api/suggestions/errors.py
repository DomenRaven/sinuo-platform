"""Map a personal preference conflict without calling it a design head change."""

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from sinuo.api.auth.errors import error_response
from sinuo.domain.public_contract import ErrorCode
from sinuo.workers.suggestions import SuggestionPreferenceConflict


def register_suggestion_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(SuggestionPreferenceConflict)
    async def preference_conflict(
        request: Request,
        _exc: SuggestionPreferenceConflict,
    ) -> JSONResponse:
        return error_response(
            request,
            status=409,
            code=ErrorCode.PREFERENCE_REVISION_CONFLICT,
            details={"category": "preference_revision"},
            retryable=False,
        )
