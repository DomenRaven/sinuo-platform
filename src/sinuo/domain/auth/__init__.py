"""Authorization vocabulary and pure decisions.

Action members, error codes, role template names, and cookie names are imported
from ``sinuo.domain.public_contract``. This package does not redefine them.
"""

from sinuo.domain.auth.cache import authorization_cache_key, cache_key_is_current
from sinuo.domain.auth.decide import Decision, GrantFact, ObjectBinding, decide, scope_covers
from sinuo.domain.auth.errors import (
    AccessEpochConflict,
    ActionForbidden,
    AuthRequired,
    InvalidSchema,
    NotFoundOrHidden,
    SourceRedacted,
)
from sinuo.domain.auth.principal import Principal
from sinuo.domain.auth.projection import project_receipt, project_visible_fields
from sinuo.domain.auth.resource import ResourceRef, account_resource
from sinuo.domain.auth.roles import GrantTemplate, creator_acl_grant, grants_for_template
from sinuo.domain.public_contract import Action, ErrorCode, RoleTemplate

__all__ = [
    "AccessEpochConflict",
    "Action",
    "ActionForbidden",
    "AuthRequired",
    "Decision",
    "ErrorCode",
    "GrantFact",
    "GrantTemplate",
    "InvalidSchema",
    "NotFoundOrHidden",
    "ObjectBinding",
    "Principal",
    "ResourceRef",
    "RoleTemplate",
    "SourceRedacted",
    "account_resource",
    "authorization_cache_key",
    "cache_key_is_current",
    "creator_acl_grant",
    "decide",
    "grants_for_template",
    "project_receipt",
    "project_visible_fields",
    "scope_covers",
]
