"""Shared action, error and vocabulary literals. Integration owner only."""

from enum import StrEnum
from urllib.parse import urlparse

CONTRACT_VERSION = "T02-T03-SHARED-1"

HTTP_SESSION_COOKIE = "sinuo_session"
HTTPS_SESSION_COOKIE = "__Host-sinuo_session"
CSRF_HEADER = "X-CSRF-Token"


class Action(StrEnum):
    PROJECT_READ = "project.read"
    PROJECT_CREATE = "project.create"
    STAGE_CONFIGURE = "stage.configure"
    PAGE_CREATE = "page.create"
    PAGE_READ = "page.read"
    PAGE_DRAFT_WRITE = "page.draft.write"
    PAGE_CHECKPOINT = "page.checkpoint"
    PAGE_BRANCH = "page.branch"
    DECISION_CONFIRM = "decision.confirm"
    ARTIFACT_UPLOAD = "artifact.upload"
    ARTIFACT_ORIGINAL_READ = "artifact.original.read"
    PREVIEW_READ = "preview.read"
    EVIDENCE_READ = "evidence.read"
    EVIDENCE_CREATE = "evidence.create"
    RUN_CREATE = "run.create"
    RUN_READ = "run.read"
    RUN_CANCEL = "run.cancel"
    TEST_RECORD = "test.record"
    OPINION_CREATE = "opinion.create"
    OPINION_EDIT_OWN = "opinion.edit_own"
    REPORT_EXPORT = "report.export"
    ACL_MANAGE = "acl.manage"
    PRIVACY_DELETE = "privacy.delete"


OBJECT_GRANT_ACTIONS: tuple[Action, ...] = tuple(
    action for action in Action if action is not Action.PROJECT_CREATE
)


class ErrorCode(StrEnum):
    AUTH_REQUIRED = "AUTH_REQUIRED"
    ACTION_FORBIDDEN = "ACTION_FORBIDDEN"
    NOT_FOUND_OR_HIDDEN = "NOT_FOUND_OR_HIDDEN"
    DRAFT_REVISION_CONFLICT = "DRAFT_REVISION_CONFLICT"
    HEAD_CHANGED = "HEAD_CHANGED"
    IDEMPOTENCY_KEY_REUSED = "IDEMPOTENCY_KEY_REUSED"
    PREFERENCE_REVISION_CONFLICT = "PREFERENCE_REVISION_CONFLICT"
    CANDIDATE_ALREADY_RESOLVED = "CANDIDATE_ALREADY_RESOLVED"
    CANDIDATE_STALE = "CANDIDATE_STALE"
    PREVIEW_CHANGED = "PREVIEW_CHANGED"
    PREVIEW_EXPIRED = "PREVIEW_EXPIRED"
    SOURCE_REDACTED = "SOURCE_REDACTED"
    UPLOAD_TOO_LARGE = "UPLOAD_TOO_LARGE"
    INVALID_LINEAGE = "INVALID_LINEAGE"
    INVALID_ANCHOR = "INVALID_ANCHOR"
    INVALID_SCHEMA = "INVALID_SCHEMA"
    REQUEST_BUDGET_EXCEEDED = "REQUEST_BUDGET_EXCEEDED"
    STORAGE_BUSY = "STORAGE_BUSY"
    PROVIDER_UNAVAILABLE = "PROVIDER_UNAVAILABLE"


class RoleTemplate(StrEnum):
    LEAD_DESIGNER = "lead_designer"
    TEAM_MEMBER = "team_member"
    REVIEWER = "reviewer"


GRANT_SCOPE_TYPES: tuple[str, ...] = (
    "project",
    "stage",
    "page",
    "page_version",
    "artifact",
    "artifact_version",
    "evidence_anchor",
)
RESOURCE_SCOPE_ACCOUNT = "account"
SENSITIVITY_LEVELS: tuple[str, ...] = ("project", "sensitive", "released")
AVAILABILITY_STATES: tuple[str, ...] = ("available", "tombstoned", "abandoned")


def session_cookie_name(public_url: str) -> str:
    scheme = urlparse(public_url).scheme
    if scheme == "https":
        return HTTPS_SESSION_COOKIE
    return HTTP_SESSION_COOKIE
