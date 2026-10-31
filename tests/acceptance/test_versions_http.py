"""HTTP path for project creation, replay, and a history read."""

from fastapi.testclient import TestClient
from sqlalchemy import text

from sinuo.api.app import create_app
from sinuo.domain.auth.projection import project_receipt
from sinuo.domain.public_contract import CSRF_HEADER
from tests.acceptance.identity_fixtures import (
    LEAD_ID,
    LEAD_LOGIN,
    LEAD_PASSWORD,
    identity_settings,
    seed_identity_world,
)

ORIGIN = "http://127.0.0.1:8000"


def test_project_command_replays_and_history_does_not_write(engine, database_url):
    settings = identity_settings(database_url)
    seed_identity_world(engine, settings)
    app = create_app(settings)
    with TestClient(app) as client:
        login = client.post(
            "/api/v1/session",
            headers={"Origin": ORIGIN},
            json={"login_name": LEAD_LOGIN, "password": LEAD_PASSWORD},
        )
        assert login.status_code == 204
        csrf = client.get("/api/v1/session").json()["data"]["csrf_token"]
        headers = {
            "Origin": ORIGIN,
            CSRF_HEADER: csrf,
            "Idempotency-Key": "synthetic-create-project",
        }
        rejected = client.post(
            "/api/v1/projects",
            headers=headers,
            json={"title": "synthetic-http-project", "actor_id": "forged"},
        )
        assert rejected.status_code == 422
        created = client.post(
            "/api/v1/projects",
            headers=headers,
            json={"title": "synthetic-http-project"},
        )
        assert created.status_code == 201, created.text
        project_id = created.json()["data"]["project_id"]
        page_id = created.json()["data"]["page_id"]
        replayed = client.post(
            "/api/v1/projects",
            headers=headers,
            json={"title": "synthetic-http-project"},
        )
        assert replayed.status_code == 201
        assert replayed.json()["data"]["project_id"] == project_id
        assert replayed.json()["meta"]["idempotency_replayed"] is True
        conflict = client.post(
            "/api/v1/projects",
            headers=headers,
            json={"title": "synthetic-other-body"},
        )
        assert conflict.status_code == 409
        assert conflict.json()["error"]["code"] == "IDEMPOTENCY_KEY_REUSED"
        first = client.get(f"/api/v1/pages/{page_id}/history")
        second = client.get(f"/api/v1/pages/{page_id}/history")
        assert first.status_code == 200
        assert first.json()["data"] == second.json()["data"]
        assert first.json()["data"]["versions"][0]["version_no"] == 1
        with engine.connect() as connection:
            revision = connection.execute(
                text("SELECT revision FROM projects WHERE id = :project_id"),
                {"project_id": project_id},
            ).scalar_one()
            head = connection.execute(
                text("SELECT head_version_id FROM pages WHERE id = :page_id"),
                {"page_id": page_id},
            ).scalar_one()
        assert revision == 1
        assert head == created.json()["data"]["page_version_id"]
    assert project_id


def test_draft_receipt_keeps_revision_and_hash():
    projected = project_receipt(
        {
            "page_id": "synthetic-page",
            "content_revision": 6,
            "content_hash": "ab" * 32,
            "excerpt": "synthetic-hidden",
        }
    )
    assert projected["content_revision"] == 6
    assert projected["content_hash"] == "ab" * 32
    assert "excerpt" not in projected


def test_revoked_membership_replay_does_not_return_stored_page(engine, database_url):
    """A page.create receipt stays in the database, but the client cannot read it."""

    settings = identity_settings(database_url)
    seed_identity_world(engine, settings)
    app = create_app(settings)
    with TestClient(app) as client:
        login = client.post(
            "/api/v1/session",
            headers={"Origin": ORIGIN},
            json={"login_name": LEAD_LOGIN, "password": LEAD_PASSWORD},
        )
        assert login.status_code == 204
        csrf = client.get("/api/v1/session").json()["data"]["csrf_token"]
        headers = {
            "Origin": ORIGIN,
            CSRF_HEADER: csrf,
            "Idempotency-Key": "synthetic-page-replay",
        }
        created = client.post(
            "/api/v1/projects",
            headers={**headers, "Idempotency-Key": "synthetic-project-for-replay"},
            json={"title": "synthetic-replay-project", "page_title": "synthetic-canary-page"},
        )
        assert created.status_code == 201, created.text
        project_id = created.json()["data"]["project_id"]
        stage_id = created.json()["data"]["stage_id"]
        body = {
            "stage_id": stage_id,
            "title": "synthetic-canary-parallel-page",
            "expected_revision": 1,
        }
        forged = client.post(
            f"/api/v1/projects/{project_id}/pages",
            headers=headers,
            json={**body, "actor_id": "forged-actor"},
        )
        assert forged.status_code == 422
        first = client.post(
            f"/api/v1/projects/{project_id}/pages",
            headers=headers,
            json=body,
        )
        assert first.status_code == 201, first.text
        page_id = first.json()["data"]["page_id"]
        version_id = first.json()["data"]["page_version_id"]
        audit_id = first.json()["data"]["audit_event_id"]
        assert page_id
        with engine.begin() as connection:
            updated = connection.execute(
                text(
                    """
                    UPDATE memberships
                    SET status = 'revoked', revoked_at = :now
                    WHERE project_id = :project_id AND account_id = :account_id
                    """
                ),
                {"now": 1_761_800_100.0, "project_id": project_id, "account_id": LEAD_ID},
            )
            assert updated.rowcount == 1
        replayed = client.post(
            f"/api/v1/projects/{project_id}/pages",
            headers=headers,
            json=body,
        )
        assert replayed.status_code == 404, replayed.text
        assert replayed.json()["error"]["code"] == "NOT_FOUND_OR_HIDDEN"
        leaked = replayed.text
        assert page_id not in leaked
        assert version_id not in leaked
        assert audit_id not in leaked
        assert "synthetic-canary-parallel-page" not in leaked
        with engine.connect() as connection:
            stored = connection.execute(
                text(
                    """
                    SELECT response_json FROM idempotency_records
                    WHERE project_id = :project_id
                      AND actor_id = :account_id
                      AND command = 'page.create'
                      AND request_key = 'synthetic-page-replay'
                    """
                ),
                {"project_id": project_id, "account_id": LEAD_ID},
            ).scalar_one()
        stored_text = stored if isinstance(stored, str) else str(stored)
        assert page_id in stored_text
        assert version_id in stored_text


def test_second_draft_write_on_the_same_revision_returns_409(engine, database_url):
    settings = identity_settings(database_url)
    seed_identity_world(engine, settings)
    app = create_app(settings)
    with TestClient(app) as client:
        login = client.post(
            "/api/v1/session",
            headers={"Origin": ORIGIN},
            json={"login_name": LEAD_LOGIN, "password": LEAD_PASSWORD},
        )
        assert login.status_code == 204
        csrf = client.get("/api/v1/session").json()["data"]["csrf_token"]
        created = client.post(
            "/api/v1/projects",
            headers={
                "Origin": ORIGIN,
                CSRF_HEADER: csrf,
                "Idempotency-Key": "synthetic-draft-project",
            },
            json={"title": "synthetic-draft-project"},
        )
        assert created.status_code == 201, created.text
        page_id = created.json()["data"]["page_id"]
        base_version_id = created.json()["data"]["page_version_id"]
        project_id = created.json()["data"]["project_id"]
        first_content = {
            "schema_version": 1,
            "nodes": [{"node_id": "synthetic-winner"}],
            "edges": [],
        }
        second_content = {
            "schema_version": 1,
            "nodes": [{"node_id": "synthetic-loser"}],
            "edges": [],
        }
        common = {
            "base_version_id": base_version_id,
            "expected_revision": 1,
        }
        first = client.patch(
            f"/api/v1/pages/{page_id}/draft",
            headers={
                "Origin": ORIGIN,
                CSRF_HEADER: csrf,
                "Idempotency-Key": "synthetic-draft-first",
            },
            json={**common, "content": first_content},
        )
        assert first.status_code == 200, first.text
        assert first.json()["data"]["content_revision"] == 2
        second = client.patch(
            f"/api/v1/pages/{page_id}/draft",
            headers={
                "Origin": ORIGIN,
                CSRF_HEADER: csrf,
                "Idempotency-Key": "synthetic-draft-second",
            },
            json={**common, "content": second_content},
        )
        assert second.status_code == 409, second.text
        assert second.json()["error"]["code"] == "DRAFT_REVISION_CONFLICT"
        assert "synthetic-loser" not in second.text
        with engine.connect() as connection:
            stored = connection.execute(
                text("SELECT content_json FROM page_drafts WHERE page_id = :page_id"),
                {"page_id": page_id},
            ).scalar_one()
        assert "synthetic-winner" in stored
        assert "synthetic-loser" not in stored
        assert project_id


def _session_headers(client: TestClient, key: str) -> dict[str, str]:
    csrf = client.get("/api/v1/session").json()["data"]["csrf_token"]
    return {"Origin": ORIGIN, CSRF_HEADER: csrf, "Idempotency-Key": key}


def test_history_keeps_confirmed_bytes_after_a_later_draft(engine, database_url):
    settings = identity_settings(database_url)
    seed_identity_world(engine, settings)
    app = create_app(settings)
    confirmed = {
        "schema_version": 1,
        "nodes": [{"node_id": "synthetic-confirmed"}],
        "edges": [],
    }
    later = {
        "schema_version": 1,
        "nodes": [{"node_id": "synthetic-later-draft"}],
        "edges": [],
    }
    with TestClient(app) as client:
        assert (
            client.post(
                "/api/v1/session",
                headers={"Origin": ORIGIN},
                json={"login_name": LEAD_LOGIN, "password": LEAD_PASSWORD},
            ).status_code
            == 204
        )
        created = client.post(
            "/api/v1/projects",
            headers=_session_headers(client, "synthetic-history-project"),
            json={"title": "synthetic-history-project"},
        )
        assert created.status_code == 201, created.text
        project_id = created.json()["data"]["project_id"]
        page_id = created.json()["data"]["page_id"]
        first_version = created.json()["data"]["page_version_id"]
        drafted = client.patch(
            f"/api/v1/pages/{page_id}/draft",
            headers=_session_headers(client, "synthetic-history-draft"),
            json={
                "base_version_id": first_version,
                "expected_revision": 1,
                "content": confirmed,
            },
        )
        assert drafted.status_code == 200, drafted.text
        saved = client.post(
            f"/api/v1/pages/{page_id}/checkpoints",
            headers=_session_headers(client, "synthetic-history-checkpoint"),
            json={
                "expected_head_version_id": first_version,
                "expected_content_revision": drafted.json()["data"]["content_revision"],
                "expected_content_hash": drafted.json()["data"]["content_hash"],
            },
        )
        assert saved.status_code == 201, saved.text
        confirmed_version = saved.json()["data"]["page_version_id"]
        with engine.connect() as connection:
            frozen = connection.execute(
                text(
                    "SELECT content_hash, content_json FROM page_versions WHERE id = :version"
                ),
                {"version": confirmed_version},
            ).one()
            draft_row = connection.execute(
                text(
                    """
                    SELECT base_version_id, content_revision
                    FROM page_drafts WHERE page_id = :page
                    """
                ),
                {"page": page_id},
            ).one()
            revision_after_save = connection.execute(
                text("SELECT revision FROM projects WHERE id = :project"),
                {"project": project_id},
            ).scalar_one()
            head_after_save = connection.execute(
                text("SELECT head_version_id FROM pages WHERE id = :page"),
                {"page": page_id},
            ).scalar_one()
        edited = client.patch(
            f"/api/v1/pages/{page_id}/draft",
            headers=_session_headers(client, "synthetic-history-later"),
            json={
                "base_version_id": draft_row.base_version_id,
                "expected_revision": int(draft_row.content_revision),
                "content": later,
            },
        )
        assert edited.status_code == 200, edited.text
        first = client.get(f"/api/v1/pages/{page_id}/history")
        second = client.get(f"/api/v1/pages/{page_id}/history")
        assert first.status_code == 200
        assert first.json()["data"] == second.json()["data"]
        hashes = {
            item["page_version_id"]: item["content_hash"]
            for item in first.json()["data"]["versions"]
        }
        assert hashes[confirmed_version] == frozen.content_hash
        with engine.connect() as connection:
            again = connection.execute(
                text("SELECT content_hash, content_json FROM page_versions WHERE id = :version"),
                {"version": confirmed_version},
            ).one()
            revision_after_get = connection.execute(
                text("SELECT revision FROM projects WHERE id = :project"),
                {"project": project_id},
            ).scalar_one()
            head_after_get = connection.execute(
                text("SELECT head_version_id FROM pages WHERE id = :page"),
                {"page": page_id},
            ).scalar_one()
        assert again.content_hash == frozen.content_hash
        assert again.content_json == frozen.content_json
        assert "synthetic-later-draft" not in again.content_json
        assert revision_after_get == revision_after_save
        assert head_after_get == head_after_save == confirmed_version


def _revoke_membership(engine, project_id: str) -> None:
    with engine.begin() as connection:
        updated = connection.execute(
            text(
                """
                UPDATE memberships
                SET status = 'revoked', revoked_at = :now
                WHERE project_id = :project_id AND account_id = :account_id
                """
            ),
            {"now": 1_761_800_200.0, "project_id": project_id, "account_id": LEAD_ID},
        )
        assert updated.rowcount == 1


def test_revoked_membership_hides_draft_checkpoint_and_branch_replays(engine, database_url):
    settings = identity_settings(database_url)
    seed_identity_world(engine, settings)
    app = create_app(settings)
    content = {
        "schema_version": 1,
        "nodes": [{"node_id": "synthetic-replay-node"}],
        "edges": [],
    }
    with TestClient(app) as client:
        assert (
            client.post(
                "/api/v1/session",
                headers={"Origin": ORIGIN},
                json={"login_name": LEAD_LOGIN, "password": LEAD_PASSWORD},
            ).status_code
            == 204
        )
        created = client.post(
            "/api/v1/projects",
            headers=_session_headers(client, "synthetic-revoke-project"),
            json={"title": "synthetic-revoke-project"},
        )
        assert created.status_code == 201, created.text
        project_id = created.json()["data"]["project_id"]
        page_id = created.json()["data"]["page_id"]
        base_version = created.json()["data"]["page_version_id"]
        draft_body = {
            "base_version_id": base_version,
            "expected_revision": 1,
            "content": content,
        }
        forged = client.patch(
            f"/api/v1/pages/{page_id}/draft",
            headers=_session_headers(client, "synthetic-draft-forged"),
            json={**draft_body, "actor_id": "forged-actor"},
        )
        assert forged.status_code == 422
        drafted = client.patch(
            f"/api/v1/pages/{page_id}/draft",
            headers=_session_headers(client, "synthetic-draft-replay"),
            json=draft_body,
        )
        assert drafted.status_code == 200, drafted.text
        draft_hash = drafted.json()["data"]["content_hash"]
        checkpoint_body = {
            "expected_head_version_id": base_version,
            "expected_content_revision": drafted.json()["data"]["content_revision"],
            "expected_content_hash": draft_hash,
        }
        forged_checkpoint = client.post(
            f"/api/v1/pages/{page_id}/checkpoints",
            headers=_session_headers(client, "synthetic-checkpoint-forged"),
            json={**checkpoint_body, "actor_id": "forged-actor"},
        )
        assert forged_checkpoint.status_code == 422
        saved = client.post(
            f"/api/v1/pages/{page_id}/checkpoints",
            headers=_session_headers(client, "synthetic-checkpoint-replay"),
            json=checkpoint_body,
        )
        assert saved.status_code == 201, saved.text
        confirmed_id = saved.json()["data"]["page_version_id"]
        branch_body = {"source_version_id": base_version, "title": "synthetic-replay-branch"}
        forged_branch = client.post(
            f"/api/v1/pages/{page_id}/branches",
            headers=_session_headers(client, "synthetic-branch-forged"),
            json={**branch_body, "actor_id": "forged-actor"},
        )
        assert forged_branch.status_code == 422
        opened = client.post(
            f"/api/v1/pages/{page_id}/branches",
            headers=_session_headers(client, "synthetic-branch-replay"),
            json=branch_body,
        )
        assert opened.status_code == 201, opened.text
        branched_page = opened.json()["data"]["page_id"]
        _revoke_membership(engine, project_id)
        hidden = {
            "draft": client.patch(
                f"/api/v1/pages/{page_id}/draft",
                headers=_session_headers(client, "synthetic-draft-replay"),
                json=draft_body,
            ),
            "checkpoint": client.post(
                f"/api/v1/pages/{page_id}/checkpoints",
                headers=_session_headers(client, "synthetic-checkpoint-replay"),
                json=checkpoint_body,
            ),
            "branch": client.post(
                f"/api/v1/pages/{page_id}/branches",
                headers=_session_headers(client, "synthetic-branch-replay"),
                json=branch_body,
            ),
        }
        for name, response in hidden.items():
            assert response.status_code == 404, name + response.text
            assert response.json()["error"]["code"] == "NOT_FOUND_OR_HIDDEN"
            assert draft_hash not in response.text
            assert confirmed_id not in response.text
            assert branched_page not in response.text
        with engine.connect() as connection:
            rows = connection.execute(
                text(
                    """
                    SELECT command, response_json FROM idempotency_records
                    WHERE project_id = :project AND actor_id = :actor
                    """
                ),
                {"project": project_id, "actor": LEAD_ID},
            ).all()
        stored = {row.command: str(row.response_json) for row in rows}
        assert draft_hash in stored["page.draft.write"]
        assert confirmed_id in stored["page.checkpoint"]
        assert branched_page in stored["page.branch"]
