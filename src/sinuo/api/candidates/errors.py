"""Map candidate command failures onto the existing error envelope."""

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from sinuo.api.auth.errors import _MESSAGES, error_response
from sinuo.application.candidates.errors import NOT_IMPLEMENTED, CandidateCommandError
from sinuo.domain.public_contract import ErrorCode

_STATUS: dict[str, tuple[int, ErrorCode]] = {
    ErrorCode.AUTH_REQUIRED.value: (401, ErrorCode.AUTH_REQUIRED),
    ErrorCode.ACTION_FORBIDDEN.value: (403, ErrorCode.ACTION_FORBIDDEN),
    ErrorCode.NOT_FOUND_OR_HIDDEN.value: (404, ErrorCode.NOT_FOUND_OR_HIDDEN),
    ErrorCode.DRAFT_REVISION_CONFLICT.value: (409, ErrorCode.DRAFT_REVISION_CONFLICT),
    ErrorCode.HEAD_CHANGED.value: (409, ErrorCode.HEAD_CHANGED),
    ErrorCode.IDEMPOTENCY_KEY_REUSED.value: (409, ErrorCode.IDEMPOTENCY_KEY_REUSED),
    ErrorCode.CANDIDATE_ALREADY_RESOLVED.value: (409, ErrorCode.CANDIDATE_ALREADY_RESOLVED),
    ErrorCode.CANDIDATE_STALE.value: (409, ErrorCode.CANDIDATE_STALE),
    ErrorCode.PREVIEW_CHANGED.value: (409, ErrorCode.PREVIEW_CHANGED),
    ErrorCode.PREVIEW_EXPIRED.value: (409, ErrorCode.PREVIEW_EXPIRED),
    ErrorCode.SOURCE_REDACTED.value: (410, ErrorCode.SOURCE_REDACTED),
    ErrorCode.INVALID_LINEAGE.value: (422, ErrorCode.INVALID_LINEAGE),
    ErrorCode.INVALID_ANCHOR.value: (422, ErrorCode.INVALID_ANCHOR),
    ErrorCode.INVALID_SCHEMA.value: (422, ErrorCode.INVALID_SCHEMA),
    NOT_IMPLEMENTED: (422, ErrorCode.INVALID_SCHEMA),
}

# error_response looks these sentences up. The codes already exist; the table did not
# list them. setdefault leaves any sentence already registered in place.
_SENTENCES = {
    ErrorCode.CANDIDATE_ALREADY_RESOLVED: "Candidate already resolved.",
    ErrorCode.CANDIDATE_STALE: "Candidate stale.",
    ErrorCode.PREVIEW_CHANGED: "Preview changed.",
    ErrorCode.PREVIEW_EXPIRED: "Preview expired.",
    ErrorCode.INVALID_LINEAGE: "Invalid lineage.",
    ErrorCode.INVALID_ANCHOR: "Invalid anchor.",
}


def register_candidate_exception_handlers(app: FastAPI) -> None:
    """Register candidate command failures. Identity and idempotency stay global."""

    for code, sentence in _SENTENCES.items():
        _MESSAGES.setdefault(code, sentence)

    @app.exception_handler(CandidateCommandError)
    async def candidate_command(request: Request, exc: CandidateCommandError) -> JSONResponse:
        found = _STATUS.get(exc.code)
        if found is None:
            raise exc
        status, code = found
        return error_response(
            request,
            status=status,
            code=code,
            details=dict(exc.details),
            retryable=False,
        )
