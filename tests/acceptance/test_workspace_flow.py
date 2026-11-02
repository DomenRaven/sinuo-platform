"""Upload, a next-step run, and confirming a stage card from the workspace."""

import hashlib
import json

from fastapi.testclient import TestClient

from sinuo.agents.contracts import SpecialistResult
from sinuo.agents.fake import FakeModelProvider
from sinuo.api.app import create_app
from sinuo.domain.public_contract import CSRF_HEADER
from tests.acceptance.identity_fixtures import (
    LEAD_LOGIN,
    LEAD_PASSWORD,
    PROJECT_A,
    RELEASED_TITLE,
    SENSITIVE_TITLE,
    TEAM_LOGIN,
    TEAM_PASSWORD,
    identity_settings,
    seed_identity_world,
)

ORIGIN = "http://127.0.0.1:8000"
TITLE = "synthetic-flow-project"
NEW_PAGE = "synthetic-flow-page"
FILE_NAME = "synthetic-flow-notes.txt"
FILE_BYTES = b"synthetic-flow-bytes"


class _StageVoice:
    def __init__(self, version_id: str, stage_id: str) -> None:
        self.version_id = version_id
        self.stage_id = stage_id

    async def execute(self, task, snapshot, upstream):
        del snapshot, upstream
        body = {
            "action": "suggest_stage_transition",
            "gaps": ["阶段判断等待证据"],
            "page_mode": "stage_blank",
            "sources": [{"page_version_id": self.version_id}],
            "summary": "待确认阶段",
            "target_stage_id": self.stage_id,
            "title": NEW_PAGE,
        }
        return SpecialistResult(
            task.task_id,
            "completed",
            json.dumps(body, ensure_ascii=False),
            role=task.role,
        )


def _login(client: TestClient, login_name: str, password: str) -> str:
    response = client.post(
        "/api/v1/session",
        headers={"Origin": ORIGIN},
        json={"login_name": login_name, "password": password},
    )
    assert response.status_code == 204, response.text
    return client.get("/api/v1/session").json()["data"]["csrf_token"]


def _headers(csrf: str, key: str) -> dict[str, str]:
    return {"Origin": ORIGIN, CSRF_HEADER: csrf, "Idempotency-Key": key}


def _app(tmp_path, database_url, engine, provider):
    settings = identity_settings(database_url)
    seed_identity_world(engine, settings)
    uploads = tmp_path / "uploads"
    objects = tmp_path / "objects"
    uploads.mkdir()
    objects.mkdir()
    app = create_app(settings)
    app.state.uploads_root = uploads
    app.state.objects_root = objects
    app.state.agent_provider = provider
    return app


def _create(client: TestClient, csrf: str) -> dict:
    response = client.post(
        "/api/v1/projects",
        headers=_headers(csrf, "synthetic-flow-create"),
        json={"title": TITLE},
    )
    assert response.status_code == 201, response.text
    return response.json()["data"]


def test_upload_is_listed_and_hidden_titles_stay_out(tmp_path, engine, database_url):
    app = _app(tmp_path, database_url, engine, FakeModelProvider())
    with TestClient(app) as client:
        csrf = _login(client, LEAD_LOGIN, LEAD_PASSWORD)
        visible = client.get(f"/api/v1/projects/{PROJECT_A}/artifacts")
        assert visible.status_code == 200, visible.text
        assert SENSITIVE_TITLE in visible.text
        assert RELEASED_TITLE in visible.text
        client.delete("/api/v1/session", headers={"Origin": ORIGIN, CSRF_HEADER: csrf})
        team = _login(client, TEAM_LOGIN, TEAM_PASSWORD)
        hidden = client.get(f"/api/v1/projects/{PROJECT_A}/artifacts")
        assert hidden.status_code == 200, hidden.text
        assert hidden.json()["data"]["artifacts"] == []
        assert hidden.json()["data"]["has_hidden_artifacts"] is True
        assert SENSITIVE_TITLE not in hidden.text
        assert RELEASED_TITLE not in hidden.text
        client.delete("/api/v1/session", headers={"Origin": ORIGIN, CSRF_HEADER: team})
        csrf = _login(client, LEAD_LOGIN, LEAD_PASSWORD)
        created = _create(client, csrf)
        digest = hashlib.sha256(FILE_BYTES).hexdigest()
        opened = client.post(
            f"/api/v1/projects/{created['project_id']}/artifacts/uploads",
            headers=_headers(csrf, "synthetic-flow-open"),
            json={
                "name": FILE_NAME,
                "media_type": "text/plain",
                "size": len(FILE_BYTES),
                "declared_hash": digest,
                "license": "synthetic-license",
                "sensitivity": "project",
                "ai_processing_allowed": 0,
            },
        )
        assert opened.status_code == 201, opened.text
        ticket = opened.json()["data"]["ticket_id"]
        staged = client.put(
            f"/api/v1/uploads/{ticket}/content",
            headers=_headers(csrf, "synthetic-flow-bytes"),
            content=FILE_BYTES,
        )
        assert staged.status_code == 200, staged.text
        done = client.post(
            f"/api/v1/uploads/{ticket}/complete",
            headers=_headers(csrf, "synthetic-flow-done"),
            json={"declared_hash": digest},
        )
        assert done.status_code == 201, done.text
        assert done.json()["data"]["version_no"] == 1
        assert done.json()["data"]["preview_generated"] is False
        listed = client.get(f"/api/v1/projects/{created['project_id']}/artifacts")
        assert listed.status_code == 200, listed.text
        card = listed.json()["data"]["artifacts"][0]
        assert card["title"] == FILE_NAME
        assert card["version_no"] == 1


def test_next_step_shows_two_duties_and_stage_confirm_moves_the_page(
    tmp_path, engine, database_url
):
    app = _app(tmp_path, database_url, engine, FakeModelProvider())
    with TestClient(app) as client:
        csrf = _login(client, LEAD_LOGIN, LEAD_PASSWORD)
        created = _create(client, csrf)
        project_id = created["project_id"]
        plain = client.post(
            f"/api/v1/projects/{project_id}/agent-runs",
            headers=_headers(csrf, "synthetic-flow-next"),
            json={
                "intent": "下一步",
                "input_mode": "confirmed",
                "page_version_id": created["page_version_id"],
            },
        )
        assert plain.status_code == 202, plain.text
        run_id = plain.json()["data"]["run_id"]
        viewed = client.get(f"/api/v1/agent-runs/{run_id}")
        assert viewed.status_code == 200, viewed.text
        duties = viewed.json()["data"]["duties"]
        assert [item["role"] for item in duties] == ["evidence", "critic"]
        assert [item["role_label"] for item in duties] == ["证据查找", "过程检查"]
        assert "课题澄清" not in viewed.text
        assert "方法建议" not in viewed.text
        card = viewed.json()["data"]["candidates"][0]
        assert card["status"] == "pending"
        assert card["preview_ready"] is False
        blocked = client.post(
            f"/api/v1/candidates/{card['candidate_id']}/preview",
            headers={"Origin": ORIGIN, CSRF_HEADER: csrf},
            json={"expected_page_version_id": created["page_version_id"]},
        )
        assert blocked.status_code == 422, blocked.text
        stages = client.get(f"/api/v1/projects/{project_id}/stages")
        counts = [len(item["pages"]) for item in stages.json()["data"]["stages"]]
        assert counts == [1, 0, 0, 0, 0, 0, 0, 0]

        second = stages.json()["data"]["stages"][1]["stage_id"]
        app.state.agent_provider = _StageVoice(created["page_version_id"], second)
        started = client.post(
            f"/api/v1/projects/{project_id}/agent-runs",
            headers=_headers(csrf, "synthetic-flow-stage"),
            json={
                "intent": "下一步",
                "input_mode": "confirmed",
                "page_version_id": created["page_version_id"],
            },
        )
        assert started.status_code == 202, started.text
        stage_run = client.get(f"/api/v1/agent-runs/{started.json()['data']['run_id']}")
        ready = [
            item for item in stage_run.json()["data"]["candidates"] if item["preview_ready"]
        ]
        assert len(ready) == 1
        assert ready[0]["title"] == NEW_PAGE
        assert ready[0]["action_label"] == "建议进入阶段"
        preview = client.post(
            f"/api/v1/candidates/{ready[0]['candidate_id']}/preview",
            headers={"Origin": ORIGIN, CSRF_HEADER: csrf},
            json={"expected_page_version_id": created["page_version_id"]},
        )
        assert preview.status_code == 200, preview.text
        still = client.get(f"/api/v1/projects/{project_id}/stages")
        assert NEW_PAGE not in still.text
        committed = client.post(
            f"/api/v1/candidates/{ready[0]['candidate_id']}/commit",
            headers=_headers(csrf, "synthetic-flow-commit"),
            json={
                "preview_id": preview.json()["data"]["preview_id"],
                "preview_hash": preview.json()["data"]["preview_hash"],
                "expected_page_version_id": created["page_version_id"],
                "expected_draft_content_revision": 1,
            },
        )
        assert committed.status_code == 201, committed.text
        moved = client.get(f"/api/v1/projects/{project_id}/stages")
        assert moved.status_code == 200, moved.text
        assert NEW_PAGE in moved.text
        assert moved.json()["data"]["stages"][1]["pages"][0]["title"] == NEW_PAGE
        home = client.get(f"/api/v1/pages/{created['page_id']}/draft")
        assert home.json()["data"]["draft"]["content_revision"] == 1
