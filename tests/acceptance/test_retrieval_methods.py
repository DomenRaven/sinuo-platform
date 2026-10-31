"""Authorized excerpt search. Synthetic Chinese text only. No sleep."""

import hashlib
import json

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from sinuo.application.auth.authorizer import SessionAuthorizer
from sinuo.application.retrieval.graph import get_graph_neighborhood
from sinuo.application.retrieval.search import search_evidence
from sinuo.application.retrieval.snapshot import get_page_snapshot
from sinuo.domain.auth.errors import AuthRequired
from sinuo.domain.auth.principal import Principal
from sinuo.domain.schema_types import LINEAGE_KINDS
from sinuo.infrastructure.database import write_uow
from sinuo.infrastructure.domain_models import (
    Account,
    Artifact,
    ArtifactVersion,
    EvidenceAnchor,
    Membership,
    ObjectGrant,
    Page,
    PageEdge,
    PageVersion,
    Project,
    Stage,
)

NOW = 1_761_800_000.0
PASSWORD = "$argon2id$synthetic-search"
QUERY = "合成竹材弯曲"
HIDDEN_EXCERPT = "合成竹材弯曲-无权"
FOREIGN_EXCERPT = "合成竹材弯曲-他项"
DECOY_LABEL = "合成竹材弯曲-非摘录"
MISS_EXCERPT = "麻绳绑扎"
PERCENT_EXCERPT = "比例100%"
UNDERSCORE_EXCERPT = "榫卯_节点"
READER_ID = "11111111-1111-4111-8111-111111111111"
OTHER_ID = "44444444-4444-4444-8444-444444444444"
PROJECT_A = "synthetic-search-alpha"
PROJECT_B = "synthetic-search-beta"
HIT_VERSION = "synthetic-search-hit-v1"
NEWER_VERSION = "synthetic-search-hit-v2"
MISS_VERSION = "synthetic-search-miss-v1"
EMPTY_VERSION = "synthetic-search-empty-v1"
HIDDEN_VERSION = "synthetic-search-hidden-v1"
PERCENT_VERSION = "synthetic-search-percent-v1"
UNDERSCORE_VERSION = "synthetic-search-score-v1"
FOREIGN_VERSION = "synthetic-search-foreign-v1"
FOREIGN_EMPTY_VERSION = "synthetic-search-foreign-empty"
SESSION_ID = "synthetic-search-session"


def test_search_evidence_returns_the_authorized_version_only(engine):
    seed_search_world(engine)
    before = _anchor_count(engine)
    principal = Principal(READER_ID, SESSION_ID, 1)
    with write_uow(engine) as session:
        authorizer = SessionAuthorizer(session)

        def run():
            return search_evidence(
                session, authorizer, principal, PROJECT_A, QUERY, 20
            )

        result = _assert_reads_only(session, run)
    assert _anchor_count(engine) == before
    assert len(result.hits) == 1
    hit = result.hits[0]
    assert hit.artifact_version_id == HIT_VERSION
    assert hit.locator == {"excerpt": QUERY, "page": 3}
    assert hit.artifact_version_id != NEWER_VERSION
    assert result.truncated is False
    assert result.counts == {"hit": 1, "no_hit": 3, "not_indexed": 2}
    statuses = {item.artifact_version_id: item.status for item in result.versions}
    assert statuses[HIT_VERSION] == "hit"
    assert statuses[MISS_VERSION] == "no_hit"
    assert statuses[PERCENT_VERSION] == "no_hit"
    assert statuses[UNDERSCORE_VERSION] == "no_hit"
    assert statuses[EMPTY_VERSION] == "not_indexed"
    assert statuses[NEWER_VERSION] == "not_indexed"
    assert HIDDEN_VERSION not in statuses
    assert FOREIGN_VERSION not in statuses
    assert FOREIGN_EMPTY_VERSION not in statuses
    rendered = _render(result)
    assert rendered.count(QUERY) == 1
    assert HIDDEN_EXCERPT not in rendered
    assert FOREIGN_EXCERPT not in rendered
    assert DECOY_LABEL not in rendered
    assert MISS_EXCERPT not in rendered
    assert HIDDEN_VERSION not in rendered
    assert FOREIGN_VERSION not in rendered

    with write_uow(engine) as session:
        authorizer = SessionAuthorizer(session)
        foreign = search_evidence(
            session, authorizer, principal, PROJECT_B, QUERY, 20
        )
        percent = search_evidence(
            session, authorizer, principal, PROJECT_A, "100%", 1
        )
        wildcard = search_evidence(session, authorizer, principal, PROJECT_A, "%", 20)
        underscore = search_evidence(
            session, authorizer, principal, PROJECT_A, "_", 20
        )
        wide = search_evidence(
            session, authorizer, principal, PROJECT_A, "竹" * 40, 20
        )
        narrow = search_evidence(session, authorizer, principal, PROJECT_A, QUERY, 1)
    assert foreign.hits == ()
    assert foreign.versions == ()
    assert foreign.counts == {"hit": 0, "no_hit": 0, "not_indexed": 0}
    assert FOREIGN_EXCERPT not in _render(foreign)
    assert FOREIGN_EMPTY_VERSION not in _render(foreign)
    assert [item.artifact_version_id for item in percent.hits] == [PERCENT_VERSION]
    assert percent.hits[0].locator["excerpt"] == PERCENT_EXCERPT
    assert QUERY not in _render(percent)
    assert [item.artifact_version_id for item in wildcard.hits] == [PERCENT_VERSION]
    assert HIT_VERSION not in {item.artifact_version_id for item in wildcard.hits}
    assert [item.artifact_version_id for item in underscore.hits] == [UNDERSCORE_VERSION]
    assert wide.hits == ()
    assert wide.counts == {"hit": 0, "no_hit": 4, "not_indexed": 2}
    wide_status = {item.artifact_version_id: item.status for item in wide.versions}
    assert wide_status[HIT_VERSION] == "no_hit"
    assert wide_status[EMPTY_VERSION] == "not_indexed"
    assert QUERY not in _render(wide)
    assert "竹" * 40 not in _render(wide)
    assert HIDDEN_EXCERPT not in _render(wide)
    assert len(narrow.hits) == 1
    assert narrow.hits[0].artifact_version_id == HIT_VERSION
    assert narrow.truncated is False
    assert _anchor_count(engine) == before


def test_search_evidence_rejects_empty_long_query_and_bad_limit(engine):
    principal = Principal(READER_ID, SESSION_ID, 1)
    with write_uow(engine) as session:
        authorizer = SessionAuthorizer(session)
        for query in ("", "   ", "竹" * 41, None):
            with pytest.raises(ValueError, match="query"):
                search_evidence(session, authorizer, principal, PROJECT_A, query, 20)
        for limit in (0, 21, -1, True, None, 1.5):
            with pytest.raises(ValueError, match="limit"):
                search_evidence(
                    session, authorizer, principal, PROJECT_A, QUERY, limit
                )


def seed_search_world(engine) -> None:
    with write_uow(engine) as session:
        session.add_all(
            [
                Project(
                    id=PROJECT_A,
                    title="synthetic-search-alpha",
                    brief="synthetic",
                    template_key=None,
                    revision=1,
                    access_epoch=1,
                    created_at=NOW,
                ),
                Project(
                    id=PROJECT_B,
                    title="synthetic-search-beta",
                    brief="synthetic",
                    template_key=None,
                    revision=1,
                    access_epoch=1,
                    created_at=NOW,
                ),
            ]
        )
        _account(session, READER_ID, "synthetic-search-reader")
        _account(session, OTHER_ID, "synthetic-search-other")
        session.flush()
        _member(session, "member-search-reader", PROJECT_A, READER_ID)
        _member(session, "member-search-other", PROJECT_B, OTHER_ID)
        _artifact(session, "synthetic-search-hit-art", PROJECT_A)
        _artifact(session, "synthetic-search-miss-art", PROJECT_A)
        _artifact(session, "synthetic-search-empty-art", PROJECT_A)
        _artifact(session, "synthetic-search-hidden-art", PROJECT_A)
        _artifact(session, "synthetic-search-percent-art", PROJECT_A)
        _artifact(session, "synthetic-search-score-art", PROJECT_A)
        _artifact(session, "synthetic-search-foreign-art", PROJECT_B)
        _artifact(session, "synthetic-search-foreign-empty-art", PROJECT_B)
        session.flush()
        _version(session, HIT_VERSION, PROJECT_A, "synthetic-search-hit-art", READER_ID, 1)
        _version(session, NEWER_VERSION, PROJECT_A, "synthetic-search-hit-art", READER_ID, 2)
        _version(session, MISS_VERSION, PROJECT_A, "synthetic-search-miss-art", READER_ID, 1)
        _version(session, EMPTY_VERSION, PROJECT_A, "synthetic-search-empty-art", READER_ID, 1)
        _version(session, HIDDEN_VERSION, PROJECT_A, "synthetic-search-hidden-art", READER_ID, 1)
        _version(
            session, PERCENT_VERSION, PROJECT_A, "synthetic-search-percent-art", READER_ID, 1
        )
        _version(
            session,
            UNDERSCORE_VERSION,
            PROJECT_A,
            "synthetic-search-score-art",
            READER_ID,
            1,
        )
        _version(session, FOREIGN_VERSION, PROJECT_B, "synthetic-search-foreign-art", OTHER_ID, 1)
        _version(
            session,
            FOREIGN_EMPTY_VERSION,
            PROJECT_B,
            "synthetic-search-foreign-empty-art",
            OTHER_ID,
            1,
        )
        session.flush()
        _anchor(
            session,
            "synthetic-search-hit-anchor",
            PROJECT_A,
            HIT_VERSION,
            READER_ID,
            {"excerpt": QUERY, "page": 3},
        )
        _anchor(
            session,
            "synthetic-search-miss-anchor",
            PROJECT_A,
            MISS_VERSION,
            READER_ID,
            {"excerpt": MISS_EXCERPT, "page": 1, "label": DECOY_LABEL},
        )
        _anchor(
            session,
            "synthetic-search-hidden-anchor",
            PROJECT_A,
            HIDDEN_VERSION,
            READER_ID,
            {"excerpt": HIDDEN_EXCERPT, "page": 9},
        )
        _anchor(
            session,
            "synthetic-search-percent-anchor",
            PROJECT_A,
            PERCENT_VERSION,
            READER_ID,
            {"excerpt": PERCENT_EXCERPT, "page": 2},
        )
        _anchor(
            session,
            "synthetic-search-score-anchor",
            PROJECT_A,
            UNDERSCORE_VERSION,
            READER_ID,
            {"excerpt": UNDERSCORE_EXCERPT, "page": 4},
        )
        _anchor(
            session,
            "synthetic-search-foreign-anchor",
            PROJECT_B,
            FOREIGN_VERSION,
            OTHER_ID,
            {"excerpt": FOREIGN_EXCERPT, "page": 8},
        )
        session.flush()
        _grant(session, "grant-search-read", PROJECT_A, READER_ID, "project.read", None)
        _grant(
            session,
            "grant-search-hit",
            PROJECT_A,
            READER_ID,
            "evidence.read",
            "synthetic-search-hit-anchor",
        )
        _grant(
            session,
            "grant-search-miss",
            PROJECT_A,
            READER_ID,
            "evidence.read",
            "synthetic-search-miss-anchor",
        )
        _grant(
            session,
            "grant-search-percent",
            PROJECT_A,
            READER_ID,
            "evidence.read",
            "synthetic-search-percent-anchor",
        )
        _grant(
            session,
            "grant-search-score",
            PROJECT_A,
            READER_ID,
            "evidence.read",
            "synthetic-search-score-anchor",
        )
        _grant(session, "grant-search-other-read", PROJECT_B, OTHER_ID, "project.read", None)
        _grant(
            session,
            "grant-search-foreign",
            PROJECT_B,
            OTHER_ID,
            "evidence.read",
            "synthetic-search-foreign-anchor",
        )


def _render(result) -> str:
    payload = {
        "hits": [
            {"artifact_version_id": hit.artifact_version_id, "locator": hit.locator}
            for hit in result.hits
        ],
        "versions": [
            {"artifact_version_id": item.artifact_version_id, "status": item.status}
            for item in result.versions
        ],
        "counts": result.counts,
        "truncated": result.truncated,
    }
    return json.dumps(payload, ensure_ascii=False, sort_keys=True)


def _anchor_count(engine) -> int:
    with Session(engine) as session:
        return int(session.scalar(select(func.count()).select_from(EvidenceAnchor)) or 0)


def _assert_reads_only(session: Session, callback):
    baseline = session.scalar(text("SELECT total_changes()"))
    seen: list[str] = []
    raw = _raw_sqlite(session)
    raw.set_trace_callback(seen.append)
    try:
        return callback()
    finally:
        raw.set_trace_callback(None)
        after = session.scalar(text("SELECT total_changes()"))
        assert after == baseline
        assert not any(_statement_writes(statement) for statement in seen)


def _statement_writes(statement: str) -> bool:
    stripped = statement.lstrip().lstrip("(")
    if not stripped:
        return False
    head = stripped.split(None, 1)[0].upper()
    return head in {"INSERT", "UPDATE", "DELETE", "REPLACE"}


def _raw_sqlite(session: Session):
    wrapped = session.connection().connection
    candidate = getattr(wrapped, "dbapi_connection", None)
    if candidate is None:
        candidate = wrapped
    if not hasattr(candidate, "set_trace_callback"):
        raise RuntimeError("sqlite connection is unavailable")
    return candidate


def _account(session: Session, account_id: str, login_name: str) -> None:
    session.add(
        Account(
            id=account_id,
            login_name=login_name,
            display_name=login_name,
            password_hash=PASSWORD,
            status="active",
            auth_epoch=1,
            can_create_project=0,
            created_at=NOW,
            disabled_at=None,
        )
    )


def _member(session: Session, membership_id: str, project_id: str, account_id: str) -> None:
    session.add(
        Membership(
            id=membership_id,
            project_id=project_id,
            account_id=account_id,
            status="active",
            role_label=None,
            created_at=NOW,
            revoked_at=None,
        )
    )


def _artifact(session: Session, artifact_id: str, project_id: str) -> None:
    session.add(
        Artifact(
            id=artifact_id,
            project_id=project_id,
            title=artifact_id,
            created_at=NOW,
        )
    )


def _version(
    session: Session,
    version_id: str,
    project_id: str,
    artifact_id: str,
    author_id: str,
    version_no: int,
) -> None:
    session.add(
        ArtifactVersion(
            id=version_id,
            project_id=project_id,
            artifact_id=artifact_id,
            version_no=version_no,
            content_hash=hashlib.sha256(version_id.encode("utf-8")).hexdigest(),
            size_bytes=8,
            media_type="application/octet-stream",
            object_key=f"synthetic/{version_id}",
            license="synthetic",
            sensitivity="project",
            ai_processing_allowed=0,
            project_revision=1,
            created_by_account_id=author_id,
            created_at=NOW,
        )
    )


def _anchor(
    session: Session,
    anchor_id: str,
    project_id: str,
    version_id: str,
    author_id: str,
    locator: dict,
) -> None:
    excerpt = locator["excerpt"]
    session.add(
        EvidenceAnchor(
            id=anchor_id,
            project_id=project_id,
            artifact_version_id=version_id,
            locator_type="text_range",
            locator_json=json.dumps(locator, ensure_ascii=False, separators=(",", ":")),
            excerpt_hash=hashlib.sha256(excerpt.encode("utf-8")).hexdigest(),
            extractor_version="synthetic-1",
            visibility="project",
            sensitivity="project",
            supersedes_id=None,
            schema_version=1,
            created_by_account_id=author_id,
            created_at=NOW,
        )
    )


def _grant(
    session: Session,
    grant_id: str,
    project_id: str,
    account_id: str,
    action: str,
    evidence_anchor_id: str | None,
) -> None:
    session.add(
        ObjectGrant(
            id=grant_id,
            project_id=project_id,
            account_id=account_id,
            action=action,
            scope_type="project" if evidence_anchor_id is None else "evidence_anchor",
            status="active",
            created_at=NOW,
            revoked_at=None,
            created_by_account_id=account_id,
            stage_id=None,
            page_id=None,
            page_version_id=None,
            artifact_id=None,
            artifact_version_id=None,
            evidence_anchor_id=evidence_anchor_id,
        )
    )


GRAPH_READER = "22222222-2222-4222-8222-222222222222"
GRAPH_OTHER = "33333333-3333-4333-8333-333333333333"
GRAPH_PROJECT = "synthetic-graph-alpha"
GRAPH_FOREIGN = "synthetic-graph-beta"
HIDDEN_TITLE = "合成隐藏页标题"
FOREIGN_TITLE = "合成他项页标题"
BEYOND_TITLE = "合成隔断页标题"
GRAPH_START = "synthetic-graph-v0"
GRAPH_HOP1 = "synthetic-graph-v1"
GRAPH_HOP2 = "synthetic-graph-v2"
GRAPH_HOP3 = "synthetic-graph-v3"
GRAPH_HIDDEN = "synthetic-graph-vh"
GRAPH_BEYOND = "synthetic-graph-vb"
GRAPH_FOREIGN_A = "synthetic-graph-vf1"
GRAPH_FOREIGN_B = "synthetic-graph-vf2"
MISSING_ACCOUNT = "99999999-9999-4999-8999-999999999999"


def test_graph_neighborhood_keeps_authorized_lineage_and_stops(engine):
    seed_graph_world(engine)
    before = _edge_count(engine)
    principal = Principal(GRAPH_READER, "synthetic-graph-session", 1)
    with write_uow(engine) as session:
        authorizer = SessionAuthorizer(session)

        def within_depth():
            return get_graph_neighborhood(
                session,
                authorizer,
                principal,
                GRAPH_PROJECT,
                GRAPH_START,
                ("branches_from",),
                2,
                30,
            )

        depth_result = _assert_reads_only(session, within_depth)

        def within_nodes():
            return get_graph_neighborhood(
                session,
                authorizer,
                principal,
                GRAPH_PROJECT,
                GRAPH_START,
                ("branches_from",),
                2,
                2,
            )

        node_result = _assert_reads_only(session, within_nodes)

        def start_only():
            return get_graph_neighborhood(
                session,
                authorizer,
                principal,
                GRAPH_PROJECT,
                GRAPH_START,
                ("branches_from", "stage_source", "merge_source"),
                0,
                30,
            )

        start_result = _assert_reads_only(session, start_only)
        outsider = Principal(GRAPH_OTHER, "synthetic-graph-other-session", 1)
        foreign = _assert_reads_only(
            session,
            lambda: get_graph_neighborhood(
                session,
                authorizer,
                outsider,
                GRAPH_PROJECT,
                GRAPH_START,
                ("branches_from",),
                2,
                30,
            ),
        )
        other_project = _assert_reads_only(
            session,
            lambda: get_graph_neighborhood(
                session,
                authorizer,
                principal,
                GRAPH_FOREIGN,
                GRAPH_FOREIGN_A,
                ("branches_from",),
                2,
                30,
            ),
        )
    assert _edge_count(engine) == before
    assert [node.page_version_id for node in depth_result.nodes] == [
        GRAPH_START,
        GRAPH_HOP1,
        GRAPH_HOP2,
    ]
    assert [(edge.edge_id, edge.kind) for edge in depth_result.edges] == [
        ("synthetic-graph-e01", "branches_from"),
        ("synthetic-graph-e12", "branches_from"),
    ]
    assert depth_result.truncated is True
    assert [node.page_version_id for node in node_result.nodes] == [GRAPH_START, GRAPH_HOP1]
    assert [edge.edge_id for edge in node_result.edges] == ["synthetic-graph-e01"]
    assert node_result.truncated is True
    assert [node.page_version_id for node in start_result.nodes] == [GRAPH_START]
    assert start_result.edges == ()
    assert start_result.truncated is True
    assert foreign.nodes == () and foreign.edges == () and foreign.truncated is False
    assert other_project.nodes == () and other_project.edges == () and other_project.truncated is False
    rendered = json.dumps(
        {
            "depth": [
                node.page_version_id for node in depth_result.nodes
            ]
            + [edge.edge_id for edge in depth_result.edges],
            "foreign": [node.page_version_id for node in other_project.nodes],
        },
        ensure_ascii=False,
    )
    assert HIDDEN_TITLE not in rendered
    assert FOREIGN_TITLE not in rendered
    assert BEYOND_TITLE not in rendered
    assert GRAPH_HIDDEN not in rendered
    assert GRAPH_BEYOND not in rendered
    assert GRAPH_HOP3 not in rendered
    assert GRAPH_FOREIGN_A not in rendered


def test_graph_neighborhood_rejects_over_limit_and_unknown_account(engine):
    seed_graph_world(engine)
    before = _edge_count(engine)
    principal = Principal(GRAPH_READER, "synthetic-graph-session", 1)
    missing = Principal(MISSING_ACCOUNT, "synthetic-graph-missing", 1)
    with write_uow(engine) as session:
        authorizer = SessionAuthorizer(session)
        for depth in (3, -1, True, None):
            with pytest.raises(ValueError, match="depth"):
                get_graph_neighborhood(
                    session,
                    authorizer,
                    principal,
                    GRAPH_PROJECT,
                    GRAPH_START,
                    ("branches_from",),
                    depth,
                    30,
                )
        for max_nodes in (0, 31, True, None):
            with pytest.raises(ValueError, match="max_nodes"):
                get_graph_neighborhood(
                    session,
                    authorizer,
                    principal,
                    GRAPH_PROJECT,
                    GRAPH_START,
                    ("branches_from",),
                    2,
                    max_nodes,
                )
        for kinds in ((), ("branches_from", "branches_from"), ("supports",), ["branches_from"]):
            with pytest.raises(ValueError, match="kinds"):
                get_graph_neighborhood(
                    session,
                    authorizer,
                    principal,
                    GRAPH_PROJECT,
                    GRAPH_START,
                    kinds,
                    2,
                    30,
                )
        with pytest.raises(AuthRequired):
            get_graph_neighborhood(
                session,
                authorizer,
                missing,
                GRAPH_PROJECT,
                GRAPH_START,
                ("branches_from",),
                1,
                30,
            )
    assert _edge_count(engine) == before
    assert set(LINEAGE_KINDS) == {"branches_from", "stage_source", "merge_source"}


def seed_graph_world(engine) -> None:
    with write_uow(engine) as session:
        session.add_all(
            [
                Project(
                    id=GRAPH_PROJECT,
                    title="synthetic-graph-alpha",
                    brief="synthetic",
                    template_key=None,
                    revision=1,
                    access_epoch=1,
                    created_at=NOW,
                ),
                Project(
                    id=GRAPH_FOREIGN,
                    title="synthetic-graph-beta",
                    brief="synthetic",
                    template_key=None,
                    revision=1,
                    access_epoch=1,
                    created_at=NOW,
                ),
            ]
        )
        _account(session, GRAPH_READER, "synthetic-graph-reader")
        _account(session, GRAPH_OTHER, "synthetic-graph-other")
        session.flush()
        _member(session, "member-graph-reader", GRAPH_PROJECT, GRAPH_READER)
        _member(session, "member-graph-other", GRAPH_FOREIGN, GRAPH_OTHER)
        session.add_all(
            [
                Stage(id="synthetic-graph-stage-a", project_id=GRAPH_PROJECT, head_config_version_id=None, created_at=NOW),
                Stage(id="synthetic-graph-stage-b", project_id=GRAPH_FOREIGN, head_config_version_id=None, created_at=NOW),
            ]
        )
        session.flush()
        pages = (
            ("synthetic-graph-p0", GRAPH_PROJECT, "synthetic-graph-stage-a", "合成起点"),
            ("synthetic-graph-p1", GRAPH_PROJECT, "synthetic-graph-stage-a", "合成一跳"),
            ("synthetic-graph-p2", GRAPH_PROJECT, "synthetic-graph-stage-a", "合成二跳"),
            ("synthetic-graph-p3", GRAPH_PROJECT, "synthetic-graph-stage-a", "合成三跳"),
            ("synthetic-graph-ph", GRAPH_PROJECT, "synthetic-graph-stage-a", HIDDEN_TITLE),
            ("synthetic-graph-pb", GRAPH_PROJECT, "synthetic-graph-stage-a", BEYOND_TITLE),
            ("synthetic-graph-pf1", GRAPH_FOREIGN, "synthetic-graph-stage-b", FOREIGN_TITLE),
            ("synthetic-graph-pf2", GRAPH_FOREIGN, "synthetic-graph-stage-b", "合成他项邻页"),
        )
        for page_id, project_id, stage_id, title in pages:
            session.add(
                Page(
                    id=page_id,
                    project_id=project_id,
                    stage_id=stage_id,
                    head_version_id=None,
                    status="active",
                    title=title,
                    created_at=NOW,
                )
            )
        session.flush()
        versions = (
            (GRAPH_START, "synthetic-graph-p0", GRAPH_PROJECT, GRAPH_READER, "合成起点正文"),
            (GRAPH_HOP1, "synthetic-graph-p1", GRAPH_PROJECT, GRAPH_READER, "合成一跳正文"),
            (GRAPH_HOP2, "synthetic-graph-p2", GRAPH_PROJECT, GRAPH_READER, "合成二跳正文"),
            (GRAPH_HOP3, "synthetic-graph-p3", GRAPH_PROJECT, GRAPH_READER, "合成三跳正文"),
            (GRAPH_HIDDEN, "synthetic-graph-ph", GRAPH_PROJECT, GRAPH_READER, HIDDEN_TITLE),
            (GRAPH_BEYOND, "synthetic-graph-pb", GRAPH_PROJECT, GRAPH_READER, BEYOND_TITLE),
            (GRAPH_FOREIGN_A, "synthetic-graph-pf1", GRAPH_FOREIGN, GRAPH_OTHER, FOREIGN_TITLE),
            (GRAPH_FOREIGN_B, "synthetic-graph-pf2", GRAPH_FOREIGN, GRAPH_OTHER, "合成他项邻页正文"),
        )
        for version_id, page_id, project_id, author_id, body in versions:
            session.add(
                PageVersion(
                    id=version_id,
                    project_id=project_id,
                    page_id=page_id,
                    version_no=1,
                    parent_version_id=None,
                    content_json=json.dumps({"body": body}, ensure_ascii=False),
                    content_hash=hashlib.sha256(version_id.encode("utf-8")).hexdigest(),
                    schema_version=1,
                    project_revision=1,
                    created_by_account_id=author_id,
                    created_at=NOW,
                )
            )
        session.flush()
        for page_id, version_id in (
            ("synthetic-graph-p0", GRAPH_START),
            ("synthetic-graph-p1", GRAPH_HOP1),
            ("synthetic-graph-p2", GRAPH_HOP2),
            ("synthetic-graph-p3", GRAPH_HOP3),
            ("synthetic-graph-ph", GRAPH_HIDDEN),
            ("synthetic-graph-pb", GRAPH_BEYOND),
            ("synthetic-graph-pf1", GRAPH_FOREIGN_A),
            ("synthetic-graph-pf2", GRAPH_FOREIGN_B),
        ):
            page = session.get(Page, page_id)
            page.head_version_id = version_id
        session.flush()
        edges = (
            ("synthetic-graph-e01", GRAPH_PROJECT, GRAPH_START, GRAPH_HOP1, "branches_from"),
            ("synthetic-graph-e12", GRAPH_PROJECT, GRAPH_HOP1, GRAPH_HOP2, "branches_from"),
            ("synthetic-graph-e23", GRAPH_PROJECT, GRAPH_HOP2, GRAPH_HOP3, "branches_from"),
            ("synthetic-graph-merge", GRAPH_PROJECT, GRAPH_START, GRAPH_HOP1, "merge_source"),
            ("synthetic-graph-hidden", GRAPH_PROJECT, GRAPH_START, GRAPH_HIDDEN, "stage_source"),
            ("synthetic-graph-beyond", GRAPH_PROJECT, GRAPH_HIDDEN, GRAPH_BEYOND, "branches_from"),
            ("synthetic-graph-foreign", GRAPH_FOREIGN, GRAPH_FOREIGN_A, GRAPH_FOREIGN_B, "branches_from"),
        )
        for edge_id, project_id, source_id, target_id, kind in edges:
            session.add(
                PageEdge(
                    id=edge_id,
                    project_id=project_id,
                    source_version_id=source_id,
                    target_version_id=target_id,
                    kind=kind,
                    project_revision=1,
                    reason=None,
                    created_at=NOW,
                )
            )
        session.flush()
        _page_grant(session, "grant-graph-project", GRAPH_PROJECT, GRAPH_READER, None)
        for grant_id, page_id in (
            ("grant-graph-p0", "synthetic-graph-p0"),
            ("grant-graph-p1", "synthetic-graph-p1"),
            ("grant-graph-p2", "synthetic-graph-p2"),
            ("grant-graph-p3", "synthetic-graph-p3"),
            ("grant-graph-pb", "synthetic-graph-pb"),
        ):
            _page_grant(session, grant_id, GRAPH_PROJECT, GRAPH_READER, page_id)
        _page_grant(session, "grant-graph-foreign-project", GRAPH_FOREIGN, GRAPH_OTHER, None)
        _page_grant(session, "grant-graph-pf1", GRAPH_FOREIGN, GRAPH_OTHER, "synthetic-graph-pf1")
        _page_grant(session, "grant-graph-pf2", GRAPH_FOREIGN, GRAPH_OTHER, "synthetic-graph-pf2")


def _edge_count(engine) -> int:
    with Session(engine) as session:
        return int(session.scalar(select(func.count()).select_from(PageEdge)) or 0)


SNAP_READER = "55555555-5555-4555-8555-555555555555"
SNAP_OTHER = "66666666-6666-4666-8666-666666666666"
SNAP_PROJECT = "synthetic-snapshot-alpha"
SNAP_FOREIGN = "synthetic-snapshot-beta"
SNAP_TITLE = "合成可见页"
SNAP_V1 = "synthetic-snapshot-v1"
SNAP_V2 = "synthetic-snapshot-v2"
SNAP_BODY_V1 = "合成第一版正文"
SNAP_BODY_V2 = "合成第二版正文"
SNAP_NODE_PAGE = "synthetic-snapshot-nodes"
SNAP_NODE_TITLE = "合成节点页"
SNAP_ARTIFACT = "synthetic-snapshot-art-v1"
HIDDEN_PAGE_TITLE = "合成无权标题"
HIDDEN_PAGE_BODY = "合成无权正文"
REVOKED_TITLE = "合成撤权标题"
REVOKED_BODY = "合成撤权正文"
FOREIGN_SNAP_TITLE = "合成他项快照标题"
FOREIGN_SNAP_BODY = "合成他项快照正文"
SNAP_HIDDEN = "synthetic-snapshot-hidden"
SNAP_REVOKED = "synthetic-snapshot-revoked"
SNAP_FOREIGN_VERSION = "synthetic-snapshot-foreign"


def test_page_snapshot_returns_the_authorized_version_only(engine):
    seed_snapshot_world(engine)
    before = _page_version_count(engine)
    principal = Principal(SNAP_READER, "synthetic-snapshot-session", 1)
    with write_uow(engine) as session:
        authorizer = SessionAuthorizer(session)

        def read_v1():
            return get_page_snapshot(
                session, authorizer, principal, SNAP_PROJECT, SNAP_V1
            )

        older = _assert_reads_only(session, read_v1)
        newer = _assert_reads_only(
            session,
            lambda: get_page_snapshot(
                session, authorizer, principal, SNAP_PROJECT, SNAP_V2
            ),
        )
        nodes = _assert_reads_only(
            session,
            lambda: get_page_snapshot(
                session, authorizer, principal, SNAP_PROJECT, SNAP_NODE_PAGE
            ),
        )
        hidden_sql: list[str] = []
        hidden_before = session.scalar(text("SELECT total_changes()"))
        hidden = _traced(
            session,
            lambda: get_page_snapshot(
                session, authorizer, principal, SNAP_PROJECT, SNAP_HIDDEN
            ),
            hidden_sql,
        )
        hidden_after = session.scalar(text("SELECT total_changes()"))
        assert hidden_after == hidden_before
        assert not any(_statement_writes(statement) for statement in hidden_sql)
        revoked = _assert_reads_only(
            session,
            lambda: get_page_snapshot(
                session, authorizer, principal, SNAP_PROJECT, SNAP_REVOKED
            ),
        )
        outsider = Principal(SNAP_OTHER, "synthetic-snapshot-other", 1)
        foreign_reader = _assert_reads_only(
            session,
            lambda: get_page_snapshot(
                session, authorizer, outsider, SNAP_PROJECT, SNAP_V1
            ),
        )
        other_project = _assert_reads_only(
            session,
            lambda: get_page_snapshot(
                session,
                authorizer,
                principal,
                SNAP_FOREIGN,
                SNAP_FOREIGN_VERSION,
            ),
        )
    assert _page_version_count(engine) == before
    assert older.page_version_id == SNAP_V1
    assert older.version_no == 1
    assert older.title == SNAP_TITLE
    assert older.body == SNAP_BODY_V1
    assert older.content_hash == _digest(SNAP_BODY_V1)
    assert SNAP_BODY_V2 not in _render_snapshot(older)
    assert newer.page_version_id == SNAP_V2
    assert newer.version_no == 2
    assert newer.body == SNAP_BODY_V2
    assert SNAP_BODY_V1 not in _render_snapshot(newer)
    assert [node.node_id for node in nodes.nodes] == ["n-source", "n-cite"]
    assert nodes.artifact_version_ids == (SNAP_ARTIFACT,)
    assert [(edge.edge_id, edge.source_node_id, edge.target_node_id) for edge in nodes.edges] == [
        ("e-cite", "n-source", "n-cite")
    ]
    assert nodes.title == SNAP_NODE_TITLE
    assert nodes.body is None
    assert hidden.page_version_id is None
    assert hidden.title is None and hidden.body is None
    assert hidden.nodes == () and hidden.edges == ()
    hidden_text = _render_snapshot(hidden)
    assert HIDDEN_PAGE_TITLE not in hidden_text
    assert HIDDEN_PAGE_BODY not in hidden_text
    assert not any("content_json" in statement for statement in hidden_sql)
    assert not any(HIDDEN_PAGE_TITLE in statement for statement in hidden_sql)
    revoked_text = _render_snapshot(revoked)
    assert REVOKED_TITLE not in revoked_text
    assert REVOKED_BODY not in revoked_text
    assert revoked.title is None and revoked.body is None
    foreign_text = _render_snapshot(foreign_reader)
    assert SNAP_TITLE not in foreign_text
    assert SNAP_BODY_V1 not in foreign_text
    assert foreign_reader.title is None
    other_text = _render_snapshot(other_project)
    assert FOREIGN_SNAP_TITLE not in other_text
    assert FOREIGN_SNAP_BODY not in other_text
    assert other_project.title is None and other_project.body is None
    assert _page_version_count(engine) == before


def test_page_snapshot_rejects_bad_ids_and_unknown_account(engine):
    seed_snapshot_world(engine)
    before = _page_version_count(engine)
    principal = Principal(SNAP_READER, "synthetic-snapshot-session", 1)
    missing = Principal(MISSING_ACCOUNT, "synthetic-snapshot-missing", 1)
    with write_uow(engine) as session:
        authorizer = SessionAuthorizer(session)
        for project_id in ("", "   ", "x" * 65, None, 1):
            with pytest.raises(ValueError, match="project_id"):
                get_page_snapshot(session, authorizer, principal, project_id, SNAP_V1)
        for version_id in ("", "   ", "y" * 65, None, 1):
            with pytest.raises(ValueError, match="page_version_id"):
                get_page_snapshot(
                    session, authorizer, principal, SNAP_PROJECT, version_id
                )
        with pytest.raises(AuthRequired):
            get_page_snapshot(
                session, authorizer, missing, SNAP_PROJECT, SNAP_V1
            )
    assert _page_version_count(engine) == before


def seed_snapshot_world(engine) -> None:
    with write_uow(engine) as session:
        session.add_all(
            [
                Project(
                    id=SNAP_PROJECT,
                    title="synthetic-snapshot-alpha",
                    brief="synthetic",
                    template_key=None,
                    revision=1,
                    access_epoch=1,
                    created_at=NOW,
                ),
                Project(
                    id=SNAP_FOREIGN,
                    title="synthetic-snapshot-beta",
                    brief="synthetic",
                    template_key=None,
                    revision=1,
                    access_epoch=1,
                    created_at=NOW,
                ),
            ]
        )
        _account(session, SNAP_READER, "synthetic-snapshot-reader")
        _account(session, SNAP_OTHER, "synthetic-snapshot-other")
        session.flush()
        _member(session, "member-snap-reader", SNAP_PROJECT, SNAP_READER)
        _member(session, "member-snap-other", SNAP_FOREIGN, SNAP_OTHER)
        session.add_all(
            [
                Stage(
                    id="synthetic-snapshot-stage-a",
                    project_id=SNAP_PROJECT,
                    head_config_version_id=None,
                    created_at=NOW,
                ),
                Stage(
                    id="synthetic-snapshot-stage-b",
                    project_id=SNAP_FOREIGN,
                    head_config_version_id=None,
                    created_at=NOW,
                ),
            ]
        )
        session.flush()
        pages = (
            ("synthetic-snapshot-p1", SNAP_PROJECT, "synthetic-snapshot-stage-a", SNAP_TITLE),
            ("synthetic-snapshot-pn", SNAP_PROJECT, "synthetic-snapshot-stage-a", SNAP_NODE_TITLE),
            (
                "synthetic-snapshot-ph",
                SNAP_PROJECT,
                "synthetic-snapshot-stage-a",
                HIDDEN_PAGE_TITLE,
            ),
            (
                "synthetic-snapshot-pr",
                SNAP_PROJECT,
                "synthetic-snapshot-stage-a",
                REVOKED_TITLE,
            ),
            (
                "synthetic-snapshot-pf",
                SNAP_FOREIGN,
                "synthetic-snapshot-stage-b",
                FOREIGN_SNAP_TITLE,
            ),
        )
        for page_id, project_id, stage_id, title in pages:
            session.add(
                Page(
                    id=page_id,
                    project_id=project_id,
                    stage_id=stage_id,
                    head_version_id=None,
                    status="active",
                    title=title,
                    created_at=NOW,
                )
            )
        session.flush()
        node_content = json.dumps(
            {
                "schema_version": 1,
                "nodes": [
                    {"node_id": "n-source"},
                    {"node_id": "n-cite", "artifact_version_id": SNAP_ARTIFACT},
                ],
                "edges": [
                    {
                        "edge_id": "e-cite",
                        "source_node_id": "n-source",
                        "target_node_id": "n-cite",
                    }
                ],
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )
        versions = (
            (SNAP_V1, "synthetic-snapshot-p1", SNAP_PROJECT, SNAP_READER, 1, None, SNAP_BODY_V1),
            (SNAP_V2, "synthetic-snapshot-p1", SNAP_PROJECT, SNAP_READER, 2, SNAP_V1, SNAP_BODY_V2),
            (
                SNAP_HIDDEN,
                "synthetic-snapshot-ph",
                SNAP_PROJECT,
                SNAP_READER,
                1,
                None,
                HIDDEN_PAGE_BODY,
            ),
            (
                SNAP_REVOKED,
                "synthetic-snapshot-pr",
                SNAP_PROJECT,
                SNAP_READER,
                1,
                None,
                REVOKED_BODY,
            ),
            (
                SNAP_FOREIGN_VERSION,
                "synthetic-snapshot-pf",
                SNAP_FOREIGN,
                SNAP_OTHER,
                1,
                None,
                FOREIGN_SNAP_BODY,
            ),
        )
        for version_id, page_id, project_id, author_id, version_no, parent_id, body in versions:
            session.add(
                PageVersion(
                    id=version_id,
                    project_id=project_id,
                    page_id=page_id,
                    version_no=version_no,
                    parent_version_id=parent_id,
                    content_json=json.dumps({"body": body}, ensure_ascii=False),
                    content_hash=_digest(body),
                    schema_version=1,
                    project_revision=1,
                    created_by_account_id=author_id,
                    created_at=NOW,
                )
            )
            session.flush()
        session.add(
            PageVersion(
                id=SNAP_NODE_PAGE,
                project_id=SNAP_PROJECT,
                page_id="synthetic-snapshot-pn",
                version_no=1,
                parent_version_id=None,
                content_json=node_content,
                content_hash=_digest(node_content),
                schema_version=1,
                project_revision=1,
                created_by_account_id=SNAP_READER,
                created_at=NOW,
            )
        )
        session.flush()
        for page_id, version_id in (
            ("synthetic-snapshot-p1", SNAP_V2),
            ("synthetic-snapshot-pn", SNAP_NODE_PAGE),
            ("synthetic-snapshot-ph", SNAP_HIDDEN),
            ("synthetic-snapshot-pr", SNAP_REVOKED),
            ("synthetic-snapshot-pf", SNAP_FOREIGN_VERSION),
        ):
            page = session.get(Page, page_id)
            page.head_version_id = version_id
        session.flush()
        _page_grant(session, "grant-snap-project", SNAP_PROJECT, SNAP_READER, None)
        _page_grant(session, "grant-snap-p1", SNAP_PROJECT, SNAP_READER, "synthetic-snapshot-p1")
        _page_grant(session, "grant-snap-pn", SNAP_PROJECT, SNAP_READER, "synthetic-snapshot-pn")
        session.add(
            ObjectGrant(
                id="grant-snap-revoked",
                project_id=SNAP_PROJECT,
                account_id=SNAP_READER,
                action="page.read",
                scope_type="page",
                status="revoked",
                created_at=NOW,
                revoked_at=NOW,
                created_by_account_id=SNAP_READER,
                stage_id=None,
                page_id="synthetic-snapshot-pr",
                page_version_id=None,
                artifact_id=None,
                artifact_version_id=None,
                evidence_anchor_id=None,
            )
        )
        _page_grant(session, "grant-snap-foreign-project", SNAP_FOREIGN, SNAP_OTHER, None)
        _page_grant(session, "grant-snap-pf", SNAP_FOREIGN, SNAP_OTHER, "synthetic-snapshot-pf")


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _page_version_count(engine) -> int:
    with Session(engine) as session:
        return int(session.scalar(select(func.count()).select_from(PageVersion)) or 0)


def _render_snapshot(result) -> str:
    payload = {
        "page_version_id": result.page_version_id,
        "page_id": result.page_id,
        "title": result.title,
        "version_no": result.version_no,
        "content_hash": result.content_hash,
        "body": result.body,
        "nodes": [
            {"node_id": node.node_id, "artifact_version_id": node.artifact_version_id}
            for node in result.nodes
        ],
        "edges": [
            {
                "edge_id": edge.edge_id,
                "source_node_id": edge.source_node_id,
                "target_node_id": edge.target_node_id,
            }
            for edge in result.edges
        ],
        "artifact_version_ids": list(result.artifact_version_ids),
    }
    return json.dumps(payload, ensure_ascii=False, sort_keys=True)


def _traced(session: Session, callback, seen: list[str]):
    raw = _raw_sqlite(session)
    raw.set_trace_callback(seen.append)
    try:
        return callback()
    finally:
        raw.set_trace_callback(None)


def _page_grant(
    session: Session,
    grant_id: str,
    project_id: str,
    account_id: str,
    page_id: str | None,
) -> None:
    session.add(
        ObjectGrant(
            id=grant_id,
            project_id=project_id,
            account_id=account_id,
            action="project.read" if page_id is None else "page.read",
            scope_type="project" if page_id is None else "page",
            status="active",
            created_at=NOW,
            revoked_at=None,
            created_by_account_id=account_id,
            stage_id=None,
            page_id=page_id,
            page_version_id=None,
            artifact_id=None,
            artifact_version_id=None,
            evidence_anchor_id=None,
        )
    )
