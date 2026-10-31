"""Domain tables and immutability triggers. Does not rewrite foundation tables.

The integration owner copies this module to migrations/versions/0003_domain_identity.py.
Head pointers are ordinary columns, not foreign keys, so downgrade has no cycle.
"""

import sqlalchemy as sa
from alembic import op

from sinuo.domain.schema_types import TOMBSTONE_OBJECT_TYPES

revision = "0003_domain_identity"
down_revision = "0002_lease_invariants"
branch_labels = None
depends_on = None

UNCONDITIONAL_IMMUTABLE_TABLES: tuple[str, ...] = (
    "page_versions",
    "artifact_versions",
    "stage_config_versions",
    "page_edges",
    "evidence_anchors",
    "decisions",
    "test_records",
    "context_snapshots",
    "context_snapshot_refs",
    "run_read_records",
    "run_events",
    "candidate_previews",
    "candidate_preview_sources",
    "candidate_dispositions",
    "agent_results",
    "audit_events",
    "consumer_receipts",
    "privacy_tombstones",
)
APPEND_ONLY_TABLES: tuple[str, ...] = UNCONDITIONAL_IMMUTABLE_TABLES + (
    "run_task_dependencies",
)

_TOMBSTONE_TABLE = {
    "project": "projects",
    "page": "pages",
    "page_version": "page_versions",
    "artifact": "artifacts",
    "artifact_version": "artifact_versions",
    "evidence_anchor": "evidence_anchors",
    "decision": "decisions",
    "test_record": "test_records",
    "review_opinion": "review_opinions",
    "context_snapshot": "context_snapshots",
    "candidate": "candidates",
    "run_read_record": "run_read_records",
}
if set(_TOMBSTONE_TABLE) != set(TOMBSTONE_OBJECT_TYPES):
    raise RuntimeError("tombstone triggers do not cover the object-type closed set")

_REGISTERED: list[tuple[str, str, str]] = []


def _add(name: str, table: str, timing: str, body: str) -> None:
    statement = f"CREATE TRIGGER {name} {timing} ON {table} BEGIN {body} END"
    _REGISTERED.append((name, table, statement))


def _forbid_change(table: str) -> None:
    message = f"{table}_immutable"
    _add(
        f"{table}_before_update",
        table,
        "BEFORE UPDATE",
        f"SELECT RAISE(ABORT, '{message}');",
    )
    _add(
        f"{table}_before_delete",
        table,
        "BEFORE DELETE",
        f"SELECT RAISE(ABORT, '{message}');",
    )


def _forbid_delete(table: str) -> None:
    _add(
        f"{table}_before_delete",
        table,
        "BEFORE DELETE",
        f"SELECT RAISE(ABORT, '{table}_delete_forbidden');",
    )


def _join(*parts: str) -> str:
    return " ".join(parts)


for _table in APPEND_ONLY_TABLES:
    _forbid_change(_table)


_add(
    "accounts_before_delete",
    "accounts",
    "BEFORE DELETE",
    "SELECT RAISE(ABORT, 'accounts_delete_forbidden');",
)
_add(
    "accounts_before_update",
    "accounts",
    "BEFORE UPDATE",
    _join(
        "SELECT RAISE(ABORT, 'accounts_identity_frozen')",
        "WHERE NEW.id IS NOT OLD.id OR NEW.created_at IS NOT OLD.created_at;",
        "SELECT RAISE(ABORT, 'accounts_auth_epoch')",
        "WHERE NEW.auth_epoch IS NOT OLD.auth_epoch",
        "AND NEW.auth_epoch IS NOT OLD.auth_epoch + 1;",
    ),
)
_forbid_delete("sessions")
_add(
    "sessions_before_update",
    "sessions",
    "BEFORE UPDATE",
    _join(
        "SELECT RAISE(ABORT, 'sessions_identity_frozen')",
        "WHERE NEW.id IS NOT OLD.id",
        "OR NEW.token_sha256 IS NOT OLD.token_sha256",
        "OR NEW.account_id IS NOT OLD.account_id",
        "OR NEW.created_at IS NOT OLD.created_at",
        "OR NEW.absolute_expires_at IS NOT OLD.absolute_expires_at",
        "OR NEW.auth_epoch IS NOT OLD.auth_epoch;",
        "SELECT RAISE(ABORT, 'sessions_revoked_at')",
        "WHERE NOT (OLD.revoked_at IS NULL",
        "OR (OLD.revoked_at IS NOT NULL AND NEW.revoked_at IS OLD.revoked_at));",
        "SELECT RAISE(ABORT, 'sessions_idle_window')",
        "WHERE NEW.idle_expires_at < OLD.idle_expires_at",
        "OR NEW.idle_expires_at > NEW.absolute_expires_at;",
    ),
)
_forbid_delete("projects")
_add(
    "projects_before_update",
    "projects",
    "BEFORE UPDATE",
    _join(
        "SELECT RAISE(ABORT, 'projects_identity_frozen')",
        "WHERE NEW.id IS NOT OLD.id OR NEW.created_at IS NOT OLD.created_at;",
        "SELECT RAISE(ABORT, 'projects_revision')",
        "WHERE NEW.revision IS NOT OLD.revision",
        "AND NEW.revision IS NOT OLD.revision + 1;",
        "SELECT RAISE(ABORT, 'projects_access_epoch')",
        "WHERE NEW.access_epoch IS NOT OLD.access_epoch",
        "AND NEW.access_epoch IS NOT OLD.access_epoch + 1;",
        "SELECT RAISE(ABORT, 'projects_revision_required')",
        "WHERE (NEW.title IS NOT OLD.title OR NEW.brief IS NOT OLD.brief",
        "OR NEW.template_key IS NOT OLD.template_key)",
        "AND NEW.revision IS NOT OLD.revision + 1;",
        "SELECT RAISE(ABORT, 'projects_epoch_only')",
        "WHERE NEW.access_epoch IS NOT OLD.access_epoch",
        "AND NEW.revision IS OLD.revision",
        "AND (NEW.title IS NOT OLD.title OR NEW.brief IS NOT OLD.brief",
        "OR NEW.template_key IS NOT OLD.template_key);",
        "SELECT RAISE(ABORT, 'projects_counters_coupled')",
        "WHERE NEW.revision IS NOT OLD.revision",
        "AND NEW.access_epoch IS NOT OLD.access_epoch;",
    ),
)
_forbid_delete("memberships")
_add(
    "memberships_before_update",
    "memberships",
    "BEFORE UPDATE",
    _join(
        "SELECT RAISE(ABORT, 'memberships_identity_frozen')",
        "WHERE NEW.id IS NOT OLD.id",
        "OR NEW.project_id IS NOT OLD.project_id",
        "OR NEW.account_id IS NOT OLD.account_id",
        "OR NEW.created_at IS NOT OLD.created_at;",
    ),
)
_forbid_delete("object_grants")
_add(
    "object_grants_before_update",
    "object_grants",
    "BEFORE UPDATE",
    _join(
        "SELECT RAISE(ABORT, 'grants_identity_frozen')",
        "WHERE NEW.id IS NOT OLD.id",
        "OR NEW.project_id IS NOT OLD.project_id",
        "OR NEW.account_id IS NOT OLD.account_id",
        "OR NEW.action IS NOT OLD.action",
        "OR NEW.scope_type IS NOT OLD.scope_type",
        "OR NEW.stage_id IS NOT OLD.stage_id",
        "OR NEW.page_id IS NOT OLD.page_id",
        "OR NEW.page_version_id IS NOT OLD.page_version_id",
        "OR NEW.artifact_id IS NOT OLD.artifact_id",
        "OR NEW.artifact_version_id IS NOT OLD.artifact_version_id",
        "OR NEW.evidence_anchor_id IS NOT OLD.evidence_anchor_id",
        "OR NEW.created_at IS NOT OLD.created_at",
        "OR NEW.created_by_account_id IS NOT OLD.created_by_account_id;",
        "SELECT RAISE(ABORT, 'grants_reopen')",
        "WHERE OLD.status = 'revoked' AND NEW.status IS NOT 'revoked';",
        "SELECT RAISE(ABORT, 'grants_revoked_at')",
        "WHERE OLD.revoked_at IS NOT NULL AND NEW.revoked_at IS NOT OLD.revoked_at;",
    ),
)
_forbid_delete("stages")
_STAGE_HEAD = _join(
    "SELECT RAISE(ABORT, 'stages_head_mismatch')",
    "WHERE NEW.head_config_version_id IS NOT NULL AND NOT EXISTS (",
    "SELECT 1 FROM stage_config_versions",
    "WHERE id = NEW.head_config_version_id",
    "AND stage_id = NEW.id AND project_id = NEW.project_id);",
)
_add("stages_before_insert", "stages", "BEFORE INSERT", _STAGE_HEAD)
_add(
    "stages_before_update",
    "stages",
    "BEFORE UPDATE",
    _join(
        "SELECT RAISE(ABORT, 'stages_identity_frozen')",
        "WHERE NEW.id IS NOT OLD.id OR NEW.project_id IS NOT OLD.project_id",
        "OR NEW.created_at IS NOT OLD.created_at;",
        _STAGE_HEAD,
        "SELECT RAISE(ABORT, 'stages_head_required')",
        "WHERE OLD.head_config_version_id IS NOT NULL",
        "AND NEW.head_config_version_id IS NULL;",
    ),
)
_forbid_delete("pages")
_PAGE_HEAD = _join(
    "SELECT RAISE(ABORT, 'pages_head_mismatch')",
    "WHERE NEW.head_version_id IS NOT NULL AND NOT EXISTS (",
    "SELECT 1 FROM page_versions WHERE id = NEW.head_version_id",
    "AND page_id = NEW.id AND project_id = NEW.project_id);",
)
_add("pages_before_insert", "pages", "BEFORE INSERT", _PAGE_HEAD)
_add(
    "pages_before_update",
    "pages",
    "BEFORE UPDATE",
    _join(
        "SELECT RAISE(ABORT, 'pages_identity_frozen')",
        "WHERE NEW.id IS NOT OLD.id OR NEW.project_id IS NOT OLD.project_id",
        "OR NEW.stage_id IS NOT OLD.stage_id OR NEW.created_at IS NOT OLD.created_at;",
        _PAGE_HEAD,
        "SELECT RAISE(ABORT, 'pages_head_required')",
        "WHERE OLD.head_version_id IS NOT NULL AND NEW.head_version_id IS NULL;",
    ),
)
_forbid_delete("page_drafts")
_add(
    "page_drafts_before_update",
    "page_drafts",
    "BEFORE UPDATE",
    _join(
        "SELECT RAISE(ABORT, 'page_drafts_identity_frozen')",
        "WHERE NEW.page_id IS NOT OLD.page_id OR NEW.project_id IS NOT OLD.project_id;",
        "SELECT RAISE(ABORT, 'page_drafts_revision')",
        "WHERE NEW.content_revision IS NOT OLD.content_revision",
        "AND NEW.content_revision IS NOT OLD.content_revision + 1;",
        "SELECT RAISE(ABORT, 'page_drafts_content_revision')",
        "WHERE (NEW.content_json IS NOT OLD.content_json",
        "OR NEW.content_hash IS NOT OLD.content_hash",
        "OR NEW.base_version_id IS NOT OLD.base_version_id)",
        "AND NEW.content_revision IS NOT OLD.content_revision + 1;",
    ),
)
_add(
    "view_states_before_update",
    "view_states",
    "BEFORE UPDATE",
    _join(
        "SELECT RAISE(ABORT, 'view_states_identity_frozen')",
        "WHERE NEW.id IS NOT OLD.id",
        "OR NEW.account_id IS NOT OLD.account_id",
        "OR NEW.page_id IS NOT OLD.page_id",
        "OR NEW.view_name IS NOT OLD.view_name",
        "OR NEW.project_id IS NOT OLD.project_id;",
        "SELECT RAISE(ABORT, 'view_states_revision')",
        "WHERE NEW.layout_revision IS NOT OLD.layout_revision + 1;",
    ),
)
_forbid_delete("artifacts")
_add(
    "artifacts_before_update",
    "artifacts",
    "BEFORE UPDATE",
    _join(
        "SELECT RAISE(ABORT, 'artifacts_identity_frozen')",
        "WHERE NEW.id IS NOT OLD.id OR NEW.project_id IS NOT OLD.project_id",
        "OR NEW.created_at IS NOT OLD.created_at;",
    ),
)
_forbid_delete("review_opinions")
_add(
    "review_opinions_before_update",
    "review_opinions",
    "BEFORE UPDATE",
    _join(
        "SELECT RAISE(ABORT, 'review_opinions_frozen')",
        "WHERE NEW.id IS NOT OLD.id",
        "OR NEW.project_id IS NOT OLD.project_id",
        "OR NEW.author_account_id IS NOT OLD.author_account_id",
        "OR NEW.page_version_id IS NOT OLD.page_version_id",
        "OR NEW.node_id IS NOT OLD.node_id",
        "OR NEW.created_at IS NOT OLD.created_at;",
        "SELECT RAISE(ABORT, 'review_opinions_revision')",
        "WHERE NEW.revision IS NOT OLD.revision + 1;",
    ),
)
_forbid_delete("agent_runs")
_add(
    "agent_runs_before_update",
    "agent_runs",
    "BEFORE UPDATE",
    _join(
        "SELECT RAISE(ABORT, 'agent_runs_identity_frozen')",
        "WHERE NEW.id IS NOT OLD.id",
        "OR NEW.project_id IS NOT OLD.project_id",
        "OR NEW.initiated_by_account_id IS NOT OLD.initiated_by_account_id",
        "OR NEW.parent_run_id IS NOT OLD.parent_run_id",
        "OR NEW.supersedes_run_id IS NOT OLD.supersedes_run_id",
        "OR NEW.context_snapshot_id IS NOT OLD.context_snapshot_id",
        "OR NEW.created_at IS NOT OLD.created_at;",
        "SELECT RAISE(ABORT, 'agent_runs_accepting')",
        "WHERE OLD.accepting_results = 0 AND NEW.accepting_results IS NOT 0;",
        "SELECT RAISE(ABORT, 'agent_runs_terminal')",
        "WHERE OLD.execution_status IN ('completed', 'partial', 'failed', 'cancelled')",
        "AND NEW.execution_status IS NOT OLD.execution_status;",
        "SELECT RAISE(ABORT, 'agent_runs_seq')",
        "WHERE NEW.next_event_seq IS NOT OLD.next_event_seq",
        "AND NEW.next_event_seq IS NOT OLD.next_event_seq + 1;",
    ),
)
_forbid_delete("run_tasks")
_add(
    "run_tasks_before_update",
    "run_tasks",
    "BEFORE UPDATE",
    _join(
        "SELECT RAISE(ABORT, 'run_tasks_identity_frozen')",
        "WHERE NEW.id IS NOT OLD.id OR NEW.run_id IS NOT OLD.run_id",
        "OR NEW.role IS NOT OLD.role OR NEW.project_id IS NOT OLD.project_id;",
        "SELECT RAISE(ABORT, 'run_tasks_terminal')",
        "WHERE OLD.status IN ('succeeded', 'failed', 'cancelled')",
        "AND (NEW.status IS NOT OLD.status",
        "OR NEW.fencing_token IS NOT OLD.fencing_token",
        "OR NEW.lease_owner IS NOT OLD.lease_owner",
        "OR NEW.lease_until IS NOT OLD.lease_until);",
    ),
)
_add(
    "run_tasks_before_insert",
    "run_tasks",
    "BEFORE INSERT",
    _join(
        "SELECT RAISE(ABORT, 'role_cap')",
        "WHERE (SELECT COUNT(DISTINCT role) FROM run_tasks",
        "WHERE run_id = NEW.run_id AND role <> NEW.role) >= 3",
        "AND NOT EXISTS (SELECT 1 FROM run_tasks",
        "WHERE run_id = NEW.run_id AND role = NEW.role);",
    ),
)
_add(
    "agent_results_before_insert",
    "agent_results",
    "BEFORE INSERT",
    _join(
        "SELECT RAISE(ABORT, 'results_closed')",
        "WHERE NEW.disposition = 'published' AND COALESCE((",
        "SELECT accepting_results FROM agent_runs",
        "WHERE id = NEW.run_id AND project_id = NEW.project_id), 0) IS NOT 1;",
        "SELECT RAISE(ABORT, 'stale_fence')",
        "WHERE NEW.disposition = 'published'",
        "AND NEW.fencing_token IS NOT (",
        "SELECT fencing_token FROM run_tasks",
        "WHERE id = NEW.task_id AND project_id = NEW.project_id);",
    ),
)
_forbid_delete("candidates")
_add(
    "candidates_before_update",
    "candidates",
    "BEFORE UPDATE",
    _join(
        "SELECT RAISE(ABORT, 'candidate_update_forbidden') WHERE NOT (",
        "OLD.status = 'pending'",
        "AND NEW.status IN ('committed', 'rejected', 'expired')",
        "AND NEW.revision = OLD.revision + 1",
        "AND NEW.id IS OLD.id",
        "AND NEW.project_id IS OLD.project_id",
        "AND NEW.run_id IS OLD.run_id",
        "AND NEW.action IS OLD.action",
        "AND NEW.page_mode IS OLD.page_mode",
        "AND NEW.proposal_json IS OLD.proposal_json",
        "AND NEW.proposal_hash IS OLD.proposal_hash",
        "AND NEW.input_hash IS OLD.input_hash",
        "AND NEW.context_snapshot_id IS OLD.context_snapshot_id",
        "AND NEW.schema_version IS OLD.schema_version",
        "AND NEW.created_at IS OLD.created_at);",
    ),
)
_add(
    "candidate_previews_before_insert",
    "candidate_previews",
    "BEFORE INSERT",
    _join(
        "SELECT RAISE(ABORT, 'preview_revision')",
        "WHERE NEW.candidate_revision IS NOT (",
        "SELECT revision FROM candidates",
        "WHERE id = NEW.candidate_id AND project_id = NEW.project_id);",
        "SELECT RAISE(ABORT, 'preview_epoch')",
        "WHERE NEW.access_epoch IS NOT (",
        "SELECT access_epoch FROM projects WHERE id = NEW.project_id);",
    ),
)
_add(
    "candidate_dispositions_before_insert",
    "candidate_dispositions",
    "BEFORE INSERT",
    _join(
        "SELECT RAISE(ABORT, 'disposition_outcome')",
        "WHERE NEW.outcome IS NOT (",
        "SELECT status FROM candidates",
        "WHERE id = NEW.candidate_id AND project_id = NEW.project_id);",
        "SELECT RAISE(ABORT, 'disposition_preview')",
        "WHERE NEW.preview_id IS NOT NULL AND NOT EXISTS (",
        "SELECT 1 FROM candidate_previews",
        "WHERE id = NEW.preview_id AND project_id = NEW.project_id",
        "AND candidate_id = NEW.candidate_id);",
    ),
)

_tombstone_parts = [
    "SELECT RAISE(ABORT, 'tombstone_target_missing')",
    "WHERE NEW.object_type = 'project'",
    "AND (NEW.object_id IS NOT NEW.project_id",
    "OR NOT EXISTS (SELECT 1 FROM projects WHERE id = NEW.project_id));",
]
for _kind, _table_name in _TOMBSTONE_TABLE.items():
    if _kind == "project":
        continue
    _tombstone_parts.extend(
        (
            "SELECT RAISE(ABORT, 'tombstone_target_missing')",
            f"WHERE NEW.object_type = '{_kind}' AND NOT EXISTS (",
            f"SELECT 1 FROM {_table_name}",
            "WHERE id = NEW.object_id AND project_id = NEW.project_id);",
        )
    )
_add(
    "privacy_tombstones_before_insert",
    "privacy_tombstones",
    "BEFORE INSERT",
    _join(*_tombstone_parts),
)
_forbid_delete("jobs")

TRIGGERS_BY_TABLE: dict[str, tuple[str, ...]] = {}
for _name, _table, _statement in _REGISTERED:
    TRIGGERS_BY_TABLE.setdefault(_table, ())
    TRIGGERS_BY_TABLE[_table] = TRIGGERS_BY_TABLE[_table] + (_name,)

# SQLite deletes rows before DROP TABLE and still checks references. A parent row
# in the same table cannot disappear while another row points at it. These
# nullable self-references are cleared after the immutability triggers are gone.
_SELF_REFERENCES: dict[str, tuple[str, ...]] = {
    "page_versions": ("parent_version_id",),
    "evidence_anchors": ("supersedes_id",),
    "decisions": ("supersedes_id",),
    "test_records": ("supersedes_id",),
    "agent_runs": ("parent_run_id", "supersedes_run_id"),
}


def trigger_statements() -> tuple[str, ...]:
    return tuple(statement for _name, _table, statement in _REGISTERED)


def trigger_sql(table: str) -> str:
    return "\n".join(
        statement for _name, owner, statement in _REGISTERED if owner == table
    )


def upgrade() -> None:
    bind = op.get_bind()
    available = bind.execute(sa.text("SELECT json_valid('{}')")).scalar_one()
    if available != 1:
        raise RuntimeError("SQLite JSON1 is required for domain documents")
    from sinuo.infrastructure.domain_models import CREATE_ORDER, Base

    for name in CREATE_ORDER:
        Base.metadata.tables[name].create(bind, checkfirst=False)
    for statement in trigger_statements():
        op.execute(sa.text(statement))


def downgrade() -> None:
    from sinuo.infrastructure.domain_models import DOWNGRADE_ORDER

    for table in DOWNGRADE_ORDER:
        for name in TRIGGERS_BY_TABLE.get(table, ()):
            op.execute(sa.text(f"DROP TRIGGER IF EXISTS {name}"))
        for column in _SELF_REFERENCES.get(table, ()):
            op.execute(sa.text(f"UPDATE {table} SET {column} = NULL"))
        op.drop_table(table)
