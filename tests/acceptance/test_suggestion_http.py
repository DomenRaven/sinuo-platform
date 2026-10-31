"""HTTP read and CAS for the signed-in actor's suggestion preference."""

from fastapi.testclient import TestClient
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from sinuo.api.app import create_app
from sinuo.domain.public_contract import CSRF_HEADER
from sinuo.infrastructure.database import write_uow
from sinuo.infrastructure.suggestions import SuggestionPreference
from tests.acceptance.identity_fixtures import (
    LEAD_LOGIN,
    LEAD_PASSWORD,
    OTHER_LOGIN,
    OTHER_PASSWORD,
    PROJECT_A,
    REVIEWER_LOGIN,
    REVIEWER_PASSWORD,
    identity_settings,
    seed_identity_world,
)

ORIGIN = "http://127.0.0.1:8000"
PATH = f"/api/v1/projects/{PROJECT_A}/suggestion-preferences/me"
SCOPE_LEAF = "synthetic-preference-scope-leaf"


def _login(client: TestClient, login_name: str, password: str) -> str:
    response = client.post(
        "/api/v1/session",
        headers={"Origin": ORIGIN},
        json={"login_name": login_name, "password": password},
    )
    assert response.status_code == 204
    token = client.get("/api/v1/session").json()["data"]["csrf_token"]
    assert isinstance(token, str) and token
    return token


def _count(engine) -> int:
    with Session(engine) as session:
        counted = session.scalar(select(func.count()).select_from(SuggestionPreference))
    assert isinstance(counted, int)
    return counted


def test_preference_http_reads_default_then_saves_only_the_actor(engine, database_url):
    settings = identity_settings(database_url)
    seed_identity_world(engine, settings)
    app = create_app(settings)
    assert PATH.replace(PROJECT_A, "{project_id}") in app.openapi()["paths"]
    before = _count(engine)
    with TestClient(app) as client:
        anonymous = client.get(PATH)
        assert anonymous.status_code == 401
        assert anonymous.headers["cache-control"] == "no-store"
        assert SCOPE_LEAF not in anonymous.text
        csrf = _login(client, LEAD_LOGIN, LEAD_PASSWORD)
        first = client.get(PATH)
        assert first.status_code == 200
        assert first.headers["cache-control"] == "no-store"
        assert first.json()["data"] == {
            "project_id": PROJECT_A,
            "mode": "manual",
            "scope": {},
            "cooldown_seconds": 0,
            "digest_interval_seconds": 86400,
            "revision": 1,
            "next_due_at": None,
            "stored": False,
        }
        assert "project_revision" not in first.json()["meta"]
        assert _count(engine) == before
        headers = {
            "Origin": ORIGIN,
            CSRF_HEADER: csrf,
            "Idempotency-Key": "synthetic-preference-save",
        }
        saved = client.patch(
            PATH,
            headers=headers,
            json={
                "mode": "event",
                "scope": {"leaf": SCOPE_LEAF},
                "cooldown_seconds": 30,
                "digest_interval_seconds": 3600,
                "expected_revision": 1,
            },
        )
        assert saved.status_code == 200, saved.text
        assert saved.json()["meta"]["idempotency_replayed"] is False
        assert "project_revision" not in saved.json()["meta"]
        assert saved.json()["data"]["mode"] == "event"
        assert saved.json()["data"]["revision"] == 2
        assert saved.json()["data"]["stored"] is True
        assert saved.json()["data"]["scope"] == {"leaf": SCOPE_LEAF}
        replayed = client.patch(
            PATH,
            headers=headers,
            json={
                "mode": "event",
                "scope": {"leaf": SCOPE_LEAF},
                "cooldown_seconds": 30,
                "digest_interval_seconds": 3600,
                "expected_revision": 1,
            },
        )
        assert replayed.status_code == 200
        assert replayed.json()["meta"]["idempotency_replayed"] is True
        assert replayed.json()["data"]["revision"] == 2
        reused = client.patch(
            PATH,
            headers=headers,
            json={
                "mode": "off",
                "scope": {"leaf": SCOPE_LEAF},
                "cooldown_seconds": 30,
                "digest_interval_seconds": 3600,
                "expected_revision": 1,
            },
        )
        assert reused.status_code == 409
        assert reused.json()["error"]["code"] == "IDEMPOTENCY_KEY_REUSED"
        stale = client.patch(
            PATH,
            headers={**headers, "Idempotency-Key": "synthetic-preference-stale"},
            json={
                "mode": "digest",
                "scope": {},
                "cooldown_seconds": 0,
                "digest_interval_seconds": 86400,
                "expected_revision": 1,
            },
        )
        assert stale.status_code == 409
        assert stale.json()["error"]["code"] == "PREFERENCE_REVISION_CONFLICT"
        assert stale.json()["error"]["details"]["category"] == "preference_revision"
        assert SCOPE_LEAF not in stale.text
        current = client.get(PATH)
        assert current.json()["data"]["mode"] == "event"
        assert current.json()["data"]["revision"] == 2
    with engine.connect() as connection:
        revision = connection.execute(
            text("SELECT revision FROM projects WHERE id = :project_id"),
            {"project_id": PROJECT_A},
        ).scalar_one()
    assert revision == 1
    with TestClient(app) as client:
        _login(client, REVIEWER_LOGIN, REVIEWER_PASSWORD)
        own = client.get(PATH)
        assert own.status_code == 200
        assert own.json()["data"]["stored"] is False
        assert own.json()["data"]["scope"] == {}
        assert SCOPE_LEAF not in own.text
        denied = client.patch(
            PATH,
            headers={
                "Origin": ORIGIN,
                CSRF_HEADER: client.get("/api/v1/session").json()["data"]["csrf_token"],
                "Idempotency-Key": "synthetic-preference-reviewer",
            },
            json={
                "mode": "off",
                "scope": {"leaf": SCOPE_LEAF},
                "cooldown_seconds": 0,
                "digest_interval_seconds": 86400,
                "expected_revision": 1,
            },
        )
        assert denied.status_code == 403
        assert denied.json()["error"]["code"] == "ACTION_FORBIDDEN"
        assert SCOPE_LEAF not in denied.text
    with TestClient(app) as client:
        _login(client, OTHER_LOGIN, OTHER_PASSWORD)
        hidden = client.get(PATH)
        assert hidden.status_code == 404
        assert hidden.json()["error"]["code"] == "NOT_FOUND_OR_HIDDEN"
        assert SCOPE_LEAF not in hidden.text
    with write_uow(engine) as session:
        rows = session.scalars(select(SuggestionPreference)).all()
    assert len(rows) == 1
    assert rows[0].revision == 2
    assert rows[0].mode == "event"
