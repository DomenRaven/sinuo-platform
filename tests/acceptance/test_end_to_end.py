"""Longitudinal path over the HTTP that is mounted today.

Upload, evidence anchors, test records, and one agent run are called.
The run injects ``FakeModelProvider``. Export generation calls
``drain_export_jobs``. Stage-blank confirmation, 3D, and a live model stay
unexecuted. This file does not sign G0–G4 and does not insert rows to fill
those gaps.
"""

import hashlib
import io
import json
import sqlite3
import zipfile
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import text

from sinuo.agents.fake import FakeModelProvider
from sinuo.api.app import create_app
from sinuo.application.exports.drain import drain_export_jobs
from sinuo.application.recovery import backup_snapshot, restore_snapshot
from sinuo.domain.public_contract import CSRF_HEADER
from tests.acceptance.identity_fixtures import (
    LEAD_ID,
    LEAD_LOGIN,
    LEAD_PASSWORD,
    identity_settings,
    seed_identity_world,
)

ORIGIN = "http://127.0.0.1:8000"
TOPIC = "夜间取物辅助产品"
SYNTHETIC = "synthetic=true"
NODE_ID = "synthetic-night-node"
OPINION = "synthetic=true。夜间取物辅助产品的合成审阅，不是真实用户研究。"
WORK_NAME = "synthetic-夜间取物-原件"
BYTES_V1 = "synthetic=true。夜间取物原件第一版".encode()
BYTES_V2 = "synthetic=true。夜间取物原件第二版".encode()
EXCERPT = "synthetic=true。夜间取物时床沿过高"
OBSERVATION = "synthetic=true。床沿仍高于膝盖"
CONDITIONS = {"task": "夜间取物", "limitation": "单人样本"}

# These steps still have no honest call on this tree. The test must not
# insert a stage-blank candidate, prepare a GLB, or send a live model.
NOT_EXECUTED: tuple[tuple[str, str], ...] = (
    (
        "预览并确认阶段空白页",
        "启动运行留下的候选动作是 draft_decision，不是阶段空白页。"
        "不插入 create_page_from_versions 候选，也不调用预览或确认。",
    ),
    (
        "3D",
        "用户决定不做 GLB 和其他 3D。本测试不准备受控 GLB，也不跑转换器。",
    ),
    (
        "真实模型",
        "live_model 本次不发。启动运行注入 FakeModelProvider，不调用 DeepSeek，"
        "也不把这次假模型运行写成模型效果。",
    ),
)


def _refuse(*_args, **_kwargs):
    raise AssertionError("live model")


def _block_live_model(monkeypatch) -> None:
    monkeypatch.setattr("sinuo.agents.deepseek.DeepSeekAdapter.__init__", _refuse)
    monkeypatch.setattr("sinuo.agents.deepseek.DeepSeekAdapter.from_settings", _refuse)
    monkeypatch.setattr("sinuo.agents.deepseek.DeepSeekAdapter.execute", _refuse)


def _headers(csrf: str, key: str) -> dict[str, str]:
    return {"Origin": ORIGIN, CSRF_HEADER: csrf, "Idempotency-Key": key}


def _sha(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _ticket(payload: bytes) -> dict[str, object]:
    return {
        "name": WORK_NAME,
        "media_type": "application/octet-stream",
        "size": len(payload),
        "declared_hash": _sha(payload),
        "license": "synthetic-license",
        "sensitivity": "released",
        "ai_processing_allowed": 0,
    }


def _upload(client: TestClient, csrf: str, project_id: str, key: str, payload: bytes):
    opened = client.post(
        f"/api/v1/projects/{project_id}/artifacts/uploads",
        headers=_headers(csrf, f"{key}-open"),
        json=_ticket(payload),
    )
    assert opened.status_code == 201, opened.text
    ticket_id = opened.json()["data"]["ticket_id"]
    staged = client.put(
        f"/api/v1/uploads/{ticket_id}/content",
        headers=_headers(csrf, f"{key}-put"),
        content=payload,
    )
    assert staged.status_code == 200, staged.text
    done = client.post(
        f"/api/v1/uploads/{ticket_id}/complete",
        headers=_headers(csrf, f"{key}-complete"),
        json={"declared_hash": _sha(payload)},
    )
    assert done.status_code == 201, done.text
    return opened, done


def _scalar(engine, sql: str, params: dict[str, object]):
    with engine.connect() as connection:
        return connection.execute(text(sql), params).scalar_one()


def _one(engine, sql: str, params: dict[str, object]):
    with engine.connect() as connection:
        return connection.execute(text(sql), params).one()


def _all(engine, sql: str, params: dict[str, object]):
    with engine.connect() as connection:
        return connection.execute(text(sql), params).all()


def _sqlite_one(path: Path, sql: str, params: tuple[object, ...]):
    connection = sqlite3.connect(path)
    try:
        row = connection.execute(sql, params).fetchone()
    finally:
        connection.close()
    assert row is not None
    return row


def _sqlite_all(path: Path, sql: str, params: tuple[object, ...]):
    connection = sqlite3.connect(path)
    try:
        rows = connection.execute(sql, params).fetchall()
    finally:
        connection.close()
    return rows


def _database_path(engine) -> Path:
    database = engine.url.database
    if not isinstance(database, str) or database == "":
        raise RuntimeError("this slice needs a file database")
    return Path(database)


def _unzip(payload: bytes) -> dict[str, bytes]:
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        return {name: archive.read(name) for name in archive.namelist()}


def _manifest_item(manifest: dict, kind: str, item_id: str) -> dict:
    found = [item for item in manifest["items"] if item["kind"] == kind and item["id"] == item_id]
    assert len(found) == 1
    return found[0]


def _object_file(root: Path, object_key: str) -> Path:
    prefix = "objects/"
    assert object_key.startswith(prefix)
    return root.joinpath(*object_key.removeprefix(prefix).split("/"))


def test_longitudinal_slice_walks_existing_http_and_records_gaps(
    engine, database_url, tmp_path, monkeypatch
):
    _block_live_model(monkeypatch)
    assert [name for name, _reason in NOT_EXECUTED] == [
        "预览并确认阶段空白页",
        "3D",
        "真实模型",
    ]
    for _name, reason in NOT_EXECUTED:
        assert reason.strip()

    assert _scalar(engine, "SELECT version_num FROM alembic_version", {}) == "0010_evidence_index"
    settings = identity_settings(database_url, model_enabled=False)
    seed_identity_world(engine, settings)
    sessions_before = _scalar(
        engine,
        """
        SELECT COUNT(*) FROM sessions
        WHERE account_id = :account_id AND revoked_at IS NULL
        """,
        {"account_id": LEAD_ID},
    )
    uploads = tmp_path / "uploads"
    objects = tmp_path / "objects"
    derived = tmp_path / "derived"
    exports = tmp_path / "exports"
    for directory in (uploads, objects, derived, exports):
        directory.mkdir()
    provider = FakeModelProvider()
    assert type(provider) is FakeModelProvider
    app = create_app(settings)
    app.state.agent_provider = provider
    app.state.uploads_root = uploads
    app.state.objects_root = objects
    app.state.derived_root = derived
    app.state.exports_root = exports
    with TestClient(app) as client:
        login = client.post(
            "/api/v1/session",
            headers={"Origin": ORIGIN},
            json={"login_name": LEAD_LOGIN, "password": LEAD_PASSWORD},
        )
        assert login.status_code == 204, login.text
        current = client.get("/api/v1/session")
        assert current.status_code == 200, current.text
        account_id = current.json()["data"]["account_id"]
        csrf = current.json()["data"]["csrf_token"]
        assert account_id == LEAD_ID
        assert isinstance(csrf, str) and csrf
        assert (
            _scalar(
                engine,
                """
                SELECT COUNT(*) FROM sessions
                WHERE account_id = :account_id AND revoked_at IS NULL
                """,
                {"account_id": LEAD_ID},
            )
            == sessions_before + 1
        )

        created = client.post(
            "/api/v1/projects",
            headers=_headers(csrf, "synthetic-e2e-project"),
            json={
                "title": TOPIC,
                "brief": SYNTHETIC,
                "page_title": "synthetic-夜间取物-第一页",
            },
        )
        assert created.status_code == 201, created.text
        project = created.json()["data"]
        project_id = project["project_id"]
        stage_id = project["stage_id"]
        first_page_id = project["page_id"]
        first_version_id = project["page_version_id"]
        assert project["project_revision"] == 1
        assert created.json()["meta"]["idempotency_replayed"] is False
        stored_project = _one(
            engine,
            "SELECT title, brief, revision FROM projects WHERE id = :project_id",
            {"project_id": project_id},
        )
        assert stored_project.title == TOPIC
        assert stored_project.brief == SYNTHETIC
        assert stored_project.revision == 1
        assert (
            _scalar(
                engine,
                "SELECT project_id FROM stages WHERE id = :stage_id",
                {"stage_id": stage_id},
            )
            == project_id
        )
        assert (
            _scalar(
                engine,
                "SELECT project_id FROM audit_events WHERE id = :audit_id",
                {"audit_id": project["audit_event_id"]},
            )
            == project_id
        )
        first_hash = _scalar(
            engine,
            "SELECT content_hash FROM page_versions WHERE id = :version_id",
            {"version_id": first_version_id},
        )
        assert (
            _one(
                engine,
                """
                SELECT stage_id, head_version_id, title
                FROM pages WHERE id = :page_id
                """,
                {"page_id": first_page_id},
            )
            == (stage_id, first_version_id, "synthetic-夜间取物-第一页")
        )

        parallel = client.post(
            f"/api/v1/projects/{project_id}/pages",
            headers=_headers(csrf, "synthetic-e2e-parallel"),
            json={
                "stage_id": stage_id,
                "title": "synthetic-夜间取物-平行页",
                "expected_revision": 1,
            },
        )
        assert parallel.status_code == 201, parallel.text
        second = parallel.json()["data"]
        second_page_id = second["page_id"]
        second_version_id = second["page_version_id"]
        assert second_page_id != first_page_id
        assert second_version_id != first_version_id
        assert second["project_revision"] == 2
        assert (
            _one(
                engine,
                """
                SELECT project_id, stage_id, head_version_id, title
                FROM pages WHERE id = :page_id
                """,
                {"page_id": second_page_id},
            )
            == (project_id, stage_id, second_version_id, "synthetic-夜间取物-平行页")
        )
        assert (
            _scalar(
                engine,
                "SELECT revision FROM projects WHERE id = :project_id",
                {"project_id": project_id},
            )
            == 2
        )
        assert (
            _scalar(
                engine,
                "SELECT COUNT(*) FROM page_edges WHERE project_id = :project_id",
                {"project_id": project_id},
            )
            == 0
        )
        assert (
            _scalar(
                engine,
                "SELECT project_id FROM audit_events WHERE id = :audit_id",
                {"audit_id": second["audit_event_id"]},
            )
            == project_id
        )

        opened_v1, done_v1 = _upload(
            client, csrf, project_id, "synthetic-e2e-upload-v1", BYTES_V1
        )
        first_ticket = opened_v1.json()["data"]
        registered_v1 = done_v1.json()["data"]
        artifact_id = first_ticket["artifact_id"]
        version_v1 = registered_v1["artifact_version_id"]
        hash_v1 = _sha(BYTES_V1)
        assert first_ticket["artifact_created"] is True
        assert first_ticket["project_id"] == project_id
        assert first_ticket["status"] == "open"
        assert opened_v1.json()["meta"]["idempotency_replayed"] is False
        assert registered_v1["artifact_id"] == artifact_id
        assert registered_v1["version_no"] == 1
        assert registered_v1["content_hash"] == hash_v1
        assert registered_v1["project_revision"] == 3
        assert registered_v1["preview_generated"] is False
        assert registered_v1["derivative_status"] == "pending"
        assert done_v1.json()["meta"]["idempotency_replayed"] is False
        assert str(uploads) not in done_v1.text
        assert str(objects) not in done_v1.text
        assert (
            _one(
                engine,
                """
                SELECT project_id, title FROM artifacts WHERE id = :artifact_id
                """,
                {"artifact_id": artifact_id},
            )
            == (project_id, WORK_NAME)
        )
        assert (
            _one(
                engine,
                """
                SELECT artifact_id, version_no, content_hash, size_bytes
                FROM artifact_versions WHERE id = :version_id
                """,
                {"version_id": version_v1},
            )
            == (artifact_id, 1, hash_v1, len(BYTES_V1))
        )
        loaded_v1 = client.get(f"/api/v1/artifact-versions/{version_v1}/content")
        assert loaded_v1.status_code == 200, loaded_v1.text
        assert loaded_v1.content == BYTES_V1
        assert hashlib.sha256(loaded_v1.content).hexdigest() == hash_v1

        anchored = client.post(
            f"/api/v1/projects/{project_id}/evidence-anchors",
            headers=_headers(csrf, "synthetic-e2e-anchor"),
            json={
                "artifact_version_id": version_v1,
                "excerpt": EXCERPT,
                "locator": {"page": 4},
            },
        )
        assert anchored.status_code == 201, anchored.text
        anchor = anchored.json()["data"]
        anchor_id = anchor["evidence_anchor_id"]
        excerpt_hash = hashlib.sha256(EXCERPT.encode("utf-8")).hexdigest()
        assert anchor["project_id"] == project_id
        assert anchor["artifact_version_id"] == version_v1
        assert anchor["created_by_account_id"] == LEAD_ID
        assert anchor["locator_type"] == "page"
        assert anchor["locator"] == {"excerpt": EXCERPT, "page": 4}
        assert anchor["excerpt_hash"] == excerpt_hash
        assert anchor["supersedes_id"] is None
        assert anchor["project_revision"] == 4
        assert anchored.json()["meta"]["idempotency_replayed"] is False
        assert (
            _one(
                engine,
                """
                SELECT project_id, artifact_version_id, locator_type, excerpt_hash, supersedes_id
                FROM evidence_anchors WHERE id = :anchor_id
                """,
                {"anchor_id": anchor_id},
            )
            == (project_id, version_v1, "page", excerpt_hash, None)
        )
        assert (
            _scalar(
                engine,
                "SELECT project_id FROM audit_events WHERE id = :audit_id",
                {"audit_id": anchor["audit_event_id"]},
            )
            == project_id
        )

        draft_body = {
            "schema_version": 1,
            "nodes": [{"node_id": NODE_ID}],
            "edges": [],
        }
        drafted = client.patch(
            f"/api/v1/pages/{first_page_id}/draft",
            headers=_headers(csrf, "synthetic-e2e-draft"),
            json={
                "base_version_id": first_version_id,
                "expected_revision": 1,
                "content": draft_body,
            },
        )
        assert drafted.status_code == 200, drafted.text
        draft = drafted.json()["data"]
        assert draft["page_id"] == first_page_id
        assert draft["content_revision"] == 2
        draft_hash = draft["content_hash"]
        assert (
            _one(
                engine,
                """
                SELECT base_version_id, content_revision, content_hash
                FROM page_drafts WHERE page_id = :page_id
                """,
                {"page_id": first_page_id},
            )
            == (first_version_id, 2, draft_hash)
        )
        assert (
            _scalar(
                engine,
                "SELECT project_id FROM audit_events WHERE id = :audit_id",
                {"audit_id": draft["audit_event_id"]},
            )
            == project_id
        )
        assert (
            _scalar(
                engine,
                "SELECT content_hash FROM page_versions WHERE id = :version_id",
                {"version_id": first_version_id},
            )
            == first_hash
        )

        checkpoint = client.post(
            f"/api/v1/pages/{first_page_id}/checkpoints",
            headers=_headers(csrf, "synthetic-e2e-checkpoint"),
            json={
                "expected_head_version_id": first_version_id,
                "expected_content_revision": 2,
                "expected_content_hash": draft_hash,
            },
        )
        assert checkpoint.status_code == 201, checkpoint.text
        saved = checkpoint.json()["data"]
        checkpoint_version_id = saved["page_version_id"]
        assert saved["version_no"] == 2
        assert saved["project_revision"] == 5
        assert checkpoint_version_id != first_version_id
        assert (
            _one(
                engine,
                """
                SELECT page_id, version_no, parent_version_id, content_hash
                FROM page_versions WHERE id = :version_id
                """,
                {"version_id": checkpoint_version_id},
            )
            == (first_page_id, 2, first_version_id, draft_hash)
        )
        assert (
            _scalar(
                engine,
                "SELECT head_version_id FROM pages WHERE id = :page_id",
                {"page_id": first_page_id},
            )
            == checkpoint_version_id
        )
        assert (
            _one(
                engine,
                """
                SELECT base_version_id, content_revision, content_hash
                FROM page_drafts WHERE page_id = :page_id
                """,
                {"page_id": first_page_id},
            )
            == (checkpoint_version_id, 3, draft_hash)
        )
        assert (
            _scalar(
                engine,
                "SELECT revision FROM projects WHERE id = :project_id",
                {"project_id": project_id},
            )
            == 5
        )
        assert (
            _scalar(
                engine,
                "SELECT content_hash FROM page_versions WHERE id = :version_id",
                {"version_id": first_version_id},
            )
            == first_hash
        )
        assert (
            _scalar(
                engine,
                "SELECT project_id FROM audit_events WHERE id = :audit_id",
                {"audit_id": saved["audit_event_id"]},
            )
            == project_id
        )

        started = client.post(
            f"/api/v1/projects/{project_id}/agent-runs",
            headers=_headers(csrf, "synthetic-e2e-run"),
            json={
                "intent": "问下一步",
                "input_mode": "confirmed",
                "page_version_id": checkpoint_version_id,
            },
        )
        assert started.status_code == 202, started.text
        assert started.headers["cache-control"] == "no-store"
        assert started.json()["meta"]["idempotency_replayed"] is False
        assert "deepseek" not in started.text.lower()
        assert "sk-" not in started.text
        run_id = started.json()["data"]["run_id"]
        snapshot_id = started.json()["data"]["snapshot_id"]
        assert started.json()["data"]["events_url"] == f"/api/v1/agent-runs/{run_id}/events"
        assert type(app.state.agent_provider) is FakeModelProvider
        assert (
            _one(
                engine,
                """
                SELECT project_id, initiated_by_account_id, execution_status, accepting_results
                FROM agent_runs WHERE id = :run_id
                """,
                {"run_id": run_id},
            )
            == (project_id, LEAD_ID, "completed", 0)
        )
        tasks = _all(
            engine,
            """
            SELECT role, status FROM run_tasks
            WHERE run_id = :run_id
            ORDER BY role
            """,
            {"run_id": run_id},
        )
        assert [(row.role, row.status) for row in tasks] == [
            ("critic", "succeeded"),
            ("evidence", "succeeded"),
        ]
        scope = json.loads(
            _scalar(
                engine,
                """
                SELECT authorization_scope_json FROM context_snapshots WHERE id = :snapshot_id
                """,
                {"snapshot_id": snapshot_id},
            )
        )
        assert scope["query"] == "问下一步"
        assert scope["question"] == ""
        assert scope["page_version_id"] == checkpoint_version_id
        assert (
            _one(
                engine,
                """
                SELECT project_id, run_id, status, action, page_mode
                FROM candidates WHERE project_id = :project_id
                """,
                {"project_id": project_id},
            )
            == (project_id, run_id, "pending", "draft_decision", None)
        )
        assert (
            _scalar(
                engine,
                "SELECT COUNT(*) FROM candidate_previews WHERE project_id = :project_id",
                {"project_id": project_id},
            )
            == 0
        )
        assert (
            _scalar(
                engine,
                "SELECT COUNT(*) FROM pages WHERE project_id = :project_id",
                {"project_id": project_id},
            )
            == 2
        )
        assert (
            _scalar(
                engine,
                "SELECT revision FROM projects WHERE id = :project_id",
                {"project_id": project_id},
            )
            == 5
        )

        recorded = client.post(
            f"/api/v1/projects/{project_id}/test-records",
            headers=_headers(csrf, "synthetic-e2e-record"),
            json={
                "artifact_version_id": version_v1,
                "conditions": CONDITIONS,
                "observation": OBSERVATION,
                "result": "未通过",
                "source": "现场记录",
            },
        )
        assert recorded.status_code == 201, recorded.text
        record = recorded.json()["data"]
        record_id = record["test_record_id"]
        assert record["project_id"] == project_id
        assert record["account_id"] == LEAD_ID
        assert record["artifact_version_id"] == version_v1
        assert record["conditions"] == CONDITIONS
        assert record["observation"] == OBSERVATION
        assert record["result"] == "未通过"
        assert record["source"] == "现场记录"
        assert record["supersedes_id"] is None
        assert record["project_revision"] == 6
        assert recorded.json()["meta"]["idempotency_replayed"] is False
        assert (
            _one(
                engine,
                """
                SELECT project_id, account_id, artifact_version_id, observation, result,
                       supersedes_id
                FROM test_records WHERE id = :record_id
                """,
                {"record_id": record_id},
            )
            == (project_id, LEAD_ID, version_v1, OBSERVATION, "未通过", None)
        )
        assert json.loads(
            _scalar(
                engine,
                "SELECT conditions_json FROM test_records WHERE id = :record_id",
                {"record_id": record_id},
            )
        ) == CONDITIONS

        opened_v2, done_v2 = _upload(
            client, csrf, project_id, "synthetic-e2e-upload-v2", BYTES_V2
        )
        registered_v2 = done_v2.json()["data"]
        version_v2 = registered_v2["artifact_version_id"]
        hash_v2 = _sha(BYTES_V2)
        assert opened_v2.json()["data"]["artifact_created"] is False
        assert opened_v2.json()["data"]["artifact_id"] == artifact_id
        assert registered_v2["artifact_id"] == artifact_id
        assert registered_v2["version_no"] == 2
        assert registered_v2["content_hash"] == hash_v2
        assert registered_v2["content_hash"] != hash_v1
        assert registered_v2["artifact_version_id"] != version_v1
        assert registered_v2["project_revision"] == 7
        assert registered_v2["preview_generated"] is False
        assert registered_v2["derivative_status"] == "pending"
        assert (
            _all(
                engine,
                """
                SELECT version_no, content_hash
                FROM artifact_versions
                WHERE artifact_id = :artifact_id
                ORDER BY version_no
                """,
                {"artifact_id": artifact_id},
            )
            == [(1, hash_v1), (2, hash_v2)]
        )
        assert (
            _scalar(
                engine,
                "SELECT artifact_version_id FROM test_records WHERE id = :record_id",
                {"record_id": record_id},
            )
            == version_v1
        )
        assert (
            _scalar(
                engine,
                "SELECT COUNT(*) FROM test_records WHERE artifact_version_id = :version_id",
                {"version_id": version_v2},
            )
            == 0
        )
        assert (
            _scalar(
                engine,
                "SELECT content_hash FROM artifact_versions WHERE id = :version_id",
                {"version_id": version_v1},
            )
            == hash_v1
        )
        still_v1 = client.get(f"/api/v1/artifact-versions/{version_v1}/content")
        loaded_v2 = client.get(f"/api/v1/artifact-versions/{version_v2}/content")
        assert still_v1.status_code == 200, still_v1.text
        assert loaded_v2.status_code == 200, loaded_v2.text
        assert still_v1.content == BYTES_V1
        assert loaded_v2.content == BYTES_V2

        history = client.get(f"/api/v1/pages/{first_page_id}/history")
        assert history.status_code == 200, history.text
        versions = {
            (item["page_version_id"], item["version_no"], item["content_hash"])
            for item in history.json()["data"]["versions"]
        }
        assert (first_version_id, 1, first_hash) in versions
        assert (checkpoint_version_id, 2, draft_hash) in versions
        assert (
            _scalar(
                engine,
                "SELECT head_version_id FROM pages WHERE id = :page_id",
                {"page_id": first_page_id},
            )
            == checkpoint_version_id
        )
        assert (
            _scalar(
                engine,
                "SELECT revision FROM projects WHERE id = :project_id",
                {"project_id": project_id},
            )
            == 7
        )

        branched = client.post(
            f"/api/v1/pages/{first_page_id}/branches",
            headers=_headers(csrf, "synthetic-e2e-branch"),
            json={
                "source_version_id": checkpoint_version_id,
                "title": "synthetic-夜间取物-历史分支",
            },
        )
        assert branched.status_code == 201, branched.text
        branch = branched.json()["data"]
        branch_page_id = branch["page_id"]
        branch_version_id = branch["page_version_id"]
        assert branch["source_version_id"] == checkpoint_version_id
        assert branch_page_id not in {first_page_id, second_page_id}
        assert (
            _scalar(
                engine,
                "SELECT head_version_id FROM pages WHERE id = :page_id",
                {"page_id": first_page_id},
            )
            == checkpoint_version_id
        )
        assert (
            _one(
                engine,
                """
                SELECT project_id, stage_id, head_version_id, title
                FROM pages WHERE id = :page_id
                """,
                {"page_id": branch_page_id},
            )
            == (project_id, stage_id, branch_version_id, "synthetic-夜间取物-历史分支")
        )
        assert (
            _one(
                engine,
                """
                SELECT kind, source_version_id, target_version_id
                FROM page_edges WHERE project_id = :project_id
                """,
                {"project_id": project_id},
            )
            == ("branches_from", checkpoint_version_id, branch_version_id)
        )
        assert (
            _scalar(
                engine,
                "SELECT revision FROM projects WHERE id = :project_id",
                {"project_id": project_id},
            )
            == 8
        )

        opinion = client.post(
            f"/api/v1/projects/{project_id}/review-opinions",
            headers=_headers(csrf, "synthetic-e2e-opinion"),
            json={
                "page_version_id": checkpoint_version_id,
                "node_id": NODE_ID,
                "body": OPINION,
                "visibility": "project",
            },
        )
        assert opinion.status_code == 201, opinion.text
        opinion_body = opinion.json()["data"]
        opinion_id = opinion_body["opinion_id"]
        assert opinion_body["project_id"] == project_id
        assert opinion_body["author_account_id"] == LEAD_ID
        assert opinion_body["page_version_id"] == checkpoint_version_id
        assert opinion_body["node_id"] == NODE_ID
        assert opinion_body["body"] == OPINION
        assert opinion_body["revision"] == 1
        assert opinion_body["project_revision"] == 9
        assert opinion.json()["meta"]["idempotency_replayed"] is False
        assert (
            _one(
                engine,
                """
                SELECT project_id, author_account_id, page_version_id, node_id, body, revision
                FROM review_opinions WHERE id = :opinion_id
                """,
                {"opinion_id": opinion_id},
            )
            == (project_id, LEAD_ID, checkpoint_version_id, NODE_ID, OPINION, 1)
        )
        assert (
            _scalar(
                engine,
                "SELECT project_id FROM audit_events WHERE id = :audit_id",
                {"audit_id": opinion_body["audit_event_id"]},
            )
            == project_id
        )

        manifest = {
            "page_version_ids": [checkpoint_version_id],
            "artifact_version_ids": [version_v1, version_v2],
            "evidence_anchor_ids": [anchor_id],
        }
        exported = client.post(
            f"/api/v1/projects/{project_id}/exports",
            headers=_headers(csrf, "synthetic-e2e-export"),
            json={
                "format": "markdown",
                "include_originals": True,
                "source_manifest": manifest,
                "confirmed_scope": manifest,
            },
        )
        assert exported.status_code == 202, exported.text
        export_body = exported.json()["data"]
        export_id = export_body["export_job_id"]
        snapshot_id = export_body["input_snapshot_id"]
        assert export_body["status"] == "queued"
        assert export_body["latest"] is False
        assert export_body["format"] == "markdown"
        assert export_body["source_manifest"]["page_version_ids"] == [checkpoint_version_id]
        assert export_body["source_manifest"]["artifact_version_ids"] == [version_v1, version_v2]
        assert export_body["source_manifest"]["evidence_anchor_ids"] == [anchor_id]
        assert exported.json()["meta"]["idempotency_replayed"] is False
        assert (
            _one(
                engine,
                """
                SELECT project_id, job_kind, status, subject_id
                FROM jobs WHERE id = :export_id
                """,
                {"export_id": export_id},
            )
            == (project_id, "export", "queued", snapshot_id)
        )
        assert (
            _scalar(
                engine,
                "SELECT project_id FROM audit_events WHERE id = :audit_id",
                {"audit_id": export_body["audit_event_id"]},
            )
            == project_id
        )
        assert (
            _scalar(
                engine,
                "SELECT revision FROM projects WHERE id = :project_id",
                {"project_id": project_id},
            )
            == 9
        )
        viewed = client.get(f"/api/v1/exports/{export_id}")
        assert viewed.status_code == 200, viewed.text
        assert viewed.json()["data"]["export_job_id"] == export_id
        assert viewed.json()["data"]["input_snapshot_id"] == snapshot_id
        assert viewed.json()["data"]["status"] == "queued"
        assert viewed.json()["data"]["latest"] is False
        withheld = client.get(f"/api/v1/exports/{export_id}/content")
        assert withheld.status_code == 404, withheld.text
        assert withheld.json()["error"]["code"] == "NOT_FOUND_OR_HIDDEN"
        assert OPINION not in withheld.text
        assert TOPIC not in withheld.text
        assert BYTES_V1 not in withheld.content
        assert BYTES_V2 not in withheld.content
        runs = drain_export_jobs(engine, exports, objects, derived)
        assert len(runs) == 1
        assert runs[0].export_id == export_id
        assert runs[0].status == "succeeded"
        assert isinstance(runs[0].output_ref, str) and runs[0].output_ref
        finished = _one(
            engine,
            """
            SELECT status, output_ref, subject_id
            FROM jobs WHERE id = :export_id
            """,
            {"export_id": export_id},
        )
        assert finished.status == "succeeded"
        assert finished.output_ref == runs[0].output_ref
        assert finished.subject_id == snapshot_id
        packed_response = client.get(f"/api/v1/exports/{export_id}/content")
        assert packed_response.status_code == 200, packed_response.text
        assert packed_response.headers["content-type"].startswith("application/zip")
        packed = _unzip(packed_response.content)
        packed_manifest = json.loads(packed["manifest.json"].decode("utf-8"))
        report = packed["report.md"].decode("utf-8")
        assert packed_manifest["input_snapshot_id"] == snapshot_id
        assert packed_manifest["latest"] is False
        assert packed_manifest["include_originals"] is True
        assert _sha(packed["manifest.json"]) == finished.output_ref
        assert _manifest_item(packed_manifest, "page_version", checkpoint_version_id)["status"] == (
            "included"
        )
        for version_id, payload, digest in (
            (version_v1, BYTES_V1, hash_v1),
            (version_v2, BYTES_V2, hash_v2),
        ):
            item = _manifest_item(packed_manifest, "artifact_version", version_id)
            assert item["original"]["status"] == "included"
            assert item["original"]["sha256"] == digest
            assert item["thumbnail"]["status"] == "missing"
            assert packed[f"originals/{version_id}"] == payload
        excerpt_item = _manifest_item(packed_manifest, "evidence_anchor", anchor_id)
        assert excerpt_item["excerpt"]["status"] == "included"
        assert EXCERPT in report
        assert WORK_NAME in report
        assert "不是最新来源" in report
        assert "最新来源：否" in report
        assert (
            _scalar(
                engine,
                "SELECT revision FROM projects WHERE id = :project_id",
                {"project_id": project_id},
            )
            == 9
        )
        object_v1 = _scalar(
            engine,
            "SELECT object_key FROM artifact_versions WHERE id = :version_id",
            {"version_id": version_v1},
        )
        object_v2 = _scalar(
            engine,
            "SELECT object_key FROM artifact_versions WHERE id = :version_id",
            {"version_id": version_v2},
        )
        assert isinstance(object_v1, str) and isinstance(object_v2, str)
        assert _object_file(objects, object_v1).read_bytes() == BYTES_V1
        assert _object_file(objects, object_v2).read_bytes() == BYTES_V2

    source = _database_path(engine)
    backup_dir = tmp_path / "backup"
    backed_up = backup_snapshot(
        source_database=source,
        objects_root=objects,
        backup_dir=backup_dir,
    )
    manifest_file = json.loads((backup_dir / "manifest.json").read_text(encoding="utf-8"))
    assert backed_up.backup_dir == backup_dir
    assert manifest_file["schema_revision"] == "0010_evidence_index"
    assert manifest_file["format_version"] == 1
    packed_originals = [item for item in manifest_file["originals"] if item["packed"] is True]
    assert {item["sha256"] for item in packed_originals} == {hash_v1, hash_v2}
    destination = tmp_path / "restored"
    restored = restore_snapshot(
        backup_dir=backup_dir,
        destination=destination,
        live_database=source,
    )
    assert restored.succeeded, restored.report
    assert restored.opened is True
    assert restored.failure is None
    restored_db = destination / "sinuo.db"
    assert restored_db.resolve() != source.resolve()
    assert (
        _one(
            engine,
            "SELECT title, brief, revision FROM projects WHERE id = :project_id",
            {"project_id": project_id},
        )
        == (TOPIC, SYNTHETIC, 9)
    )
    assert _sqlite_one(
        restored_db,
        "SELECT title, brief, revision FROM projects WHERE id = ?",
        (project_id,),
    ) == (TOPIC, SYNTHETIC, 9)
    assert _sqlite_one(
        restored_db,
        "SELECT head_version_id FROM pages WHERE id = ?",
        (first_page_id,),
    ) == (checkpoint_version_id,)
    assert _sqlite_one(
        restored_db,
        "SELECT stage_id, head_version_id FROM pages WHERE id = ?",
        (second_page_id,),
    ) == (stage_id, second_version_id)
    assert _sqlite_one(
        restored_db,
        "SELECT kind, source_version_id, target_version_id FROM page_edges WHERE project_id = ?",
        (project_id,),
    ) == ("branches_from", checkpoint_version_id, branch_version_id)
    assert _sqlite_one(
        restored_db,
        """
        SELECT author_account_id, page_version_id, node_id, body
        FROM review_opinions WHERE id = ?
        """,
        (opinion_id,),
    ) == (LEAD_ID, checkpoint_version_id, NODE_ID, OPINION)
    assert _sqlite_one(
        restored_db,
        "SELECT project_id, job_kind, status, subject_id, output_ref FROM jobs WHERE id = ?",
        (export_id,),
    ) == (project_id, "export", "succeeded", snapshot_id, runs[0].output_ref)
    assert _sqlite_all(
        restored_db,
        """
        SELECT version_no, content_hash FROM artifact_versions
        WHERE artifact_id = ? ORDER BY version_no
        """,
        (artifact_id,),
    ) == [(1, hash_v1), (2, hash_v2)]
    assert _sqlite_one(
        restored_db,
        "SELECT artifact_version_id, excerpt_hash FROM evidence_anchors WHERE id = ?",
        (anchor_id,),
    ) == (version_v1, excerpt_hash)
    assert _sqlite_one(
        restored_db,
        "SELECT artifact_version_id, observation FROM test_records WHERE id = ?",
        (record_id,),
    ) == (version_v1, OBSERVATION)
    assert _sqlite_one(
        restored_db,
        "SELECT execution_status, accepting_results FROM agent_runs WHERE id = ?",
        (run_id,),
    ) == ("completed", 0)
    assert _sqlite_one(
        restored_db,
        "SELECT status, action, page_mode FROM candidates WHERE run_id = ?",
        (run_id,),
    ) == ("pending", "draft_decision", None)
    assert _object_file(destination / "objects", object_v1).read_bytes() == BYTES_V1
    assert _object_file(destination / "objects", object_v2).read_bytes() == BYTES_V2
    assert (
        _scalar(
            engine,
            """
            SELECT COUNT(*) FROM sessions
            WHERE account_id = :account_id AND revoked_at IS NULL
            """,
            {"account_id": LEAD_ID},
        )
        >= 1
    )
    restored_sessions = sqlite3.connect(restored_db)
    try:
        restored_open = restored_sessions.execute(
            "SELECT COUNT(*) FROM sessions WHERE revoked_at IS NULL"
        ).fetchone()
    finally:
        restored_sessions.close()
    assert restored_open == (0,)
    assert restored.report["checks"]["page_history"] == "match"
    assert restored.report["checks"]["branches"] == "match"
    assert restored.report["checks"]["permissions"] == "match"
    assert "previews" in restored.report["not_restored"]
