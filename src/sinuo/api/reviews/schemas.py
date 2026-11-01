"""Review opinion bodies. Unknown fields, including a client author, are rejected."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from sinuo.api.auth.schemas import parse_json
from sinuo.domain.auth.errors import InvalidSchema


class OpinionCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    page_version_id: str = Field(min_length=1, max_length=64)
    node_id: str | None = Field(default=None, min_length=1, max_length=64)
    body: str = Field(min_length=1, max_length=8000)
    visibility: Literal["project", "restricted"]


class OpinionPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_revision: int = Field(ge=1, strict=True)
    body: str = Field(min_length=1, max_length=8000)


def _parse(model: type[BaseModel], payload: object) -> BaseModel:
    if not isinstance(payload, dict):
        raise InvalidSchema()
    try:
        return model.model_validate(payload)
    except ValidationError as error:
        raise InvalidSchema() from error


def parse_opinion_create(raw: bytes) -> OpinionCreate:
    return _parse(OpinionCreate, parse_json(raw))  # type: ignore[return-value]


def parse_opinion_patch(raw: bytes) -> OpinionPatch:
    return _parse(OpinionPatch, parse_json(raw))  # type: ignore[return-value]
