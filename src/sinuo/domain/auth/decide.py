"""Pure authorization decision. No FastAPI, SQLAlchemy, filesystem, or model SDK."""

from dataclasses import dataclass
from enum import Enum

from sinuo.domain.auth.principal import Principal
from sinuo.domain.auth.resource import ResourceRef
from sinuo.domain.public_contract import (
    AVAILABILITY_STATES,
    GRANT_SCOPE_TYPES,
    RESOURCE_SCOPE_ACCOUNT,
    SENSITIVITY_LEVELS,
    Action,
)
from sinuo.domain.schema_types import ClosedSetError

_TARGET_BY_SCOPE: dict[str, str] = {
    "stage": "stage_id",
    "page": "page_id",
    "page_version": "page_version_id",
    "artifact": "artifact_id",
    "artifact_version": "artifact_version_id",
    "evidence_anchor": "evidence_anchor_id",
}
_SENSITIVE_OBJECT_SCOPES = frozenset({"artifact", "artifact_version", "evidence_anchor"})
_SENSITIVE_ACTIONS = frozenset({Action.ARTIFACT_ORIGINAL_READ, Action.EVIDENCE_READ})


class Decision(Enum):
    ALLOW = "allow"
    FORBIDDEN = "forbidden"
    HIDDEN = "hidden"
    REDACTED = "redacted"


@dataclass(frozen=True)
class GrantFact:
    action: str
    scope_type: str
    status: str
    stage_id: str | None = None
    page_id: str | None = None
    page_version_id: str | None = None
    artifact_id: str | None = None
    artifact_version_id: str | None = None
    evidence_anchor_id: str | None = None


@dataclass(frozen=True)
class ObjectBinding:
    """Parent ids loaded by the authorizer. They are not a second client resource."""

    stage_id: str | None = None
    page_id: str | None = None
    artifact_id: str | None = None


def _target_value(resource: ResourceRef, name: str) -> str | None:
    return getattr(resource, name)


def _validate_resource(resource: ResourceRef) -> None:
    scope = resource.scope_type
    if scope == RESOURCE_SCOPE_ACCOUNT:
        if resource.project_id is not None or any(resource.target_ids()):
            raise ClosedSetError("account scope has no project or object target")
    elif scope == "project":
        if not resource.project_id or any(resource.target_ids()):
            raise ClosedSetError("project scope names only the project")
    elif scope in _TARGET_BY_SCOPE:
        if not resource.project_id:
            raise ClosedSetError("object scope requires project_id")
        chosen = _TARGET_BY_SCOPE[scope]
        for name in _TARGET_BY_SCOPE.values():
            value = _target_value(resource, name)
            if name == chosen and not value:
                raise ClosedSetError(f"{scope} requires {chosen}")
            if name != chosen and value is not None:
                raise ClosedSetError(f"{scope} leaves other targets empty")
    else:
        raise ClosedSetError("scope_type is outside the grant and account set")
    if scope not in {*GRANT_SCOPE_TYPES, RESOURCE_SCOPE_ACCOUNT}:
        raise ClosedSetError("scope_type is outside the grant and account set")
    if resource.sensitivity is not None and resource.sensitivity not in SENSITIVITY_LEVELS:
        raise ClosedSetError("sensitivity is outside the closed set")
    if resource.availability is not None and resource.availability not in AVAILABILITY_STATES:
        raise ClosedSetError("availability is outside the closed set")


def scope_covers(grant: GrantFact, resource: ResourceRef, binding: ObjectBinding) -> bool:
    if grant.status != "active":
        return False
    if grant.scope_type == "project":
        return resource.project_id is not None
    if grant.scope_type == "stage":
        return resource.scope_type == "stage" and resource.stage_id == grant.stage_id
    if grant.scope_type == "page":
        if resource.scope_type == "page":
            return resource.page_id == grant.page_id
        if resource.scope_type == "page_version":
            return binding.page_id is not None and binding.page_id == grant.page_id
        return False
    if grant.scope_type == "page_version":
        return (
            resource.scope_type == "page_version"
            and resource.page_version_id == grant.page_version_id
        )
    if grant.scope_type == "artifact":
        return resource.scope_type == "artifact" and resource.artifact_id == grant.artifact_id
    if grant.scope_type == "artifact_version":
        return (
            resource.scope_type == "artifact_version"
            and resource.artifact_version_id == grant.artifact_version_id
        )
    if grant.scope_type == "evidence_anchor":
        return (
            resource.scope_type == "evidence_anchor"
            and resource.evidence_anchor_id == grant.evidence_anchor_id
        )
    return False


def _assigned(grants: tuple[GrantFact, ...], resource: ResourceRef, binding: ObjectBinding) -> bool:
    """Project-level project.read does not assign every child object."""

    if resource.scope_type == "project":
        return True
    for grant in grants:
        if grant.action == Action.PROJECT_READ.value and grant.scope_type == "project":
            continue
        if scope_covers(grant, resource, binding):
            return True
    return False


def _matching(
    grants: tuple[GrantFact, ...],
    action: Action,
    resource: ResourceRef,
    binding: ObjectBinding,
) -> bool:
    for grant in grants:
        if grant.action != action.value:
            continue
        if scope_covers(grant, resource, binding):
            return True
    return False


def _sensitivity_allows(action: Action, resource: ResourceRef) -> bool:
    if resource.sensitivity != "sensitive":
        return True
    if resource.scope_type not in _SENSITIVE_OBJECT_SCOPES:
        return True
    return action in _SENSITIVE_ACTIONS


def decide(
    action: Action,
    resource: ResourceRef,
    principal: Principal,
    *,
    account_status: str,
    can_create_project: bool,
    membership_status: str | None,
    grants: tuple[GrantFact, ...] = (),
    binding: ObjectBinding | None = None,
    url_project_id: str | None = None,
) -> Decision:
    """Default deny. Membership and grants are facts the caller already loaded."""

    if not isinstance(action, Action):
        raise TypeError("action must be an Action member")
    if not isinstance(principal, Principal):
        raise TypeError("principal must be a Principal")
    _validate_resource(resource)
    resolved = binding or ObjectBinding()
    if account_status != "active":
        return Decision.HIDDEN
    if url_project_id is not None and url_project_id != resource.project_id:
        return Decision.HIDDEN
    if action is Action.PROJECT_CREATE:
        if resource.scope_type != RESOURCE_SCOPE_ACCOUNT:
            return Decision.FORBIDDEN
        if can_create_project:
            return Decision.ALLOW
        return Decision.FORBIDDEN
    if membership_status != "active":
        return Decision.HIDDEN
    if resource.availability == "tombstoned":
        if _assigned(grants, resource, resolved) or _matching(grants, action, resource, resolved):
            return Decision.REDACTED
        return Decision.HIDDEN
    if _matching(grants, action, resource, resolved):
        if not _sensitivity_allows(action, resource):
            if _assigned(grants, resource, resolved):
                return Decision.FORBIDDEN
            return Decision.HIDDEN
        if action is Action.OPINION_EDIT_OWN and resource.author_account_id != principal.account_id:
            return Decision.FORBIDDEN
        return Decision.ALLOW
    if resource.scope_type == "project" or _assigned(grants, resource, resolved):
        return Decision.FORBIDDEN
    return Decision.HIDDEN
