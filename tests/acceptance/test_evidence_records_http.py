"""Evidence anchors and test records bind to one artifact version.

Corrections append a row that points at the old row. create_app mounts both
routers.
"""

import hashlib
import json

from fastapi.testclient import TestClient
from sqlalchemy import text

from sinuo.api.app import create_app
from sinuo.domain.public_contract import CSRF_HEADER
from sinuo.infrastructure.database import write_uow
from sinuo.infrastructure.domain_models import ArtifactVersion
from tests.acceptance.identity_fixtures import (
    LEAD_ID,
    LEAD_LOGIN,
    LEAD_PASSWORD,
    NOW,
    OTHER_LOGIN,
    OTHER_PASSWORD,
    PROJECT_A,
    PROJECT_B,
    TEAM_LOGIN,
    TEAM_PASSWORD,
    identity_settings,
    seed_identity_world,
)

ORIGIN = "http://127.0.0.1:8000"
ANCHORS = f"/api/v1/projects/{PROJECT_A}/evidence-anchors"
RECORDS = f"/api/v1/projects/{PROJECT_A}/test-records"
ANCHOR_PATH = "/api/v1/projects/{project_id}/evidence-anchors"
RECORD_PATH = "/api/v1/projects/{project_id}/test-records"
V2_ID = "synthetic-released-v2"
EXCERPT = "夜间取物时床沿过高"
CORRECTED_EXCERPT = "夜间取物时床沿被调低"
REGION_EXCERPT = "访谈里提到夜间灯光不足"
TIMECODE_EXCERPT = "访谈第12秒提到床沿"
OBSERVATION = "床沿仍高于膝盖"
CORRECTED_OBSERVATION = "补记：只试了一次"
V2_OBSERVATION = "第二版床沿可用"


def test_create_app_mounts_evidence_and_record_routes(database_url):
    settings = identity_settings(database_url)
    mounted = _methods(create_app(settings))
    assert ("POST", ANCHOR_PATH) in mounted
    assert ("POST", RECORD_PATH) in mounted


def test_anchor_binds_exact_version_and_correction_appends(engine, database_url):
    world, app = _prepare(engine, database_url)
    seeded = _anchor(engine, world.evidence_id)
    started = _revision(engine)
    body = _anchor_body(world.released_version_id, EXCERPT, {"page": 4})
    with TestClient(app) as client:
        anonymous = client.post(
            ANCHORS,
            headers={"Origin": ORIGIN, "Idempotency-Key": "synthetic-anon-anchor"},
            json=body,
        )
        assert anonymous.status_code == 401, anonymous.text
        csrf = _login(client, LEAD_LOGIN, LEAD_PASSWORD)
        forged = client.post(
            ANCHORS,
            headers=_headers(csrf, "synthetic-forged-anchor"),
            json={**body, "created_by_account_id": "forged-author"},
        )
        assert forged.status_code == 422, forged.text
        assert forged.json()["error"]["code"] == "INVALID_SCHEMA"
        unplaced = client.post(
            ANCHORS,
            headers=_headers(csrf, "synthetic-unplaced-anchor"),
            json=_anchor_body(world.released_version_id, EXCERPT, {}),
        )
        assert unplaced.status_code == 422, unplaced.text
        assert unplaced.json()["error"]["code"] == "INVALID_ANCHOR"
        assert unplaced.json()["error"]["details"]["category"] == "locator"
        latest = client.post(
            ANCHORS,
            headers=_headers(csrf, "synthetic-latest-anchor"),
            json=_anchor_body("latest", EXCERPT, {"page": 4}),
        )
        assert latest.status_code == 422, latest.text
        assert latest.json()["error"]["code"] == "INVALID_ANCHOR"
        assert latest.json()["error"]["details"]["category"] == "artifact_version_id"
        missing_csrf = client.post(
            ANCHORS,
            headers={"Origin": ORIGIN, "Idempotency-Key": "synthetic-csrf-anchor"},
            json=body,
        )
        assert missing_csrf.status_code == 403, missing_csrf.text
        assert missing_csrf.json()["error"]["details"]["category"] == "csrf"

        created = client.post(ANCHORS, headers=_headers(csrf, "synthetic-anchor"), json=body)
        assert created.status_code == 201, created.text
        payload = created.json()["data"]
        anchor_id = payload["evidence_anchor_id"]
        assert payload["artifact_version_id"] == world.released_version_id
        assert payload["created_by_account_id"] == LEAD_ID
        assert payload["locator_type"] == "page"
        assert payload["locator"] == {"excerpt": EXCERPT, "page": 4}
        assert payload["excerpt_hash"] == _sha(EXCERPT)
        assert payload["supersedes_id"] is None
        assert payload["sensitivity"] == "released"
        assert payload["visibility"] == "project"
        assert created.json()["meta"]["idempotency_replayed"] is False
        assert created.json()["meta"]["project_revision"] == started + 1

        replayed = client.post(ANCHORS, headers=_headers(csrf, "synthetic-anchor"), json=body)
        assert replayed.status_code == 201, replayed.text
        assert replayed.json()["data"]["evidence_anchor_id"] == anchor_id
        assert replayed.json()["meta"]["idempotency_replayed"] is True
        assert _revision(engine) == started + 1

        conflict = client.post(
            ANCHORS,
            headers=_headers(csrf, "synthetic-anchor"),
            json=_anchor_body(world.released_version_id, CORRECTED_EXCERPT, {"page": 4}),
        )
        assert conflict.status_code == 409, conflict.text
        assert conflict.json()["error"]["code"] == "IDEMPOTENCY_KEY_REUSED"

        foreign = client.post(
            ANCHORS,
            headers=_headers(csrf, "synthetic-foreign-anchor"),
            json=_anchor_body(_foreign_version(engine), EXCERPT, {"page": 4}),
        )
        assert foreign.status_code == 404, foreign.text
        assert foreign.json()["error"]["code"] == "NOT_FOUND_OR_HIDDEN"

        correction = client.post(
            ANCHORS,
            headers=_headers(csrf, "synthetic-anchor-correction"),
            json={
                **_anchor_body(world.released_version_id, CORRECTED_EXCERPT, {"page": 5}),
                "supersedes_id": anchor_id,
            },
        )
        assert correction.status_code == 201, correction.text
        corrected = correction.json()["data"]
        corrected_id = corrected["evidence_anchor_id"]
        assert corrected_id != anchor_id
        assert corrected["supersedes_id"] == anchor_id
        assert corrected["artifact_version_id"] == world.released_version_id
        assert corrected["locator"] == {"excerpt": CORRECTED_EXCERPT, "page": 5}

        retarget = client.post(
            ANCHORS,
            headers=_headers(csrf, "synthetic-anchor-retarget"),
            json={
                **_anchor_body(world.sensitive_version_id, CORRECTED_EXCERPT, {"page": 5}),
                "supersedes_id": anchor_id,
            },
        )
        assert retarget.status_code == 422, retarget.text
        assert retarget.json()["error"]["code"] == "INVALID_ANCHOR"
        assert retarget.json()["error"]["details"]["category"] == "artifact_version_id"
        assert _superseding_anchors(engine, anchor_id) == 1

        crossed = client.post(
            ANCHORS,
            headers=_headers(csrf, "synthetic-anchor-cross-supersede"),
            json={
                **_anchor_body(world.released_version_id, CORRECTED_EXCERPT, {"page": 5}),
                "supersedes_id": world.foreign_evidence_id,
            },
        )
        assert crossed.status_code == 404, crossed.text

        sensitive = client.post(
            ANCHORS,
            headers=_headers(csrf, "synthetic-sensitive-anchor"),
            json=_anchor_body(world.sensitive_version_id, REGION_EXCERPT, {"region": "床头灯"}),
        )
        assert sensitive.status_code == 201, sensitive.text
        sensitive_body = sensitive.json()["data"]
        assert sensitive_body["artifact_version_id"] == world.sensitive_version_id
        assert sensitive_body["locator_type"] == "region"
        assert sensitive_body["locator"]["excerpt"] == REGION_EXCERPT
        assert sensitive_body["locator"]["region"] == "床头灯"
        assert sensitive_body["sensitivity"] == "sensitive"
        assert sensitive_body["visibility"] == "restricted"

        weakened = client.post(
            ANCHORS,
            headers=_headers(csrf, "synthetic-weak-anchor"),
            json={
                **_anchor_body(world.sensitive_version_id, REGION_EXCERPT, {"region": "床头灯"}),
                "visibility": "project",
            },
        )
        assert weakened.status_code == 422, weakened.text
        assert weakened.json()["error"]["code"] == "INVALID_ANCHOR"
        assert weakened.json()["error"]["details"]["category"] == "visibility"

        timed = client.post(
            ANCHORS,
            headers=_headers(csrf, "synthetic-timecode-anchor"),
            json=_anchor_body(
                world.released_version_id,
                TIMECODE_EXCERPT,
                {"timecode": "00:00:12"},
            ),
        )
        assert timed.status_code == 201, timed.text
        assert timed.json()["data"]["locator_type"] == "timecode"
        assert timed.json()["data"]["locator"]["timecode"] == "00:00:12"
        assert timed.json()["data"]["artifact_version_id"] == world.released_version_id

        _logout(client, csrf)
        team = _login(client, TEAM_LOGIN, TEAM_PASSWORD)
        denied = client.post(ANCHORS, headers=_headers(team, "synthetic-team-anchor"), json=body)
        assert denied.status_code == 403, denied.text
        assert denied.json()["error"]["details"]["action"] == "evidence.create"
        _logout(client, team)
        other = _login(client, OTHER_LOGIN, OTHER_PASSWORD)
        hidden = client.post(ANCHORS, headers=_headers(other, "synthetic-other-anchor"), json=body)
        assert hidden.status_code == 404, hidden.text
        assert hidden.json()["error"]["code"] == "NOT_FOUND_OR_HIDDEN"

    original = _anchor(engine, anchor_id)
    follow_up = _anchor(engine, corrected_id)
    assert tuple(original) == (
        world.released_version_id,
        "page",
        json.dumps({"excerpt": EXCERPT, "page": 4}, ensure_ascii=False, separators=(",", ":")),
        _sha(EXCERPT),
        None,
        "project",
        "released",
        LEAD_ID,
        1,
    )
    assert follow_up.supersedes_id == anchor_id
    assert follow_up.artifact_version_id == world.released_version_id
    assert follow_up.excerpt_hash == _sha(CORRECTED_EXCERPT)
    assert json.loads(follow_up.locator_json)["page"] == 5
    assert tuple(_anchor(engine, world.evidence_id)) == tuple(seeded)
    assert _revision(engine) == started + 4
    assert _command_count(engine, "evidence.create") == 4
    audit = _audit(engine, anchor_id)
    assert audit.command == "evidence.create"
    assert audit.actor_id == LEAD_ID
    assert "excerpt" not in json.loads(audit.payload)


def test_record_binds_exact_version_and_correction_appends(engine, database_url):
    world, app = _prepare(engine, database_url)
    _add_version(engine, V2_ID)
    started = _revision(engine)
    assert _record_count(engine) == 0
    conditions = {"task": "夜间取物", "limitation": "单人样本"}
    body = _record_body(world.released_version_id, OBSERVATION, "未通过", conditions)
    with TestClient(app) as client:
        anonymous = client.post(
            RECORDS,
            headers={"Origin": ORIGIN, "Idempotency-Key": "synthetic-anon-record"},
            json=body,
        )
        assert anonymous.status_code == 401, anonymous.text
        csrf = _login(client, LEAD_LOGIN, LEAD_PASSWORD)
        forged = client.post(
            RECORDS,
            headers=_headers(csrf, "synthetic-forged-record"),
            json={**body, "account_id": "forged-actor"},
        )
        assert forged.status_code == 422, forged.text
        assert forged.json()["error"]["code"] == "INVALID_SCHEMA"
        empty = client.post(
            RECORDS,
            headers=_headers(csrf, "synthetic-empty-conditions"),
            json=_record_body(world.released_version_id, OBSERVATION, "未通过", {}),
        )
        assert empty.status_code == 422, empty.text
        assert empty.json()["error"]["code"] == "INVALID_SCHEMA"
        latest = client.post(
            RECORDS,
            headers=_headers(csrf, "synthetic-latest-record"),
            json=_record_body("latest", OBSERVATION, "未通过", conditions),
        )
        assert latest.status_code == 422, latest.text
        assert latest.json()["error"]["code"] == "INVALID_ANCHOR"
        assert latest.json()["error"]["details"]["category"] == "artifact_version_id"
        missing_csrf = client.post(
            RECORDS,
            headers={"Origin": ORIGIN, "Idempotency-Key": "synthetic-csrf-record"},
            json=body,
        )
        assert missing_csrf.status_code == 403, missing_csrf.text
        assert missing_csrf.json()["error"]["details"]["category"] == "csrf"

        created = client.post(RECORDS, headers=_headers(csrf, "synthetic-record-v1"), json=body)
        assert created.status_code == 201, created.text
        payload = created.json()["data"]
        record_id = payload["test_record_id"]
        assert payload["artifact_version_id"] == world.released_version_id
        assert payload["account_id"] == LEAD_ID
        assert payload["conditions"] == conditions
        assert payload["observation"] == OBSERVATION
        assert payload["result"] == "未通过"
        assert payload["source"] == "现场记录"
        assert payload["supersedes_id"] is None
        assert created.json()["meta"]["idempotency_replayed"] is False
        assert payload["project_revision"] == started + 1

        replayed = client.post(RECORDS, headers=_headers(csrf, "synthetic-record-v1"), json=body)
        assert replayed.status_code == 201, replayed.text
        assert replayed.json()["data"]["test_record_id"] == record_id
        assert replayed.json()["meta"]["idempotency_replayed"] is True
        assert _record_count(engine) == 1

        conflict_body = _record_body(
            world.released_version_id,
            CORRECTED_OBSERVATION,
            "未通过",
            conditions,
        )
        conflict = client.post(
            RECORDS,
            headers=_headers(csrf, "synthetic-record-v1"),
            json=conflict_body,
        )
        assert conflict.status_code == 409, conflict.text
        assert conflict.json()["error"]["code"] == "IDEMPOTENCY_KEY_REUSED"

        second = client.post(
            RECORDS,
            headers=_headers(csrf, "synthetic-record-v2"),
            json=_record_body(V2_ID, V2_OBSERVATION, "通过", conditions),
        )
        assert second.status_code == 201, second.text
        second_id = second.json()["data"]["test_record_id"]
        assert second_id != record_id
        assert second.json()["data"]["artifact_version_id"] == V2_ID
        assert second.json()["data"]["supersedes_id"] is None

        correction = client.post(
            RECORDS,
            headers=_headers(csrf, "synthetic-record-correction"),
            json={
                **_record_body(
                    world.released_version_id,
                    CORRECTED_OBSERVATION,
                    "未通过",
                    conditions,
                ),
                "supersedes_id": record_id,
            },
        )
        assert correction.status_code == 201, correction.text
        corrected = correction.json()["data"]
        corrected_id = corrected["test_record_id"]
        assert corrected["supersedes_id"] == record_id
        assert corrected["artifact_version_id"] == world.released_version_id
        assert corrected["observation"] == CORRECTED_OBSERVATION

        retarget = client.post(
            RECORDS,
            headers=_headers(csrf, "synthetic-record-retarget"),
            json={
                **_record_body(V2_ID, CORRECTED_OBSERVATION, "未通过", conditions),
                "supersedes_id": record_id,
            },
        )
        assert retarget.status_code == 422, retarget.text
        assert retarget.json()["error"]["code"] == "INVALID_ANCHOR"
        assert retarget.json()["error"]["details"]["category"] == "artifact_version_id"
        assert _superseding_records(engine, record_id) == 1

        hidden = client.post(
            RECORDS,
            headers=_headers(csrf, "synthetic-foreign-record"),
            json=_record_body(_foreign_version(engine), OBSERVATION, "未通过", conditions),
        )
        assert hidden.status_code == 404, hidden.text
        assert hidden.json()["error"]["code"] == "NOT_FOUND_OR_HIDDEN"

        _logout(client, csrf)
        team = _login(client, TEAM_LOGIN, TEAM_PASSWORD)
        denied = client.post(RECORDS, headers=_headers(team, "synthetic-team-record"), json=body)
        assert denied.status_code == 403, denied.text
        assert denied.json()["error"]["details"]["action"] == "test.record"
        _logout(client, team)
        other = _login(client, OTHER_LOGIN, OTHER_PASSWORD)
        outsider = client.post(
            RECORDS,
            headers=_headers(other, "synthetic-other-record"),
            json=body,
        )
        assert outsider.status_code == 404, outsider.text

    original = _record(engine, record_id)
    follow_up = _record(engine, corrected_id)
    moved = _record(engine, second_id)
    assert original.artifact_version_id == world.released_version_id
    assert original.account_id == LEAD_ID
    assert original.observation == OBSERVATION
    assert original.result == "未通过"
    assert original.supersedes_id is None
    assert original.project_revision == started + 1
    assert json.loads(original.conditions_json) == conditions
    assert follow_up.supersedes_id == record_id
    assert follow_up.artifact_version_id == world.released_version_id
    assert follow_up.observation == CORRECTED_OBSERVATION
    assert moved.artifact_version_id == V2_ID
    assert moved.supersedes_id is None
    assert moved.observation == V2_OBSERVATION
    assert _record_count(engine) == 3
    assert _revision(engine) == started + 3
    assert _command_count(engine, "test.record") == 3
    audit = _audit(engine, record_id)
    assert audit.command == "test.record"
    assert audit.actor_id == LEAD_ID
    stored = json.loads(audit.payload)
    assert stored["artifact_version_id"] == world.released_version_id
    assert "observation" not in stored
    assert "result" not in stored


def _prepare(engine, database_url):
    settings = identity_settings(database_url)
    world = seed_identity_world(engine, settings)
    return world, create_app(settings)


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


def _headers(csrf: str, key: str) -> dict[str, str]:
    return {"Origin": ORIGIN, CSRF_HEADER: csrf, "Idempotency-Key": key}


def _anchor_body(version_id: str, excerpt: str, locator: dict) -> dict:
    return {"artifact_version_id": version_id, "excerpt": excerpt, "locator": locator}


def _record_body(version_id: str, observation: str, result: str, conditions: dict) -> dict:
    return {
        "artifact_version_id": version_id,
        "conditions": conditions,
        "observation": observation,
        "result": result,
        "source": "现场记录",
    }


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _add_version(engine, version_id: str) -> None:
    with write_uow(engine) as session:
        session.add(
            ArtifactVersion(
                id=version_id,
                project_id=PROJECT_A,
                artifact_id="synthetic-artifact-released",
                version_no=2,
                content_hash=_sha(version_id),
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


def _foreign_version(engine) -> str:
    with engine.connect() as connection:
        value = connection.execute(
            text("SELECT id FROM artifact_versions WHERE project_id = :project_id"),
            {"project_id": PROJECT_B},
        ).scalar_one()
    return str(value)


def _revision(engine) -> int:
    with engine.connect() as connection:
        value = connection.execute(
            text("SELECT revision FROM projects WHERE id = :project_id"),
            {"project_id": PROJECT_A},
        ).scalar_one()
    return int(value)


def _anchor(engine, anchor_id: str):
    with engine.connect() as connection:
        return connection.execute(
            text(
                """
                SELECT artifact_version_id, locator_type, locator_json, excerpt_hash,
                       supersedes_id, visibility, sensitivity, created_by_account_id,
                       schema_version
                FROM evidence_anchors
                WHERE id = :anchor_id
                """
            ),
            {"anchor_id": anchor_id},
        ).one()


def _record(engine, record_id: str):
    with engine.connect() as connection:
        return connection.execute(
            text(
                """
                SELECT account_id, artifact_version_id, conditions_json, observation, result,
                       source, supersedes_id, project_revision
                FROM test_records
                WHERE id = :record_id
                """
            ),
            {"record_id": record_id},
        ).one()


def _audit(engine, subject_id: str):
    with engine.connect() as connection:
        return connection.execute(
            text(
                """
                SELECT command, actor_id, payload
                FROM audit_events
                WHERE subject_id = :subject_id
                """
            ),
            {"subject_id": subject_id},
        ).one()


def _command_count(engine, command: str) -> int:
    with engine.connect() as connection:
        value = connection.execute(
            text(
                """
                SELECT count(*)
                FROM audit_events
                WHERE project_id = :project_id AND command = :command
                """
            ),
            {"project_id": PROJECT_A, "command": command},
        ).scalar_one()
    return int(value)


def _superseding_anchors(engine, anchor_id: str) -> int:
    with engine.connect() as connection:
        value = connection.execute(
            text("SELECT count(*) FROM evidence_anchors WHERE supersedes_id = :anchor_id"),
            {"anchor_id": anchor_id},
        ).scalar_one()
    return int(value)


def _superseding_records(engine, record_id: str) -> int:
    with engine.connect() as connection:
        value = connection.execute(
            text("SELECT count(*) FROM test_records WHERE supersedes_id = :record_id"),
            {"record_id": record_id},
        ).scalar_one()
    return int(value)


def _record_count(engine) -> int:
    with engine.connect() as connection:
        value = connection.execute(text("SELECT count(*) FROM test_records")).scalar_one()
    return int(value)


def test_anchor_rebuilds_evidence_index(engine, database_url):
    """建锚点后立即重建证据全文索引，使证据可被 search_evidence 命中。"""
    world, app = _prepare(engine, database_url)
    body = _anchor_body(world.released_version_id, EXCERPT, {"page": 4})
    with TestClient(app) as client:
        csrf = _login(client, LEAD_LOGIN, LEAD_PASSWORD)
        created = client.post(ANCHORS, headers=_headers(csrf, "synthetic-anchor-index"), json=body)
        assert created.status_code == 201, created.text
        anchor_id = created.json()["data"]["evidence_anchor_id"]
    with engine.connect() as connection:
        indexed = connection.execute(
            text(
                "SELECT count(*) FROM evidence_excerpt_index "
                "WHERE anchor_id = :anchor_id AND artifact_version_id = :version_id"
            ),
            {"anchor_id": anchor_id, "version_id": world.released_version_id},
        ).scalar_one()
    assert int(indexed) == 1
