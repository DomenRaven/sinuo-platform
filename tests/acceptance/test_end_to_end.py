"""First executable slice of the longitudinal path.

Walks HTTP that exists today, then calls the restore function once.
Steps without an endpoint are recorded here and are not filled in by
calling services. This file does not sign G0–G4.
"""

import json
import sqlite3
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import text

from sinuo.api.app import create_app
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

# These steps have no HTTP on this tree. The test must not call services
# or insert rows to make them look finished.
NOT_EXECUTED: tuple[tuple[str, str], ...] = (
    (
        "上传原件",
        "没有上传 HTTP。request_upload_ticket 和 register_original 只是服务函数，本测试不调用。",
    ),
    (
        "创建作品",
        "没有创建 Artifact 的 HTTP。本测试不插入作品行。",
    ),
    (
        "写入证据锚点",
        "没有写入证据锚点的 HTTP 或服务函数。不调用 search_evidence 或 rebuild_evidence_index 冒充已经定位。",
    ),
    (
        "登记测试记录",
        "没有登记 TestRecord 的 HTTP，也没有写入函数。不插入测试行。",
    ),
    (
        "启动智能体运行",
        "open_agent_run 和 run_fake_chain 没有启动用的 HTTP。假模型链只有读取端点。不调用这些函数，也不读取一个不存在的 run。",
    ),
    (
        "预览并确认阶段空白页",
        "预览和确认端点存在，但候选要由运行留下。没有运行就没有 candidate_id。不插入候选再调用预览或确认。",
    ),
    (
        "上传作品新版本并保留旧测试",
        "依赖没有 HTTP 的上传和测试登记。本测试不调用登记函数来假装旧测试仍绑在旧版本。",
    ),
    (
        "导出包生成与下载",
        "POST /exports 只把任务记成 queued。没有触发生成的 HTTP。不调用 run_export_job。queued 时 GET /content 返回 404，这只说明包还没生成。",
    ),
    (
        "3D",
        "用户决定不做 GLB 和其他 3D。本测试不准备受控 GLB，也不跑转换器。",
    ),
    (
        "真实模型",
        "live_model 本次不发。不调用真实模型，也不把假模型链读成模型效果。",
    ),
)


def _headers(csrf: str, key: str) -> dict[str, str]:
    return {"Origin": ORIGIN, CSRF_HEADER: csrf, "Idempotency-Key": key}


def _scalar(engine, sql: str, params: dict[str, object]):
    with engine.connect() as connection:
        return connection.execute(text(sql), params).scalar_one()


def _one(engine, sql: str, params: dict[str, object]):
    with engine.connect() as connection:
        return connection.execute(text(sql), params).one()


def _sqlite_one(path: Path, sql: str, params: tuple[object, ...]):
    connection = sqlite3.connect(path)
    try:
        row = connection.execute(sql, params).fetchone()
    finally:
        connection.close()
    assert row is not None
    return row


def _database_path(engine) -> Path:
    database = engine.url.database
    if not isinstance(database, str) or database == "":
        raise RuntimeError("this slice needs a file database")
    return Path(database)


def test_longitudinal_slice_walks_existing_http_and_records_gaps(engine, database_url, tmp_path):
    assert [name for name, _reason in NOT_EXECUTED] == [
        "上传原件",
        "创建作品",
        "写入证据锚点",
        "登记测试记录",
        "启动智能体运行",
        "预览并确认阶段空白页",
        "上传作品新版本并保留旧测试",
        "导出包生成与下载",
        "3D",
        "真实模型",
    ]
    for _name, reason in NOT_EXECUTED:
        assert reason.strip()

    assert _scalar(engine, "SELECT version_num FROM alembic_version", {}) == "0010_evidence_index"
    settings = identity_settings(database_url)
    seed_identity_world(engine, settings)
    sessions_before = _scalar(
        engine,
        """
        SELECT COUNT(*) FROM sessions
        WHERE account_id = :account_id AND revoked_at IS NULL
        """,
        {"account_id": LEAD_ID},
    )
    app = create_app(settings)
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
        assert saved["project_revision"] == 3
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
            == 3
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
            == 3
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
            == 4
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
        assert opinion_body["project_revision"] == 5
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
            "artifact_version_ids": [],
            "evidence_anchor_ids": [],
        }
        exported = client.post(
            f"/api/v1/projects/{project_id}/exports",
            headers=_headers(csrf, "synthetic-e2e-export"),
            json={
                "format": "markdown",
                "include_originals": False,
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
            == 5
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

        for table in (
            "artifacts",
            "artifact_versions",
            "evidence_anchors",
            "test_records",
            "agent_runs",
            "candidates",
        ):
            assert (
                _scalar(
                    engine,
                    f"SELECT COUNT(*) FROM {table} WHERE project_id = :project_id",
                    {"project_id": project_id},
                )
                == 0
            )

    source = _database_path(engine)
    objects = tmp_path / "objects"
    objects.mkdir()
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
        == (TOPIC, SYNTHETIC, 5)
    )
    assert _sqlite_one(
        restored_db,
        "SELECT title, brief, revision FROM projects WHERE id = ?",
        (project_id,),
    ) == (TOPIC, SYNTHETIC, 5)
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
        "SELECT project_id, job_kind, status, subject_id FROM jobs WHERE id = ?",
        (export_id,),
    ) == (project_id, "export", "queued", snapshot_id)
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
