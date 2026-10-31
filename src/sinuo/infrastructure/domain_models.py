"""SQLite mappings for the domain revision. Uses the existing foundation Base."""

from sqlalchemy import (
    CheckConstraint,
    Float,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    literal_column,
)
from sqlalchemy import text as sql_text
from sqlalchemy.orm import Mapped, mapped_column

from sinuo.domain.schema_types import (
    ACCOUNT_STATUSES,
    CANDIDATE_ACTIONS,
    CANDIDATE_STATUSES,
    DISPOSITION_OUTCOMES,
    EXECUTION_STATUSES,
    GRANT_ACTIONS,
    GRANT_SCOPE_TYPES,
    GRANT_STATUSES,
    JOB_KINDS,
    JOB_STATUSES,
    LINEAGE_KINDS,
    LOCATOR_TYPES,
    MEMBERSHIP_STATUSES,
    PAGE_MODES,
    PAGE_STATUSES,
    PREVIEW_SOURCE_KINDS,
    RESULT_DISPOSITIONS,
    RUN_ROLES,
    SENSITIVITY_LEVELS,
    TASK_STATUSES,
    TOMBSTONE_OBJECT_TYPES,
    TOMBSTONE_REASON_CATEGORIES,
    VISIBILITY_LEVELS,
)
from sinuo.infrastructure.models import Base

FOUNDATION_TABLES: tuple[str, ...] = (
    "idempotency_records",
    "outbox_events",
    "outbox_deliveries",
)
# Child tables first. login_throttles has no foreign keys and is dropped first.
# object_grants is ahead of evidence and versions because those are its parents.
DOWNGRADE_ORDER: tuple[str, ...] = (
    "login_throttles",
    "privacy_tombstones",
    "consumer_receipts",
    "jobs",
    "candidate_dispositions",
    "candidate_preview_sources",
    "candidate_previews",
    "candidates",
    "run_events",
    "run_read_records",
    "agent_results",
    "run_task_dependencies",
    "run_tasks",
    "agent_runs",
    "context_snapshot_refs",
    "context_snapshots",
    "audit_events",
    "object_grants",
    "review_opinions",
    "test_records",
    "decisions",
    "evidence_anchors",
    "page_edges",
    "page_drafts",
    "view_states",
    "page_versions",
    "pages",
    "stage_config_versions",
    "stages",
    "artifact_versions",
    "artifacts",
    "memberships",
    "sessions",
    "accounts",
    "projects",
)
CREATE_ORDER: tuple[str, ...] = tuple(reversed(DOWNGRADE_ORDER))

_FINITE_LEASE = (
    "lease_until BETWEEN -1.7976931348623157e308 AND 1.7976931348623157e308"
)
_SCOPE_COLUMN = {
    "project": None,
    "stage": "stage_id",
    "page": "page_id",
    "page_version": "page_version_id",
    "artifact": "artifact_id",
    "artifact_version": "artifact_version_id",
    "evidence_anchor": "evidence_anchor_id",
}
if set(_SCOPE_COLUMN) != set(GRANT_SCOPE_TYPES):
    raise RuntimeError("grant scope columns do not match public_contract")
_TARGET_COLUMNS = tuple(
    column
    for column in (_SCOPE_COLUMN[scope] for scope in GRANT_SCOPE_TYPES)
    if column is not None
)


def sql_in(column: str, values: tuple[str, ...]) -> str:
    rendered = ", ".join("'" + item.replace("'", "''") + "'" for item in values)
    return f"{column} IN ({rendered})"


def hex64(column: str, *, nullable: bool = False) -> str:
    rule = f"length({column}) = 64 AND {column} NOT GLOB '*[^0-9a-f]*'"
    if nullable:
        return f"({column} IS NULL OR ({rule}))"
    return f"({rule})"


def json_ok(column: str, *, nullable: bool = False) -> str:
    if nullable:
        return f"({column} IS NULL OR json_valid({column}))"
    return f"json_valid({column})"


def fk_cols(
    columns: tuple[str, ...],
    ref_table: str,
    ref_columns: tuple[str, ...],
    name: str,
) -> ForeignKeyConstraint:
    return ForeignKeyConstraint(
        list(columns),
        [f"{ref_table}.{column}" for column in ref_columns],
        ondelete="RESTRICT",
        onupdate="RESTRICT",
        name=name,
    )


def same_project(column: str, ref_table: str, name: str) -> ForeignKeyConstraint:
    return fk_cols(("project_id", column), ref_table, ("project_id", "id"), name)


def project_fk(name: str) -> ForeignKey:
    return ForeignKey("projects.id", ondelete="RESTRICT", onupdate="RESTRICT", name=name)


def account_fk(name: str) -> ForeignKey:
    return ForeignKey("accounts.id", ondelete="RESTRICT", onupdate="RESTRICT", name=name)


def lease_rule(statuses: tuple[str, ...]) -> str:
    idle = ", ".join(f"'{item}'" for item in statuses if item != "running")
    return (
        "((status = 'running' AND lease_owner IS NOT NULL "
        "AND length(trim(lease_owner)) > 0 AND lease_until IS NOT NULL "
        f"AND {_FINITE_LEASE} AND fencing_token > 0) OR "
        f"(status IN ({idle}) AND lease_owner IS NULL AND lease_until IS NULL))"
    )


def _grant_target_sql() -> str:
    parts: list[str] = []
    for scope in GRANT_SCOPE_TYPES:
        target = _SCOPE_COLUMN[scope]
        terms = [f"scope_type = '{scope}'"]
        for column in _TARGET_COLUMNS:
            if column == target:
                terms.append(f"{column} IS NOT NULL")
            else:
                terms.append(f"{column} IS NULL")
        parts.append("(" + " AND ".join(terms) + ")")
    return "(" + " OR ".join(parts) + ")"


def _page_mode_sql() -> str:
    gated = ("create_page_from_versions", "suggest_stage_transition")
    listed = ", ".join(f"'{item}'" for item in gated)
    return (
        f"((action IN ({listed}) AND page_mode IS NOT NULL AND {sql_in('page_mode', PAGE_MODES)}) "
        f"OR (action NOT IN ({listed}) AND page_mode IS NULL))"
    )


def _source_kind_sql() -> str:
    targets = ("page_version_id", "artifact_version_id", "draft_page_id")
    kind_column = {
        "page_version": "page_version_id",
        "artifact_version": "artifact_version_id",
        "draft_stamp": "draft_page_id",
    }
    parts: list[str] = []
    for kind in PREVIEW_SOURCE_KINDS:
        chosen = kind_column[kind]
        terms = [f"source_kind = '{kind}'"]
        for column in targets:
            terms.append(f"{column} IS NOT NULL" if column == chosen else f"{column} IS NULL")
        if kind == "draft_stamp":
            terms.append("draft_content_revision >= 1")
            terms.append(hex64("draft_content_hash"))
        else:
            terms.append("draft_content_revision IS NULL")
            terms.append("draft_content_hash IS NULL")
        parts.append("(" + " AND ".join(terms) + ")")
    return "(" + " OR ".join(parts) + ")"


def _grant_indexes() -> tuple[Index, ...]:
    specs = (
        ("uq_grants_project_active", "project", ()),
        ("uq_grants_stage_active", "stage", ("stage_id",)),
        ("uq_grants_page_active", "page", ("page_id",)),
        ("uq_grants_page_version_active", "page_version", ("page_version_id",)),
        ("uq_grants_artifact_active", "artifact", ("artifact_id",)),
        (
            "uq_grants_artifact_version_active",
            "artifact_version",
            ("artifact_version_id",),
        ),
        ("uq_grants_evidence_active", "evidence_anchor", ("evidence_anchor_id",)),
    )
    built: list[Index] = []
    for name, scope, extra in specs:
        where = sql_text(f"scope_type = '{scope}' AND status = 'active'")
        built.append(
            Index(
                name,
                "project_id",
                "account_id",
                "action",
                *extra,
                unique=True,
                sqlite_where=where,
            )
        )
    return tuple(built)


_DRAFT_TRIPLE_SQL = (
    "((draft_content_json IS NULL AND draft_content_hash IS NULL "
    "AND draft_content_revision IS NULL) OR "
    "(json_valid(draft_content_json) AND draft_content_revision >= 1 AND "
    "length(draft_content_hash) = 64 AND draft_content_hash NOT GLOB '*[^0-9a-f]*'))"
)
_OBJECT_KEY_SQL = (
    "length(object_key) > 0 AND substr(object_key, 1, 1) <> '/' "
    "AND instr(object_key, '..') = 0 AND instr(object_key, char(92)) = 0"
)
_RESULT_SQL = (
    f"({sql_in('disposition', RESULT_DISPOSITIONS)} AND {json_ok('usage_json')} AND "
    "((disposition = 'published' AND output_json IS NOT NULL AND json_valid(output_json) "
    "AND diagnostic_code IS NULL) OR "
    "(disposition = 'discarded_late' AND output_json IS NULL AND diagnostic_code IS NOT NULL)))"
)
_READ_ONE_SQL = (
    "((page_version_id IS NOT NULL) + (artifact_version_id IS NOT NULL) "
    "+ (evidence_anchor_id IS NOT NULL) = 1)"
)
_DECISION_TARGET_SQL = (
    "(page_version_id IS NOT NULL OR artifact_version_id IS NOT NULL OR stage_id IS NOT NULL)"
)
_ACTIVE_OR_REVOKED = (
    "(status = 'active' AND revoked_at IS NULL) OR "
    "(status = 'revoked' AND revoked_at IS NOT NULL)"
)


class Project(Base):
    __tablename__ = "projects"
    __table_args__ = (
        CheckConstraint("revision >= 1", name="ck_projects_revision"),
        CheckConstraint("access_epoch >= 1", name="ck_projects_access_epoch"),
        CheckConstraint("length(trim(title)) > 0", name="ck_projects_title"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    title: Mapped[str] = mapped_column(String(200))
    brief: Mapped[str | None] = mapped_column(Text, nullable=True)
    template_key: Mapped[str | None] = mapped_column(String(100), nullable=True)
    revision: Mapped[int] = mapped_column(Integer)
    access_epoch: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[float] = mapped_column(Float)


class Account(Base):
    __tablename__ = "accounts"
    __table_args__ = (
        CheckConstraint("length(id) = 36", name="ck_accounts_id_len"),
        CheckConstraint(
            "length(login_name) BETWEEN 1 AND 64 AND login_name = trim(login_name)",
            name="ck_accounts_login_name",
        ),
        CheckConstraint("password_hash LIKE '$argon2id$%'", name="ck_accounts_password_hash"),
        CheckConstraint(sql_in("status", ACCOUNT_STATUSES), name="ck_accounts_status"),
        CheckConstraint("auth_epoch >= 1", name="ck_accounts_auth_epoch"),
        CheckConstraint("can_create_project IN (0, 1)", name="ck_accounts_can_create"),
        CheckConstraint(
            "(status = 'active' AND disabled_at IS NULL) OR "
            "(status = 'disabled' AND disabled_at IS NOT NULL)",
            name="ck_accounts_disabled",
        ),
        CheckConstraint("length(trim(display_name)) > 0", name="ck_accounts_display_name"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    login_name: Mapped[str] = mapped_column(String(128), unique=True)
    display_name: Mapped[str] = mapped_column(String(200))
    password_hash: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(16))
    auth_epoch: Mapped[int] = mapped_column(Integer)
    can_create_project: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[float] = mapped_column(Float)
    disabled_at: Mapped[float | None] = mapped_column(Float, nullable=True)


class LoginThrottle(Base):
    __tablename__ = "login_throttles"
    __table_args__ = (CheckConstraint("attempts >= 0", name="ck_login_throttles_attempts"),)

    bucket_key: Mapped[str] = mapped_column(String(128), primary_key=True)
    window_started_at: Mapped[float] = mapped_column(Float)
    attempts: Mapped[int] = mapped_column(Integer)
    updated_at: Mapped[float] = mapped_column(Float)


class Session(Base):
    __tablename__ = "sessions"
    __table_args__ = (
        CheckConstraint(hex64("token_sha256"), name="ck_sessions_token"),
        CheckConstraint(hex64("csrf_sha256", nullable=True), name="ck_sessions_csrf"),
        CheckConstraint("auth_epoch >= 1", name="ck_sessions_auth_epoch"),
        CheckConstraint("idle_expires_at <= absolute_expires_at", name="ck_sessions_idle"),
        CheckConstraint(
            "revoked_at IS NULL OR revoked_at >= created_at",
            name="ck_sessions_revoked",
        ),
        Index("ix_sessions_account_revoked", "account_id", "revoked_at"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    account_id: Mapped[str] = mapped_column(String(64), account_fk("fk_sessions_account"))
    token_sha256: Mapped[str] = mapped_column(String(64), unique=True)
    csrf_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    auth_epoch: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[float] = mapped_column(Float)
    last_active_at: Mapped[float] = mapped_column(Float)
    idle_expires_at: Mapped[float] = mapped_column(Float)
    absolute_expires_at: Mapped[float] = mapped_column(Float)
    revoked_at: Mapped[float | None] = mapped_column(Float, nullable=True)


class Membership(Base):
    __tablename__ = "memberships"
    __table_args__ = (
        UniqueConstraint("project_id", "account_id", name="uq_memberships_project_account"),
        UniqueConstraint("project_id", "id", name="uq_memberships_project_id"),
        CheckConstraint(sql_in("status", MEMBERSHIP_STATUSES), name="ck_memberships_status"),
        CheckConstraint(_ACTIVE_OR_REVOKED, name="ck_memberships_revoked"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    project_id: Mapped[str] = mapped_column(String(64), project_fk("fk_memberships_project"))
    account_id: Mapped[str] = mapped_column(String(64), account_fk("fk_memberships_account"))
    status: Mapped[str] = mapped_column(String(16))
    role_label: Mapped[str | None] = mapped_column(String(100), nullable=True)
    created_at: Mapped[float] = mapped_column(Float)
    revoked_at: Mapped[float | None] = mapped_column(Float, nullable=True)


class Artifact(Base):
    __tablename__ = "artifacts"
    __table_args__ = (
        UniqueConstraint("project_id", "id", name="uq_artifacts_project_id"),
        CheckConstraint("length(trim(title)) > 0", name="ck_artifacts_title"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    project_id: Mapped[str] = mapped_column(String(64), project_fk("fk_artifacts_project"))
    title: Mapped[str] = mapped_column(String(200))
    created_at: Mapped[float] = mapped_column(Float)


class ArtifactVersion(Base):
    __tablename__ = "artifact_versions"
    __table_args__ = (
        UniqueConstraint("artifact_id", "version_no", name="uq_artifact_versions_number"),
        UniqueConstraint("project_id", "id", name="uq_artifact_versions_project_id"),
        fk_cols(
            ("project_id", "artifact_id"),
            "artifacts",
            ("project_id", "id"),
            "fk_artifact_versions_artifact",
        ),
        CheckConstraint("version_no >= 1", name="ck_artifact_versions_number"),
        CheckConstraint("size_bytes >= 0", name="ck_artifact_versions_size"),
        CheckConstraint(hex64("content_hash"), name="ck_artifact_versions_hash"),
        CheckConstraint(_OBJECT_KEY_SQL, name="ck_artifact_versions_object_key"),
        CheckConstraint(
            sql_in("sensitivity", SENSITIVITY_LEVELS),
            name="ck_artifact_versions_sensitivity",
        ),
        CheckConstraint("ai_processing_allowed IN (0, 1)", name="ck_artifact_versions_ai"),
        CheckConstraint("project_revision >= 1", name="ck_artifact_versions_project_revision"),
        CheckConstraint("length(trim(media_type)) > 0", name="ck_artifact_versions_media"),
        CheckConstraint("length(trim(license)) > 0", name="ck_artifact_versions_license"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    project_id: Mapped[str] = mapped_column(String(64))
    artifact_id: Mapped[str] = mapped_column(String(64))
    version_no: Mapped[int] = mapped_column(Integer)
    content_hash: Mapped[str] = mapped_column(String(64))
    size_bytes: Mapped[int] = mapped_column(Integer)
    media_type: Mapped[str] = mapped_column(String(255))
    object_key: Mapped[str] = mapped_column(String(512))
    license: Mapped[str] = mapped_column(String(200))
    sensitivity: Mapped[str] = mapped_column(String(32))
    ai_processing_allowed: Mapped[int] = mapped_column(Integer)
    project_revision: Mapped[int] = mapped_column(Integer)
    created_by_account_id: Mapped[str] = mapped_column(
        String(64), account_fk("fk_artifact_versions_author")
    )
    created_at: Mapped[float] = mapped_column(Float)


class Stage(Base):
    __tablename__ = "stages"
    __table_args__ = (UniqueConstraint("project_id", "id", name="uq_stages_project_id"),)

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    project_id: Mapped[str] = mapped_column(String(64), project_fk("fk_stages_project"))
    head_config_version_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[float] = mapped_column(Float)


class StageConfigVersion(Base):
    __tablename__ = "stage_config_versions"
    __table_args__ = (
        UniqueConstraint("stage_id", "version_no", name="uq_stage_config_number"),
        UniqueConstraint("project_id", "id", name="uq_stage_config_project_id"),
        UniqueConstraint("project_id", "stage_id", "id", name="uq_stage_config_project_stage"),
        fk_cols(
            ("project_id", "stage_id"),
            "stages",
            ("project_id", "id"),
            "fk_stage_config_stage",
        ),
        CheckConstraint("version_no >= 1", name="ck_stage_config_number"),
        CheckConstraint("position >= 0", name="ck_stage_config_position"),
        CheckConstraint(json_ok("goal_json"), name="ck_stage_config_goal"),
        CheckConstraint("effective_project_revision >= 1", name="ck_stage_config_effective"),
        CheckConstraint("schema_version >= 1", name="ck_stage_config_schema"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    project_id: Mapped[str] = mapped_column(String(64))
    stage_id: Mapped[str] = mapped_column(String(64))
    version_no: Mapped[int] = mapped_column(Integer)
    position: Mapped[int] = mapped_column(Integer)
    goal_json: Mapped[str] = mapped_column(Text)
    effective_project_revision: Mapped[int] = mapped_column(Integer)
    schema_version: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[float] = mapped_column(Float)


class Page(Base):
    __tablename__ = "pages"
    __table_args__ = (
        UniqueConstraint("project_id", "id", name="uq_pages_project_id"),
        fk_cols(("project_id", "stage_id"), "stages", ("project_id", "id"), "fk_pages_stage"),
        CheckConstraint(sql_in("status", PAGE_STATUSES), name="ck_pages_status"),
        CheckConstraint("length(trim(title)) > 0", name="ck_pages_title"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    project_id: Mapped[str] = mapped_column(String(64))
    stage_id: Mapped[str] = mapped_column(String(64))
    head_version_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    status: Mapped[str] = mapped_column(String(16))
    title: Mapped[str] = mapped_column(String(200))
    created_at: Mapped[float] = mapped_column(Float)


class PageVersion(Base):
    __tablename__ = "page_versions"
    __table_args__ = (
        UniqueConstraint("page_id", "version_no", name="uq_page_versions_number"),
        UniqueConstraint("project_id", "id", name="uq_page_versions_project_id"),
        UniqueConstraint("project_id", "page_id", "id", name="uq_page_versions_project_page"),
        fk_cols(("project_id", "page_id"), "pages", ("project_id", "id"), "fk_page_versions_page"),
        fk_cols(
            ("project_id", "page_id", "parent_version_id"),
            "page_versions",
            ("project_id", "page_id", "id"),
            "fk_page_versions_parent",
        ),
        CheckConstraint("version_no >= 1", name="ck_page_versions_number"),
        CheckConstraint(json_ok("content_json"), name="ck_page_versions_content"),
        CheckConstraint(hex64("content_hash"), name="ck_page_versions_hash"),
        CheckConstraint("schema_version >= 1", name="ck_page_versions_schema"),
        CheckConstraint("project_revision >= 1", name="ck_page_versions_project_revision"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    project_id: Mapped[str] = mapped_column(String(64))
    page_id: Mapped[str] = mapped_column(String(64))
    version_no: Mapped[int] = mapped_column(Integer)
    parent_version_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    content_json: Mapped[str] = mapped_column(Text)
    content_hash: Mapped[str] = mapped_column(String(64))
    schema_version: Mapped[int] = mapped_column(Integer)
    project_revision: Mapped[int] = mapped_column(Integer)
    created_by_account_id: Mapped[str] = mapped_column(
        String(64), account_fk("fk_page_versions_author")
    )
    created_at: Mapped[float] = mapped_column(Float)


class ViewState(Base):
    __tablename__ = "view_states"
    __table_args__ = (
        UniqueConstraint("account_id", "page_id", "view_name", name="uq_view_states_natural"),
        fk_cols(("project_id", "page_id"), "pages", ("project_id", "id"), "fk_view_states_page"),
        CheckConstraint("layout_revision >= 1", name="ck_view_states_revision"),
        CheckConstraint(hex64("layout_hash"), name="ck_view_states_hash"),
        CheckConstraint(json_ok("layout_json"), name="ck_view_states_layout"),
        CheckConstraint("length(trim(view_name)) > 0", name="ck_view_states_name"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    account_id: Mapped[str] = mapped_column(String(64), account_fk("fk_view_states_account"))
    project_id: Mapped[str] = mapped_column(String(64))
    page_id: Mapped[str] = mapped_column(String(64))
    view_name: Mapped[str] = mapped_column(String(100))
    layout_revision: Mapped[int] = mapped_column(Integer)
    layout_hash: Mapped[str] = mapped_column(String(64))
    layout_json: Mapped[str] = mapped_column(Text)
    updated_at: Mapped[float] = mapped_column(Float)


class PageDraft(Base):
    __tablename__ = "page_drafts"
    __table_args__ = (
        fk_cols(("project_id", "page_id"), "pages", ("project_id", "id"), "fk_page_drafts_page"),
        fk_cols(
            ("project_id", "page_id", "base_version_id"),
            "page_versions",
            ("project_id", "page_id", "id"),
            "fk_page_drafts_base",
        ),
        CheckConstraint("content_revision >= 1", name="ck_page_drafts_revision"),
        CheckConstraint(hex64("content_hash"), name="ck_page_drafts_hash"),
        CheckConstraint(json_ok("content_json"), name="ck_page_drafts_content"),
        CheckConstraint("schema_version >= 1", name="ck_page_drafts_schema"),
    )

    page_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    project_id: Mapped[str] = mapped_column(String(64))
    base_version_id: Mapped[str] = mapped_column(String(64))
    content_revision: Mapped[int] = mapped_column(Integer)
    content_hash: Mapped[str] = mapped_column(String(64))
    content_json: Mapped[str] = mapped_column(Text)
    schema_version: Mapped[int] = mapped_column(Integer)
    updated_at: Mapped[float] = mapped_column(Float)


class PageEdge(Base):
    __tablename__ = "page_edges"
    __table_args__ = (
        UniqueConstraint(
            "source_version_id",
            "target_version_id",
            "kind",
            name="uq_page_edges_endpoints",
        ),
        UniqueConstraint("project_id", "id", name="uq_page_edges_project_id"),
        same_project("source_version_id", "page_versions", "fk_page_edges_source"),
        same_project("target_version_id", "page_versions", "fk_page_edges_target"),
        CheckConstraint("source_version_id <> target_version_id", name="ck_page_edges_distinct"),
        CheckConstraint(sql_in("kind", LINEAGE_KINDS), name="ck_page_edges_kind"),
        CheckConstraint("project_revision >= 1", name="ck_page_edges_project_revision"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    project_id: Mapped[str] = mapped_column(String(64))
    source_version_id: Mapped[str] = mapped_column(String(64))
    target_version_id: Mapped[str] = mapped_column(String(64))
    kind: Mapped[str] = mapped_column(String(32))
    project_revision: Mapped[int] = mapped_column(Integer)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[float] = mapped_column(Float)


class EvidenceAnchor(Base):
    __tablename__ = "evidence_anchors"
    __table_args__ = (
        UniqueConstraint("project_id", "id", name="uq_evidence_project_id"),
        same_project("artifact_version_id", "artifact_versions", "fk_evidence_artifact_version"),
        same_project("supersedes_id", "evidence_anchors", "fk_evidence_supersedes"),
        CheckConstraint(sql_in("locator_type", LOCATOR_TYPES), name="ck_evidence_locator"),
        CheckConstraint(json_ok("locator_json"), name="ck_evidence_locator_json"),
        CheckConstraint(hex64("excerpt_hash"), name="ck_evidence_excerpt"),
        CheckConstraint(sql_in("visibility", VISIBILITY_LEVELS), name="ck_evidence_visibility"),
        CheckConstraint(sql_in("sensitivity", SENSITIVITY_LEVELS), name="ck_evidence_sensitivity"),
        CheckConstraint("schema_version >= 1", name="ck_evidence_schema"),
        CheckConstraint("length(trim(extractor_version)) > 0", name="ck_evidence_extractor"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    project_id: Mapped[str] = mapped_column(String(64))
    artifact_version_id: Mapped[str] = mapped_column(String(64))
    locator_type: Mapped[str] = mapped_column(String(32))
    locator_json: Mapped[str] = mapped_column(Text)
    excerpt_hash: Mapped[str] = mapped_column(String(64))
    extractor_version: Mapped[str] = mapped_column(String(64))
    visibility: Mapped[str] = mapped_column(String(32))
    sensitivity: Mapped[str] = mapped_column(String(32))
    supersedes_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    schema_version: Mapped[int] = mapped_column(Integer)
    created_by_account_id: Mapped[str] = mapped_column(String(64), account_fk("fk_evidence_author"))
    created_at: Mapped[float] = mapped_column(Float)


class Decision(Base):
    __tablename__ = "decisions"
    __table_args__ = (
        UniqueConstraint("project_id", "id", name="uq_decisions_project_id"),
        same_project("page_version_id", "page_versions", "fk_decisions_page_version"),
        same_project("artifact_version_id", "artifact_versions", "fk_decisions_artifact_version"),
        same_project("stage_id", "stages", "fk_decisions_stage"),
        same_project("supersedes_id", "decisions", "fk_decisions_supersedes"),
        CheckConstraint(_DECISION_TARGET_SQL, name="ck_decisions_target"),
        CheckConstraint("project_revision >= 1", name="ck_decisions_project_revision"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    project_id: Mapped[str] = mapped_column(String(64))
    account_id: Mapped[str] = mapped_column(String(64), account_fk("fk_decisions_account"))
    body: Mapped[str] = mapped_column(Text)
    reason: Mapped[str] = mapped_column(Text)
    page_version_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    artifact_version_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    stage_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    supersedes_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    project_revision: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[float] = mapped_column(Float)


class TestRecord(Base):
    __tablename__ = "test_records"
    __table_args__ = (
        UniqueConstraint("project_id", "id", name="uq_test_records_project_id"),
        same_project(
            "artifact_version_id",
            "artifact_versions",
            "fk_test_records_artifact_version",
        ),
        same_project("supersedes_id", "test_records", "fk_test_records_supersedes"),
        CheckConstraint(json_ok("conditions_json"), name="ck_test_records_conditions"),
        CheckConstraint("project_revision >= 1", name="ck_test_records_project_revision"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    project_id: Mapped[str] = mapped_column(String(64))
    account_id: Mapped[str] = mapped_column(String(64), account_fk("fk_test_records_account"))
    artifact_version_id: Mapped[str] = mapped_column(String(64))
    conditions_json: Mapped[str] = mapped_column(Text)
    observation: Mapped[str] = mapped_column(Text)
    result: Mapped[str] = mapped_column(Text)
    source: Mapped[str | None] = mapped_column(Text, nullable=True)
    supersedes_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    project_revision: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[float] = mapped_column(Float)


class ReviewOpinion(Base):
    __tablename__ = "review_opinions"
    __table_args__ = (
        UniqueConstraint("project_id", "id", name="uq_review_opinions_project_id"),
        same_project("page_version_id", "page_versions", "fk_review_opinions_page_version"),
        CheckConstraint("revision >= 1", name="ck_review_opinions_revision"),
        CheckConstraint(
            sql_in("visibility", VISIBILITY_LEVELS),
            name="ck_review_opinions_visibility",
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    project_id: Mapped[str] = mapped_column(String(64))
    author_account_id: Mapped[str] = mapped_column(
        String(64), account_fk("fk_review_opinions_author")
    )
    page_version_id: Mapped[str] = mapped_column(String(64))
    node_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    body: Mapped[str] = mapped_column(Text)
    revision: Mapped[int] = mapped_column(Integer)
    visibility: Mapped[str] = mapped_column(String(32))
    created_at: Mapped[float] = mapped_column(Float)
    updated_at: Mapped[float] = mapped_column(Float)


class ObjectGrant(Base):
    __tablename__ = "object_grants"
    __table_args__ = (
        same_project("stage_id", "stages", "fk_grants_stage"),
        same_project("page_id", "pages", "fk_grants_page"),
        same_project("page_version_id", "page_versions", "fk_grants_page_version"),
        same_project("artifact_id", "artifacts", "fk_grants_artifact"),
        same_project("artifact_version_id", "artifact_versions", "fk_grants_artifact_version"),
        same_project("evidence_anchor_id", "evidence_anchors", "fk_grants_evidence"),
        CheckConstraint(sql_in("action", GRANT_ACTIONS), name="ck_grants_action"),
        CheckConstraint(sql_in("scope_type", GRANT_SCOPE_TYPES), name="ck_grants_scope"),
        CheckConstraint(sql_in("status", GRANT_STATUSES), name="ck_grants_status"),
        CheckConstraint(_grant_target_sql(), name="ck_grants_target"),
        CheckConstraint(_ACTIVE_OR_REVOKED, name="ck_grants_revoked"),
        *_grant_indexes(),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    project_id: Mapped[str] = mapped_column(String(64), project_fk("fk_grants_project"))
    account_id: Mapped[str] = mapped_column(String(64), account_fk("fk_grants_account"))
    action: Mapped[str] = mapped_column(String(64))
    scope_type: Mapped[str] = mapped_column(String(32))
    status: Mapped[str] = mapped_column(String(16))
    created_at: Mapped[float] = mapped_column(Float)
    revoked_at: Mapped[float | None] = mapped_column(Float, nullable=True)
    created_by_account_id: Mapped[str] = mapped_column(
        String(64), account_fk("fk_grants_created_by")
    )
    stage_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    page_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    page_version_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    artifact_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    artifact_version_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    evidence_anchor_id: Mapped[str | None] = mapped_column(String(64), nullable=True)


class AuditEvent(Base):
    __tablename__ = "audit_events"
    __table_args__ = (
        CheckConstraint(json_ok("payload"), name="ck_audit_events_payload"),
        CheckConstraint(
            "project_revision IS NULL OR project_revision >= 1",
            name="ck_audit_events_revision",
        ),
        Index("ix_audit_events_project_created", "project_id", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    project_id: Mapped[str] = mapped_column(String(64))
    actor_id: Mapped[str] = mapped_column(String(64), account_fk("fk_audit_events_actor"))
    command: Mapped[str] = mapped_column(String(100))
    subject_type: Mapped[str] = mapped_column(String(64))
    subject_id: Mapped[str] = mapped_column(String(64))
    project_revision: Mapped[int | None] = mapped_column(Integer, nullable=True)
    payload: Mapped[str] = mapped_column(Text)
    created_at: Mapped[float] = mapped_column(Float)


class ContextSnapshot(Base):
    __tablename__ = "context_snapshots"
    __table_args__ = (
        UniqueConstraint("project_id", "id", name="uq_context_snapshots_project_id"),
        fk_cols(
            ("project_id", "draft_page_id"),
            "pages",
            ("project_id", "id"),
            "fk_context_snapshots_draft_page",
        ),
        CheckConstraint(json_ok("authorization_scope_json"), name="ck_snapshots_scope"),
        CheckConstraint(json_ok("exclusions_json"), name="ck_snapshots_exclusions"),
        CheckConstraint(json_ok("budget_json"), name="ck_snapshots_budget"),
        CheckConstraint("access_epoch >= 1", name="ck_snapshots_epoch"),
        CheckConstraint(
            "index_revision IS NULL OR index_revision >= 0",
            name="ck_snapshots_index_revision",
        ),
        CheckConstraint(_DRAFT_TRIPLE_SQL, name="ck_snapshots_draft_stamp"),
        CheckConstraint("length(trim(intent)) > 0", name="ck_snapshots_intent"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    project_id: Mapped[str] = mapped_column(String(64), project_fk("fk_context_snapshots_project"))
    intent: Mapped[str] = mapped_column(Text)
    template_version: Mapped[str] = mapped_column(String(64))
    method_card_version: Mapped[str] = mapped_column(String(64))
    authorization_scope_json: Mapped[str] = mapped_column(Text)
    access_epoch: Mapped[int] = mapped_column(Integer)
    exclusions_json: Mapped[str] = mapped_column(Text)
    index_revision: Mapped[int | None] = mapped_column(Integer, nullable=True)
    budget_json: Mapped[str] = mapped_column(Text)
    draft_page_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    draft_content_revision: Mapped[int | None] = mapped_column(Integer, nullable=True)
    draft_content_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    draft_content_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[float] = mapped_column(Float)


class ContextSnapshotRef(Base):
    __tablename__ = "context_snapshot_refs"
    __table_args__ = (
        same_project("snapshot_id", "context_snapshots", "fk_snapshot_refs_snapshot"),
        same_project("page_version_id", "page_versions", "fk_snapshot_refs_page_version"),
    )

    snapshot_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    page_version_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    project_id: Mapped[str] = mapped_column(String(64))


class AgentRun(Base):
    __tablename__ = "agent_runs"
    __table_args__ = (
        UniqueConstraint("project_id", "id", name="uq_agent_runs_project_id"),
        same_project("parent_run_id", "agent_runs", "fk_agent_runs_parent"),
        same_project("supersedes_run_id", "agent_runs", "fk_agent_runs_supersedes"),
        same_project("context_snapshot_id", "context_snapshots", "fk_agent_runs_snapshot"),
        CheckConstraint(
            sql_in("execution_status", EXECUTION_STATUSES),
            name="ck_agent_runs_status",
        ),
        CheckConstraint("accepting_results IN (0, 1)", name="ck_agent_runs_accepting"),
        CheckConstraint("next_event_seq >= 0", name="ck_agent_runs_seq"),
        CheckConstraint(json_ok("budget_json"), name="ck_agent_runs_budget"),
        CheckConstraint(
            "parent_run_id IS NULL OR parent_run_id <> id",
            name="ck_agent_runs_parent",
        ),
        CheckConstraint(
            "supersedes_run_id IS NULL OR supersedes_run_id <> id",
            name="ck_agent_runs_supersedes",
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    project_id: Mapped[str] = mapped_column(String(64))
    initiated_by_account_id: Mapped[str] = mapped_column(
        String(64), account_fk("fk_agent_runs_initiator")
    )
    parent_run_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    supersedes_run_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    context_snapshot_id: Mapped[str] = mapped_column(String(64))
    execution_status: Mapped[str] = mapped_column(String(32))
    accepting_results: Mapped[int] = mapped_column(Integer)
    next_event_seq: Mapped[int] = mapped_column(Integer)
    budget_json: Mapped[str] = mapped_column(Text)
    created_at: Mapped[float] = mapped_column(Float)
    closed_at: Mapped[float | None] = mapped_column(Float, nullable=True)
    origin: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        insert_default="manual",
        server_default=sql_text("'manual'"),
    )
    preference_revision: Mapped[int | None] = mapped_column(Integer, nullable=True)


class RunTask(Base):
    __tablename__ = "run_tasks"
    __table_args__ = (
        UniqueConstraint("run_id", "id", name="uq_run_tasks_run_id"),
        UniqueConstraint("project_id", "id", name="uq_run_tasks_project_id"),
        fk_cols(("project_id", "run_id"), "agent_runs", ("project_id", "id"), "fk_run_tasks_run"),
        CheckConstraint(sql_in("role", RUN_ROLES), name="ck_run_tasks_role"),
        CheckConstraint(sql_in("status", TASK_STATUSES), name="ck_run_tasks_status"),
        CheckConstraint("mandatory IN (0, 1)", name="ck_run_tasks_mandatory"),
        CheckConstraint(hex64("input_hash"), name="ck_run_tasks_input"),
        CheckConstraint("attempt_no >= 0 AND fencing_token >= 0", name="ck_run_tasks_counters"),
        CheckConstraint(lease_rule(TASK_STATUSES), name="ck_run_tasks_lease"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    run_id: Mapped[str] = mapped_column(String(64))
    project_id: Mapped[str] = mapped_column(String(64))
    role: Mapped[str] = mapped_column(String(32))
    status: Mapped[str] = mapped_column(String(32))
    mandatory: Mapped[int] = mapped_column(Integer)
    input_hash: Mapped[str] = mapped_column(String(64))
    available_at: Mapped[float] = mapped_column(Float)
    attempt_no: Mapped[int] = mapped_column(Integer)
    lease_owner: Mapped[str | None] = mapped_column(String(100), nullable=True)
    lease_until: Mapped[float | None] = mapped_column(Float, nullable=True)
    fencing_token: Mapped[int] = mapped_column(Integer)
    output_ref: Mapped[str | None] = mapped_column(String(200), nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)


class RunTaskDependency(Base):
    __tablename__ = "run_task_dependencies"
    __table_args__ = (
        fk_cols(
            ("run_id", "task_id"),
            "run_tasks",
            ("run_id", "id"),
            "fk_run_task_dependencies_task",
        ),
        fk_cols(
            ("run_id", "depends_on_task_id"),
            "run_tasks",
            ("run_id", "id"),
            "fk_run_task_dependencies_prior",
        ),
        CheckConstraint("task_id <> depends_on_task_id", name="ck_run_task_dependencies_distinct"),
    )

    run_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    task_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    depends_on_task_id: Mapped[str] = mapped_column(String(64), primary_key=True)


class AgentResult(Base):
    __tablename__ = "agent_results"
    __table_args__ = (
        UniqueConstraint("run_id", "task_id", "attempt_no", name="uq_agent_results_attempt"),
        fk_cols(
            ("project_id", "run_id"),
            "agent_runs",
            ("project_id", "id"),
            "fk_agent_results_run",
        ),
        fk_cols(
            ("project_id", "task_id"),
            "run_tasks",
            ("project_id", "id"),
            "fk_agent_results_task",
        ),
        fk_cols(("run_id", "task_id"), "run_tasks", ("run_id", "id"), "fk_agent_results_run_task"),
        CheckConstraint(_RESULT_SQL, name="ck_agent_results_disposition"),
        CheckConstraint(hex64("input_hash"), name="ck_agent_results_input"),
        CheckConstraint("attempt_no >= 0 AND fencing_token >= 0", name="ck_agent_results_counters"),
        Index(
            "uq_agent_results_published",
            "run_id",
            "task_id",
            "input_hash",
            unique=True,
            sqlite_where=sql_text("disposition = 'published'"),
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    project_id: Mapped[str] = mapped_column(String(64))
    run_id: Mapped[str] = mapped_column(String(64))
    task_id: Mapped[str] = mapped_column(String(64))
    input_hash: Mapped[str] = mapped_column(String(64))
    attempt_no: Mapped[int] = mapped_column(Integer)
    fencing_token: Mapped[int] = mapped_column(Integer)
    disposition: Mapped[str] = mapped_column(String(32))
    output_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    diagnostic_code: Mapped[str | None] = mapped_column(String(100), nullable=True)
    usage_json: Mapped[str] = mapped_column(Text)
    created_at: Mapped[float] = mapped_column(Float)


class RunReadRecord(Base):
    __tablename__ = "run_read_records"
    __table_args__ = (
        UniqueConstraint("run_id", "tool_call_id", name="uq_run_read_records_call"),
        fk_cols(("project_id", "run_id"), "agent_runs", ("project_id", "id"), "fk_run_reads_run"),
        fk_cols(("project_id", "task_id"), "run_tasks", ("project_id", "id"), "fk_run_reads_task"),
        fk_cols(("run_id", "task_id"), "run_tasks", ("run_id", "id"), "fk_run_reads_run_task"),
        same_project("page_version_id", "page_versions", "fk_run_reads_page_version"),
        same_project("artifact_version_id", "artifact_versions", "fk_run_reads_artifact_version"),
        same_project("evidence_anchor_id", "evidence_anchors", "fk_run_reads_evidence"),
        CheckConstraint(_READ_ONE_SQL, name="ck_run_reads_target"),
        CheckConstraint(hex64("content_hash"), name="ck_run_reads_hash"),
        CheckConstraint("index_revision >= 0", name="ck_run_reads_index"),
        CheckConstraint("access_epoch >= 1", name="ck_run_reads_epoch"),
        CheckConstraint("truncated IN (0, 1)", name="ck_run_reads_truncated"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    project_id: Mapped[str] = mapped_column(String(64))
    run_id: Mapped[str] = mapped_column(String(64))
    task_id: Mapped[str] = mapped_column(String(64))
    tool_call_id: Mapped[str] = mapped_column(String(64))
    page_version_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    artifact_version_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    evidence_anchor_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    content_hash: Mapped[str] = mapped_column(String(64))
    index_revision: Mapped[int] = mapped_column(Integer)
    access_epoch: Mapped[int] = mapped_column(Integer)
    read_at: Mapped[float] = mapped_column(Float)
    truncated: Mapped[int] = mapped_column(Integer)
    miss_scope: Mapped[str | None] = mapped_column(String(64), nullable=True)


class RunEvent(Base):
    __tablename__ = "run_events"
    __table_args__ = (
        UniqueConstraint("run_id", "seq", name="uq_run_events_seq"),
        fk_cols(("project_id", "run_id"), "agent_runs", ("project_id", "id"), "fk_run_events_run"),
        fk_cols(("project_id", "task_id"), "run_tasks", ("project_id", "id"), "fk_run_events_task"),
        fk_cols(("run_id", "task_id"), "run_tasks", ("run_id", "id"), "fk_run_events_run_task"),
        same_project("context_snapshot_id", "context_snapshots", "fk_run_events_snapshot"),
        CheckConstraint("seq >= 1", name="ck_run_events_seq"),
        CheckConstraint("schema_version >= 1", name="ck_run_events_schema"),
        CheckConstraint(json_ok("payload"), name="ck_run_events_payload"),
        CheckConstraint("length(trim(event_type)) > 0", name="ck_run_events_type"),
    )

    event_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    project_id: Mapped[str] = mapped_column(String(64))
    run_id: Mapped[str] = mapped_column(String(64))
    seq: Mapped[int] = mapped_column(Integer)
    event_type: Mapped[str] = mapped_column(String(100))
    schema_version: Mapped[int] = mapped_column(Integer)
    task_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    trace_id: Mapped[str] = mapped_column(String(64))
    context_snapshot_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    payload: Mapped[str] = mapped_column(Text)
    created_at: Mapped[float] = mapped_column(Float)


class Candidate(Base):
    __tablename__ = "candidates"
    __table_args__ = (
        UniqueConstraint("project_id", "id", name="uq_candidates_project_id"),
        fk_cols(("project_id", "run_id"), "agent_runs", ("project_id", "id"), "fk_candidates_run"),
        same_project("context_snapshot_id", "context_snapshots", "fk_candidates_snapshot"),
        CheckConstraint(sql_in("action", CANDIDATE_ACTIONS), name="ck_candidates_action"),
        CheckConstraint(sql_in("status", CANDIDATE_STATUSES), name="ck_candidates_status"),
        CheckConstraint(_page_mode_sql(), name="ck_candidates_page_mode"),
        CheckConstraint("revision >= 1", name="ck_candidates_revision"),
        CheckConstraint(json_ok("proposal_json"), name="ck_candidates_proposal"),
        CheckConstraint(hex64("proposal_hash"), name="ck_candidates_proposal_hash"),
        CheckConstraint(hex64("input_hash"), name="ck_candidates_input"),
        CheckConstraint("schema_version >= 1", name="ck_candidates_schema"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    project_id: Mapped[str] = mapped_column(String(64))
    run_id: Mapped[str] = mapped_column(String(64))
    action: Mapped[str] = mapped_column(String(64))
    page_mode: Mapped[str | None] = mapped_column(String(32), nullable=True)
    status: Mapped[str] = mapped_column(String(16))
    revision: Mapped[int] = mapped_column(Integer)
    proposal_json: Mapped[str] = mapped_column(Text)
    proposal_hash: Mapped[str] = mapped_column(String(64))
    input_hash: Mapped[str] = mapped_column(String(64))
    context_snapshot_id: Mapped[str] = mapped_column(String(64))
    schema_version: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[float] = mapped_column(Float)


class CandidatePreview(Base):
    __tablename__ = "candidate_previews"
    __table_args__ = (
        UniqueConstraint("project_id", "id", name="uq_candidate_previews_project_id"),
        same_project("candidate_id", "candidates", "fk_previews_candidate"),
        CheckConstraint("candidate_revision >= 1", name="ck_previews_candidate_revision"),
        CheckConstraint("access_epoch >= 1", name="ck_previews_epoch"),
        CheckConstraint(json_ok("user_patch_json"), name="ck_previews_patch"),
        CheckConstraint(json_ok("effective_proposal_json"), name="ck_previews_effective"),
        CheckConstraint(hex64("user_patch_hash"), name="ck_previews_patch_hash"),
        CheckConstraint(hex64("preview_hash"), name="ck_previews_hash"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    project_id: Mapped[str] = mapped_column(String(64))
    candidate_id: Mapped[str] = mapped_column(String(64))
    candidate_revision: Mapped[int] = mapped_column(Integer)
    account_id: Mapped[str] = mapped_column(String(64), account_fk("fk_previews_account"))
    access_epoch: Mapped[int] = mapped_column(Integer)
    user_patch_json: Mapped[str] = mapped_column(Text)
    user_patch_hash: Mapped[str] = mapped_column(String(64))
    effective_proposal_json: Mapped[str] = mapped_column(Text)
    preview_hash: Mapped[str] = mapped_column(String(64))
    expires_at: Mapped[float] = mapped_column(Float)
    created_at: Mapped[float] = mapped_column(Float)


class CandidatePreviewSource(Base):
    __tablename__ = "candidate_preview_sources"
    __table_args__ = (
        same_project("preview_id", "candidate_previews", "fk_preview_sources_preview"),
        same_project("page_version_id", "page_versions", "fk_preview_sources_page_version"),
        same_project(
            "artifact_version_id",
            "artifact_versions",
            "fk_preview_sources_artifact_version",
        ),
        same_project("draft_page_id", "pages", "fk_preview_sources_draft_page"),
        CheckConstraint(
            sql_in("source_kind", PREVIEW_SOURCE_KINDS),
            name="ck_preview_sources_kind",
        ),
        CheckConstraint(_source_kind_sql(), name="ck_preview_sources_target"),
        Index(
            "uq_candidate_preview_sources_natural",
            "preview_id",
            "source_kind",
            literal_column("ifnull(page_version_id, '')"),
            literal_column("ifnull(artifact_version_id, '')"),
            literal_column("ifnull(draft_page_id, '')"),
            unique=True,
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    project_id: Mapped[str] = mapped_column(String(64))
    preview_id: Mapped[str] = mapped_column(String(64))
    source_kind: Mapped[str] = mapped_column(String(32))
    page_version_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    artifact_version_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    draft_page_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    draft_content_revision: Mapped[int | None] = mapped_column(Integer, nullable=True)
    draft_content_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)


class CandidateDisposition(Base):
    __tablename__ = "candidate_dispositions"
    __table_args__ = (
        UniqueConstraint("candidate_id", name="uq_dispositions_candidate"),
        same_project("candidate_id", "candidates", "fk_dispositions_candidate"),
        same_project("preview_id", "candidate_previews", "fk_dispositions_preview"),
        CheckConstraint(sql_in("outcome", DISPOSITION_OUTCOMES), name="ck_dispositions_outcome"),
        CheckConstraint(
            "(outcome = 'committed' AND preview_id IS NOT NULL) OR "
            "outcome IN ('rejected', 'expired')",
            name="ck_dispositions_preview",
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    candidate_id: Mapped[str] = mapped_column(String(64))
    project_id: Mapped[str] = mapped_column(String(64))
    account_id: Mapped[str] = mapped_column(String(64), account_fk("fk_dispositions_account"))
    outcome: Mapped[str] = mapped_column(String(16))
    preview_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[float] = mapped_column(Float)


class Job(Base):
    __tablename__ = "jobs"
    __table_args__ = (
        CheckConstraint(sql_in("job_kind", JOB_KINDS), name="ck_jobs_kind"),
        CheckConstraint(sql_in("status", JOB_STATUSES), name="ck_jobs_status"),
        CheckConstraint(hex64("input_hash"), name="ck_jobs_input"),
        CheckConstraint("attempt_no >= 0 AND fencing_token >= 0", name="ck_jobs_counters"),
        CheckConstraint(lease_rule(JOB_STATUSES), name="ck_jobs_lease"),
        CheckConstraint(json_ok("payload"), name="ck_jobs_payload"),
        CheckConstraint("length(trim(subject_type)) > 0", name="ck_jobs_subject_type"),
        CheckConstraint("length(trim(subject_id)) > 0", name="ck_jobs_subject_id"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    project_id: Mapped[str] = mapped_column(String(64), project_fk("fk_jobs_project"))
    job_kind: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(32))
    available_at: Mapped[float] = mapped_column(Float)
    attempt_no: Mapped[int] = mapped_column(Integer)
    lease_owner: Mapped[str | None] = mapped_column(String(100), nullable=True)
    lease_until: Mapped[float | None] = mapped_column(Float, nullable=True)
    fencing_token: Mapped[int] = mapped_column(Integer)
    input_hash: Mapped[str] = mapped_column(String(64))
    output_ref: Mapped[str | None] = mapped_column(String(200), nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    subject_type: Mapped[str] = mapped_column(String(64))
    subject_id: Mapped[str] = mapped_column(String(64))
    source_event_id: Mapped[str | None] = mapped_column(
        String(64),
        ForeignKey(
            "outbox_events.event_id",
            ondelete="RESTRICT",
            onupdate="RESTRICT",
            name="fk_jobs_source_event",
        ),
        nullable=True,
    )
    payload: Mapped[str] = mapped_column(Text)
    created_at: Mapped[float] = mapped_column(Float)


class ConsumerReceipt(Base):
    __tablename__ = "consumer_receipts"

    event_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey(
            "outbox_events.event_id",
            ondelete="RESTRICT",
            onupdate="RESTRICT",
            name="fk_consumer_receipts_event",
        ),
        primary_key=True,
    )
    consumer_name: Mapped[str] = mapped_column(String(100), primary_key=True)
    project_id: Mapped[str] = mapped_column(String(64))
    effect_kind: Mapped[str] = mapped_column(String(64))
    effect_ref: Mapped[str] = mapped_column(String(200))
    created_at: Mapped[float] = mapped_column(Float)


class PrivacyTombstone(Base):
    __tablename__ = "privacy_tombstones"
    __table_args__ = (
        UniqueConstraint(
            "project_id",
            "object_type",
            "object_id",
            name="uq_privacy_tombstones_object",
        ),
        CheckConstraint(sql_in("object_type", TOMBSTONE_OBJECT_TYPES), name="ck_tombstones_type"),
        CheckConstraint(
            sql_in("reason_category", TOMBSTONE_REASON_CATEGORIES),
            name="ck_tombstones_reason",
        ),
        CheckConstraint("access_epoch >= 1", name="ck_tombstones_epoch"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    project_id: Mapped[str] = mapped_column(String(64), project_fk("fk_tombstones_project"))
    object_type: Mapped[str] = mapped_column(String(64))
    object_id: Mapped[str] = mapped_column(String(64))
    reason_category: Mapped[str] = mapped_column(String(64))
    access_epoch: Mapped[int] = mapped_column(Integer)
    audit_event_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey(
            "audit_events.id",
            ondelete="RESTRICT",
            onupdate="RESTRICT",
            name="fk_tombstones_audit",
        ),
    )
    created_at: Mapped[float] = mapped_column(Float)


_domain_tables = set(Base.metadata.tables) - set(FOUNDATION_TABLES)
if _domain_tables != set(DOWNGRADE_ORDER):
    missing = sorted(set(DOWNGRADE_ORDER) - _domain_tables)
    extra = sorted(_domain_tables - set(DOWNGRADE_ORDER))
    raise RuntimeError(f"domain table inventory drifted missing={missing} extra={extra}")
