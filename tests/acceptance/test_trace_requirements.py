"""F08 read-only trace from a page version or a requirement node.

The page document still has no kind field unless a test writes it directly.
drafts.py is not involved. Bodies stay off the response without evidence.read
or test.record. A test on another artifact version stays on that version.
"""

import hashlib
import json
from dataclasses import dataclass

from fastapi.testclient import TestClient
from sqlalchemy import text

from sinuo.api.app import create_app
from sinuo.application.evidence.anchors import create_anchor
from sinuo.application.records.tests import create_test_record
from sinuo.application.trace.routes import router
from sinuo.domain.public_contract import CSRF_HEADER
from sinuo.infrastructure.database import write_uow
from sinuo.infrastructure.domain_models import (
    ArtifactVersion,
    AuditEvent,
    PageVersion,
    PrivacyTombstone,
)
from tests.acceptance.identity_fixtures import (
    CITATION,
    LEAD_ID,
    LEAD_LOGIN,
    LEAD_PASSWORD,
    NOW,
    OTHER_LOGIN,
    OTHER_PASSWORD,
    PAGE_A,
    PROJECT_A,
    PROJECT_B,
    REVIEWER_LOGIN,
    REVIEWER_PASSWORD,
    TEAM_LOGIN,
    TEAM_PASSWORD,
    identity_settings,
    seed_identity_world,
)

ORIGIN = "http://127.0.0.1:8000"
TRACE = "/api/v1/projects/{project_id}/trace"
V2_ID = "synthetic-released-v2"
TYPED_VERSION = "synthetic-page-version-typed"
NODE_REQUIREMENT = "synthetic-requirement"
NODE_EVIDENCE = "synthetic-evidence-node"
NODE_TEST = "synthetic-test-node"
NODE_BARE = "synthetic-bare-link"
NODE_UNTYPED = "synthetic-node-released"
EXCERPT_PAGE = "synthetic-excerpt-page"
EXCERPT_REGION = "synthetic-excerpt-region"
EXCERPT_TIME = "synthetic-excerpt-time"
EXCERPT_V2 = "synthetic-excerpt-v2"
EXCERPT_CORRECTION = "synthetic-excerpt-correction"
OBSERVATION = "synthetic-observation-v1"
OBSERVATION_V2 = "synthetic-observation-v2"
CONDITION = "synthetic-condition-token"
SOURCE = "synthetic-source-v1"
RESULT = "synthetic-result-v1"
REGION = "区域甲"
TIMECODE = "00:00:12"
KIND_GAP = "page content has no requirement, evidence, or test kind"
SECRETS = (
    EXCERPT_PAGE,
    EXCERPT_REGION,
    EXCERPT_TIME,
    EXCERPT_V2,
    EXCERPT_CORRECTION,
    OBSERVATION,
    OBSERVATION_V2,
    CONDITION,
    SOURCE,
    RESULT,
    CITATION,
)


@dataclass(frozen=True)
class _Ready:
    app: object
    evidence_id: str
    released_version_id: str
    sensitive_version_id: str
    foreign_evidence_id: str
    head_id: str
    page_anchor_id: str
    region_anchor_id: str
    time_anchor_id: str
    correction_id: str
    v2_anchor_id: str
    record_id: str


def test_trace_router_is_a_get():
    found: set[tuple[str, str]] = set()
    for route in router.routes:
        path = getattr(route, "path", None)
        for method in getattr(route, "methods", set()) or set():
            if isinstance(path, str):
                found.add((method, path))
    assert ("GET", TRACE) in found


def test_page_version_returns_places_and_pins_tests(engine, database_url):
    ready = _prepare(engine, database_url)
    before = _census(engine)
    with TestClient(ready.app) as client:
        _login(client, LEAD_LOGIN, LEAD_PASSWORD)
        opened = client.get(_trace(PROJECT_A), params={"page_version_id": ready.head_id})
        assert opened.status_code == 200, opened.text
        assert _census(engine) == before
        data = opened.json()["data"]
        assert data["requirements"] == []
        assert {"field": "node.kind", "detail": KIND_GAP} in data["gaps"]
        assert [gap["detail"] for gap in data["gaps"] if gap["field"] == "locator.place"] == [
            ready.evidence_id
        ]
        assert ready.released_version_id in data["pinned_artifact_version_ids"]
        assert ready.sensitive_version_id in data["pinned_artifact_version_ids"]
        assert V2_ID not in data["pinned_artifact_version_ids"]
        assert V2_ID not in opened.text
        assert EXCERPT_V2 not in opened.text
        assert CITATION not in opened.text
        page = _one(data["anchors"], ready.page_anchor_id)
        assert page["artifact_version_id"] == ready.released_version_id
        assert page["page"] == 4
        assert page["region"] is None
        assert page["timecode"] is None
        assert page["excerpt"] == EXCERPT_PAGE
        assert page["excerpt_visible"] is True
        region = _one(data["anchors"], ready.region_anchor_id)
        assert region["region"] == REGION
        assert region["excerpt"] == EXCERPT_REGION
        timed = _one(data["anchors"], ready.time_anchor_id)
        assert timed["timecode"] == TIMECODE
        assert timed["excerpt"] == EXCERPT_TIME
        corrected = _one(data["anchors"], ready.correction_id)
        assert corrected["supersedes_id"] == ready.page_anchor_id
        assert corrected["artifact_version_id"] == ready.released_version_id
        assert corrected["excerpt"] == EXCERPT_CORRECTION
        assert _one(data["anchors"], ready.evidence_id)["excerpt"] is None
        assert [item["test_record_id"] for item in data["tests"]] == [ready.record_id]
        pinned = data["tests"][0]
        assert pinned["artifact_version_id"] == ready.released_version_id
        assert pinned["observation"] == OBSERVATION
        assert pinned["result"] == RESULT
        assert pinned["source"] == SOURCE
        assert pinned["conditions"] == {"task": CONDITION}
        assert pinned["body_visible"] is True
        foreign = client.get(_trace(PROJECT_B), params={"page_version_id": ready.head_id})
        assert foreign.status_code == 200, foreign.text
        assert foreign.json()["data"]["anchors"] == []
        assert foreign.json()["data"]["tests"] == []
        assert EXCERPT_PAGE not in foreign.text
        assert CITATION not in foreign.text
        _tombstone(engine, ready.page_anchor_id)
        hidden = client.get(_trace(PROJECT_A), params={"page_version_id": ready.head_id})
        assert hidden.status_code == 200, hidden.text
        assert _census(engine) == before
        body = hidden.json()["data"]
        redacted = _one(body["anchors"], ready.page_anchor_id)
        assert redacted["page"] == 4
        assert redacted["excerpt"] is None
        assert redacted["excerpt_visible"] is False
        assert EXCERPT_PAGE not in hidden.text
        assert _one(body["anchors"], ready.region_anchor_id)["excerpt"] == EXCERPT_REGION
        assert _one(body["anchors"], ready.correction_id)["excerpt"] == EXCERPT_CORRECTION


def test_requirement_node_uses_named_anchors_only(engine, database_url):
    ready = _prepare(engine, database_url)
    with TestClient(ready.app) as client:
        _login(client, LEAD_LOGIN, LEAD_PASSWORD)
        opened = client.get(_trace(PROJECT_A), params={"node_id": NODE_REQUIREMENT})
        assert opened.status_code == 200, opened.text
        data = opened.json()["data"]
        assert data["node_id"] == NODE_REQUIREMENT
        assert data["page_version_id"] is None
        assert data["pinned_artifact_version_ids"] == [ready.released_version_id]
        assert {item["evidence_anchor_id"] for item in data["anchors"]} == {
            ready.page_anchor_id,
            ready.region_anchor_id,
            ready.correction_id,
        }
        assert ready.time_anchor_id not in opened.text
        assert ready.v2_anchor_id not in opened.text
        assert EXCERPT_TIME not in opened.text
        assert EXCERPT_V2 not in opened.text
        assert CITATION not in opened.text
        assert ready.foreign_evidence_id not in {
            item["evidence_anchor_id"] for item in data["anchors"]
        }
        assert {"field": "evidence_anchor_id", "detail": ready.foreign_evidence_id} in data["gaps"]
        requirement = data["requirements"]
        assert len(requirement) == 1
        assert requirement[0]["node_id"] == NODE_REQUIREMENT
        assert requirement[0]["page_version_id"] == TYPED_VERSION
        assert set(requirement[0]["evidence_anchor_ids"]) == {
            ready.page_anchor_id,
            ready.region_anchor_id,
            ready.correction_id,
        }
        for anchor in data["anchors"]:
            assert anchor["requirement_node_ids"] == [NODE_REQUIREMENT]
            assert anchor["excerpt_visible"] is True
        assert [item["test_record_id"] for item in data["tests"]] == [ready.record_id]
        assert data["tests"][0]["artifact_version_id"] == ready.released_version_id
        untyped = client.get(_trace(PROJECT_A), params={"node_id": NODE_UNTYPED})
        assert untyped.status_code == 200, untyped.text
        bare = untyped.json()["data"]
        assert bare["requirements"] == []
        assert bare["anchors"] == []
        assert bare["tests"] == []
        assert bare["pinned_artifact_version_ids"] == []
        assert bare["gaps"] == [{"field": "node.kind", "detail": NODE_UNTYPED}]
        assert EXCERPT_PAGE not in untyped.text
        other_kind = client.get(_trace(PROJECT_A), params={"node_id": NODE_TEST})
        assert other_kind.status_code == 200, other_kind.text
        assert other_kind.json()["data"]["anchors"] == []
        assert other_kind.json()["data"]["tests"] == []
        assert other_kind.json()["data"]["gaps"] == [
            {"field": "node.kind", "detail": f"{NODE_TEST} is test"}
        ]
        assert OBSERVATION not in other_kind.text
        missing = client.get(_trace(PROJECT_A), params={"node_id": "synthetic-missing-node"})
        assert missing.status_code == 200, missing.text
        assert missing.json()["data"]["gaps"] == []
        assert missing.json()["data"]["anchors"] == []
        assert EXCERPT_PAGE not in missing.text
        without_kind = client.get(_trace(PROJECT_A), params={"node_id": NODE_BARE})
        assert without_kind.status_code == 200, without_kind.text
        assert without_kind.json()["data"]["anchors"] == []
        assert without_kind.json()["data"]["gaps"] == [{"field": "node.kind", "detail": NODE_BARE}]
        assert EXCERPT_TIME not in without_kind.text


def test_callers_without_read_do_not_receive_bodies(engine, database_url):
    ready = _prepare(engine, database_url)
    with TestClient(ready.app) as client:
        anonymous = client.get(_trace(PROJECT_A), params={"page_version_id": ready.head_id})
        assert anonymous.status_code == 401, anonymous.text
        assert anonymous.json()["error"]["code"] == "AUTH_REQUIRED"
        assert EXCERPT_PAGE not in anonymous.text
        for login_name, password in (
            (REVIEWER_LOGIN, REVIEWER_PASSWORD),
            (TEAM_LOGIN, TEAM_PASSWORD),
        ):
            token = _login(client, login_name, password)
            opened = client.get(_trace(PROJECT_A), params={"page_version_id": ready.head_id})
            assert opened.status_code == 200, opened.text
            data = opened.json()["data"]
            page = _one(data["anchors"], ready.page_anchor_id)
            assert page["page"] == 4
            assert page["artifact_version_id"] == ready.released_version_id
            assert page["excerpt"] is None
            assert page["excerpt_visible"] is False
            assert _one(data["anchors"], ready.region_anchor_id)["region"] == REGION
            assert _one(data["anchors"], ready.time_anchor_id)["timecode"] == TIMECODE
            pinned = _one_test(data["tests"], ready.record_id)
            assert pinned["artifact_version_id"] == ready.released_version_id
            assert pinned["observation"] is None
            assert pinned["result"] is None
            assert pinned["source"] is None
            assert pinned["conditions"] is None
            assert pinned["body_visible"] is False
            for secret in SECRETS:
                assert secret not in opened.text
            _logout(client, token)
        token = _login(client, OTHER_LOGIN, OTHER_PASSWORD)
        hidden = client.get(_trace(PROJECT_A), params={"page_version_id": ready.head_id})
        assert hidden.status_code == 200, hidden.text
        assert hidden.json()["data"]["anchors"] == []
        assert hidden.json()["data"]["tests"] == []
        assert hidden.json()["data"]["requirements"] == []
        for secret in SECRETS:
            assert secret not in hidden.text
        _logout(client, token)


def test_query_shape_rejects_alias_and_ambiguous_target(engine, database_url):
    ready = _prepare(engine, database_url)
    before = _census(engine)
    with TestClient(ready.app) as client:
        _login(client, LEAD_LOGIN, LEAD_PASSWORD)
        for params in (
            {},
            {"page_version_id": ready.head_id, "node_id": NODE_REQUIREMENT},
            {"page_version_id": "latest"},
            {"node_id": "head"},
            {"page_version_id": ""},
        ):
            rejected = client.get(_trace(PROJECT_A), params=params)
            assert rejected.status_code == 422, rejected.text
            assert rejected.json()["error"]["code"] == "INVALID_SCHEMA"
            for secret in SECRETS:
                assert secret not in rejected.text
        assert _census(engine) == before


def _prepare(engine, database_url) -> _Ready:
    settings = identity_settings(database_url)
    world = seed_identity_world(engine, settings)
    head_id = _text_one(
        engine, "SELECT head_version_id FROM pages WHERE id = :page_id", page_id=PAGE_A
    )
    with write_uow(engine) as session:
        session.add(
            ArtifactVersion(
                id=V2_ID,
                project_id=PROJECT_A,
                artifact_id="synthetic-artifact-released",
                version_no=2,
                content_hash=_sha(V2_ID),
                size_bytes=8,
                media_type="application/octet-stream",
                object_key="synthetic/released-v2-object-key",
                license="synthetic",
                sensitivity="released",
                ai_processing_allowed=0,
                project_revision=1,
                created_by_account_id=LEAD_ID,
                created_at=NOW,
            )
        )
        session.flush()
        page = create_anchor(
            session,
            project_id=PROJECT_A,
            actor_id=LEAD_ID,
            artifact_version_id=world.released_version_id,
            excerpt=EXCERPT_PAGE,
            locator={"page": 4},
            supersedes_id=None,
            visibility=None,
            extractor_version=None,
            now=NOW,
        )
        region = create_anchor(
            session,
            project_id=PROJECT_A,
            actor_id=LEAD_ID,
            artifact_version_id=world.released_version_id,
            excerpt=EXCERPT_REGION,
            locator={"region": REGION},
            supersedes_id=None,
            visibility=None,
            extractor_version=None,
            now=NOW,
        )
        timed = create_anchor(
            session,
            project_id=PROJECT_A,
            actor_id=LEAD_ID,
            artifact_version_id=world.released_version_id,
            excerpt=EXCERPT_TIME,
            locator={"timecode": TIMECODE},
            supersedes_id=None,
            visibility=None,
            extractor_version=None,
            now=NOW,
        )
        correction = create_anchor(
            session,
            project_id=PROJECT_A,
            actor_id=LEAD_ID,
            artifact_version_id=world.released_version_id,
            excerpt=EXCERPT_CORRECTION,
            locator={"page": 4},
            supersedes_id=page.evidence_anchor_id,
            visibility=None,
            extractor_version=None,
            now=NOW,
        )
        later = create_anchor(
            session,
            project_id=PROJECT_A,
            actor_id=LEAD_ID,
            artifact_version_id=V2_ID,
            excerpt=EXCERPT_V2,
            locator={"page": 9},
            supersedes_id=None,
            visibility=None,
            extractor_version=None,
            now=NOW,
        )
        record = create_test_record(
            session,
            project_id=PROJECT_A,
            actor_id=LEAD_ID,
            artifact_version_id=world.released_version_id,
            conditions={"task": CONDITION},
            observation=OBSERVATION,
            result=RESULT,
            source=SOURCE,
            supersedes_id=None,
            now=NOW,
        )
        create_test_record(
            session,
            project_id=PROJECT_A,
            actor_id=LEAD_ID,
            artifact_version_id=V2_ID,
            conditions={"task": "synthetic-v2-task"},
            observation=OBSERVATION_V2,
            result="synthetic-result-v2",
            source="synthetic-source-v2",
            supersedes_id=None,
            now=NOW,
        )
        document = {
            "schema_version": 1,
            "nodes": [
                {
                    "node_id": NODE_REQUIREMENT,
                    "kind": "requirement",
                    "evidence_anchor_ids": [
                        page.evidence_anchor_id,
                        world.foreign_evidence_id,
                    ],
                },
                {
                    "node_id": NODE_EVIDENCE,
                    "kind": "evidence",
                    "evidence_anchor_id": region.evidence_anchor_id,
                },
                {
                    "node_id": NODE_TEST,
                    "kind": "test",
                    "artifact_version_id": world.released_version_id,
                },
                {
                    "node_id": NODE_BARE,
                    "evidence_anchor_id": timed.evidence_anchor_id,
                },
            ],
            "edges": [
                {
                    "edge_id": "synthetic-supports-edge",
                    "source_node_id": NODE_EVIDENCE,
                    "target_node_id": NODE_REQUIREMENT,
                    "kind": "supports",
                }
            ],
        }
        content_json = json.dumps(
            document, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        session.add(
            PageVersion(
                id=TYPED_VERSION,
                project_id=PROJECT_A,
                page_id=PAGE_A,
                version_no=2,
                parent_version_id=head_id,
                content_json=content_json,
                content_hash=hashlib.sha256(content_json.encode("utf-8")).hexdigest(),
                schema_version=1,
                project_revision=1,
                created_by_account_id=LEAD_ID,
                created_at=NOW,
            )
        )
    app = create_app(settings)
    if ("GET", TRACE) not in _methods(app):
        app.include_router(router)
    return _Ready(
        app=app,
        evidence_id=world.evidence_id,
        released_version_id=world.released_version_id,
        sensitive_version_id=world.sensitive_version_id,
        foreign_evidence_id=world.foreign_evidence_id,
        head_id=str(head_id),
        page_anchor_id=page.evidence_anchor_id,
        region_anchor_id=region.evidence_anchor_id,
        time_anchor_id=timed.evidence_anchor_id,
        correction_id=correction.evidence_anchor_id,
        v2_anchor_id=later.evidence_anchor_id,
        record_id=record.test_record_id,
    )


def _trace(project_id: str) -> str:
    return f"/api/v1/projects/{project_id}/trace"


def _one(anchors: list[dict], anchor_id: str) -> dict:
    found = [item for item in anchors if item["evidence_anchor_id"] == anchor_id]
    assert len(found) == 1
    return found[0]


def _one_test(records: list[dict], record_id: str) -> dict:
    found = [item for item in records if item["test_record_id"] == record_id]
    assert len(found) == 1
    return found[0]


def _census(engine) -> tuple[int, int, int, int]:
    with engine.connect() as connection:
        counts = []
        for table in ("evidence_anchors", "test_records", "page_versions"):
            value = connection.execute(text(f"SELECT count(*) FROM {table}")).scalar_one()
            counts.append(int(value))
        revision = connection.execute(
            text("SELECT revision FROM projects WHERE id = :project_id"),
            {"project_id": PROJECT_A},
        ).scalar_one()
    return counts[0], counts[1], counts[2], int(revision)


def _text_one(engine, statement: str, **params: str) -> str:
    with engine.connect() as connection:
        return str(connection.execute(text(statement), params).scalar_one())


def _tombstone(engine, anchor_id: str) -> None:
    with write_uow(engine) as session:
        session.add(
            AuditEvent(
                id="synthetic-trace-audit",
                project_id=PROJECT_A,
                actor_id=LEAD_ID,
                command="privacy.delete",
                subject_type="evidence_anchor",
                subject_id=anchor_id,
                project_revision=1,
                payload="{}",
                created_at=NOW,
            )
        )
        session.flush()
        session.add(
            PrivacyTombstone(
                id="synthetic-trace-tomb",
                project_id=PROJECT_A,
                object_type="evidence_anchor",
                object_id=anchor_id,
                reason_category="policy",
                access_epoch=1,
                audit_event_id="synthetic-trace-audit",
                created_at=NOW,
            )
        )


def _methods(app) -> set[tuple[str, str]]:
    found: set[tuple[str, str]] = set()

    def walk(routes) -> None:
        for route in routes:
            path = getattr(route, "path", None)
            for method in getattr(route, "methods", set()) or set():
                if isinstance(path, str):
                    found.add((method, path))
            nested = getattr(route, "original_router", None)
            if nested is not None:
                walk(nested.routes)

    walk(app.routes)
    return found


def _login(client: TestClient, login_name: str, password: str) -> str:
    response = client.post(
        "/api/v1/session",
        headers={"Origin": ORIGIN},
        json={"login_name": login_name, "password": password},
    )
    assert response.status_code == 204, response.text
    current = client.get("/api/v1/session")
    assert current.status_code == 200, current.text
    token = current.json()["data"]["csrf_token"]
    assert isinstance(token, str) and token
    return token


def _logout(client: TestClient, csrf: str) -> None:
    response = client.delete(
        "/api/v1/session",
        headers={"Origin": ORIGIN, CSRF_HEADER: csrf},
    )
    assert response.status_code == 204, response.text


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()
