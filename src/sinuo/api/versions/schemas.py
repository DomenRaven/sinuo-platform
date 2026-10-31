"""Version command bodies. Unknown fields are rejected."""

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from sinuo.api.auth.schemas import parse_json
from sinuo.domain.auth.errors import InvalidSchema


class ProjectCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1, max_length=200)
    brief: str | None = None
    template_key: str | None = None
    page_title: str | None = None


class ParallelPageCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    stage_id: str = Field(min_length=1, max_length=64)
    title: str = Field(min_length=1, max_length=200)
    expected_revision: int = Field(ge=1)


class CheckpointBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_head_version_id: str = Field(min_length=1, max_length=64)
    expected_content_revision: int = Field(ge=1)
    expected_content_hash: str = Field(min_length=64, max_length=64)


class BranchBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_version_id: str = Field(min_length=1, max_length=64)
    title: str = Field(min_length=1, max_length=200)


class DraftPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    base_version_id: str = Field(min_length=1, max_length=64)
    expected_revision: int = Field(ge=1)
    content: dict


def _parse(model: type[BaseModel], payload: object) -> BaseModel:
    if not isinstance(payload, dict):
        raise InvalidSchema()
    try:
        return model.model_validate(payload)
    except ValidationError as error:
        raise InvalidSchema() from error


def parse_project(raw: bytes) -> ProjectCreate:
    return _parse(ProjectCreate, parse_json(raw))  # type: ignore[return-value]


def parse_parallel_page(raw: bytes) -> ParallelPageCreate:
    return _parse(ParallelPageCreate, parse_json(raw))  # type: ignore[return-value]


def parse_checkpoint(raw: bytes) -> CheckpointBody:
    return _parse(CheckpointBody, parse_json(raw))  # type: ignore[return-value]


def parse_branch(raw: bytes) -> BranchBody:
    return _parse(BranchBody, parse_json(raw))  # type: ignore[return-value]


def parse_draft(raw: bytes) -> DraftPatch:
    return _parse(DraftPatch, parse_json(raw))  # type: ignore[return-value]
