"""Closed bodies for candidate preview, commit, and reject."""

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from sinuo.api.auth.schemas import parse_json
from sinuo.domain.auth.errors import InvalidSchema


class PreviewBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    user_patch: dict[str, Any] | None = None
    expected_page_version_id: str | None = Field(default=None, min_length=1, max_length=64)
    expected_draft_content_revision: int | None = Field(default=None, ge=1, strict=True)


class CommitBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    preview_id: str = Field(min_length=1, max_length=64)
    preview_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    expected_page_version_id: str = Field(min_length=1, max_length=64)
    expected_draft_content_revision: int | None = Field(ge=1, strict=True)
    user_patch: dict[str, Any] | None = None


class RejectBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: str | None = Field(default=None, max_length=500)


def _parse(model: type[BaseModel], payload: object) -> BaseModel:
    if not isinstance(payload, dict):
        raise InvalidSchema()
    try:
        return model.model_validate(payload)
    except ValidationError as error:
        raise InvalidSchema() from error


def parse_preview(raw: bytes) -> PreviewBody:
    return _parse(PreviewBody, parse_json(raw))  # type: ignore[return-value]


def parse_commit(raw: bytes) -> CommitBody:
    return _parse(CommitBody, parse_json(raw))  # type: ignore[return-value]


def parse_reject(raw: bytes) -> RejectBody:
    return _parse(RejectBody, parse_json(raw))  # type: ignore[return-value]
