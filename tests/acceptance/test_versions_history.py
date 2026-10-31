"""Checkpoint, history, branch, and layout on a real SQLite database."""

import hashlib
import json
import re
from threading import Barrier, Thread

import pytest
from sqlalchemy import event, text

from sinuo.application.versions.branches import branch_from_version
from sinuo.application.versions.checkpoints import CheckpointConflict, checkpoint_page
from sinuo.application.versions.drafts import update_draft
from sinuo.application.versions.history import read_history
from sinuo.application.versions.layouts import LayoutConflict, save_layout
from sinuo.domain.schema_types import canonical_document, canonical_json, document_hash
from sinuo.infrastructure.database import write_uow
from tests.acceptance.test_domain_schema import (
    ACCOUNT_A,
    NOW,
    PAGE_A,
    PAGE_B,
    PROJECT_A,
    require_domain_head,
    seed,
)

_WRITE = re.compile(r"\b(INSERT|UPDATE|DELETE|REPLACE)\b", re.IGNORECASE)


@pytest.fixture
def ready(engine):
    require_domain_head(engine)
    with engine.begin() as connection:
        seed(connection)
    return engine


def _one(engine, sql, params=None):
    with engine.connect() as connection:
        return connection.execute(text(sql), params or {}).one()


def _scalar(engine, sql, params=None):
    with engine.connect() as connection:
        return connection.execute(text(sql), params or {}).scalar_one()


def _all(engine, sql, params=None):
    with engine.connect() as connection:
        return [tuple(row) for row in connection.execute(text(sql), params or {}).all()]


def _draft(engine):
    return _one(
        engine,
        """
        SELECT base_version_id, content_revision, content_hash, content_json
        FROM page_drafts WHERE page_id = :page
        """,
        {"page": PAGE_A},
    )


def _project(engine):
    return _one(
        engine,
        "SELECT revision, access_epoch FROM projects WHERE id = :project",
        {"project": PROJECT_A},
    )


def _versions(engine, page_id):
    return _all(
        engine,
        """
        SELECT id, version_no, parent_version_id, content_json, content_hash,
               project_revision, created_at
        FROM page_versions WHERE page_id = :page ORDER BY version_no, id
        """,
        {"page": page_id},
    )


def _page(engine, page_id):
    return _one(
        engine,
        """
        SELECT id, project_id, stage_id, head_version_id, status, title, created_at
        FROM pages WHERE id = :page
        """,
        {"page": page_id},
    )


def _confirm(engine, *, head, revision, content_hash, now):
    with write_uow(engine) as session:
        return checkpoint_page(
            session,
            project_id=PROJECT_A,
            page_id=PAGE_A,
            expected_head_version_id=head,
            expected_content_revision=revision,
            expected_content_hash=content_hash,
            actor_id=ACCOUNT_A,
            now=now,
        )


@pytest.mark.integration
def test_checkpoint_appends_and_keeps_old_bytes(ready):
    before_a1 = _versions(ready, PAGE_A)[0]
    before_a2 = _versions(ready, PAGE_A)[1]
    draft = _draft(ready)
    assert _project(ready).revision == 4
    assert _project(ready).access_epoch == 1
    result = _confirm(
        ready,
        head=draft.base_version_id,
        revision=draft.content_revision,
        content_hash=draft.content_hash,
        now=NOW + 1,
    )
    stored = _versions(ready, PAGE_A)
    assert stored[0] == before_a1
    assert stored[1] == before_a2
    assert stored[2][0] == result.page_version_id
    assert stored[2][1] == 3
    assert stored[2][2] == "synthetic-pv-a2"
    assert stored[2][3] == draft.content_json
    assert stored[2][4] == draft.content_hash
    assert _page(ready, PAGE_A).head_version_id == result.page_version_id
    moved = _draft(ready)
    assert moved.base_version_id == result.page_version_id
    assert moved.content_revision == draft.content_revision + 1
    assert moved.content_json == draft.content_json
    assert moved.content_hash == draft.content_hash
    assert _project(ready).revision == 5
    assert _project(ready).access_epoch == 1
    assert _scalar(
        ready, "SELECT layout_revision FROM view_states WHERE id = 'synthetic-view'"
    ) == 7
    audit = _one(
        ready,
        """
        SELECT command, subject_id, project_revision, payload
        FROM audit_events WHERE id = :id
        """,
        {"id": result.audit_event_id},
    )
    assert audit.command == "page.checkpoint"
    assert audit.subject_id == result.page_version_id
    assert audit.project_revision == 5
    assert json.loads(audit.payload)["parent_version_id"] == "synthetic-pv-a2"
    outbox = _one(
        ready,
        "SELECT event_type, schema_version, payload FROM outbox_events WHERE event_id = :id",
        {"id": result.outbox_event_id},
    )
    payload = outbox.payload if isinstance(outbox.payload, dict) else json.loads(outbox.payload)
    assert outbox.event_type == "page.version_created"
    assert outbox.schema_version == 1
    assert payload["page_version_id"] == result.page_version_id
    edited = {
        "schema_version": 1,
        "nodes": [{"node_id": "synthetic-node-1"}, {"node_id": "synthetic-node-2"}],
        "edges": [
            {
                "edge_id": "synthetic-edge-n1",
                "source_node_id": "synthetic-node-1",
                "target_node_id": "synthetic-node-2",
            }
        ],
    }
    body = canonical_document(edited)
    with write_uow(ready) as session:
        update_draft(
            session,
            page_id=PAGE_A,
            project_id=PROJECT_A,
            base_version_id=result.page_version_id,
            expected_revision=result.content_revision,
            content_json=body,
            content_hash=document_hash(edited),
            now=NOW + 2,
        )
    for _ in range(4):
        with write_uow(ready) as session:
            history = read_history(session, project_id=PROJECT_A, page_id=PAGE_A)
    assert [item.page_version_id for item in history] == [
        "synthetic-pv-a1",
        "synthetic-pv-a2",
        result.page_version_id,
    ]
    assert history[0].content_hash == before_a1[4]
    assert history[1].content_hash == before_a2[4]
    assert history[2].content_json == draft.content_json
    assert _versions(ready, PAGE_A)[:2] == [before_a1, before_a2]
    assert _versions(ready, PAGE_A)[2][3:5] == (draft.content_json, draft.content_hash)
    assert _page(ready, PAGE_A).head_version_id == result.page_version_id
    assert _project(ready).revision == 5
    assert _project(ready).access_epoch == 1
    assert _draft(ready).content_revision == result.content_revision + 1


@pytest.mark.integration
def test_checkpoint_conflict_rolls_back_unless_one_row_moves(ready):
    before_versions = _versions(ready, PAGE_A)
    before_page = tuple(_page(ready, PAGE_A))
    before_draft = _draft(ready)
    before_project = tuple(_project(ready))
    audits = _scalar(ready, "SELECT count(*) FROM audit_events")
    outbox = _scalar(ready, "SELECT count(*) FROM outbox_events")

    def unchanged() -> None:
        assert _versions(ready, PAGE_A) == before_versions
        assert tuple(_page(ready, PAGE_A)) == before_page
        assert _draft(ready) == before_draft
        assert tuple(_project(ready)) == before_project
        assert _scalar(ready, "SELECT count(*) FROM audit_events") == audits
        assert _scalar(ready, "SELECT count(*) FROM outbox_events") == outbox

    with pytest.raises(CheckpointConflict):
        _confirm(
            ready,
            head="synthetic-pv-a1",
            revision=before_draft.content_revision,
            content_hash=before_draft.content_hash,
            now=NOW + 1,
        )
    unchanged()
    with pytest.raises(CheckpointConflict):
        _confirm(
            ready,
            head=before_draft.base_version_id,
            revision=before_draft.content_revision + 9,
            content_hash=before_draft.content_hash,
            now=NOW + 2,
        )
    unchanged()
    result = _confirm(
        ready,
        head=before_draft.base_version_id,
        revision=before_draft.content_revision,
        content_hash=before_draft.content_hash,
        now=NOW + 3,
    )
    assert _page(ready, PAGE_A).head_version_id == result.page_version_id
    assert len(_versions(ready, PAGE_A)) == 3
    assert _project(ready).revision == 5
    assert _project(ready).access_epoch == 1


@pytest.mark.integration
@pytest.mark.concurrency
def test_two_checkpoints_cannot_both_move_the_same_head(ready):
    draft = _draft(ready)
    barrier = Barrier(2)
    outcomes: list[str] = []

    def attempt() -> None:
        barrier.wait(timeout=5)
        try:
            with write_uow(ready) as session:
                checkpoint_page(
                    session,
                    project_id=PROJECT_A,
                    page_id=PAGE_A,
                    expected_head_version_id=draft.base_version_id,
                    expected_content_revision=draft.content_revision,
                    expected_content_hash=draft.content_hash,
                    actor_id=ACCOUNT_A,
                    now=NOW + 1,
                )
            outcomes.append("ok")
        except CheckpointConflict:
            outcomes.append("conflict")

    first = Thread(target=attempt)
    second = Thread(target=attempt)
    first.start()
    second.start()
    first.join(timeout=10)
    second.join(timeout=10)
    assert not first.is_alive()
    assert not second.is_alive()
    assert sorted(outcomes) == ["conflict", "ok"]
    assert _scalar(
        ready,
        "SELECT count(*) FROM page_versions WHERE page_id = :page",
        {"page": PAGE_A},
    ) == 3
    assert _project(ready).revision == 5
    assert _draft(ready).content_revision == 6


@pytest.mark.integration
def test_read_history_selects_without_moving_pointers(ready):
    before_versions = _versions(ready, PAGE_A)
    before_page = tuple(_page(ready, PAGE_A))
    before_draft = tuple(_draft(ready))
    before_project = tuple(_project(ready))
    statements: list[str] = []

    def capture(_connection, _cursor, statement, _parameters, _context, _executemany) -> None:
        statements.append(statement)

    seen = None
    for _ in range(4):
        with write_uow(ready) as session:
            event.listen(session.connection(), "before_cursor_execute", capture)
            try:
                seen = read_history(session, project_id=PROJECT_A, page_id=PAGE_A)
                assert not session.new
                assert not session.dirty
                assert not session.deleted
            finally:
                event.remove(session.connection(), "before_cursor_execute", capture)
    assert seen is not None
    assert [item.page_version_id for item in seen] == ["synthetic-pv-a1", "synthetic-pv-a2"]
    assert seen[0].content_hash == before_versions[0][4]
    assert seen[1].content_hash == before_versions[1][4]
    assert statements
    for statement in statements:
        assert statement.lstrip().upper().startswith("SELECT")
        assert _WRITE.search(statement) is None
    assert _versions(ready, PAGE_A) == before_versions
    assert tuple(_page(ready, PAGE_A)) == before_page
    assert tuple(_draft(ready)) == before_draft
    assert tuple(_project(ready)) == before_project
    assert _page(ready, PAGE_B).head_version_id == "synthetic-pv-b1"


@pytest.mark.integration
def test_branch_keeps_later_source_versions_and_skips_tests(ready):
    edited = {
        "schema_version": 1,
        "nodes": [
            {"node_id": "synthetic-node-1", "artifact_version_id": "synthetic-art-v1"},
            {"node_id": "synthetic-node-2"},
        ],
        "edges": [
            {
                "edge_id": "synthetic-edge-n1",
                "source_node_id": "synthetic-node-1",
                "target_node_id": "synthetic-node-2",
            }
        ],
    }
    body = canonical_document(edited)
    digest = document_hash(edited)
    draft = _draft(ready)
    with write_uow(ready) as session:
        update_draft(
            session,
            page_id=PAGE_A,
            project_id=PROJECT_A,
            base_version_id=draft.base_version_id,
            expected_revision=draft.content_revision,
            content_json=body,
            content_hash=digest,
            now=NOW + 1,
        )
    higher = _confirm(
        ready,
        head=draft.base_version_id,
        revision=draft.content_revision + 1,
        content_hash=digest,
        now=NOW + 2,
    )
    source_page = tuple(_page(ready, PAGE_A))
    source_versions = _versions(ready, PAGE_A)
    tests = _all(
        ready,
        """
        SELECT id, artifact_version_id, observation, result, project_revision
        FROM test_records ORDER BY id
        """,
    )
    opinions = _all(
        ready,
        "SELECT id, page_version_id, node_id, body FROM review_opinions ORDER BY id",
    )
    decisions = _all(ready, "SELECT id, page_version_id, body FROM decisions ORDER BY id")
    artifacts = _scalar(ready, "SELECT count(*) FROM artifact_versions")
    layout = _scalar(ready, "SELECT layout_revision FROM view_states WHERE id = 'synthetic-view'")
    with write_uow(ready) as session:
        older = branch_from_version(
            session,
            project_id=PROJECT_A,
            source_version_id="synthetic-pv-a2",
            actor_id=ACCOUNT_A,
            title="older-route",
            now=NOW + 3,
        )
    assert tuple(_page(ready, PAGE_A)) == source_page
    assert _versions(ready, PAGE_A) == source_versions
    assert _page(ready, PAGE_A).head_version_id == higher.page_version_id
    assert older.page_id not in {PAGE_A, PAGE_B}
    assert older.source_page_id == PAGE_A
    older_version = _one(
        ready,
        """
        SELECT page_id, version_no, parent_version_id, content_json
        FROM page_versions WHERE id = :id
        """,
        {"id": older.page_version_id},
    )
    assert older_version.page_id == older.page_id
    assert older_version.version_no == 1
    assert older_version.parent_version_id is None
    assert _page(ready, older.page_id).head_version_id == older.page_version_id
    older_nodes = json.loads(older_version.content_json)["nodes"]
    assert {node["node_id"] for node in older_nodes} != {"synthetic-node-1"}
    assert older_nodes[0]["artifact_version_id"] == "synthetic-art-v1"
    edge = _one(
        ready,
        """
        SELECT source_version_id, target_version_id, kind, reason
        FROM page_edges WHERE id = :id
        """,
        {"id": older.edge_id},
    )
    assert edge.source_version_id == "synthetic-pv-a2"
    assert edge.target_version_id == older.page_version_id
    assert edge.kind == "branches_from"
    older_reason = json.loads(edge.reason)
    assert older_reason["nodes"][0]["origin_node_id"] == "synthetic-node-1"
    assert older_reason["nodes"][0]["node_id"] == older_nodes[0]["node_id"]
    assert _project(ready).revision == higher.project_revision + 1
    assert _project(ready).access_epoch == 1
    assert _draft(ready).content_revision == higher.content_revision
    with write_uow(ready) as session:
        branched = branch_from_version(
            session,
            project_id=PROJECT_A,
            source_version_id=higher.page_version_id,
            actor_id=ACCOUNT_A,
            title="edge-route",
            now=NOW + 4,
        )
    assert tuple(_page(ready, PAGE_A)) == source_page
    assert _versions(ready, PAGE_A) == source_versions
    cloned = json.loads(
        _scalar(
            ready,
            "SELECT content_json FROM page_versions WHERE id = :id",
            {"id": branched.page_version_id},
        )
    )
    source_document = json.loads(source_versions[2][3])
    assert {node["node_id"] for node in cloned["nodes"]}.isdisjoint(
        {node["node_id"] for node in source_document["nodes"]}
    )
    assert {edge["edge_id"] for edge in cloned["edges"]}.isdisjoint(
        {edge["edge_id"] for edge in source_document["edges"]}
    )
    assert [node.get("artifact_version_id") for node in cloned["nodes"]] == [
        node.get("artifact_version_id") for node in source_document["nodes"]
    ]
    assert cloned["edges"][0]["source_node_id"] == cloned["nodes"][0]["node_id"]
    assert cloned["edges"][0]["target_node_id"] == cloned["nodes"][1]["node_id"]
    parent = _scalar(
        ready,
        "SELECT parent_version_id FROM page_versions WHERE id = :id",
        {"id": branched.page_version_id},
    )
    assert parent is None
    assert _scalar(
        ready,
        "SELECT version_no FROM page_versions WHERE id = :id",
        {"id": branched.page_version_id},
    ) == 1
    mapping = json.loads(
        _scalar(ready, "SELECT reason FROM page_edges WHERE id = :id", {"id": branched.edge_id})
    )
    assert {item["origin_node_id"] for item in mapping["nodes"]} == {
        node["node_id"] for node in source_document["nodes"]
    }
    assert {item["node_id"] for item in mapping["nodes"]} == {
        node["node_id"] for node in cloned["nodes"]
    }
    assert {item["origin_edge_id"] for item in mapping["edges"]} == {"synthetic-edge-n1"}
    assert _scalar(
        ready,
        "SELECT kind FROM page_edges WHERE id = :id",
        {"id": branched.edge_id},
    ) == "branches_from"
    assert _all(
        ready,
        """
        SELECT id, artifact_version_id, observation, result, project_revision
        FROM test_records ORDER BY id
        """,
    ) == tests
    assert _all(
        ready,
        "SELECT id, page_version_id, node_id, body FROM review_opinions ORDER BY id",
    ) == opinions
    assert _all(ready, "SELECT id, page_version_id, body FROM decisions ORDER BY id") == decisions
    assert _scalar(ready, "SELECT count(*) FROM artifact_versions") == artifacts
    assert _scalar(
        ready, "SELECT layout_revision FROM view_states WHERE id = 'synthetic-view'"
    ) == layout
    later = _confirm(
        ready,
        head=higher.page_version_id,
        revision=higher.content_revision,
        content_hash=digest,
        now=NOW + 5,
    )
    survived = _versions(ready, PAGE_A)
    assert survived[:3] == source_versions
    assert survived[3][0] == later.page_version_id
    assert survived[3][1] == 4
    assert _page(ready, PAGE_A).head_version_id == later.page_version_id
    assert _page(ready, older.page_id).head_version_id == older.page_version_id
    assert _page(ready, branched.page_id).head_version_id == branched.page_version_id
    assert _all(
        ready,
        """
        SELECT id, artifact_version_id, observation, result, project_revision
        FROM test_records ORDER BY id
        """,
    ) == tests
    assert _project(ready).access_epoch == 1


@pytest.mark.integration
def test_save_layout_advances_only_layout_revision(ready):
    before_versions = _versions(ready, PAGE_A)
    before_draft = tuple(_draft(ready))
    before_project = tuple(_project(ready))
    before_head = _page(ready, PAGE_A).head_version_id
    with pytest.raises(LayoutConflict):
        with write_uow(ready) as session:
            save_layout(
                session,
                view_state_id="synthetic-view",
                account_id=ACCOUNT_A,
                project_id=PROJECT_A,
                page_id=PAGE_A,
                expected_layout_revision=6,
                layout={"zoom": 1},
                now=NOW + 1,
            )
    assert _scalar(
        ready, "SELECT layout_revision FROM view_states WHERE id = 'synthetic-view'"
    ) == 7
    for zoom, expected in ((2, 7), (3, 8)):
        layout = {"zoom": zoom}
        with write_uow(ready) as session:
            revised = save_layout(
                session,
                view_state_id="synthetic-view",
                account_id=ACCOUNT_A,
                project_id=PROJECT_A,
                page_id=PAGE_A,
                expected_layout_revision=expected,
                layout=layout,
                now=NOW + zoom,
            )
        assert revised == expected + 1
        stored = _one(
            ready,
            """
            SELECT layout_revision, layout_json, layout_hash
            FROM view_states WHERE id = 'synthetic-view'
            """,
        )
        encoded = canonical_json(layout)
        assert stored.layout_revision == expected + 1
        assert stored.layout_json == encoded
        assert stored.layout_hash == hashlib.sha256(encoded.encode()).hexdigest()
    with pytest.raises(LayoutConflict):
        with write_uow(ready) as session:
            save_layout(
                session,
                view_state_id="synthetic-view",
                account_id=ACCOUNT_A,
                project_id=PROJECT_A,
                page_id=PAGE_A,
                expected_layout_revision=8,
                layout={"zoom": 4},
                now=NOW + 4,
            )
    assert _scalar(
        ready, "SELECT layout_revision FROM view_states WHERE id = 'synthetic-view'"
    ) == 9
    assert _versions(ready, PAGE_A) == before_versions
    assert tuple(_draft(ready)) == before_draft
    assert tuple(_project(ready)) == before_project
    assert _page(ready, PAGE_A).head_version_id == before_head
    assert _scalar(
        ready, "SELECT max(version_no) FROM page_versions WHERE page_id = :page", {"page": PAGE_A}
    ) == 2
