"""Authorized review opinions stay on the page version they were written against."""

import json

from fastapi.testclient import TestClient
from sqlalchemy import text

from sinuo.api.app import create_app
from sinuo.domain.public_contract import CSRF_HEADER
from sinuo.domain.schema_types import canonical_document, document_hash
from sinuo.infrastructure.database import write_uow
from sinuo.infrastructure.domain_models import PageDraft
from tests.acceptance.identity_fixtures import (
    LEAD_LOGIN,
    LEAD_PASSWORD,
    NOW,
    OTHER_LOGIN,
    OTHER_PASSWORD,
    PAGE_A,
    PROJECT_A,
    REVIEWER_ID,
    REVIEWER_LOGIN,
    REVIEWER_PASSWORD,
    TEAM_LOGIN,
    TEAM_PASSWORD,
    identity_settings,
    seed_identity_world,
)

ORIGIN = "http://127.0.0.1:8000"
ORIGINAL = "synthetic-opinion-original"
EDITED = "synthetic-opinion-edited"
FORGED = "synthetic-opinion-forged-rewrite"
REVOKED_TEXT = "synthetic-opinion-revoked-rewrite"


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


def _version(engine) -> tuple[str, str]:
    with engine.connect() as connection:
        row = connection.execute(
            text(
                """
                SELECT id, content_json
                FROM page_versions
                WHERE page_id = :page_id
                """
            ),
            {"page_id": PAGE_A},
        ).one()
    return str(row.id), str(row.content_json)


def _opinion(engine, opinion_id: str):
    with engine.connect() as connection:
        return connection.execute(
            text(
                """
                SELECT author_account_id, page_version_id, node_id, body, revision, visibility
                FROM review_opinions
                WHERE id = :opinion_id
                """
            ),
            {"opinion_id": opinion_id},
        ).one()


def _project_revision(engine) -> int:
    with engine.connect() as connection:
        value = connection.execute(
            text("SELECT revision FROM projects WHERE id = :project_id"),
            {"project_id": PROJECT_A},
        ).scalar_one()
    return int(value)


def _post(client, csrf: str, version_id: str, *, key: str, body: str, node_id: str | None):
    payload: dict[str, object] = {
        "page_version_id": version_id,
        "body": body,
        "visibility": "project",
    }
    if node_id is not None:
        payload["node_id"] = node_id
    return client.post(
        f"/api/v1/projects/{PROJECT_A}/review-opinions",
        headers=_headers(csrf, key),
        json=payload,
    )


def _patch(client, csrf: str, opinion_id: str, *, key: str, revision: int, body: str, extra=None):
    payload: dict[str, object] = {"expected_revision": revision, "body": body}
    if extra:
        payload.update(extra)
    return client.patch(
        f"/api/v1/review-opinions/{opinion_id}",
        headers=_headers(csrf, key),
        json=payload,
    )


def test_author_edits_own_opinion(engine, database_url):
    settings = identity_settings(database_url)
    seed_identity_world(engine, settings)
    version_id, frozen = _version(engine)
    app = create_app(settings)
    with TestClient(app) as client:
        csrf = _login(client, REVIEWER_LOGIN, REVIEWER_PASSWORD)
        forged_body = {
            "page_version_id": version_id,
            "node_id": "synthetic-node-sensitive",
            "body": ORIGINAL,
            "visibility": "project",
            "author_account_id": "forged-author",
        }
        forged = client.post(
            f"/api/v1/projects/{PROJECT_A}/review-opinions",
            headers=_headers(csrf, "synthetic-opinion-forged-author"),
            json=forged_body,
        )
        assert forged.status_code == 422, forged.text
        assert forged.json()["error"]["code"] == "INVALID_SCHEMA"
        missing = _post(
            client,
            csrf,
            version_id,
            key="synthetic-opinion-missing-node",
            body=ORIGINAL,
            node_id="synthetic-missing-node",
        )
        assert missing.status_code == 422, missing.text
        assert missing.json()["error"]["code"] == "INVALID_ANCHOR"
        created = _post(
            client,
            csrf,
            version_id,
            key="synthetic-opinion-create",
            body=ORIGINAL,
            node_id="synthetic-node-sensitive",
        )
        assert created.status_code == 201, created.text
        data = created.json()["data"]
        opinion_id = data["opinion_id"]
        assert data["author_account_id"] == REVIEWER_ID
        assert data["project_id"] == PROJECT_A
        assert data["page_version_id"] == version_id
        assert data["node_id"] == "synthetic-node-sensitive"
        assert data["body"] == ORIGINAL
        assert data["revision"] == 1
        assert created.json()["meta"]["idempotency_replayed"] is False
        assert _project_revision(engine) == 2
        replayed = _post(
            client,
            csrf,
            version_id,
            key="synthetic-opinion-create",
            body=ORIGINAL,
            node_id="synthetic-node-sensitive",
        )
        assert replayed.status_code == 201, replayed.text
        assert replayed.json()["data"]["opinion_id"] == opinion_id
        assert replayed.json()["meta"]["idempotency_replayed"] is True
        assert _project_revision(engine) == 2
        conflict = _post(
            client,
            csrf,
            version_id,
            key="synthetic-opinion-create",
            body=EDITED,
            node_id="synthetic-node-sensitive",
        )
        assert conflict.status_code == 409, conflict.text
        assert conflict.json()["error"]["code"] == "IDEMPOTENCY_KEY_REUSED"
        edited = _patch(
            client,
            csrf,
            opinion_id,
            key="synthetic-opinion-edit",
            revision=1,
            body=EDITED,
        )
        assert edited.status_code == 200, edited.text
        assert edited.json()["data"]["body"] == EDITED
        assert edited.json()["data"]["revision"] == 2
        assert edited.json()["data"]["page_version_id"] == version_id
        assert edited.json()["data"]["author_account_id"] == REVIEWER_ID
        assert edited.json()["meta"]["idempotency_replayed"] is False
        stale = _patch(
            client,
            csrf,
            opinion_id,
            key="synthetic-opinion-stale",
            revision=1,
            body="synthetic-opinion-stale-body",
        )
        assert stale.status_code == 409, stale.text
        assert stale.json()["error"]["code"] == "DRAFT_REVISION_CONFLICT"
        assert stale.json()["error"]["details"]["category"] == "opinion_revision"
        assert "synthetic-opinion-stale-body" not in stale.text
    row = _opinion(engine, opinion_id)
    assert row.author_account_id == REVIEWER_ID
    assert row.page_version_id == version_id
    assert row.node_id == "synthetic-node-sensitive"
    assert row.body == EDITED
    assert row.revision == 2
    assert _project_revision(engine) == 3
    with engine.connect() as connection:
        stored = connection.execute(
            text("SELECT content_json FROM page_versions WHERE id = :version_id"),
            {"version_id": version_id},
        ).scalar_one()
        count = connection.execute(
            text("SELECT count(*) FROM page_versions WHERE page_id = :page_id"),
            {"page_id": PAGE_A},
        ).scalar_one()
        payloads = connection.execute(
            text(
                """
                SELECT payload FROM audit_events
                WHERE subject_type = 'review_opinion' AND subject_id = :opinion_id
                """
            ),
            {"opinion_id": opinion_id},
        ).scalars().all()
    assert stored == frozen
    assert count == 1
    assert payloads
    for payload in payloads:
        assert ORIGINAL not in payload
        assert EDITED not in payload


def test_non_author_cannot_edit_opinion(engine, database_url):
    settings = identity_settings(database_url)
    seed_identity_world(engine, settings)
    version_id, _frozen = _version(engine)
    app = create_app(settings)
    with TestClient(app) as client:
        csrf = _login(client, REVIEWER_LOGIN, REVIEWER_PASSWORD)
        created = _post(
            client,
            csrf,
            version_id,
            key="synthetic-opinion-non-author",
            body=ORIGINAL,
            node_id="synthetic-node-sensitive",
        )
        assert created.status_code == 201, created.text
        opinion_id = created.json()["data"]["opinion_id"]
        _logout(client, csrf)
        team = _login(client, TEAM_LOGIN, TEAM_PASSWORD)
        team_edit = _patch(
            client,
            team,
            opinion_id,
            key="synthetic-opinion-team-edit",
            revision=1,
            body=FORGED,
        )
        assert team_edit.status_code == 403, team_edit.text
        assert team_edit.json()["error"]["code"] == "ACTION_FORBIDDEN"
        assert ORIGINAL not in team_edit.text
        _logout(client, team)
        lead = _login(client, LEAD_LOGIN, LEAD_PASSWORD)
        lead_edit = _patch(
            client,
            lead,
            opinion_id,
            key="synthetic-opinion-lead-edit",
            revision=1,
            body=FORGED,
        )
        assert lead_edit.status_code == 403, lead_edit.text
        assert lead_edit.json()["error"]["code"] == "ACTION_FORBIDDEN"
        assert ORIGINAL not in lead_edit.text
    row = _opinion(engine, opinion_id)
    assert row.author_account_id == REVIEWER_ID
    assert row.body == ORIGINAL
    assert row.revision == 1
    assert row.page_version_id == version_id
    assert FORGED not in row.body


def test_revoked_author_cannot_edit_opinion(engine, database_url):
    settings = identity_settings(database_url)
    seed_identity_world(engine, settings)
    version_id, _frozen = _version(engine)
    app = create_app(settings)
    with TestClient(app) as client:
        csrf = _login(client, REVIEWER_LOGIN, REVIEWER_PASSWORD)
        created = _post(
            client,
            csrf,
            version_id,
            key="synthetic-opinion-before-revoke",
            body=ORIGINAL,
            node_id="synthetic-node-sensitive",
        )
        assert created.status_code == 201, created.text
        opinion_id = created.json()["data"]["opinion_id"]
        with engine.begin() as connection:
            updated = connection.execute(
                text(
                    """
                    UPDATE object_grants
                    SET status = 'revoked', revoked_at = :now
                    WHERE project_id = :project_id
                      AND account_id = :account_id
                      AND action = 'opinion.edit_own'
                      AND status = 'active'
                    """
                ),
                {
                    "now": 1_761_800_100.0,
                    "project_id": PROJECT_A,
                    "account_id": REVIEWER_ID,
                },
            )
            assert updated.rowcount == 1
        denied = _patch(
            client,
            csrf,
            opinion_id,
            key="synthetic-opinion-after-revoke",
            revision=1,
            body=REVOKED_TEXT,
        )
        assert denied.status_code == 403, denied.text
        assert denied.json()["error"]["code"] == "ACTION_FORBIDDEN"
        assert ORIGINAL not in denied.text
        visible = client.get(f"/api/v1/review-opinions/{opinion_id}")
        assert visible.status_code == 200, visible.text
        assert visible.json()["data"]["body"] == ORIGINAL
        assert visible.json()["data"]["page_version_id"] == version_id
    row = _opinion(engine, opinion_id)
    assert row.body == ORIGINAL
    assert row.revision == 1
    assert row.page_version_id == version_id
    assert REVOKED_TEXT not in row.body


def test_later_page_version_keeps_the_original_anchor(engine, database_url):
    settings = identity_settings(database_url)
    seed_identity_world(engine, settings)
    version_id, frozen = _version(engine)
    app = create_app(settings)
    with TestClient(app) as client:
        csrf = _login(client, REVIEWER_LOGIN, REVIEWER_PASSWORD)
        created = _post(
            client,
            csrf,
            version_id,
            key="synthetic-opinion-anchored",
            body=ORIGINAL,
            node_id="synthetic-node-sensitive",
        )
        assert created.status_code == 201, created.text
        opinion_id = created.json()["data"]["opinion_id"]
        rebound = _patch(
            client,
            csrf,
            opinion_id,
            key="synthetic-opinion-rebind",
            revision=1,
            body=EDITED,
            extra={"page_version_id": "synthetic-later-version"},
        )
        assert rebound.status_code == 422, rebound.text
        _logout(client, csrf)
        later = json.loads(frozen)
        later["nodes"] = [*later["nodes"], {"node_id": "synthetic-node-later"}]
        stored = canonical_document(later)
        digest = document_hash(later)
        with write_uow(engine) as session:
            session.add(
                PageDraft(
                    page_id=PAGE_A,
                    project_id=PROJECT_A,
                    base_version_id=version_id,
                    content_revision=1,
                    content_hash=digest,
                    content_json=stored,
                    schema_version=1,
                    updated_at=NOW,
                )
            )
        lead = _login(client, LEAD_LOGIN, LEAD_PASSWORD)
        saved = client.post(
            f"/api/v1/pages/{PAGE_A}/checkpoints",
            headers=_headers(lead, "synthetic-opinion-checkpoint"),
            json={
                "expected_head_version_id": version_id,
                "expected_content_revision": 1,
                "expected_content_hash": digest,
            },
        )
        assert saved.status_code == 201, saved.text
        new_version_id = saved.json()["data"]["page_version_id"]
        assert new_version_id != version_id
        _logout(client, lead)
        reviewer = _login(client, REVIEWER_LOGIN, REVIEWER_PASSWORD)
        edited = _patch(
            client,
            reviewer,
            opinion_id,
            key="synthetic-opinion-edit-after-version",
            revision=1,
            body=EDITED,
        )
        assert edited.status_code == 200, edited.text
        assert edited.json()["data"]["page_version_id"] == version_id
        assert edited.json()["data"]["node_id"] == "synthetic-node-sensitive"
    row = _opinion(engine, opinion_id)
    assert row.page_version_id == version_id
    assert row.node_id == "synthetic-node-sensitive"
    assert row.body == EDITED
    with engine.connect() as connection:
        head = connection.execute(
            text("SELECT head_version_id FROM pages WHERE id = :page_id"),
            {"page_id": PAGE_A},
        ).scalar_one()
        original = connection.execute(
            text("SELECT content_json FROM page_versions WHERE id = :version_id"),
            {"version_id": version_id},
        ).scalar_one()
        count = connection.execute(
            text("SELECT count(*) FROM page_versions WHERE page_id = :page_id"),
            {"page_id": PAGE_A},
        ).scalar_one()
    assert head == new_version_id
    assert original == frozen
    assert "synthetic-node-later" not in original
    assert count == 2


def test_page_reader_can_read_and_outsider_cannot(engine, database_url):
    settings = identity_settings(database_url)
    seed_identity_world(engine, settings)
    version_id, _frozen = _version(engine)
    app = create_app(settings)
    with TestClient(app) as client:
        csrf = _login(client, REVIEWER_LOGIN, REVIEWER_PASSWORD)
        created = _post(
            client,
            csrf,
            version_id,
            key="synthetic-opinion-readable",
            body=ORIGINAL,
            node_id=None,
        )
        assert created.status_code == 201, created.text
        opinion_id = created.json()["data"]["opinion_id"]
        assert created.json()["data"]["node_id"] is None
        assert created.json()["data"]["page_version_id"] == version_id
        _logout(client, csrf)
        team = _login(client, TEAM_LOGIN, TEAM_PASSWORD)
        visible = client.get(f"/api/v1/review-opinions/{opinion_id}")
        assert visible.status_code == 200, visible.text
        assert visible.json()["data"]["body"] == ORIGINAL
        assert visible.json()["data"]["page_version_id"] == version_id
        assert visible.json()["data"]["author_account_id"] == REVIEWER_ID
        denied = _patch(
            client,
            team,
            opinion_id,
            key="synthetic-opinion-reader-edit",
            revision=1,
            body=FORGED,
        )
        assert denied.status_code == 403, denied.text
        _logout(client, team)
        _login(client, OTHER_LOGIN, OTHER_PASSWORD)
        hidden = client.get(f"/api/v1/review-opinions/{opinion_id}")
        assert hidden.status_code == 404, hidden.text
        assert hidden.json()["error"]["code"] == "NOT_FOUND_OR_HIDDEN"
        assert ORIGINAL not in hidden.text
        _logout(client, client.get("/api/v1/session").json()["data"]["csrf_token"])
        anonymous = client.get(f"/api/v1/review-opinions/{opinion_id}")
        assert anonymous.status_code == 401, anonymous.text
        assert ORIGINAL not in anonymous.text
    row = _opinion(engine, opinion_id)
    assert row.body == ORIGINAL
    assert row.page_version_id == version_id
    assert row.node_id is None
