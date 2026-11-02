"""F03 page-node kinds on the draft document."""

import json

import pytest
from sqlalchemy import text

from sinuo.application.versions.drafts import save_draft, save_typed_draft
from sinuo.domain.page_nodes import (
    DUTY_OF_KIND,
    HUMAN_CONFIRMATION,
    NODE_KINDS,
    NODE_LABELS,
    PageNodeRejected,
    canonical_typed_document,
    content_digest,
    read_page_nodes,
)
from sinuo.domain.schema_types import RUN_ROLES, canonical_document, document_hash
from sinuo.infrastructure.database import write_uow
from tests.acceptance.test_domain_schema import ACCOUNT_A, NOW, PAGE_A, PROJECT_A, seed

_BASE = "synthetic-pv-a2"
_REVISION = 5


@pytest.fixture
def ready(engine):
    from tests.acceptance.test_domain_schema import require_domain_head

    require_domain_head(engine)
    with engine.begin() as connection:
        seed(connection)
    return engine


def _page(nodes: list[dict], edges: list[dict] | None = None) -> dict:
    return {"schema_version": 1, "nodes": nodes, "edges": [] if edges is None else edges}


def _kind_node(kind: str, **extra: str) -> dict:
    return {"node_id": f"kind-{kind}", "kind": kind, **extra}


def _revision(engine) -> int:
    with engine.connect() as connection:
        value = connection.execute(
            text("SELECT content_revision FROM page_drafts WHERE page_id = :page"),
            {"page": PAGE_A},
        ).scalar_one()
    return int(value)


def _stored(engine) -> str:
    with engine.connect() as connection:
        return str(
            connection.execute(
                text("SELECT content_json FROM page_drafts WHERE page_id = :page"),
                {"page": PAGE_A},
            ).scalar_one()
        )


def _save(session, document: dict, writer: str, *, revision: int = _REVISION):
    return save_typed_draft(
        session,
        page_id=PAGE_A,
        project_id=PROJECT_A,
        base_version_id=_BASE,
        expected_revision=revision,
        document=document,
        writer=writer,
        actor_id=ACCOUNT_A,
        now=NOW + 9,
    )


def test_human_can_write_all_seven_kinds(ready):
    document = _page(
        [_kind_node(kind) for kind in NODE_KINDS],
        edges=[
            {
                "edge_id": "link-problem-evidence",
                "source_node_id": "kind-problem",
                "target_node_id": "kind-evidence",
            }
        ],
    )
    with write_uow(ready) as session:
        revision, _audit = _save(session, document, "human")
    assert revision == 6
    stored = _stored(ready)
    nodes = read_page_nodes(stored)
    assert [node.kind for node in nodes] == list(NODE_KINDS)
    assert [node.duty for node in nodes] == [DUTY_OF_KIND[kind] for kind in NODE_KINDS]
    assert [node.record for node in nodes] == ["formal"] * len(NODE_KINDS)
    assert all(node.legacy is False for node in nodes)
    body = json.loads(stored)
    assert body["edges"][0]["edge_id"] == "link-problem-evidence"
    assert stored == canonical_typed_document(document, writer="human")
    with ready.connect() as connection:
        digest = connection.execute(
            text("SELECT content_hash FROM page_drafts WHERE page_id = :page"),
            {"page": PAGE_A},
        ).scalar_one()
    assert digest == content_digest(stored) 


def test_illegal_kind_is_rejected(ready):
    assert _revision(ready) == _REVISION
    for kind in ("concept", "material", "method", "问题", ""):
        with pytest.raises(PageNodeRejected) as caught:
            canonical_typed_document(_page([_kind_node(kind)]), writer="human")
        assert caught.value.code == "kind"
    with pytest.raises(PageNodeRejected) as caught:
        with write_uow(ready) as session:
            _save(session, _page([_kind_node("concept")]), "human")
    assert caught.value.code == "kind"
    assert _revision(ready) == _REVISION


def test_duties_follow_the_contract():
    assert DUTY_OF_KIND == {
        "problem": "clarification",
        "evidence": "evidence",
        "insight": "evidence",
        "requirement": "critic",
        "artifact": HUMAN_CONFIRMATION,
        "test": HUMAN_CONFIRMATION,
        "decision": HUMAN_CONFIRMATION,
    }
    assert [NODE_LABELS[kind] for kind in NODE_KINDS] == [
        "问题",
        "证据",
        "洞察",
        "要求",
        "作品",
        "测试",
        "决定",
    ]
    assert set(RUN_ROLES) == {"clarification", "evidence", "method", "critic"}
    assert "method" not in set(DUTY_OF_KIND.values())
    proposed = _page(
        [
            {"node_id": "problem-1", "kind": "problem"},
            {"node_id": "evidence-1", "kind": "evidence"},
            {"node_id": "insight-1", "kind": "insight"},
            {"node_id": "requirement-1", "kind": "requirement"},
        ]
    )
    nodes = read_page_nodes(canonical_typed_document(proposed, writer="model"))
    assert [(node.kind, node.duty, node.record) for node in nodes] == [
        ("problem", "clarification", "proposal"),
        ("evidence", "evidence", "proposal"),
        ("insight", "evidence", "proposal"),
        ("requirement", "critic", "proposal"),
    ]
    for kind in NODE_KINDS:
        with pytest.raises(PageNodeRejected) as caught:
            canonical_typed_document(
                _page([_kind_node(kind, duty="method")]),
                writer="human",
            )
        assert caught.value.code == "duty"


def test_legacy_node_id_draft_stays_readable(ready):
    seeded = read_page_nodes(_stored(ready))
    assert len(seeded) == 1
    assert seeded[0].node_id == "synthetic-node-1"
    assert seeded[0].kind is None
    assert seeded[0].duty is None
    assert seeded[0].legacy is True
    assert seeded[0].artifact_version_id == "synthetic-art-v1"

    legacy = {"schema_version": 1, "nodes": [{"node_id": "only-id"}], "edges": []}
    body = canonical_document(legacy)
    with write_uow(ready) as session:
        save_draft(
            session,
            page_id=PAGE_A,
            project_id=PROJECT_A,
            base_version_id=_BASE,
            expected_revision=_REVISION,
            content_json=body,
            content_hash=document_hash(legacy),
            actor_id=ACCOUNT_A,
            now=NOW + 4,
        )
    assert _stored(ready) == body
    found = read_page_nodes(_stored(ready))
    assert len(found) == 1
    assert found[0].node_id == "only-id"
    assert found[0].kind is None
    assert found[0].duty is None
    assert found[0].record is None
    assert found[0].artifact_version_id is None
    bare = '{"nodes":[{"node_id":"only-id"}]}'
    assert read_page_nodes(bare)[0].node_id == "only-id"
    assert read_page_nodes(bare)[0].kind is None


def test_model_cannot_write_formal_artifact_test_or_decision(ready):
    assert _revision(ready) == _REVISION
    for kind in ("artifact", "test", "decision"):
        with pytest.raises(PageNodeRejected) as caught:
            with write_uow(ready) as session:
                _save(session, _page([_kind_node(kind)]), "model")
        assert caught.value.code == "formal"
        assert _revision(ready) == _REVISION
    forged = json.dumps(
        {
            "schema_version": 1,
            "nodes": [
                {
                    "node_id": "forged-decision",
                    "kind": "decision",
                    "duty": "human",
                    "record": "formal",
                }
            ],
            "edges": [],
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    with pytest.raises(PageNodeRejected) as caught:
        with write_uow(ready) as session:
            save_draft(
                session,
                page_id=PAGE_A,
                project_id=PROJECT_A,
                base_version_id=_BASE,
                expected_revision=_REVISION,
                content_json=forged,
                content_hash=content_digest(forged),
                actor_id=ACCOUNT_A,
                now=NOW + 8,
            )
    assert caught.value.code == "formal"
    assert _revision(ready) == _REVISION
