"""Export command body. Unknown fields are rejected. DOCX is not a format here."""

import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from sinuo.api.auth.schemas import parse_json
from sinuo.domain.auth.errors import InvalidSchema

_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}\Z")
_LISTS = ("page_version_ids", "artifact_version_ids", "evidence_anchor_ids")


class SourceManifest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    page_version_ids: list[str] = Field(default_factory=list, max_length=50)
    artifact_version_ids: list[str] = Field(default_factory=list, max_length=50)
    evidence_anchor_ids: list[str] = Field(default_factory=list, max_length=50)

    @model_validator(mode="after")
    def ids_are_closed(self) -> "SourceManifest":
        seen: set[str] = set()
        total = 0
        for name in _LISTS:
            values = getattr(self, name)
            total += len(values)
            for value in values:
                if _ID.fullmatch(value) is None or value in seen:
                    raise ValueError(name)
                seen.add(value)
        if total < 1:
            raise ValueError("empty manifest")
        return self


class ExportCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    format: Literal["markdown"]
    include_originals: bool = Field(default=False, strict=True)
    source_manifest: SourceManifest
    confirmed_scope: SourceManifest

    @field_validator("include_originals")
    @classmethod
    def originals_are_bool(cls, value: bool) -> bool:
        if type(value) is not bool:
            raise ValueError("include_originals")
        return value

    @model_validator(mode="after")
    def scope_matches_manifest(self) -> "ExportCreate":
        if self.confirmed_scope != self.source_manifest:
            raise ValueError("confirmed_scope")
        return self


def parse_export_create(raw: bytes) -> ExportCreate:
    payload = parse_json(raw)
    if not isinstance(payload, dict):
        raise InvalidSchema()
    try:
        return ExportCreate.model_validate(payload)
    except ValidationError as error:
        raise InvalidSchema() from error
