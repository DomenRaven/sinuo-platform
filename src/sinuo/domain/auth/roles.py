"""Role templates expand into grant rows. authorize() does not call this."""

from dataclasses import dataclass

from sinuo.domain.public_contract import Action, RoleTemplate
from sinuo.domain.schema_types import ClosedSetError

_LEAD_ACTIONS: tuple[Action, ...] = (
    Action.PROJECT_READ,
    Action.STAGE_CONFIGURE,
    Action.PAGE_CREATE,
    Action.PAGE_READ,
    Action.PAGE_DRAFT_WRITE,
    Action.PAGE_CHECKPOINT,
    Action.PAGE_BRANCH,
    Action.DECISION_CONFIRM,
    Action.ARTIFACT_UPLOAD,
    Action.ARTIFACT_ORIGINAL_READ,
    Action.PREVIEW_READ,
    Action.EVIDENCE_READ,
    Action.EVIDENCE_CREATE,
    Action.RUN_CREATE,
    Action.RUN_READ,
    Action.RUN_CANCEL,
    Action.TEST_RECORD,
    Action.OPINION_CREATE,
    Action.OPINION_EDIT_OWN,
    Action.REPORT_EXPORT,
)

_PAGE_TEAM: tuple[Action, ...] = (Action.PAGE_READ, Action.PAGE_DRAFT_WRITE)
_PAGE_REVIEW: tuple[Action, ...] = (
    Action.PAGE_READ,
    Action.PREVIEW_READ,
    Action.OPINION_CREATE,
    Action.OPINION_EDIT_OWN,
)
_EXCLUDED: frozenset[Action] = frozenset(
    {Action.PROJECT_CREATE, Action.ACL_MANAGE, Action.PRIVACY_DELETE}
)


@dataclass(frozen=True)
class GrantTemplate:
    action: Action
    scope_type: str
    page_id: str | None = None


def _reject_excluded(actions: tuple[Action, ...]) -> None:
    if any(action in _EXCLUDED for action in actions):
        raise ClosedSetError("role templates cannot include create, acl, or privacy delete")


def grants_for_template(
    template: RoleTemplate,
    page_ids: tuple[str, ...] | list[str] = (),
) -> tuple[GrantTemplate, ...]:
    """Default grants for one template. page_ids are required for member and reviewer."""

    if not isinstance(template, RoleTemplate):
        template = RoleTemplate(str(template))
    pages = tuple(dict.fromkeys(page_ids))
    if any(not page_id for page_id in pages):
        raise ClosedSetError("page_ids must be non-empty strings")
    if template is RoleTemplate.LEAD_DESIGNER:
        if pages:
            raise ClosedSetError("lead_designer is project scoped")
        _reject_excluded(_LEAD_ACTIONS)
        return tuple(GrantTemplate(action, "project") for action in _LEAD_ACTIONS)
    if template is RoleTemplate.TEAM_MEMBER:
        if not pages:
            raise ClosedSetError("team_member requires page_ids")
        _reject_excluded((Action.PROJECT_READ, *_PAGE_TEAM))
        rows = [GrantTemplate(Action.PROJECT_READ, "project")]
        for page_id in pages:
            rows.extend(GrantTemplate(action, "page", page_id) for action in _PAGE_TEAM)
        return tuple(rows)
    if template is RoleTemplate.REVIEWER:
        if not pages:
            raise ClosedSetError("reviewer requires page_ids")
        _reject_excluded((Action.PROJECT_READ, *_PAGE_REVIEW))
        rows = [GrantTemplate(Action.PROJECT_READ, "project")]
        for page_id in pages:
            rows.extend(GrantTemplate(action, "page", page_id) for action in _PAGE_REVIEW)
        return tuple(rows)
    raise ClosedSetError("unknown role template")


def creator_acl_grant() -> GrantTemplate:
    """The extra project-level acl.manage row seeded for a project creator."""

    return GrantTemplate(Action.ACL_MANAGE, "project")
