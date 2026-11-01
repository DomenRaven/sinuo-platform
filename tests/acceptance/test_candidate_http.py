"""HTTP path for candidate preview, commit, and reject.

Login, Origin, and CSRF follow tests/acceptance/test_versions_http.py.
The candidate is a synthetic draft_decision on the identity fixture page.
"""

import hashlib
import json
import re

from fastapi.testclient import TestClient
from sqlalchemy import text

from sinuo.api.app import create_app
from sinuo.domain.public_contract import CSRF_HEADER
from sinuo.domain.schema_types import canonical_document, canonical_json, document_hash
from sinuo.infrastructure.database import write_uow
from sinuo.infrastructure.domain_models import AgentRun, Candidate, ContextSnapshot, PageDraft
from tests.acceptance.identity_fixtures import (
    LEAD_ID,
    LEAD_LOGIN,
    LEAD_PASSWORD,
    NOW,
    PAGE_A,
    PROJECT_A,
    identity_settings,
    seed_identity_world,
)

ORIGIN = "http://127.0.0.1:8000"
_PATCHED_NODE = "synthetic-patched-node"
_HASH = re.compile(r"^[0-9a-f]{64}$")


def _scalar(engine, sql: str, **params):
    with engine.connect() as connection:
        return connection.execute(text(sql), params).scalar_one()


def _row(engine, sql: str, **params):
    with engine.connect() as connection:
        return connection.execute(text(sql), params).one()


def _seed_draft_candidate(engine, candidate_id: str) -> dict[str, str | int]:
    version_id = _scalar(
        engine,
        "SELECT head_version_id FROM pages WHERE id = :page_id",
        page_id=PAGE_A,
    )
    if not isinstance(version_id, str):
        raise AssertionError("synthetic page has no head")
    content = {
        "schema_version": 1,
        "nodes": [{"node_id": "synthetic-node-1"}],
        "edges": [],
    }
    proposal = {
        "base_version_id": version_id,
        "node_id": "synthetic-decision-node",
        "page_id": PAGE_A,
        "schema_version": 1,
    }
    proposal_json = canonical_json(proposal)
    with write_uow(engine) as session:
        session.add(
            PageDraft(
                page_id=PAGE_A,
                project_id=PROJECT_A,
                base_version_id=version_id,
                content_revision=1,
                content_hash=document_hash(content),
                content_json=canonical_document(content),
                schema_version=1,
                updated_at=NOW,
            )
        )
        session.add(
            ContextSnapshot(
                id=f"{candidate_id}-snapshot",
                project_id=PROJECT_A,
                intent="synthetic intent",
                template_version="synthetic-template",
                method_card_version="synthetic-method",
                authorization_scope_json="{}",
                access_epoch=1,
                exclusions_json="{}",
                index_revision=None,
                budget_json="{}",
                draft_page_id=None,
                draft_content_revision=None,
                draft_content_hash=None,
                draft_content_json=None,
                created_at=NOW,
            )
        )
        session.flush()
        session.add(
            AgentRun(
                id=f"{candidate_id}-run",
                project_id=PROJECT_A,
                initiated_by_account_id=LEAD_ID,
                parent_run_id=None,
                supersedes_run_id=None,
                context_snapshot_id=f"{candidate_id}-snapshot",
                execution_status="queued",
                accepting_results=1,
                next_event_seq=0,
                budget_json="{}",
                created_at=NOW,
                closed_at=None,
                origin="manual",
            )
        )
        session.flush()
        session.add(
            Candidate(
                id=candidate_id,
                project_id=PROJECT_A,
                run_id=f"{candidate_id}-run",
                action="draft_decision",
                page_mode=None,
                status="pending",
                revision=1,
                proposal_json=proposal_json,
                proposal_hash=hashlib.sha256(proposal_json.encode("utf-8")).hexdigest(),
                input_hash=hashlib.sha256(candidate_id.encode("utf-8")).hexdigest(),
                context_snapshot_id=f"{candidate_id}-snapshot",
                schema_version=1,
                created_at=NOW,
            )
        )
    return {"page_version_id": version_id, "draft_revision": 1}


def _app(engine, database_url):
    settings = identity_settings(database_url)
    seed_identity_world(engine, settings)
    return create_app(settings)


def _login(client: TestClient) -> dict[str, str]:
    login = client.post(
        "/api/v1/session",
        headers={"Origin": ORIGIN},
        json={"login_name": LEAD_LOGIN, "password": LEAD_PASSWORD},
    )
    assert login.status_code == 204, login.text
    csrf = client.get("/api/v1/session").json()["data"]["csrf_token"]
    return {"Origin": ORIGIN, CSRF_HEADER: csrf}


def _preview_body(seeded: dict[str, str | int]) -> dict:
    return {
        "user_patch": {"node_id": _PATCHED_NODE},
        "expected_page_version_id": seeded["page_version_id"],
        "expected_draft_content_revision": seeded["draft_revision"],
    }


def _post_preview(client: TestClient, headers: dict[str, str], candidate_id: str, body: dict):
    return client.post(
        f"/api/v1/candidates/{candidate_id}/preview",
        headers={**headers, "Idempotency-Key": "synthetic-preview-ignored"},
        json=body,
    )


def test_preview_inserts_a_new_row_and_returns_the_saved_hash(engine, database_url):
    candidate_id = "synthetic-http-preview"
    app = _app(engine, database_url)
    seeded = _seed_draft_candidate(engine, candidate_id)
    with TestClient(app) as client:
        headers = _login(client)
        forged = client.post(
            f"/api/v1/candidates/{candidate_id}/preview",
            headers=headers,
            json={**_preview_body(seeded), "project_id": "forged-project"},
        )
        assert forged.status_code == 422
        assert forged.json()["error"]["code"] == "INVALID_SCHEMA"
        first = _post_preview(client, headers, candidate_id, _preview_body(seeded))
        assert first.status_code == 200, first.text
        second = _post_preview(client, headers, candidate_id, _preview_body(seeded))
        assert second.status_code == 200, second.text
    payload = first.json()
    data = payload["data"]
    assert set(payload["meta"]) == {"request_id", "version"}
    assert first.headers["cache-control"] == "no-store"
    assert data["preview_id"] != second.json()["data"]["preview_id"]
    assert _HASH.fullmatch(data["preview_hash"])
    assert not str(data["preview_hash"]).startswith("sha256:")
    assert data["expires_at"].endswith("Z") and "T" in data["expires_at"]
    assert data["effective_proposal"]["node_id"] == _PATCHED_NODE
    assert data["effective_proposal"]["page_id"] == PAGE_A
    assert {item["source_kind"] for item in data["preconditions"]} == {
        "page_version",
        "draft_stamp",
    }
    assert data["affected_objects"]
    stored = _scalar(
        engine,
        "SELECT preview_hash FROM candidate_previews WHERE id = :preview_id",
        preview_id=data["preview_id"],
    )
    assert stored == data["preview_hash"]
    assert _scalar(engine, "SELECT count(*) FROM candidate_previews") == 2
    assert _scalar(engine, "SELECT count(*) FROM candidate_dispositions") == 0
    assert _scalar(
        engine,
        "SELECT revision FROM projects WHERE id = :project_id",
        project_id=PROJECT_A,
    ) == 1


def test_commit_replays_the_same_body_without_a_new_project_revision(engine, database_url):
    candidate_id = "synthetic-http-commit"
    app = _app(engine, database_url)
    seeded = _seed_draft_candidate(engine, candidate_id)
    versions_before = _scalar(
        engine,
        "SELECT count(*) FROM page_versions WHERE project_id = :project_id",
        project_id=PROJECT_A,
    )
    with TestClient(app) as client:
        headers = _login(client)
        preview = _post_preview(client, headers, candidate_id, _preview_body(seeded))
        assert preview.status_code == 200, preview.text
        body = {
            "preview_id": preview.json()["data"]["preview_id"],
            "preview_hash": preview.json()["data"]["preview_hash"],
            "expected_page_version_id": seeded["page_version_id"],
            "expected_draft_content_revision": seeded["draft_revision"],
        }
        command = {**headers, "Idempotency-Key": "synthetic-commit-once"}
        first = client.post(f"/api/v1/candidates/{candidate_id}/commit", headers=command, json=body)
        assert first.status_code == 201, first.text
        replayed = client.post(
            f"/api/v1/candidates/{candidate_id}/commit",
            headers=command,
            json=body,
        )
        assert replayed.status_code == 200, replayed.text
        conflict = client.post(
            f"/api/v1/candidates/{candidate_id}/commit",
            headers=command,
            json={**body, "preview_id": "synthetic-other-preview"},
        )
        assert conflict.status_code == 409, conflict.text
    assert first.json()["meta"]["idempotency_replayed"] is False
    assert "project_revision" not in first.json()["meta"]
    assert "project_revision" not in first.json()["data"]
    assert first.json()["meta"]["audit_event_id"] == first.json()["data"]["audit_event_id"]
    assert first.json()["data"]["status"] == "committed"
    assert first.json()["data"]["content_revision"] == 2
    assert "title" not in first.json()["data"]
    assert replayed.json()["meta"]["idempotency_replayed"] is True
    assert replayed.json()["data"]["disposition_id"] == first.json()["data"]["disposition_id"]
    assert conflict.json()["error"]["code"] == "IDEMPOTENCY_KEY_REUSED"
    assert _scalar(
        engine,
        "SELECT revision FROM projects WHERE id = :project_id",
        project_id=PROJECT_A,
    ) == 1
    assert versions_before == _scalar(
        engine,
        "SELECT count(*) FROM page_versions WHERE project_id = :project_id",
        project_id=PROJECT_A,
    )
    assert _scalar(engine, "SELECT count(*) FROM candidate_dispositions") == 1
    document = json.loads(
        _scalar(
            engine,
            "SELECT content_json FROM page_drafts WHERE page_id = :page_id",
            page_id=PAGE_A,
        )
    )
    assert _PATCHED_NODE in {node["node_id"] for node in document["nodes"]}
    assert _scalar(
        engine,
        "SELECT status FROM candidates WHERE id = :candidate_id",
        candidate_id=candidate_id,
    ) == "committed"


def test_reject_records_a_reason_and_replays_without_touching_the_draft(engine, database_url):
    candidate_id = "synthetic-http-reject"
    app = _app(engine, database_url)
    _seed_draft_candidate(engine, candidate_id)
    draft_before = _row(
        engine,
        "SELECT content_revision, content_hash FROM page_drafts WHERE page_id = :page_id",
        page_id=PAGE_A,
    )
    with TestClient(app) as client:
        headers = {
            **_login(client),
            "Idempotency-Key": "synthetic-reject-once",
        }
        first = client.post(
            f"/api/v1/candidates/{candidate_id}/reject",
            headers=headers,
            json={"reason": "synthetic-no"},
        )
        assert first.status_code == 201, first.text
        replayed = client.post(
            f"/api/v1/candidates/{candidate_id}/reject",
            headers=headers,
            json={"reason": "synthetic-no"},
        )
        assert replayed.status_code == 200, replayed.text
    assert first.json()["data"]["status"] == "rejected"
    assert "project_revision" not in first.json()["meta"]
    assert first.json()["meta"]["idempotency_replayed"] is False
    assert first.json()["meta"]["audit_event_id"] == first.json()["data"]["audit_event_id"]
    assert replayed.json()["meta"]["idempotency_replayed"] is True
    assert replayed.json()["data"]["disposition_id"] == first.json()["data"]["disposition_id"]
    disposition = _row(
        engine,
        """
        SELECT outcome, reason, preview_id FROM candidate_dispositions
        WHERE candidate_id = :candidate_id
        """,
        candidate_id=candidate_id,
    )
    assert tuple(disposition) == ("rejected", "synthetic-no", None)
    assert tuple(draft_before) == tuple(
        _row(
            engine,
            "SELECT content_revision, content_hash FROM page_drafts WHERE page_id = :page_id",
            page_id=PAGE_A,
        )
    )
    assert _scalar(
        engine,
        "SELECT revision FROM projects WHERE id = :project_id",
        project_id=PROJECT_A,
    ) == 1


def test_wrong_preview_hash_is_preview_changed(engine, database_url):
    candidate_id = "synthetic-http-hash"
    app = _app(engine, database_url)
    seeded = _seed_draft_candidate(engine, candidate_id)
    with TestClient(app) as client:
        headers = _login(client)
        preview = _post_preview(client, headers, candidate_id, _preview_body(seeded))
        assert preview.status_code == 200, preview.text
        saved = preview.json()["data"]["preview_hash"]
        wrong = saved[:-1] + ("0" if saved[-1] != "0" else "1")
        rejected = client.post(
            f"/api/v1/candidates/{candidate_id}/commit",
            headers={**headers, "Idempotency-Key": "synthetic-wrong-hash"},
            json={
                "preview_id": preview.json()["data"]["preview_id"],
                "preview_hash": wrong,
                "expected_page_version_id": seeded["page_version_id"],
                "expected_draft_content_revision": seeded["draft_revision"],
            },
        )
        assert rejected.status_code == 409, rejected.text
    error = rejected.json()["error"]
    assert error["code"] == "PREVIEW_CHANGED"
    assert error["message"] == "Preview changed."
    assert error["retryable"] is False
    assert error["request_id"]
    assert _scalar(
        engine,
        "SELECT status FROM candidates WHERE id = :candidate_id",
        candidate_id=candidate_id,
    ) == "pending"
    assert _scalar(engine, "SELECT count(*) FROM candidate_dispositions") == 0
    assert _scalar(
        engine,
        "SELECT content_revision FROM page_drafts WHERE page_id = :page_id",
        page_id=PAGE_A,
    ) == 1


def test_anonymous_candidate_commands_require_a_session(engine, database_url):
    candidate_id = "synthetic-http-anon"
    app = _app(engine, database_url)
    seeded = _seed_draft_candidate(engine, candidate_id)
    origin = {"Origin": ORIGIN}
    with TestClient(app) as client:
        preview = client.post(
            f"/api/v1/candidates/{candidate_id}/preview",
            headers=origin,
            json=_preview_body(seeded),
        )
        commit = client.post(
            f"/api/v1/candidates/{candidate_id}/commit",
            headers=origin,
            json={
                "preview_id": "synthetic-preview",
                "preview_hash": "ab" * 32,
                "expected_page_version_id": seeded["page_version_id"],
                "expected_draft_content_revision": seeded["draft_revision"],
            },
        )
        reject = client.post(
            f"/api/v1/candidates/{candidate_id}/reject",
            headers=origin,
            json={},
        )
    for response in (preview, commit, reject):
        assert response.status_code == 401, response.text
        assert response.json()["error"]["code"] == "AUTH_REQUIRED"
    assert _scalar(engine, "SELECT count(*) FROM candidate_previews") == 0
    assert _scalar(engine, "SELECT count(*) FROM candidate_dispositions") == 0


def test_openapi_routes_include_a_success_and_a_failure_example(database_url):
    app = create_app(identity_settings(database_url))
    spec = app.openapi()
    for path in (
        "/api/v1/candidates/{candidate_id}/preview",
        "/api/v1/candidates/{candidate_id}/commit",
        "/api/v1/candidates/{candidate_id}/reject",
    ):
        responses = spec["paths"][path]["post"]["responses"]
        success = set()
        failure = set()
        for status, payload in responses.items():
            documented = payload.get("content", {}).get("application/json", {})
            if "example" not in documented and "examples" not in documented:
                continue
            code = str(status)
            if code.startswith("2"):
                success.add(code)
            if code.startswith("4"):
                failure.add(code)
        assert success, path
        assert failure, path
