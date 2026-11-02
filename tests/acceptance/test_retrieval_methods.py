"""Authorized excerpt search. Synthetic Chinese text only. No sleep."""

import hashlib
import json

import conftest
import pytest
from alembic import command
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from sinuo.application.auth.authorizer import SessionAuthorizer
from sinuo.application.retrieval.graph import get_graph_neighborhood
from sinuo.application.retrieval.methods import compare_versions, lookup_method
from sinuo.application.retrieval.search import (
    EVIDENCE_INDEX,
    rebuild_evidence_index,
    search_evidence,
)
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
from sinuo.infrastructure.methods import MethodCard

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
        for alias in ("latest", "head", "current", "newest", "LATEST", "", "  "):
            with pytest.raises(ValueError, match="artifact_version_id"):
                search_evidence(
                    session,
                    authorizer,
                    principal,
                    PROJECT_A,
                    QUERY,
                    20,
                    artifact_version_id=alias,
                )


INDEX_READER = "55555555-5555-4555-8555-555555555555"
INDEX_PROJECT = "synthetic-index-alpha"
INDEX_ARTIFACT = "synthetic-index-art"
INDEX_V1 = "synthetic-index-v1"
INDEX_V2 = "synthetic-index-v2"
INDEX_ANCHOR = "synthetic-index-anchor"
INDEX_SESSION = "synthetic-index-session"
_INDEX_OBJECTS = {
    EVIDENCE_INDEX,
    *(
        f"{EVIDENCE_INDEX}_{suffix}"
        for suffix in ("config", "content", "data", "docsize", "idx")
    ),
}


def test_anchor_without_an_index_row_is_not_indexed(engine):
    project = "synthetic-unindexed-alpha"
    reader = "66666666-6666-4666-8666-666666666666"
    artifact = "synthetic-unindexed-art"
    anchored = "synthetic-unindexed-v1"
    empty = "synthetic-unindexed-empty"
    anchor = "synthetic-unindexed-anchor"
    with write_uow(engine) as session:
        _search_project(session, project, reader, "synthetic-unindexed-reader")
        _artifact(session, artifact, project)
        session.flush()
        _version(session, anchored, project, artifact, reader, 1)
        _version(session, empty, project, artifact, reader, 2)
        session.flush()
        _anchor(session, anchor, project, anchored, reader, {"excerpt": QUERY, "page": 3})
        session.flush()
        _grant(session, "grant-unindexed-read", project, reader, "project.read", None)
        _grant(session, "grant-unindexed-evidence", project, reader, "evidence.read", anchor)
    principal = Principal(reader, "synthetic-unindexed-session", 1)
    with write_uow(engine) as session:
        authorizer = SessionAuthorizer(session)

        def run():
            return search_evidence(session, authorizer, principal, project, QUERY, 20)

        before = _assert_reads_only(session, run)
    assert before.hits == ()
    assert before.counts == {"hit": 0, "no_hit": 0, "not_indexed": 2}
    statuses = {item.artifact_version_id: item.status for item in before.versions}
    assert statuses[anchored] == "not_indexed"
    assert statuses[empty] == "not_indexed"
    assert QUERY not in _render(before)
    with write_uow(engine) as session:
        inserted = rebuild_evidence_index(session)
    assert inserted == 1
    with write_uow(engine) as session:
        found = search_evidence(
            session, SessionAuthorizer(session), principal, project, QUERY, 20
        )
    assert [hit.artifact_version_id for hit in found.hits] == [anchored]
    assert found.hits[0].locator == {"excerpt": QUERY, "page": 3}
    assert found.hits[0].artifact_version_id != empty
    assert found.counts == {"hit": 1, "no_hit": 0, "not_indexed": 1}
    found_status = {item.artifact_version_id: item.status for item in found.versions}
    assert found_status[anchored] == "hit"
    assert found_status[empty] == "not_indexed"


def test_rebuild_keeps_the_indexed_version_id(engine):
    with write_uow(engine) as session:
        _search_project(session, INDEX_PROJECT, INDEX_READER, "synthetic-index-reader")
        _artifact(session, INDEX_ARTIFACT, INDEX_PROJECT)
        session.flush()
        _version(session, INDEX_V1, INDEX_PROJECT, INDEX_ARTIFACT, INDEX_READER, 1)
        session.flush()
        _anchor(
            session,
            INDEX_ANCHOR,
            INDEX_PROJECT,
            INDEX_V1,
            INDEX_READER,
            {"excerpt": QUERY, "page": 3},
        )
        session.flush()
        _grant(session, "grant-index-read", INDEX_PROJECT, INDEX_READER, "project.read", None)
        _grant(
            session,
            "grant-index-evidence",
            INDEX_PROJECT,
            INDEX_READER,
            "evidence.read",
            INDEX_ANCHOR,
        )
        inserted = rebuild_evidence_index(session)
    assert inserted == 1
    with Session(engine) as session:
        anchor = session.get(EvidenceAnchor, INDEX_ANCHOR)
        assert anchor is not None
        first_locator = anchor.locator_json
        first_index = _index_row(session, INDEX_ANCHOR)
    assert first_index.artifact_version_id == INDEX_V1
    assert first_index.project_id == INDEX_PROJECT
    assert first_index.excerpt == QUERY
    assert first_index.excerpt_hash == hashlib.sha256(QUERY.encode("utf-8")).hexdigest()
    assert first_index.generator
    assert first_index.index_revision
    with write_uow(engine) as session:
        _version(session, INDEX_V2, INDEX_PROJECT, INDEX_ARTIFACT, INDEX_READER, 2)
        session.flush()
        before_changes = session.scalar(text("SELECT total_changes()"))
        seen: list[str] = []
        raw = _raw_sqlite(session)
        raw.set_trace_callback(seen.append)
        try:
            again = rebuild_evidence_index(session)
        finally:
            raw.set_trace_callback(None)
        after_changes = session.scalar(text("SELECT total_changes()"))
        assert again == 0
        assert after_changes == before_changes
        assert not any(_statement_writes(statement) for statement in seen)
        authorizer = SessionAuthorizer(session)
        principal = Principal(INDEX_READER, INDEX_SESSION, 1)
        plain = search_evidence(session, authorizer, principal, INDEX_PROJECT, QUERY, 20)
        historical = search_evidence(
            session,
            authorizer,
            principal,
            INDEX_PROJECT,
            QUERY,
            20,
            artifact_version_id=INDEX_V1,
        )
        newer = search_evidence(
            session,
            authorizer,
            principal,
            INDEX_PROJECT,
            QUERY,
            20,
            artifact_version_id=INDEX_V2,
        )
    with Session(engine) as session:
        anchor = session.get(EvidenceAnchor, INDEX_ANCHOR)
        assert anchor is not None
        assert anchor.artifact_version_id == INDEX_V1
        assert anchor.locator_json == first_locator
        second_index = _index_row(session, INDEX_ANCHOR)
        pointed = session.execute(
            text(
                f"SELECT anchor_id FROM {EVIDENCE_INDEX} "
                "WHERE artifact_version_id = :version_id"
            ),
            {"version_id": INDEX_V2},
        ).all()
        stored_v1 = session.get(ArtifactVersion, INDEX_V1)
        stored_v2 = session.get(ArtifactVersion, INDEX_V2)
    assert second_index.artifact_version_id == first_index.artifact_version_id == INDEX_V1
    assert second_index.excerpt == QUERY
    assert pointed == []
    assert stored_v1 is not None and stored_v1.version_no == 1
    assert stored_v2 is not None and stored_v2.version_no == 2
    assert stored_v1.artifact_id == stored_v2.artifact_id == INDEX_ARTIFACT
    assert [hit.artifact_version_id for hit in plain.hits] == [INDEX_V1]
    assert plain.hits[0].locator == {"excerpt": QUERY, "page": 3}
    plain_status = {item.artifact_version_id: item.status for item in plain.versions}
    assert plain_status[INDEX_V1] == "hit"
    assert plain_status[INDEX_V2] == "not_indexed"
    assert [hit.artifact_version_id for hit in historical.hits] == [INDEX_V1]
    assert [item.artifact_version_id for item in historical.versions] == [INDEX_V1]
    assert newer.hits == ()
    assert QUERY not in _render(newer)
    assert [item.artifact_version_id for item in newer.versions] == [INDEX_V2]
    assert newer.versions[0].status == "not_indexed"
    assert newer.counts == {"hit": 0, "no_hit": 0, "not_indexed": 1}


def test_three_character_query_uses_trigram_and_shorter_uses_substring(engine):
    seed_search_world(engine)
    principal = Principal(READER_ID, SESSION_ID, 1)
    with write_uow(engine) as session:
        authorizer = SessionAuthorizer(session)
        trigram_sql: list[str] = []
        short_sql: list[str] = []
        raw = _raw_sqlite(session)
        raw.set_trace_callback(trigram_sql.append)
        try:
            trigram = search_evidence(session, authorizer, principal, PROJECT_A, "竹材弯", 20)
        finally:
            raw.set_trace_callback(None)
        raw.set_trace_callback(short_sql.append)
        try:
            short = search_evidence(session, authorizer, principal, PROJECT_A, "_", 20)
        finally:
            raw.set_trace_callback(None)
    assert [hit.artifact_version_id for hit in trigram.hits] == [HIT_VERSION]
    assert trigram.counts == {"hit": 1, "no_hit": 3, "not_indexed": 2}
    rendered = _render(trigram)
    assert HIDDEN_EXCERPT not in rendered
    assert FOREIGN_EXCERPT not in rendered
    assert DECOY_LABEL not in rendered
    assert any(
        "anchor_id IN" in statement and f"{EVIDENCE_INDEX} MATCH" in statement
        for statement in trigram_sql
    )
    assert not any("json_extract" in statement for statement in trigram_sql)
    assert [hit.artifact_version_id for hit in short.hits] == [UNDERSCORE_VERSION]
    assert any(
        "anchor_id IN" in statement and "excerpt LIKE" in statement for statement in short_sql
    )
    assert not any(f"{EVIDENCE_INDEX} MATCH" in statement for statement in short_sql)


def test_downgrade_drops_only_the_evidence_index(engine):
    with engine.begin() as connection:
        before = _application_tables(connection)
    assert len(before) == 50
    assert _INDEX_OBJECTS <= before
    with engine.begin() as connection:
        command.downgrade(conftest.alembic_config(connection), "0009_method_cards")
        version = connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
        after = _application_tables(connection)
    assert version == "0009_method_cards"
    assert after == before - _INDEX_OBJECTS
    assert "method_cards" in after
    assert "evidence_anchors" in after
    assert "artifact_versions" in after
    with engine.begin() as connection:
        command.upgrade(conftest.alembic_config(connection), "head")
        restored_version = connection.execute(
            text("SELECT version_num FROM alembic_version")
        ).scalar_one()
        restored = _application_tables(connection)
    assert restored_version == "0010_evidence_index"
    assert restored == before


def _search_project(session: Session, project_id: str, reader_id: str, login_name: str) -> None:
    session.add(
        Project(
            id=project_id,
            title=project_id,
            brief="synthetic",
            template_key=None,
            revision=1,
            access_epoch=1,
            created_at=NOW,
        )
    )
    _account(session, reader_id, login_name)
    session.flush()
    _member(session, f"member-{project_id}", project_id, reader_id)


def _index_row(session: Session, anchor_id: str):
    return session.execute(
        text(
            f"""
            SELECT anchor_id, project_id, artifact_version_id, excerpt,
                   excerpt_hash, generator, index_revision
            FROM {EVIDENCE_INDEX}
            WHERE anchor_id = :anchor_id
            """
        ),
        {"anchor_id": anchor_id},
    ).one()


def _application_tables(connection) -> set[str]:
    names = set(
        connection.execute(
            text(
                "SELECT name FROM sqlite_master "
                "WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
            )
        ).scalars()
    )
    return names - {"alembic_version"}


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
        rebuild_evidence_index(session)


def test_search_evidence_multi_version_filter(engine):
    seed_search_world(engine)
    principal = Principal(READER_ID, SESSION_ID, 1)
    with write_uow(engine) as session:
        authorizer = SessionAuthorizer(session)

        def run(*, versions):
            return search_evidence(
                session,
                authorizer,
                principal,
                PROJECT_A,
                QUERY,
                20,
                artifact_version_ids=versions,
            )

        # 限定 HIT_VERSION：命中
        result = run(versions=(HIT_VERSION,))
        assert [item.artifact_version_id for item in result.hits] == [HIT_VERSION]
        assert result.counts["hit"] == 1
        # 限定 MISS_VERSION（摘录不匹配 query）：无命中，记为 no_hit
        missed = run(versions=(MISS_VERSION,))
        assert missed.hits == ()
        assert missed.counts["no_hit"] == 1
        # 多版本：HIT + PERCENT，query 只命中 HIT
        multi = run(versions=(HIT_VERSION, PERCENT_VERSION))
        assert [item.artifact_version_id for item in multi.hits] == [HIT_VERSION]
        # 空集合：什么都不匹配
        empty = run(versions=())
        assert empty.hits == ()
        assert empty.versions == ()
        assert empty.counts == {"hit": 0, "no_hit": 0, "not_indexed": 0}


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


METHOD_READER = "12121212-1212-4212-8212-121212121212"
METHOD_OTHER = "13131313-1313-4313-8313-131313131313"
METHOD_PROJECT = "synthetic-method-alpha"
METHOD_FOREIGN = "synthetic-method-beta"
OBSERVE_V1 = "synthetic-method-observe-v1"
OBSERVE_V2 = "synthetic-method-observe-v2"
MOLD_CARD = "synthetic-method-mold-v1"
STAGE_CARD = "synthetic-method-stage-v1"
FOREIGN_CARD = "synthetic-method-foreign-v1"
OBSERVE_STEP = "观察步骤先记下夜间动作"
MOLD_STEP = "模具步骤不应被强推"
NEWER_STEP = "新版本模具步骤"
STAGE_STEP = "只凭阶段强推"
FOREIGN_STEP = "他项方法步骤"
OBSERVE_SOURCE = "合成来源第3页"
PAGE_LEFT = "synthetic-compare-page-v1"
PAGE_RIGHT = "synthetic-compare-page-v2"
PAGE_HIDDEN = "synthetic-compare-page-hidden"
PAGE_MISSING = "synthetic-compare-page-missing"
ART_LEFT = "synthetic-compare-art-v1"
ART_RIGHT = "synthetic-compare-art-v2"
ART_HIDDEN = "synthetic-compare-art-hidden"
OBJECT_KEY = "synthetic/synthetic-compare-art-v1"
HIDDEN_TITLE = "合成无权对比标题"
VISIBLE_TITLE = "合成可见对比页"


def test_lookup_method_returns_applicable_cards_only(engine):
    _seed_methods(engine)
    before = _method_count(engine)
    principal = Principal(METHOD_READER, "synthetic-method-session", 1)
    with write_uow(engine) as session:
        authorizer = SessionAuthorizer(session)
        hit = _assert_reads_only(
            session,
            lambda: lookup_method(
                session,
                authorizer,
                principal,
                METHOD_PROJECT,
                "夜间观察怎么做",
                "家庭厨房",
                {"consent": True},
                12,
            ),
        )
    assert _method_count(engine) == before
    assert hit.status == "hit"
    assert [card.version_id for card in hit.cards] == [OBSERVE_V1]
    card = hit.cards[0]
    assert card.version_no == 1
    assert card.source["title"] == "情境观察"
    assert card.source["locator"] == OBSERVE_SOURCE
    assert card.inputs == ("同意记录",)
    assert card.steps == (OBSERVE_STEP,)
    assert card.outputs == ("观察记录",)
    assert card.limits == ("不能代替访谈",)
    assert "question_terms" in card.applicable
    rendered = _render_lookup(hit)
    assert OBSERVE_STEP in rendered
    assert OBSERVE_SOURCE in rendered
    for absent in (MOLD_STEP, NEWER_STEP, STAGE_STEP, FOREIGN_STEP, OBSERVE_V2):
        assert absent not in rendered
    with write_uow(engine) as session:
        authorizer = SessionAuthorizer(session)
        gap = lookup_method(
            session,
            authorizer,
            principal,
            METHOD_PROJECT,
            "夜间观察怎么做",
            "",
            {},
            12,
        )
        unrelated = lookup_method(
            session,
            authorizer,
            principal,
            METHOD_PROJECT,
            "注塑无关问题",
            "家庭",
            {"consent": True},
            12,
        )
        stage_only = lookup_method(
            session,
            authorizer,
            principal,
            METHOD_PROJECT,
            "discover",
            "家庭",
            {"consent": True},
            12,
        )
        foreign = lookup_method(
            session,
            authorizer,
            principal,
            METHOD_FOREIGN,
            "夜间观察怎么做",
            "家庭厨房",
            {"consent": True},
            12,
        )
    assert gap.status == "no_match" and gap.cards == ()
    assert "requires:consent" in gap.gaps[0].missing
    assert "situation:家庭" in gap.gaps[0].missing
    assert OBSERVE_STEP not in _render_lookup(gap)
    assert unrelated.status == "no_match" and unrelated.cards == () and unrelated.gaps == ()
    assert STAGE_STEP not in _render_lookup(stage_only)
    assert stage_only.cards == ()
    assert foreign.cards == () and foreign.gaps == ()
    assert FOREIGN_STEP not in _render_lookup(foreign)
    with pytest.raises(IntegrityError):
        with engine.begin() as connection:
            connection.execute(
                text("UPDATE method_cards SET name = 'changed' WHERE id = :card_id"),
                {"card_id": OBSERVE_V1},
            )
    with engine.connect() as connection:
        stored = connection.execute(
            text("SELECT name FROM method_cards WHERE id = :card_id"),
            {"card_id": OBSERVE_V1},
        ).scalar_one()
    assert stored == "情境观察"


def test_lookup_method_rejects_bad_arguments(engine):
    principal = Principal(METHOD_READER, "synthetic-method-session", 1)
    with write_uow(engine) as session:
        authorizer = SessionAuthorizer(session)
        for question in ("", "   ", "问" * 81, None):
            with pytest.raises(ValueError, match="question"):
                lookup_method(
                    session,
                    authorizer,
                    principal,
                    METHOD_PROJECT,
                    question,
                    "",
                    {},
                    12,
                )
        for limit in (0, 13, True, None):
            with pytest.raises(ValueError, match="limit"):
                lookup_method(
                    session,
                    authorizer,
                    principal,
                    METHOD_PROJECT,
                    "夜间观察",
                    "",
                    {},
                    limit,
                )
        with pytest.raises(AuthRequired):
            lookup_method(
                session,
                authorizer,
                Principal("14141414-1414-4414-8414-141414141414", "missing", 1),
                METHOD_PROJECT,
                "夜间观察",
                "",
                {},
                12,
            )


def test_compare_versions_accepts_only_exact_ids(engine):
    _seed_compare(engine)
    before_pages = _page_version_count(engine)
    principal = Principal(METHOD_READER, "synthetic-method-session", 1)
    with write_uow(engine) as session:
        authorizer = SessionAuthorizer(session)
        for left, right, field in (
            ("latest", PAGE_RIGHT, "left_id"),
            (PAGE_LEFT, "HEAD", "right_id"),
            (PAGE_LEFT, "current", "right_id"),
            (PAGE_LEFT, "newest", "right_id"),
        ):
            with pytest.raises(ValueError, match=field):
                compare_versions(
                    session,
                    authorizer,
                    principal,
                    METHOD_PROJECT,
                    left,
                    right,
                    "page_version",
                )
        with pytest.raises(ValueError, match="version_id"):
            compare_versions(
                session,
                authorizer,
                principal,
                METHOD_PROJECT,
                PAGE_LEFT,
                PAGE_LEFT,
                "page_version",
            )
        compared = _assert_reads_only(
            session,
            lambda: compare_versions(
                session,
                authorizer,
                principal,
                METHOD_PROJECT,
                PAGE_LEFT,
                PAGE_RIGHT,
                "page_version",
            ),
        )
        missing = compare_versions(
            session,
            authorizer,
            principal,
            METHOD_PROJECT,
            PAGE_LEFT,
            PAGE_MISSING,
            "page_version",
        )
        hidden = compare_versions(
            session,
            authorizer,
            principal,
            METHOD_PROJECT,
            PAGE_LEFT,
            PAGE_HIDDEN,
            "page_version",
        )
        artifacts = compare_versions(
            session,
            authorizer,
            principal,
            METHOD_PROJECT,
            ART_LEFT,
            ART_RIGHT,
            "artifact_version",
        )
        hidden_art = compare_versions(
            session,
            authorizer,
            principal,
            METHOD_PROJECT,
            ART_LEFT,
            ART_HIDDEN,
            "artifact_version",
        )
    assert _page_version_count(engine) == before_pages
    assert compared.semantic_claim is None
    assert compared.left.version_no == 1
    assert compared.right.version_no == 2
    assert compared.left.title == VISIBLE_TITLE
    changes = {item.node_id: item.change for item in compared.nodes}
    assert changes == {"n-old": "removed", "n-new": "added", "n-keep": "changed"}
    assert compared.tests[0].artifact_version_id == ART_LEFT
    assert compared.tests[0].bound_side == "left"
    assert compared.tests[0].other_side_status == "needs_review"
    assert missing.left.version_no == 1
    assert missing.right.status == "missing"
    assert missing.right.version_no is None
    assert missing.right.version_id == PAGE_MISSING
    assert missing.nodes == ()
    hidden_text = _render_compare(hidden)
    assert hidden.right.status == "hidden"
    assert hidden.right.title is None
    assert HIDDEN_TITLE not in hidden_text
    assert artifacts.bytes_differ is True
    assert artifacts.semantic_claim is None
    assert artifacts.tests[0].other_side_status == "needs_review"
    art_text = _render_compare(artifacts)
    assert "CAD" not in art_text
    assert OBJECT_KEY not in art_text
    assert hidden_art.right.status == "hidden"
    assert hidden_art.right.content_hash is None
    assert hidden_art.bytes_differ is None


def _seed_methods(engine) -> None:
    with write_uow(engine) as session:
        _method_project(session, METHOD_PROJECT)
        _method_project(session, METHOD_FOREIGN)
        _method_account(session, METHOD_READER, "synthetic-method-reader")
        _method_account(session, METHOD_OTHER, "synthetic-method-other")
        session.flush()
        _method_member(session, "member-method-reader", METHOD_PROJECT, METHOD_READER)
        _method_member(session, "member-method-other", METHOD_FOREIGN, METHOD_OTHER)
        _method_grant(session, "grant-method-read", METHOD_PROJECT, METHOD_READER)
        _method_grant(session, "grant-method-foreign", METHOD_FOREIGN, METHOD_OTHER)
        session.add_all(
            [
                _card(
                    OBSERVE_V1,
                    METHOD_PROJECT,
                    "synthetic-observe",
                    1,
                    "情境观察",
                    OBSERVE_SOURCE,
                    ["夜间观察"],
                    ["家庭"],
                    ["consent"],
                    ["discover"],
                    ["同意记录"],
                    [OBSERVE_STEP],
                    ["观察记录"],
                    ["不能代替访谈"],
                ),
                _card(
                    OBSERVE_V2,
                    METHOD_PROJECT,
                    "synthetic-observe",
                    2,
                    "情境观察",
                    "合成新版本来源",
                    ["模具公差"],
                    [],
                    [],
                    ["discover"],
                    ["新输入"],
                    [NEWER_STEP],
                    ["新产物"],
                    ["新限制"],
                ),
                _card(
                    MOLD_CARD,
                    METHOD_PROJECT,
                    "synthetic-mold",
                    1,
                    "模具",
                    "合成模具来源",
                    ["模具公差"],
                    [],
                    [],
                    ["discover"],
                    ["模具输入"],
                    [MOLD_STEP],
                    ["模具产物"],
                    ["模具限制"],
                ),
                _card(
                    STAGE_CARD,
                    METHOD_PROJECT,
                    "synthetic-stage-card",
                    1,
                    "阶段空卡",
                    "合成阶段来源",
                    [],
                    [],
                    [],
                    ["discover"],
                    ["阶段输入"],
                    [STAGE_STEP],
                    ["阶段产物"],
                    ["阶段限制"],
                ),
                _card(
                    FOREIGN_CARD,
                    METHOD_FOREIGN,
                    "synthetic-foreign-card",
                    1,
                    "他项方法",
                    "他项来源",
                    ["夜间观察"],
                    ["家庭"],
                    ["consent"],
                    [],
                    ["他项输入"],
                    [FOREIGN_STEP],
                    ["他项产物"],
                    ["他项限制"],
                ),
            ]
        )


def _seed_compare(engine) -> None:
    with write_uow(engine) as session:
        _method_project(session, METHOD_PROJECT)
        _method_account(session, METHOD_READER, "synthetic-method-reader")
        session.flush()
        _method_member(session, "member-compare-reader", METHOD_PROJECT, METHOD_READER)
        _method_grant(session, "grant-compare-read", METHOD_PROJECT, METHOD_READER)
        session.add(
            Stage(
                id="synthetic-compare-stage",
                project_id=METHOD_PROJECT,
                head_config_version_id=None,
                created_at=NOW,
            )
        )
        session.flush()
        _artifact(session, "synthetic-compare-art", METHOD_PROJECT)
        _artifact(session, "synthetic-compare-hidden-art", METHOD_PROJECT)
        session.flush()
        _version(session, ART_LEFT, METHOD_PROJECT, "synthetic-compare-art", METHOD_READER, 1)
        _version(session, ART_RIGHT, METHOD_PROJECT, "synthetic-compare-art", METHOD_READER, 2)
        _version(
            session,
            ART_HIDDEN,
            METHOD_PROJECT,
            "synthetic-compare-hidden-art",
            METHOD_READER,
            1,
        )
        session.flush()
        for page_id, title in (
            ("synthetic-compare-visible", VISIBLE_TITLE),
            ("synthetic-compare-secret", HIDDEN_TITLE),
        ):
            session.add(
                Page(
                    id=page_id,
                    project_id=METHOD_PROJECT,
                    stage_id="synthetic-compare-stage",
                    head_version_id=None,
                    status="active",
                    title=title,
                    created_at=NOW,
                )
            )
        session.flush()
        _compare_page(
            session,
            PAGE_LEFT,
            "synthetic-compare-visible",
            1,
            ["n-keep", "n-old"],
            [ART_LEFT, ART_LEFT],
        )
        _compare_page(
            session,
            PAGE_RIGHT,
            "synthetic-compare-visible",
            2,
            ["n-keep", "n-new"],
            [ART_RIGHT, ART_RIGHT],
        )
        _compare_page(
            session,
            PAGE_HIDDEN,
            "synthetic-compare-secret",
            1,
            ["n-secret"],
            [ART_HIDDEN],
        )
        session.flush()
        visible = session.get(Page, "synthetic-compare-visible")
        secret = session.get(Page, "synthetic-compare-secret")
        assert visible is not None and secret is not None
        visible.head_version_id = PAGE_RIGHT
        secret.head_version_id = PAGE_HIDDEN
        from sinuo.infrastructure.domain_models import TestRecord

        session.add(
            TestRecord(
                id="synthetic-compare-test",
                project_id=METHOD_PROJECT,
                account_id=METHOD_READER,
                artifact_version_id=ART_LEFT,
                conditions_json="{}",
                observation="synthetic",
                result="synthetic",
                source="synthetic",
                supersedes_id=None,
                project_revision=1,
                created_at=NOW,
            )
        )
        _page_grant(
            session,
            "grant-compare-visible",
            METHOD_PROJECT,
            METHOD_READER,
            "synthetic-compare-visible",
        )
        session.add(
            ObjectGrant(
                id="grant-compare-art-left",
                project_id=METHOD_PROJECT,
                account_id=METHOD_READER,
                action="preview.read",
                scope_type="artifact_version",
                status="active",
                created_at=NOW,
                revoked_at=None,
                created_by_account_id=METHOD_READER,
                stage_id=None,
                page_id=None,
                page_version_id=None,
                artifact_id=None,
                artifact_version_id=ART_LEFT,
                evidence_anchor_id=None,
            )
        )
        session.add(
            ObjectGrant(
                id="grant-compare-art-right",
                project_id=METHOD_PROJECT,
                account_id=METHOD_READER,
                action="preview.read",
                scope_type="artifact_version",
                status="active",
                created_at=NOW,
                revoked_at=None,
                created_by_account_id=METHOD_READER,
                stage_id=None,
                page_id=None,
                page_version_id=None,
                artifact_id=None,
                artifact_version_id=ART_RIGHT,
                evidence_anchor_id=None,
            )
        )


def _method_project(session: Session, project_id: str) -> None:
    session.add(
        Project(
            id=project_id,
            title=project_id,
            brief="synthetic",
            template_key=None,
            revision=1,
            access_epoch=1,
            created_at=NOW,
        )
    )


def _method_account(session: Session, account_id: str, login_name: str) -> None:
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


def _method_member(session: Session, member_id: str, project_id: str, account_id: str) -> None:
    session.add(
        Membership(
            id=member_id,
            project_id=project_id,
            account_id=account_id,
            status="active",
            role_label=None,
            created_at=NOW,
            revoked_at=None,
        )
    )


def _method_grant(session: Session, grant_id: str, project_id: str, account_id: str) -> None:
    session.add(
        ObjectGrant(
            id=grant_id,
            project_id=project_id,
            account_id=account_id,
            action="project.read",
            scope_type="project",
            status="active",
            created_at=NOW,
            revoked_at=None,
            created_by_account_id=account_id,
            stage_id=None,
            page_id=None,
            page_version_id=None,
            artifact_id=None,
            artifact_version_id=None,
            evidence_anchor_id=None,
        )
    )


def _card(
    card_id: str,
    project_id: str,
    card_key: str,
    version_no: int,
    name: str,
    locator: str,
    terms: list[str],
    situations: list[str],
    requires: list[str],
    stages: list[str],
    inputs: list[str],
    steps: list[str],
    outputs: list[str],
    limits: list[str],
) -> MethodCard:
    return MethodCard(
        id=card_id,
        library_key=project_id,
        project_id=project_id,
        card_key=card_key,
        version_no=version_no,
        name=name,
        source_json=_json({"title": name, "locator": locator, "license": "synthetic"}),
        applicable_json=_json(
            {
                "question_terms": terms,
                "requires": requires,
                "situations": situations,
                "stage_names": stages,
            }
        ),
        inputs_json=_json(inputs),
        steps_json=_json(steps),
        outputs_json=_json(outputs),
        limits_json=_json(limits),
        created_at=NOW,
    )


def _compare_page(
    session: Session,
    version_id: str,
    page_id: str,
    version_no: int,
    node_ids: list[str],
    artifact_ids: list[str],
) -> None:
    nodes = [
        {"artifact_version_id": artifact_id, "node_id": node_id}
        for node_id, artifact_id in zip(node_ids, artifact_ids, strict=True)
    ]
    body = _json({"edges": [], "nodes": nodes, "schema_version": 1})
    session.add(
        PageVersion(
            id=version_id,
            project_id=METHOD_PROJECT,
            page_id=page_id,
            version_no=version_no,
            parent_version_id=None,
            content_json=body,
            content_hash=hashlib.sha256(body.encode("utf-8")).hexdigest(),
            schema_version=1,
            project_revision=1,
            created_by_account_id=METHOD_READER,
            created_at=NOW,
        )
    )


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _method_count(engine) -> int:
    with Session(engine) as session:
        return int(session.scalar(select(func.count()).select_from(MethodCard)) or 0)


def _render_lookup(result) -> str:
    payload = {
        "status": result.status,
        "cards": [
            {
                "version_id": card.version_id,
                "steps": list(card.steps),
                "source": card.source,
                "inputs": list(card.inputs),
                "outputs": list(card.outputs),
                "limits": list(card.limits),
            }
            for card in result.cards
        ],
        "gaps": [
            {"card_key": gap.card_key, "missing": list(gap.missing)} for gap in result.gaps
        ],
    }
    return json.dumps(payload, ensure_ascii=False)


def _render_compare(result) -> str:
    payload = {
        "left": {
            "title": result.left.title,
            "status": result.left.status,
            "content_hash": result.left.content_hash,
            "version_no": result.left.version_no,
        },
        "right": {
            "title": result.right.title,
            "status": result.right.status,
            "content_hash": result.right.content_hash,
            "version_no": result.right.version_no,
        },
        "semantic_claim": result.semantic_claim,
        "bytes_differ": result.bytes_differ,
        "tests": [item.test_id for item in result.tests],
    }
    return json.dumps(payload, ensure_ascii=False)


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
