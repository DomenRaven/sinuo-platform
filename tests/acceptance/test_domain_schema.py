"""Acceptance checks for the domain head.

Database assertions stay on revision 0008_ticket_expiry and 43 product tables.
The 0003 draft remains the domain revision that 0004 follows.
"""

import ast
import hashlib
import importlib.util
import os
import re
import subprocess
import sys
from pathlib import Path
from threading import Barrier, Thread

import conftest
import pytest
from alembic import command
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from sinuo.api.app import create_app
from sinuo.config import Settings
from sinuo.domain.public_contract import (
    GRANT_SCOPE_TYPES,
    SENSITIVITY_LEVELS,
    Action,
)
from sinuo.domain.schema_types import (
    GRANT_ACTIONS,
    INDEPENDENT_COUNTERS,
    AclEpochError,
    ClosedSetError,
    CrossProjectDocument,
    DocumentSchemaError,
    HistoryMutation,
    ImmutableRecord,
    PointerSnapshot,
    acl_commit,
    assert_append_only,
    assert_closed_set,
    assert_command_project,
    assert_counter_moved_only,
    assert_history_statements,
    assert_job_subject,
    assert_no_pointer_movement,
    assert_opinion_anchor,
    assert_role_set,
    assert_same_project_document,
    assert_snapshot_reference,
    canonical_document,
    canonical_locator,
    canonical_result,
    correct_artifact_version,
    correct_decision,
    correct_page_version,
    correct_test_record,
    document_hash,
    history_statements,
    initial_project_counters,
    normalize_login_name,
)
from sinuo.infrastructure.database import write_uow
from sinuo.infrastructure.models import IdempotencyRecord, OutboxDelivery, OutboxEvent
from sinuo.workers.leases import claim_delivery, complete_delivery

ROOT = Path(__file__).resolve().parents[2]
DRAFT = ROOT / "workpool" / "handoffs" / "proposals" / "0003_domain_identity.draft.py"
NOW = 1_700_000_000.0
PASSWORD = "$argon2id$synthetic-hash"
ACCOUNT_A = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaa1"
ACCOUNT_B = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbb2"
PROJECT_A = "synthetic-project-alpha"
PROJECT_B = "synthetic-project-beta"
PAGE_A = "synthetic-page-a"
PAGE_B = "synthetic-page-b"
SCHEMA_TYPES = ROOT / "src" / "sinuo" / "domain" / "schema_types.py"
DOMAIN_MODELS = ROOT / "src" / "sinuo" / "infrastructure" / "domain_models.py"


def digest(label: str) -> str:
    return hashlib.sha256(label.encode("utf-8")).hexdigest()


def require_domain_head(engine) -> None:
    with engine.begin() as connection:
        command.upgrade(conftest.alembic_config(connection), "head")
        version = connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
        names = application_tables(connection)
        foreign_keys = connection.execute(text("PRAGMA foreign_keys")).scalar_one()
        foreign_key_check = connection.execute(text("PRAGMA foreign_key_check")).all()
        integrity = connection.execute(text("PRAGMA integrity_check")).scalar_one()
    assert version == "0008_ticket_expiry"
    assert len(names) == 43
    assert foreign_keys == 1
    assert foreign_key_check == []
    assert integrity == "ok"


def application_tables(connection) -> set[str]:
    names = set(
        connection.execute(
            text("SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'")
        ).scalars()
    )
    return names - {"alembic_version"}


def execute(connection, sql: str, params: dict | None = None):
    connection.execute(text(sql), params or {})


def reject(connection, sql: str, params: dict | None = None) -> None:
    with pytest.raises(IntegrityError):
        connection.execute(text(sql), params or {})


def page_body() -> str:
    return canonical_document(
        {
            "schema_version": 1,
            "nodes": [
                {"node_id": "synthetic-node-1", "artifact_version_id": "synthetic-art-v1"}
            ],
            "edges": [],
        }
    )


def seed(connection) -> int:
    """Insert the synthetic fixture. Returns the edge count before lineage is added."""
    body = page_body()
    content_hash = document_hash(
        {
            "schema_version": 1,
            "nodes": [
                {"node_id": "synthetic-node-1", "artifact_version_id": "synthetic-art-v1"}
            ],
            "edges": [],
        }
    )
    layout = canonical_document(
        {"schema_version": 1, "nodes": [{"node_id": "synthetic-node-1"}], "edges": []}
    )
    execute(
        connection,
        """
        INSERT INTO projects
        (id, title, brief, template_key, revision, access_epoch, created_at)
        VALUES (:project_a, 'synthetic-alpha', NULL, NULL, 4, 1, :now),
               (:project_b, 'synthetic-beta', NULL, NULL, 1, 1, :now)
        """,
        {"project_a": PROJECT_A, "project_b": PROJECT_B, "now": NOW},
    )
    execute(
        connection,
        """
        INSERT INTO accounts (
            id, login_name, display_name, password_hash, status, auth_epoch,
            can_create_project, created_at, disabled_at
        ) VALUES
        (:account_a, 'synthetic-lead', 'synthetic-lead', :password, 'active', 1, 1, :now, NULL),
        (
            :account_b, 'synthetic-reviewer', 'synthetic-reviewer', :password,
            'active', 1, 0, :now, NULL
        )
        """,
        {"account_a": ACCOUNT_A, "account_b": ACCOUNT_B, "password": PASSWORD, "now": NOW},
    )
    execute(
        connection,
        """
        INSERT INTO memberships (
            id, project_id, account_id, status, role_label, created_at, revoked_at
        ) VALUES
        ('synthetic-mem-a1', :project_a, :account_a, 'active', NULL, :now, NULL),
        ('synthetic-mem-a2', :project_b, :account_a, 'active', NULL, :now, NULL),
        ('synthetic-mem-b1', :project_a, :account_b, 'active', NULL, :now, NULL)
        """,
        {
            "project_a": PROJECT_A,
            "project_b": PROJECT_B,
            "account_a": ACCOUNT_A,
            "account_b": ACCOUNT_B,
            "now": NOW,
        },
    )
    execute(
        connection,
        """
        INSERT INTO artifacts (id, project_id, title, created_at) VALUES
        ('synthetic-art', :project_a, 'synthetic-art', :now),
        ('synthetic-art-b', :project_b, 'synthetic-art-b', :now)
        """,
        {"project_a": PROJECT_A, "project_b": PROJECT_B, "now": NOW},
    )
    execute(
        connection,
        """
        INSERT INTO artifact_versions (
            id, project_id, artifact_id, version_no, content_hash, size_bytes, media_type,
            object_key, license, sensitivity, ai_processing_allowed, project_revision,
            created_by_account_id, created_at
        ) VALUES
        ('synthetic-art-v1', :project_a, 'synthetic-art', 1, :content_hash, 10, 'image/png',
         'synthetic/object-a', 'synthetic-license', 'project', 0, 1, :account_a, :now),
        ('synthetic-art-v2', :project_a, 'synthetic-art', 2, :content_hash, 12, 'image/png',
         'synthetic/object-a', 'synthetic-license', 'sensitive', 1, 4, :account_a, :now),
        ('synthetic-art-b-v1', :project_b, 'synthetic-art-b', 1, :content_hash, 3, 'image/png',
         'synthetic/object-b', 'synthetic-license', 'project', 0, 1, :account_a, :now)
        """,
        {
            "project_a": PROJECT_A,
            "project_b": PROJECT_B,
            "content_hash": digest("bytes"),
            "account_a": ACCOUNT_A,
            "now": NOW,
        },
    )
    execute(
        connection,
        """
        INSERT INTO stages (id, project_id, head_config_version_id, created_at) VALUES
        ('synthetic-stage', :project_a, NULL, :now),
        ('synthetic-stage-b', :project_b, NULL, :now)
        """,
        {"project_a": PROJECT_A, "project_b": PROJECT_B, "now": NOW},
    )
    execute(
        connection,
        """
        INSERT INTO stage_config_versions (
            id, project_id, stage_id, version_no, position, goal_json,
            effective_project_revision, schema_version, created_at
        ) VALUES
        (
            'synthetic-cfg-1', :project_a, 'synthetic-stage', 1, 0,
            '{"goal":"synthetic"}', 1, 1, :now
        ),
        (
            'synthetic-cfg-2', :project_a, 'synthetic-stage', 2, 1,
            '{"goal":"synthetic"}', 1, 1, :now
        ),
        (
            'synthetic-cfg-3', :project_a, 'synthetic-stage', 3, 2,
            '{"goal":"synthetic"}', 1, 1, :now
        )
        """,
        {"project_a": PROJECT_A, "now": NOW},
    )
    execute(
        connection,
        "UPDATE stages SET head_config_version_id = 'synthetic-cfg-3' WHERE id = 'synthetic-stage'",
    )
    execute(
        connection,
        """
        INSERT INTO pages (
            id, project_id, stage_id, head_version_id, status, title, created_at
        ) VALUES
        (:page_a, :project_a, 'synthetic-stage', NULL, 'active', 'synthetic-page-a', :now),
        (:page_b, :project_a, 'synthetic-stage', NULL, 'active', 'synthetic-page-b', :now)
        """,
        {"page_a": PAGE_A, "page_b": PAGE_B, "project_a": PROJECT_A, "now": NOW},
    )
    edges_before = connection.execute(text("SELECT count(*) FROM page_edges")).scalar_one()
    execute(
        connection,
        """
        INSERT INTO page_versions (
            id, project_id, page_id, version_no, parent_version_id, content_json, content_hash,
            schema_version, project_revision, created_by_account_id, created_at
        ) VALUES
        (
            'synthetic-pv-a1', :project_a, :page_a, 1, NULL, :body, :content_hash,
            1, 1, :account_a, :now
        ),
        (
            'synthetic-pv-a2', :project_a, :page_a, 2, 'synthetic-pv-a1', :body,
            :content_hash, 1, 4, :account_a, :now
        ),
        (
            'synthetic-pv-b1', :project_a, :page_b, 1, NULL, :body, :content_hash,
            1, 4, :account_a, :now
        )
        """,
        {
            "project_a": PROJECT_A,
            "page_a": PAGE_A,
            "page_b": PAGE_B,
            "body": body,
            "content_hash": content_hash,
            "account_a": ACCOUNT_A,
            "now": NOW,
        },
    )
    execute(
        connection,
        "UPDATE pages SET head_version_id = 'synthetic-pv-a2' WHERE id = :page_a",
        {"page_a": PAGE_A},
    )
    execute(
        connection,
        "UPDATE pages SET head_version_id = 'synthetic-pv-b1' WHERE id = :page_b",
        {"page_b": PAGE_B},
    )
    execute(
        connection,
        """
        INSERT INTO page_drafts (
            page_id, project_id, base_version_id, content_revision, content_hash,
            content_json, schema_version, updated_at
        ) VALUES (
            :page_a, :project_a, 'synthetic-pv-a2', 5, :content_hash, :body, 1, :now
        )
        """,
        {
            "page_a": PAGE_A,
            "project_a": PROJECT_A,
            "content_hash": content_hash,
            "body": body,
            "now": NOW,
        },
    )
    execute(
        connection,
        """
        INSERT INTO view_states (
            id, account_id, project_id, page_id, view_name, layout_revision, layout_hash,
            layout_json, updated_at
        ) VALUES (
            'synthetic-view', :account_a, :project_a, :page_a, 'synthetic-board', 7,
            :layout_hash, :layout, :now
        )
        """,
        {
            "account_a": ACCOUNT_A,
            "project_a": PROJECT_A,
            "page_a": PAGE_A,
            "layout_hash": digest("layout"),
            "layout": layout,
            "now": NOW,
        },
    )
    execute(
        connection,
        """
        INSERT INTO page_edges (
            id, project_id, source_version_id, target_version_id, kind, project_revision,
            reason, created_at
        ) VALUES (
            'synthetic-edge', :project_a, 'synthetic-pv-a2', 'synthetic-pv-b1',
            'branches_from', 4, NULL, :now
        )
        """,
        {"project_a": PROJECT_A, "now": NOW},
    )
    execute(
        connection,
        """
        INSERT INTO evidence_anchors (
            id, project_id, artifact_version_id, locator_type, locator_json, excerpt_hash,
            extractor_version, visibility, sensitivity, supersedes_id, schema_version,
            created_by_account_id, created_at
        ) VALUES (
            'synthetic-evidence', :project_a, 'synthetic-art-v1', 'region', '{"box":[0,0,1,1]}',
            :excerpt_hash, 'synthetic-extractor', 'project', 'project', NULL, 1, :account_a, :now
        )
        """,
        {
            "project_a": PROJECT_A,
            "excerpt_hash": digest("excerpt"),
            "account_a": ACCOUNT_A,
            "now": NOW,
        },
    )
    execute(
        connection,
        """
        INSERT INTO decisions (
            id, project_id, account_id, body, reason, page_version_id, artifact_version_id,
            stage_id, supersedes_id, project_revision, created_at
        ) VALUES (
            'synthetic-decision', :project_a, :account_a, 'synthetic decision', 'synthetic reason',
            'synthetic-pv-a2', NULL, NULL, NULL, 4, :now
        )
        """,
        {"project_a": PROJECT_A, "account_a": ACCOUNT_A, "now": NOW},
    )
    execute(
        connection,
        """
        INSERT INTO test_records (
            id, project_id, account_id, artifact_version_id, conditions_json, observation,
            result, source, supersedes_id, project_revision, created_at
        ) VALUES (
            'synthetic-test', :project_a, :account_a, 'synthetic-art-v2', '{}',
            'synthetic observation', 'synthetic result', NULL, NULL, 4, :now
        )
        """,
        {"project_a": PROJECT_A, "account_a": ACCOUNT_A, "now": NOW},
    )
    execute(
        connection,
        """
        INSERT INTO review_opinions (
            id, project_id, author_account_id, page_version_id, node_id, body, revision,
            visibility, created_at, updated_at
        ) VALUES (
            'synthetic-opinion', :project_a, :account_a, 'synthetic-pv-a2', 'synthetic-node-1',
            'synthetic opinion', 2, 'project', :now, :now
        )
        """,
        {"project_a": PROJECT_A, "account_a": ACCOUNT_A, "now": NOW},
    )
    execute(
        connection,
        """
        INSERT INTO object_grants (
            id, project_id, account_id, action, scope_type, status, created_at, revoked_at,
            created_by_account_id, stage_id, page_id, page_version_id, artifact_id,
            artifact_version_id, evidence_anchor_id
        ) VALUES (
            'synthetic-grant', :project_a, :account_a, 'acl.manage', 'project', 'active', :now,
            NULL, :account_a, NULL, NULL, NULL, NULL, NULL, NULL
        )
        """,
        {"project_a": PROJECT_A, "account_a": ACCOUNT_A, "now": NOW},
    )
    execute(
        connection,
        """
        INSERT INTO audit_events (
            id, project_id, actor_id, command, subject_type, subject_id, project_revision,
            payload, created_at
        ) VALUES (
            'synthetic-audit', :project_a, :account_a, 'privacy.delete', 'page_version',
            'synthetic-pv-a1', 4, '{"object_id":"synthetic-pv-a1"}', :now
        )
        """,
        {"project_a": PROJECT_A, "account_a": ACCOUNT_A, "now": NOW},
    )
    execute(
        connection,
        """
        INSERT INTO context_snapshots (
            id, project_id, intent, template_version, method_card_version,
            authorization_scope_json, access_epoch, exclusions_json, index_revision,
            budget_json, draft_page_id, draft_content_revision, draft_content_hash,
            draft_content_json, created_at
        ) VALUES (
            'synthetic-snapshot', :project_a, 'synthetic intent', 'synthetic-template',
            'synthetic-method', '{}', 1, '{}', NULL, '{}', NULL, NULL, NULL, NULL, :now
        )
        """,
        {"project_a": PROJECT_A, "now": NOW},
    )
    execute(
        connection,
        """
        INSERT INTO context_snapshot_refs (snapshot_id, page_version_id, project_id)
        VALUES ('synthetic-snapshot', 'synthetic-pv-a2', :project_a)
        """,
        {"project_a": PROJECT_A},
    )
    execute(
        connection,
        """
        INSERT INTO agent_runs (
            id, project_id, initiated_by_account_id, parent_run_id, supersedes_run_id,
            context_snapshot_id, execution_status, accepting_results, next_event_seq,
            budget_json, created_at, closed_at
        ) VALUES (
            'synthetic-run', :project_a, :account_a, NULL, NULL, 'synthetic-snapshot',
            'queued', 1, 8, '{}', :now, NULL
        )
        """,
        {"project_a": PROJECT_A, "account_a": ACCOUNT_A, "now": NOW},
    )
    for role, task_id in (("clarification", "synthetic-task-1"), ("evidence", "synthetic-task-2")):
        execute(
            connection,
            """
            INSERT INTO run_tasks (
                id, run_id, project_id, role, status, mandatory, input_hash, available_at,
                attempt_no, lease_owner, lease_until, fencing_token, output_ref, last_error
            ) VALUES (
                :task_id, 'synthetic-run', :project_a, :role, 'queued', 1, :input_hash, :now,
                0, NULL, NULL, 0, NULL, NULL
            )
            """,
            {
                "task_id": task_id,
                "project_a": PROJECT_A,
                "role": role,
                "input_hash": digest(role),
                "now": NOW,
            },
        )
    execute(
        connection,
        """
        INSERT INTO candidates (
            id, project_id, run_id, action, page_mode, status, revision, proposal_json,
            proposal_hash, input_hash, context_snapshot_id, schema_version, created_at
        ) VALUES (
            'synthetic-candidate', :project_a, 'synthetic-run', 'add_node', NULL, 'pending', 1,
            '{}', :proposal_hash, :proposal_hash, 'synthetic-snapshot', 1, :now
        )
        """,
        {"project_a": PROJECT_A, "proposal_hash": digest("proposal"), "now": NOW},
    )
    execute(
        connection,
        """
        INSERT INTO candidate_previews (
            id, project_id, candidate_id, candidate_revision, account_id, access_epoch,
            user_patch_json, user_patch_hash, effective_proposal_json, preview_hash,
            expires_at, created_at
        ) VALUES (
            'synthetic-preview', :project_a, 'synthetic-candidate', 1, :account_a, 1, '{}',
            :preview_hash, '{}', :preview_hash, :expires, :now
        )
        """,
        {
            "project_a": PROJECT_A,
            "account_a": ACCOUNT_A,
            "preview_hash": digest("preview"),
            "expires": NOW + 10,
            "now": NOW,
        },
    )
    execute(
        connection,
        """
        INSERT INTO candidate_preview_sources (
            id, project_id, preview_id, source_kind, page_version_id, artifact_version_id,
            draft_page_id, draft_content_revision, draft_content_hash
        ) VALUES (
            'synthetic-source', :project_a, 'synthetic-preview', 'page_version',
            'synthetic-pv-a2', NULL, NULL, NULL, NULL
        )
        """,
        {"project_a": PROJECT_A},
    )
    execute(
        connection,
        """
        INSERT INTO privacy_tombstones (
            id, project_id, object_type, object_id, reason_category, access_epoch,
            audit_event_id, created_at
        ) VALUES (
            'synthetic-tomb', :project_a, 'page_version', 'synthetic-pv-a1', 'policy', 1,
            'synthetic-audit', :now
        )
        """,
        {"project_a": PROJECT_A, "now": NOW},
    )
    execute(
        connection,
        """
        INSERT INTO login_throttles (bucket_key, window_started_at, attempts, updated_at)
        VALUES ('synthetic-bucket', :now, 1, :now)
        """,
        {"now": NOW},
    )
    return edges_before


def counter_snapshot(connection) -> dict[str, int]:
    return {
        "content_revision": connection.execute(
            text("SELECT content_revision FROM page_drafts WHERE page_id = :page_id"),
            {"page_id": PAGE_A},
        ).scalar_one(),
        "layout_revision": connection.execute(
            text("SELECT layout_revision FROM view_states WHERE id = 'synthetic-view'")
        ).scalar_one(),
        "page_version_no": connection.execute(
            text("SELECT max(version_no) FROM page_versions WHERE page_id = :page_id"),
            {"page_id": PAGE_A},
        ).scalar_one(),
        "project_revision": connection.execute(
            text("SELECT revision FROM projects WHERE id = :project_id"),
            {"project_id": PROJECT_A},
        ).scalar_one(),
        "stage_version_no": connection.execute(
            text("SELECT version_no FROM stage_config_versions WHERE id = 'synthetic-cfg-3'")
        ).scalar_one(),
        "artifact_version_no": connection.execute(
            text(
                "SELECT max(version_no) FROM artifact_versions "
                "WHERE artifact_id = 'synthetic-art'"
            )
        ).scalar_one(),
        "candidate_revision": connection.execute(
            text("SELECT revision FROM candidates WHERE id = 'synthetic-candidate'")
        ).scalar_one(),
        "opinion_revision": connection.execute(
            text("SELECT revision FROM review_opinions WHERE id = 'synthetic-opinion'")
        ).scalar_one(),
        "next_event_seq": connection.execute(
            text("SELECT next_event_seq FROM agent_runs WHERE id = 'synthetic-run'")
        ).scalar_one(),
    }


def pointers(connection) -> PointerSnapshot:
    return PointerSnapshot(
        project_revision=connection.execute(
            text("SELECT revision FROM projects WHERE id = :project_id"),
            {"project_id": PROJECT_A},
        ).scalar_one(),
        head_version_id=connection.execute(
            text("SELECT head_version_id FROM pages WHERE id = :page_id"),
            {"page_id": PAGE_A},
        ).scalar_one(),
        head_config_version_id=connection.execute(
            text("SELECT head_config_version_id FROM stages WHERE id = 'synthetic-stage'")
        ).scalar_one(),
        content_revision=connection.execute(
            text("SELECT content_revision FROM page_drafts WHERE page_id = :page_id"),
            {"page_id": PAGE_A},
        ).scalar_one(),
        layout_revision=connection.execute(
            text("SELECT layout_revision FROM view_states WHERE id = 'synthetic-view'")
        ).scalar_one(),
    )


def load_draft():
    spec = importlib.util.spec_from_file_location("draft_0003_domain_identity", DRAFT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def imported_modules(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    modules: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.append(node.module)
    return modules


def overwrite_page_version(current: dict, new_content: dict) -> None:
    assert_append_only(
        old_id=current["id"],
        new_id=current["id"],
        old_digest=current["content_hash"],
        new_digest=document_hash(new_content),
    )


def test_schema_types_stay_free_of_storage_and_auth_imports():
    modules = imported_modules(SCHEMA_TYPES)
    banned = ("sqlalchemy", "fastapi", "sinuo.domain.auth", "sinuo.infrastructure")
    for module in modules:
        for item in banned:
            assert not module.startswith(item)


def test_domain_models_reuse_the_foundation_base():
    source = DOMAIN_MODELS.read_text(encoding="utf-8")
    assert "DeclarativeBase" not in source
    assert "class IdempotencyRecord" not in source
    assert "class OutboxEvent" not in source
    assert "class OutboxDelivery" not in source
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sinuo.infrastructure.domain_models as models\n"
            "from sinuo.infrastructure.models import Base\n"
            "assert models.Base is Base\n"
            "assert len(Base.metadata.tables) == 38\n"
            "assert models.DOWNGRADE_ORDER[0] == 'login_throttles'\n"
            "assert models.DOWNGRADE_ORDER[-1] == 'projects'\n"
            "assert set(models.FOUNDATION_TABLES).isdisjoint(models.DOWNGRADE_ORDER)\n"
            "assert len(models.DOWNGRADE_ORDER) == 35\n",
        ],
        cwd=ROOT,
        env={**os.environ, "PYTHONPATH": str(ROOT / "src")},
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr


def test_draft_revision_is_executable_and_does_not_rewrite_foundation():
    module = load_draft()
    source = DRAFT.read_text(encoding="utf-8")
    assert module.revision == "0003_domain_identity"
    assert module.down_revision == "0002_lease_invariants"
    assert module.branch_labels is None
    assert module.depends_on is None
    for token in ("batch_alter_table", "foreign_keys"):
        assert token not in source
    for token in ("VACUUM", "PRAGMA"):
        assert token not in source.upper()
    assert re.search(r"\bCOMMIT\b", source.upper()) is None
    for table in module.UNCONDITIONAL_IMMUTABLE_TABLES:
        sql = module.trigger_sql(table).upper()
        assert "BEFORE UPDATE" in sql
        assert "BEFORE DELETE" in sql
        assert "UPDATE OF" not in sql
        assert "RAISE(ABORT" in sql
        assert " WHEN " not in f" {sql} "


def test_closed_sets_and_login_name_come_from_the_shared_vocabulary():
    assert Action.PROJECT_CREATE.value not in GRANT_ACTIONS
    assert "acl.manage" in GRANT_ACTIONS
    from sinuo.domain import schema_types

    assert schema_types.SENSITIVITY_LEVELS is SENSITIVITY_LEVELS
    assert schema_types.GRANT_SCOPE_TYPES is GRANT_SCOPE_TYPES
    with pytest.raises(ClosedSetError):
        assert_closed_set("lineage_kind", "next")
    assert normalize_login_name("  Synthetic-Lead  ") == "synthetic-lead"
    with pytest.raises(ClosedSetError):
        normalize_login_name("   ")


def test_append_only_corrections_reject_reusing_an_id():
    current = {
        "id": "synthetic-pv-a1",
        "page_id": PAGE_A,
        "project_id": PROJECT_A,
        "version_no": 1,
        "content_hash": digest("old"),
    }
    replacement = {
        "schema_version": 1,
        "nodes": [{"node_id": "synthetic-node-2"}],
        "edges": [],
    }
    with pytest.raises(ImmutableRecord):
        overwrite_page_version(current, replacement)
    corrected = correct_page_version(current, replacement, new_id="synthetic-pv-a2")
    assert corrected["id"] == "synthetic-pv-a2"
    assert corrected["parent_version_id"] == "synthetic-pv-a1"
    assert corrected["version_no"] == 2
    assert current["content_hash"] == digest("old")
    artifact = {
        "id": "synthetic-art-v1",
        "artifact_id": "synthetic-art",
        "project_id": PROJECT_A,
        "version_no": 1,
        "content_hash": digest("bytes"),
        "object_key": "synthetic/object-a",
        "license": "synthetic-license",
        "sensitivity": "project",
    }
    revised = correct_artifact_version(artifact, new_id="synthetic-art-v2", sensitivity="released")
    assert revised["version_no"] == 2
    assert revised["content_hash"] == artifact["content_hash"]
    assert revised["object_key"] == artifact["object_key"]
    decision = {
        "id": "synthetic-decision",
        "project_id": PROJECT_A,
        "account_id": ACCOUNT_A,
        "body": "synthetic original",
        "page_version_id": "synthetic-pv-a2",
        "artifact_version_id": None,
        "stage_id": None,
    }
    follow_up = correct_decision(
        decision,
        new_id="synthetic-decision-2",
        body="synthetic correction",
        reason="synthetic reason",
    )
    assert decision["body"] == "synthetic original"
    assert follow_up["supersedes_id"] == decision["id"]
    record = {
        "id": "synthetic-test",
        "project_id": PROJECT_A,
        "account_id": ACCOUNT_A,
        "artifact_version_id": "synthetic-art-v2",
    }
    corrected_record = correct_test_record(
        record,
        new_id="synthetic-test-2",
        observation="synthetic observation",
        result="synthetic result",
    )
    assert corrected_record["supersedes_id"] == record["id"]
    assert "observation" not in record


def test_project_creation_does_not_increment_an_existing_epoch():
    assert initial_project_counters() == (1, 1)
    with pytest.raises(AclEpochError):
        acl_commit(previous_epoch=0, next_epoch=1)
    with pytest.raises(AclEpochError):
        acl_commit(previous_epoch=1, next_epoch=1)
    with pytest.raises(AclEpochError):
        acl_commit(previous_epoch=1, next_epoch=3)
    assert acl_commit(previous_epoch=1, next_epoch=2) == 2


def test_documents_reject_cross_project_and_forbidden_keys():
    document = {
        "schema_version": 1,
        "nodes": [{"node_id": "synthetic-node-1", "artifact_version_id": "synthetic-art-b-v1"}],
        "edges": [],
    }
    with pytest.raises(CrossProjectDocument):
        assert_same_project_document(
            PROJECT_A,
            document,
            {"synthetic-art-b-v1": PROJECT_B, "synthetic-art-v1": PROJECT_A},
        )
    assert_same_project_document(
        PROJECT_A,
        {
            "schema_version": 1,
            "nodes": [{"node_id": "synthetic-node-1", "artifact_version_id": "synthetic-art-v1"}],
            "edges": [],
        },
        {"synthetic-art-v1": PROJECT_A},
    )
    with pytest.raises(DocumentSchemaError):
        assert_opinion_anchor(
            {"schema_version": 1, "nodes": [{"node_id": "synthetic-node-1"}], "edges": []},
            "synthetic-missing-node",
        )
    with pytest.raises(DocumentSchemaError):
        canonical_locator({"excerpt": "synthetic quote"})
    with pytest.raises(DocumentSchemaError):
        canonical_result({"prompt": "synthetic prompt"})
    with pytest.raises(DocumentSchemaError):
        canonical_document({"schema_version": 1, "nodes": [], "edges": [], "extra": True})
    with pytest.raises(CrossProjectDocument):
        assert_snapshot_reference(
            snapshot_project_id=PROJECT_A,
            referenced_project_id=PROJECT_B,
            membership_project_ids=(PROJECT_A, PROJECT_B),
        )
    with pytest.raises(CrossProjectDocument):
        assert_job_subject(job_project_id=PROJECT_A, subject_project_id=PROJECT_B)
    assert_command_project(project_id=f"account:{ACCOUNT_A}", transaction_project_id=PROJECT_A)
    with pytest.raises(CrossProjectDocument):
        assert_command_project(project_id=PROJECT_B, transaction_project_id=PROJECT_A)


def test_one_run_cannot_hold_four_roles_and_history_reads_do_not_move_pointers():
    assert_role_set(("evidence", "evidence", "method", "critic"))
    with pytest.raises(ClosedSetError):
        assert_role_set(("clarification", "evidence", "method", "critic"))
    with pytest.raises(ClosedSetError):
        assert_role_set(("sub_clarification",))
    assert_history_statements()
    with pytest.raises(HistoryMutation):
        assert_history_statements(("UPDATE pages SET head_version_id = 'synthetic-moved'",))
    before = PointerSnapshot(4, "synthetic-pv-a2", "synthetic-cfg-3", 5, 7)
    assert_no_pointer_movement(before, before)
    moved = PointerSnapshot(4, "synthetic-moved-head", "synthetic-cfg-3", 5, 7)
    with pytest.raises(HistoryMutation):
        assert_no_pointer_movement(before, moved)


@pytest.mark.integration
def test_empty_database_upgrades_to_domain_head(bare_engine):
    require_domain_head(bare_engine)
    with bare_engine.connect() as connection:
        names = application_tables(connection)
    assert "login_throttles" in names
    assert {"idempotency_records", "outbox_events", "outbox_deliveries"} <= names
    assert len(names) == 43


@pytest.mark.integration
def test_upgrade_preserves_foundation_rows_and_lease(bare_engine):
    with bare_engine.begin() as connection:
        command.upgrade(conftest.alembic_config(connection), "0002_lease_invariants")
    with write_uow(bare_engine) as session:
        session.add(
            IdempotencyRecord(
                project_id="project-synthetic",
                actor_id="actor-synthetic",
                command="synthetic.confirm",
                request_key="synthetic-key",
                request_hash=digest("request"),
                response_status=201,
                response_json={"synthetic": True},
                created_at=NOW,
            )
        )
        event = OutboxEvent(
            event_id="synthetic-event",
            project_id="project-synthetic",
            event_type="synthetic.created",
            schema_version=1,
            payload={"synthetic": True},
            created_at=NOW,
        )
        session.add(event)
        session.flush()
        session.add(OutboxDelivery(event_id=event.event_id, consumer_name="synthetic", attempts=0))
        session.flush()
        lease = claim_delivery(
            session,
            event_id=event.event_id,
            consumer_name="synthetic",
            owner="worker",
            now=0,
            ttl_seconds=10,
        )
    assert lease is not None
    before_rows = foundation_rows(bare_engine)
    before_sql = foundation_ddl(bare_engine)
    with bare_engine.begin() as connection:
        command.upgrade(conftest.alembic_config(connection), "head")
        version = connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
    assert version == "0008_ticket_expiry"
    assert foundation_rows(bare_engine) == before_rows
    assert foundation_ddl(bare_engine) == before_sql
    with write_uow(bare_engine) as session:
        complete_delivery(session, lease, now=1)
    with Session(bare_engine) as session:
        delivery = session.get(OutboxDelivery, ("synthetic-event", "synthetic"))
        assert delivery is not None
        assert delivery.state == "delivered"


def foundation_rows(engine) -> dict[str, list[dict]]:
    with engine.connect() as connection:
        dumped = {}
        for name in ("idempotency_records", "outbox_events", "outbox_deliveries"):
            dumped[name] = [
                dict(row)
                for row in connection.execute(text(f"SELECT * FROM {name}")).mappings()
            ]
        return dumped


def foundation_ddl(engine):
    with engine.connect() as connection:
        return connection.execute(
            text(
                "SELECT name, sql FROM sqlite_master WHERE tbl_name IN "
                "('idempotency_records', 'outbox_events', 'outbox_deliveries') "
                "AND sql IS NOT NULL ORDER BY name"
            )
        ).all()


@pytest.mark.integration
def test_parallel_pages_have_no_default_edge(bare_engine):
    require_domain_head(bare_engine)
    with bare_engine.begin() as connection:
        assert seed(connection) == 0


@pytest.mark.integration
def test_counters_stay_independent_and_versions_are_unique(bare_engine):
    require_domain_head(bare_engine)
    with bare_engine.begin() as connection:
        seed(connection)
        before = counter_snapshot(connection)
        assert before == {
            "content_revision": 5,
            "layout_revision": 7,
            "page_version_no": 2,
            "project_revision": 4,
            "stage_version_no": 3,
            "artifact_version_no": 2,
            "candidate_revision": 1,
            "opinion_revision": 2,
            "next_event_seq": 8,
        }
        assert set(before) == set(INDEPENDENT_COUNTERS)
        execute(
            connection,
            """
            UPDATE page_drafts
            SET content_json = :body, content_hash = :content_hash,
                content_revision = content_revision + 1, updated_at = :now
            WHERE page_id = :page_id
              AND base_version_id IS :base_version
              AND content_revision = 5
            """,
            {
                "body": canonical_document(
                    {"schema_version": 1, "nodes": [{"node_id": "synthetic-node-2"}], "edges": []}
                ),
                "content_hash": digest("draft-cas"),
                "now": NOW + 1,
                "page_id": PAGE_A,
                "base_version": "synthetic-pv-a2",
            },
        )
        after = counter_snapshot(connection)
        assert_counter_moved_only(before, after, changed="content_revision")
        assert after["content_revision"] == 6
        reject(
            connection,
            """
            INSERT INTO page_versions (
                id, project_id, page_id, version_no, parent_version_id, content_json,
                content_hash, schema_version, project_revision, created_by_account_id, created_at
            ) VALUES (
                'synthetic-pv-dup', :project_a, :page_a, 1, NULL, '{}', :content_hash, 1, 1,
                :account_a, :now
            )
            """,
            {
                "project_a": PROJECT_A,
                "page_a": PAGE_A,
                "content_hash": digest("dup-page"),
                "account_a": ACCOUNT_A,
                "now": NOW,
            },
        )
        reject(
            connection,
            """
            INSERT INTO artifact_versions (
                id, project_id, artifact_id, version_no, content_hash, size_bytes, media_type,
                object_key, license, sensitivity, ai_processing_allowed, project_revision,
                created_by_account_id, created_at
            ) VALUES (
                'synthetic-art-dup', :project_a, 'synthetic-art', 1, :content_hash, 1,
                'image/png', 'synthetic/object-dup', 'synthetic-license', 'project', 0, 1,
                :account_a, :now
            )
            """,
            {
                "project_a": PROJECT_A,
                "content_hash": digest("dup-art"),
                "account_a": ACCOUNT_A,
                "now": NOW,
            },
        )
        execute(
            connection,
            """
            INSERT INTO idempotency_records (
                project_id, actor_id, command, request_key, request_hash, response_status,
                response_json, created_at
            ) VALUES (
                :project_a, 'actor-synthetic', 'synthetic.confirm', 'synthetic-key',
                :request_hash, 201, :response_json, :now
            )
            """,
            {
                "project_a": PROJECT_A,
                "request_hash": digest("request"),
                "response_json": '{"synthetic": true}',
                "now": NOW,
            },
        )
        reject(
            connection,
            """
            INSERT INTO idempotency_records (
                project_id, actor_id, command, request_key, request_hash, response_status,
                response_json, created_at
            ) VALUES (
                :project_a, 'actor-synthetic', 'synthetic.confirm', 'synthetic-key',
                :request_hash, 201, :response_json, :now
            )
            """,
            {
                "project_a": PROJECT_A,
                "request_hash": digest("request"),
                "response_json": '{"synthetic": true}',
                "now": NOW,
            },
        )


@pytest.mark.integration
def test_version_rows_and_tombstones_reject_raw_sql_mutation(bare_engine):
    require_domain_head(bare_engine)
    with bare_engine.begin() as connection:
        seed(connection)
        original = connection.execute(
            text("SELECT content_hash FROM page_versions WHERE id = 'synthetic-pv-a1'")
        ).scalar_one()
    with bare_engine.connect() as second:
        reject(second, "UPDATE page_versions SET content_json = '{}' WHERE id = 'synthetic-pv-a1'")
        reject(second, "DELETE FROM page_versions WHERE id = 'synthetic-pv-a1'")
        reject(
            second,
            "INSERT OR REPLACE INTO page_versions ("
            "id, project_id, page_id, version_no, parent_version_id, content_json, content_hash, "
            "schema_version, project_revision, created_by_account_id, created_at"
            ") VALUES ("
            "'synthetic-pv-a1', :project_a, :page_a, 1, NULL, '{}', :content_hash, 1, 1, "
            ":account_a, :now)",
            {
                "project_a": PROJECT_A,
                "page_a": PAGE_A,
                "content_hash": digest("replaced"),
                "account_a": ACCOUNT_A,
                "now": NOW,
            },
        )
        for column, value in (
            ("content_hash", digest("changed")),
            ("object_key", "synthetic/changed"),
            ("license", "synthetic-other-license"),
            ("sensitivity", "released"),
        ):
            reject(
                second,
                f"UPDATE artifact_versions SET {column} = :value WHERE id = 'synthetic-art-v1'",
                {"value": value},
            )
        reject(
            second,
            "UPDATE privacy_tombstones SET reason_category = 'policy' WHERE id = 'synthetic-tomb'",
        )
        reject(second, "DELETE FROM privacy_tombstones WHERE id = 'synthetic-tomb'")
        triggers = second.execute(
            text(
                "SELECT tbl_name, name FROM sqlite_master WHERE type = 'trigger' "
                "AND tbl_name IN ('page_versions', 'artifact_versions') ORDER BY tbl_name, name"
            )
        ).all()
        stored = second.execute(
            text("SELECT content_hash FROM page_versions WHERE id = 'synthetic-pv-a1'")
        ).scalar_one()
    assert stored == original
    names = {(table, name) for table, name in triggers}
    assert ("page_versions", "page_versions_before_update") in names
    assert ("page_versions", "page_versions_before_delete") in names
    assert ("artifact_versions", "artifact_versions_before_update") in names
    assert ("artifact_versions", "artifact_versions_before_delete") in names


@pytest.mark.integration
def test_composite_foreign_keys_reject_cross_project_links(bare_engine):
    require_domain_head(bare_engine)
    body = page_body()
    content_hash = digest("cross")
    with bare_engine.begin() as connection:
        seed(connection)
        reject(
            connection,
            """
            INSERT INTO pages (
                id, project_id, stage_id, head_version_id, status, title, created_at
            ) VALUES (
                'synthetic-page-x', :project_a, 'synthetic-stage-b', NULL, 'active',
                'synthetic-cross', :now
            )
            """,
            {"project_a": PROJECT_A, "now": NOW},
        )
        reject(
            connection,
            """
            INSERT INTO page_versions (
                id, project_id, page_id, version_no, parent_version_id, content_json,
                content_hash, schema_version, project_revision, created_by_account_id, created_at
            ) VALUES (
                'synthetic-pv-cross', :project_a, :page_b, 2, 'synthetic-pv-a1', :body,
                :content_hash, 1, 1, :account_a, :now
            )
            """,
            {
                "project_a": PROJECT_A,
                "page_b": PAGE_B,
                "body": body,
                "content_hash": content_hash,
                "account_a": ACCOUNT_A,
                "now": NOW,
            },
        )
        reject(
            connection,
            """
            INSERT INTO page_edges (
                id, project_id, source_version_id, target_version_id, kind, project_revision,
                reason, created_at
            ) VALUES (
                'synthetic-edge-x', :project_b, 'synthetic-pv-a2', 'synthetic-pv-b1',
                'branches_from', 1, NULL, :now
            )
            """,
            {"project_b": PROJECT_B, "now": NOW},
        )
        reject(
            connection,
            """
            INSERT INTO page_drafts (
                page_id, project_id, base_version_id, content_revision, content_hash,
                content_json, schema_version, updated_at
            ) VALUES (
                :page_b, :project_a, 'synthetic-pv-a2', 1, :content_hash, :body, 1, :now
            )
            """,
            {
                "page_b": PAGE_B,
                "project_a": PROJECT_A,
                "content_hash": content_hash,
                "body": body,
                "now": NOW,
            },
        )
        reject(
            connection,
            """
            INSERT INTO evidence_anchors (
                id, project_id, artifact_version_id, locator_type, locator_json, excerpt_hash,
                extractor_version, visibility, sensitivity, supersedes_id, schema_version,
                created_by_account_id, created_at
            ) VALUES (
                'synthetic-evidence-x', :project_a, 'synthetic-art-b-v1', 'page', '{}',
                :excerpt_hash, 'synthetic-extractor', 'project', 'project', NULL, 1,
                :account_a, :now
            )
            """,
            {
                "project_a": PROJECT_A,
                "excerpt_hash": digest("cross-excerpt"),
                "account_a": ACCOUNT_A,
                "now": NOW,
            },
        )
        reject(
            connection,
            """
            INSERT INTO object_grants (
                id, project_id, account_id, action, scope_type, status, created_at, revoked_at,
                created_by_account_id, stage_id, page_id, page_version_id, artifact_id,
                artifact_version_id, evidence_anchor_id
            ) VALUES (
                'synthetic-grant-x', :project_b, :account_a, 'page.read', 'page', 'active', :now,
                NULL, :account_a, NULL, :page_a, NULL, NULL, NULL, NULL
            )
            """,
            {"project_b": PROJECT_B, "account_a": ACCOUNT_A, "page_a": PAGE_A, "now": NOW},
        )
        reject(
            connection,
            """
            INSERT INTO context_snapshot_refs (snapshot_id, page_version_id, project_id)
            VALUES ('synthetic-snapshot', 'synthetic-pv-a2', :project_b)
            """,
            {"project_b": PROJECT_B},
        )
        execute(
            connection,
            """
            INSERT INTO run_tasks (
                id, run_id, project_id, role, status, mandatory, input_hash, available_at,
                attempt_no, lease_owner, lease_until, fencing_token, output_ref, last_error
            ) VALUES (
                'synthetic-task-method', 'synthetic-run', :project_a, 'method', 'queued', 1,
                :input_hash, :now, 0, NULL, NULL, 0, NULL, NULL
            )
            """,
            {"project_a": PROJECT_A, "input_hash": digest("method"), "now": NOW},
        )
        reject(
            connection,
            """
            INSERT INTO run_tasks (
                id, run_id, project_id, role, status, mandatory, input_hash, available_at,
                attempt_no, lease_owner, lease_until, fencing_token, output_ref, last_error
            ) VALUES (
                'synthetic-task-critic', 'synthetic-run', :project_a, 'critic', 'queued', 1,
                :input_hash, :now, 0, NULL, NULL, 0, NULL, NULL
            )
            """,
            {"project_a": PROJECT_A, "input_hash": digest("critic"), "now": NOW},
        )


@pytest.mark.integration
def test_history_selects_do_not_move_heads(bare_engine):
    require_domain_head(bare_engine)
    supplied = {
        "project_id": PROJECT_A,
        "page_id": PAGE_A,
        "stage_id": "synthetic-stage",
        "view_state_id": "synthetic-view",
    }
    with bare_engine.begin() as connection:
        seed(connection)
        before = pointers(connection)
        for statement in history_statements():
            needed = {
                name: supplied[name] for name in re.findall(r":(\w+)", statement)
            }
            connection.execute(text(statement), needed)
        after = pointers(connection)
    assert_history_statements()
    assert_no_pointer_movement(before, after)


@pytest.mark.integration
def test_preferences_and_throttles_can_be_removed(bare_engine):
    require_domain_head(bare_engine)
    with bare_engine.begin() as connection:
        seed(connection)
        execute(connection, "DELETE FROM view_states WHERE id = 'synthetic-view'")
        execute(
            connection,
            "UPDATE login_throttles SET attempts = 2 WHERE bucket_key = 'synthetic-bucket'",
        )
        execute(connection, "DELETE FROM login_throttles WHERE bucket_key = 'synthetic-bucket'")
        reject(
            connection,
            """
            INSERT INTO accounts (
                id, login_name, display_name, password_hash, status, auth_epoch,
                can_create_project, created_at, disabled_at
            ) VALUES (
                'cccccccc-cccc-4ccc-8ccc-ccccccccccc3', 'synthetic-bad', 'synthetic-bad',
                'plaintext', 'active', 1, 0, :now, NULL
            )
            """,
            {"now": NOW},
        )


@pytest.mark.integration
def test_downgrade_drops_domain_tables_and_keeps_foundation_rows(bare_engine):
    with bare_engine.begin() as connection:
        command.upgrade(conftest.alembic_config(connection), "0002_lease_invariants")
    with write_uow(bare_engine) as session:
        session.add(
            IdempotencyRecord(
                project_id="project-synthetic",
                actor_id="actor-synthetic",
                command="synthetic.confirm",
                request_key="synthetic-key",
                request_hash=digest("request"),
                response_status=201,
                response_json={"synthetic": True},
                created_at=NOW,
            )
        )
    with bare_engine.begin() as connection:
        command.upgrade(conftest.alembic_config(connection), "head")
        assert (
            connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
            == "0008_ticket_expiry"
        )
        seed(connection)
        command.downgrade(conftest.alembic_config(connection), "0002_lease_invariants")
        assert (
            connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
            == "0002_lease_invariants"
        )
        assert application_tables(connection) == {
            "idempotency_records",
            "outbox_events",
            "outbox_deliveries",
        }
        receipt_count = connection.execute(
            text("SELECT count(*) FROM idempotency_records")
        ).scalar_one()
        assert receipt_count == 1
        command.upgrade(conftest.alembic_config(connection), "head")
        assert (
            connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
            == "0008_ticket_expiry"
        )
        assert len(application_tables(connection)) == 43


@pytest.mark.integration
def test_readyz_reports_domain_revision(engine, database_url):
    app = create_app(Settings(_env_file=None, database_url=database_url))
    with TestClient(app) as client:
        response = client.get("/readyz")
    assert response.status_code == 200
    assert response.json() == {
        "status": "ready",
        "scope": "foundation",
        "schema_revision": "0008_ticket_expiry",
    }


@pytest.mark.integration
def test_terminal_task_and_draft_content_reject_ordinary_updates(engine):
    require_domain_head(engine)
    with engine.begin() as connection:
        seed(connection)
        execute(
            connection,
            "UPDATE run_tasks SET status = 'succeeded' WHERE id = 'synthetic-task-1'",
        )
        reject(
            connection,
            "UPDATE run_tasks SET status = 'retry_wait' WHERE id = 'synthetic-task-1'",
        )
        reject(
            connection,
            """
            UPDATE run_tasks
            SET status = 'running', fencing_token = 4, lease_owner = 'synthetic', lease_until = :now
            WHERE id = 'synthetic-task-1'
            """,
            {"now": NOW},
        )
        status = connection.execute(
            text("SELECT status FROM run_tasks WHERE id = 'synthetic-task-1'")
        ).scalar_one()
        assert status == "succeeded"
        reject(
            connection,
            """
            UPDATE page_drafts
            SET content_json = :body, content_hash = :other_hash
            WHERE page_id = :page_id
            """,
            {
                "body": '{"edges":[],"nodes":[],"schema_version":1}',
                "other_hash": digest("other-draft"),
                "page_id": PAGE_A,
            },
        )
        reject(
            connection,
            "UPDATE pages SET head_version_id = NULL WHERE id = :page_id",
            {"page_id": PAGE_A},
        )


def _trigger_names(connection) -> set[str]:
    return set(
        connection.execute(
            text("SELECT name FROM sqlite_master WHERE type = 'trigger'")
        ).scalars()
    )


@pytest.mark.integration
def test_database_stamped_0003_receives_repair(bare_engine):
    with bare_engine.begin() as connection:
        command.upgrade(conftest.alembic_config(connection), "0003_domain_identity")
        before = _trigger_names(connection)
        assert "accounts_login_name_before_insert" not in before
        assert "jobs_before_update" not in before
        command.upgrade(conftest.alembic_config(connection), "head")
        after = _trigger_names(connection)
        version = connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
    assert version == "0008_ticket_expiry"
    assert "accounts_login_name_before_insert" in after
    assert "jobs_before_update" in after
    assert "run_tasks_before_update" in after


@pytest.mark.integration
def test_login_name_and_terminal_job_are_enforced(engine):
    require_domain_head(engine)
    password = "$argon2id$synthetic-hash"
    with engine.begin() as connection:
        execute(
            connection,
            """
            INSERT INTO accounts (
                id, login_name, display_name, password_hash, status, auth_epoch,
                can_create_project, created_at, disabled_at
            ) VALUES (
                'dddddddd-dddd-4ddd-8ddd-ddddddddddd1', 'synthetic-lead', 'Synthetic',
                :password, 'active', 1, 1, :now, NULL
            )
            """,
            {"password": password, "now": NOW},
        )
        reject(
            connection,
            """
            INSERT INTO accounts (
                id, login_name, display_name, password_hash, status, auth_epoch,
                can_create_project, created_at, disabled_at
            ) VALUES (
                'dddddddd-dddd-4ddd-8ddd-ddddddddddd2', :name, 'Synthetic',
                :password, 'active', 1, 0, :now, NULL
            )
            """,
            {"name": "Synthetic-Lead", "password": password, "now": NOW},
        )
        reject(
            connection,
            """
            INSERT INTO accounts (
                id, login_name, display_name, password_hash, status, auth_epoch,
                can_create_project, created_at, disabled_at
            ) VALUES (
                'dddddddd-dddd-4ddd-8ddd-ddddddddddd3', :name, 'Synthetic',
                :password, 'active', 1, 0, :now, NULL
            )
            """,
            {"name": "\u03a9mega-user", "password": password, "now": NOW},
        )
        execute(
            connection,
            """
            INSERT INTO accounts (
                id, login_name, display_name, password_hash, status, auth_epoch,
                can_create_project, created_at, disabled_at
            ) VALUES (
                'dddddddd-dddd-4ddd-8ddd-ddddddddddd4', :name, 'Synthetic',
                :password, 'active', 1, 0, :now, NULL
            )
            """,
            {"name": "\u03c9mega-user", "password": password, "now": NOW},
        )
        execute(
            connection,
            """
            INSERT INTO projects (
                id, title, brief, template_key, revision, access_epoch, created_at
            )
            VALUES ('synthetic-job-project', 'synthetic-job', NULL, NULL, 1, 1, :now)
            """,
            {"now": NOW},
        )
        execute(
            connection,
            """
            INSERT INTO jobs (
                id, project_id, job_kind, status, available_at, attempt_no, lease_owner,
                lease_until, fencing_token, input_hash, output_ref, last_error, subject_type,
                subject_id, source_event_id, payload, created_at
            ) VALUES (
                'synthetic-job', 'synthetic-job-project', 'index', 'succeeded', :now, 1,
                NULL, NULL, 1, :digest, NULL, NULL, 'page', 'synthetic-page', NULL, '{}', :now
            )
            """,
            {"now": NOW, "digest": digest("job")},
        )
        reject(
            connection,
            """
            UPDATE jobs
            SET status = 'running', lease_owner = 'old-worker', lease_until = :now,
                fencing_token = 2
            WHERE id = 'synthetic-job'
            """,
            {"now": NOW},
        )


@pytest.mark.concurrency
def test_equivalent_canonical_login_names_race(engine):
    require_domain_head(engine)
    barrier = Barrier(2)
    errors: list[str] = []

    def insert(account_id: str) -> None:
        barrier.wait(timeout=5)
        try:
            with engine.begin() as connection:
                connection.execute(
                    text(
                        """
                        INSERT INTO accounts (
                            id, login_name, display_name, password_hash, status, auth_epoch,
                            can_create_project, created_at, disabled_at
                        ) VALUES (
                            :id, 'synthetic-race', 'Synthetic', :password, 'active', 1, 0,
                            :now, NULL
                        )
                        """
                    ),
                    {
                        "id": account_id,
                        "password": "$argon2id$synthetic-hash",
                        "now": NOW,
                    },
                )
        except IntegrityError:
            errors.append(account_id)

    first = Thread(target=insert, args=("eeeeeeee-eeee-4eee-8eee-eeeeeeeeeee1",))
    second = Thread(target=insert, args=("eeeeeeee-eeee-4eee-8eee-eeeeeeeeeee2",))
    first.start()
    second.start()
    first.join(timeout=5)
    second.join(timeout=5)
    assert not first.is_alive()
    assert not second.is_alive()
    with engine.connect() as connection:
        count = connection.execute(
            text("SELECT count(*) FROM accounts WHERE login_name = 'synthetic-race'")
        ).scalar_one()
    assert count == 1
    assert len(errors) == 1


@pytest.mark.integration
def test_consume_candidate_targets_one_row_and_rolls_back(engine):
    from sinuo.application.cas import CasMismatch, consume_candidate

    require_domain_head(engine)
    with engine.begin() as connection:
        seed(connection)
        execute(
            connection,
            """
            INSERT INTO candidates (
                id, project_id, run_id, action, page_mode, status, revision, proposal_json,
                proposal_hash, input_hash, context_snapshot_id, schema_version, created_at
            ) VALUES (
                'synthetic-candidate-2', :project_a, 'synthetic-run', 'add_node', NULL,
                'pending', 1, '{}', :proposal_hash, :proposal_hash, 'synthetic-snapshot',
                1, :now
            )
            """,
            {"project_a": PROJECT_A, "proposal_hash": digest("proposal-2"), "now": NOW},
        )
        execute(
            connection,
            """
            UPDATE candidates
            SET status = 'committed', revision = revision + 1
            WHERE status = 'pending'
            """,
        )
        statuses = connection.execute(text("SELECT id, status FROM candidates ORDER BY id")).all()
        execute(
            connection,
            """
            INSERT INTO candidates (
                id, project_id, run_id, action, page_mode, status, revision, proposal_json,
                proposal_hash, input_hash, context_snapshot_id, schema_version, created_at
            ) VALUES (
                'synthetic-candidate-3', :project_a, 'synthetic-run', 'add_node', NULL,
                'pending', 1, '{}', :proposal_hash, :left_hash, 'synthetic-snapshot', 1, :now
            )
            """,
            {
                "project_a": PROJECT_A,
                "proposal_hash": digest("proposal-3"),
                "left_hash": digest("left"),
                "now": NOW,
            },
        )
        execute(
            connection,
            """
            INSERT INTO candidates (
                id, project_id, run_id, action, page_mode, status, revision, proposal_json,
                proposal_hash, input_hash, context_snapshot_id, schema_version, created_at
            ) VALUES (
                'synthetic-candidate-4', :project_a, 'synthetic-run', 'add_node', NULL,
                'pending', 1, '{}', :proposal_hash, :right_hash, 'synthetic-snapshot', 1, :now
            )
            """,
            {
                "project_a": PROJECT_A,
                "proposal_hash": digest("proposal-4"),
                "right_hash": digest("right"),
                "now": NOW,
            },
        )
    assert statuses == [
        ("synthetic-candidate", "committed"),
        ("synthetic-candidate-2", "committed"),
    ]
    with write_uow(engine) as session:
        consume_candidate(
            session,
            project_id=PROJECT_A,
            candidate_id="synthetic-candidate-3",
            expected_revision=1,
            new_status="committed",
        )
    with engine.connect() as connection:
        other = connection.execute(
            text("SELECT status FROM candidates WHERE id = 'synthetic-candidate-4'")
        ).scalar_one()
    assert other == "pending"
    with pytest.raises(CasMismatch):
        with write_uow(engine) as session:
            session.add(
                OutboxEvent(
                    event_id="synthetic-cas-event",
                    project_id=PROJECT_A,
                    event_type="synthetic.cas",
                    schema_version=1,
                    payload={"synthetic": True},
                    created_at=NOW,
                )
            )
            consume_candidate(
                session,
                project_id=PROJECT_A,
                candidate_id="synthetic-candidate-3",
                expected_revision=1,
                new_status="rejected",
            )
    with engine.connect() as connection:
        leaked = connection.execute(
            text("SELECT count(*) FROM outbox_events WHERE event_id = 'synthetic-cas-event'")
        ).scalar_one()
    assert leaked == 0
