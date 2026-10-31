"""Closed request bodies for login and access changes."""

import json

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from sinuo.domain.auth.errors import InvalidSchema
from sinuo.domain.public_contract import RoleTemplate


class LoginBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    login_name: str = Field(min_length=1, max_length=256)
    password: str = Field(min_length=1, max_length=128)


class AccessPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_access_epoch: int = Field(ge=1)
    subject_account_id: str = Field(min_length=36, max_length=36)
    role_template: RoleTemplate | None = None
    page_ids: list[str] = Field(default_factory=list)
    membership_status: str | None = None
    revoke_grant_ids: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def command_is_closed(self) -> "AccessPatch":
        if self.membership_status not in {None, "active", "revoked"}:
            raise ValueError("membership_status")
        if (
            self.role_template is None
            and self.membership_status is None
            and not self.revoke_grant_ids
        ):
            raise ValueError("empty access change")
        if self.membership_status == "revoked" and self.role_template is not None:
            raise ValueError("revoke and grant")
        if self.role_template is RoleTemplate.LEAD_DESIGNER and self.page_ids:
            raise ValueError("lead_designer is project scoped")
        needs_pages = self.role_template in {RoleTemplate.TEAM_MEMBER, RoleTemplate.REVIEWER}
        if needs_pages and not self.page_ids:
            raise ValueError("page_ids required")
        if any(not page_id for page_id in self.page_ids):
            raise ValueError("page_ids")
        return self


def parse_json(raw: bytes) -> object:
    if not raw:
        raise InvalidSchema()
    try:
        return json.loads(raw)
    except json.JSONDecodeError as error:
        raise InvalidSchema() from error


def parse_login(payload: object) -> LoginBody:
    if not isinstance(payload, dict):
        raise InvalidSchema()
    try:
        return LoginBody.model_validate(payload)
    except ValidationError as error:
        raise InvalidSchema() from error


def parse_access(payload: object) -> AccessPatch:
    if not isinstance(payload, dict):
        raise InvalidSchema()
    try:
        return AccessPatch.model_validate(payload)
    except ValidationError as error:
        raise InvalidSchema() from error
