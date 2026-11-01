"""HTTP upload: create an artifact, stage bytes, register a new version.

``create_app`` mounts these routes. The same name and new bytes keep the
previous content hash.
"""

import hashlib
import json
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import text

import sinuo.api.assets.uploads as upload_api
from sinuo.api.app import create_app
from sinuo.api.assets.uploads import UPLOAD_BYTE_LIMIT
from sinuo.domain.public_contract import CSRF_HEADER
from tests.acceptance.identity_fixtures import (
    LEAD_LOGIN,
    LEAD_PASSWORD,
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
NAME = "synthetic-upload-work"
BYTES_A = b"sinuo-upload-bytes-a"
BYTES_B = b"sinuo-upload-bytes-b"


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


def _sha(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _ticket_body(name: str, payload: bytes, *, artifact_id: str | None = None) -> dict[str, object]:
    body: dict[str, object] = {
        "name": name,
        "media_type": "application/octet-stream",
        "size": len(payload),
        "declared_hash": _sha(payload),
        "license": "synthetic-license",
        "sensitivity": "project",
        "ai_processing_allowed": 0,
    }
    if artifact_id is not None:
        body["artifact_id"] = artifact_id
    return body


def _files(root: Path) -> list[str]:
    return sorted(path.relative_to(root).as_posix() for path in root.rglob("*") if path.is_file())


def _prepare(tmp_path: Path, database_url: str, engine):
    settings = identity_settings(database_url)
    seed_identity_world(engine, settings)
    uploads = tmp_path / "uploads"
    objects = tmp_path / "objects"
    uploads.mkdir()
    objects.mkdir()
    app = create_app(settings)
    app.state.uploads_root = uploads
    app.state.objects_root = objects
    return app, uploads, objects


def _versions(engine, artifact_id: str) -> list[tuple[int, str, int]]:
    with engine.connect() as connection:
        rows = connection.execute(
            text(
                """
                SELECT version_no, content_hash, project_revision
                FROM artifact_versions
                WHERE artifact_id = :artifact_id
                ORDER BY version_no
                """
            ),
            {"artifact_id": artifact_id},
        ).all()
    return [(int(row.version_no), str(row.content_hash), int(row.project_revision)) for row in rows]


def _titles(engine, project_id: str, title: str) -> list[str]:
    with engine.connect() as connection:
        rows = connection.execute(
            text(
                """
                SELECT id FROM artifacts
                WHERE project_id = :project_id AND title = :title
                ORDER BY id
                """
            ),
            {"project_id": project_id, "title": title},
        ).all()
    return [str(row.id) for row in rows]


def _sensitive_hash(engine) -> str:
    with engine.connect() as connection:
        value = connection.execute(
            text(
                """
                SELECT content_hash FROM artifact_versions
                WHERE artifact_id = 'synthetic-artifact-sensitive'
                """
            )
        ).scalar_one()
    return str(value)


def _open(client, csrf: str, *, project_id: str, key: str, name: str, payload: bytes):
    return client.post(
        f"/api/v1/projects/{project_id}/artifacts/uploads",
        headers=_headers(csrf, key),
        json=_ticket_body(name, payload),
    )


def _put(client, csrf: str, ticket_id: str, *, key: str, payload: bytes):
    return client.put(
        f"/api/v1/uploads/{ticket_id}/content",
        headers=_headers(csrf, key),
        content=payload,
    )


def _complete(client, csrf: str, ticket_id: str, *, key: str, declared_hash: str):
    return client.post(
        f"/api/v1/uploads/{ticket_id}/complete",
        headers=_headers(csrf, key),
        json={"declared_hash": declared_hash},
    )


def test_create_app_mounts_upload_routes():
    app = create_app(identity_settings("sqlite://"))
    paths = app.openapi()["paths"]
    assert "/api/v1/projects/{project_id}/artifacts/uploads" in paths
    opened = paths["/api/v1/projects/{project_id}/artifacts/uploads"]["post"]["responses"]
    staged = paths["/api/v1/uploads/{ticket_id}/content"]["put"]["responses"]
    registered = paths["/api/v1/uploads/{ticket_id}/complete"]["post"]["responses"]
    assert "ACTION_FORBIDDEN" in json.dumps(opened["403"])
    assert "artifact_created" in json.dumps(opened["201"])
    assert "UPLOAD_TOO_LARGE" in json.dumps(staged["413"])
    assert "staged" in json.dumps(staged["200"])
    assert "derivative_status" in json.dumps(registered["201"])
    assert "declared_hash_mismatch" in json.dumps(registered["422"])


def test_same_name_new_bytes_keep_the_previous_hash(tmp_path, engine, database_url):
    app, uploads, objects = _prepare(tmp_path, database_url, engine)
    sensitive = _sensitive_hash(engine)
    with TestClient(app) as client:
        csrf = _login(client, LEAD_LOGIN, LEAD_PASSWORD)
        opened = _open(
            client,
            csrf,
            project_id=PROJECT_A,
            key="synthetic-upload-open-a",
            name=NAME,
            payload=BYTES_A,
        )
        assert opened.status_code == 201, opened.text
        first = opened.json()["data"]
        assert first["artifact_created"] is True
        assert first["status"] == "open"
        assert first["project_id"] == PROJECT_A
        assert first["declared_hash"] == _sha(BYTES_A)
        assert first["ticket_id"] == first["upload_id"]
        artifact_id = first["artifact_id"]
        ticket_a = first["ticket_id"]
        assert opened.json()["meta"]["idempotency_replayed"] is False
        reused_key = _open(
            client,
            csrf,
            project_id=PROJECT_A,
            key="synthetic-upload-open-a",
            name=NAME,
            payload=BYTES_B,
        )
        assert reused_key.status_code == 409, reused_key.text
        assert reused_key.json()["error"]["code"] == "IDEMPOTENCY_KEY_REUSED"
        replayed = _open(
            client,
            csrf,
            project_id=PROJECT_A,
            key="synthetic-upload-open-a",
            name=NAME,
            payload=BYTES_A,
        )
        assert replayed.status_code == 201, replayed.text
        assert replayed.json()["meta"]["idempotency_replayed"] is True
        assert replayed.json()["data"] == first
        assert _titles(engine, PROJECT_A, NAME) == [artifact_id]
        staged = _put(client, csrf, ticket_a, key="synthetic-upload-put-a", payload=BYTES_A)
        assert staged.status_code == 200, staged.text
        assert staged.json()["data"]["status"] == "staged"
        assert staged.json()["data"]["byte_size"] == len(BYTES_A)
        assert staged.json()["meta"]["idempotency_replayed"] is False
        staged_again = _put(
            client, csrf, ticket_a, key="synthetic-upload-put-a", payload=BYTES_A
        )
        assert staged_again.status_code == 200, staged_again.text
        assert staged_again.json()["meta"]["idempotency_replayed"] is True
        assert _files(uploads) == [f"{ticket_a}/attempt.part"]
        done = _complete(
            client,
            csrf,
            ticket_a,
            key="synthetic-upload-complete-a",
            declared_hash=_sha(BYTES_A),
        )
        assert done.status_code == 201, done.text
        registered = done.json()["data"]
        assert registered["artifact_id"] == artifact_id
        assert registered["version_no"] == 1
        assert registered["content_hash"] == _sha(BYTES_A)
        assert registered["preview_generated"] is False
        assert registered["derivative_status"] == "pending"
        assert registered["preview_state"] == "unavailable"
        assert "object_key" not in registered
        assert str(uploads) not in done.text
        assert str(objects) not in done.text
        assert done.json()["meta"]["idempotency_replayed"] is False
        version_a = registered["artifact_version_id"]
        revision_a = registered["project_revision"]
        replayed_done = _complete(
            client,
            csrf,
            ticket_a,
            key="synthetic-upload-complete-a",
            declared_hash=_sha(BYTES_A),
        )
        assert replayed_done.status_code == 201, replayed_done.text
        assert replayed_done.json()["meta"]["idempotency_replayed"] is True
        assert replayed_done.json()["data"]["artifact_version_id"] == version_a
        assert _versions(engine, artifact_id) == [(1, _sha(BYTES_A), revision_a)]
        second = _open(
            client,
            csrf,
            project_id=PROJECT_A,
            key="synthetic-upload-open-b",
            name=NAME,
            payload=BYTES_B,
        )
        assert second.status_code == 201, second.text
        assert second.json()["data"]["artifact_created"] is False
        assert second.json()["data"]["artifact_id"] == artifact_id
        ticket_b = second.json()["data"]["ticket_id"]
        assert ticket_b != ticket_a
        staged_b = _put(client, csrf, ticket_b, key="synthetic-upload-put-b", payload=BYTES_B)
        assert staged_b.status_code == 200, staged_b.text
        done_b = _complete(
            client,
            csrf,
            ticket_b,
            key="synthetic-upload-complete-b",
            declared_hash=_sha(BYTES_B),
        )
        assert done_b.status_code == 201, done_b.text
        registered_b = done_b.json()["data"]
        assert registered_b["version_no"] == 2
        assert registered_b["content_hash"] == _sha(BYTES_B)
        assert registered_b["content_hash"] != _sha(BYTES_A)
        assert registered_b["artifact_version_id"] != version_a
        assert registered_b["derivative_status"] == "pending"
        revision_b = registered_b["project_revision"]
        assert revision_b > revision_a
        closed = _complete(
            client,
            csrf,
            ticket_a,
            key="synthetic-upload-complete-a-again",
            declared_hash=_sha(BYTES_A),
        )
        assert closed.status_code == 409, closed.text
        assert closed.json()["error"]["code"] == "HEAD_CHANGED"
        assert closed.json()["error"]["details"]["category"] == "ticket_closed"
        assert _versions(engine, artifact_id) == [
            (1, _sha(BYTES_A), revision_a),
            (2, _sha(BYTES_B), revision_b),
        ]
        assert _titles(engine, PROJECT_A, NAME) == [artifact_id]
        assert _sensitive_hash(engine) == sensitive
        loaded_a = client.get(f"/api/v1/artifact-versions/{version_a}/content")
        loaded_b = client.get(
            f"/api/v1/artifact-versions/{registered_b['artifact_version_id']}/content"
        )
        assert loaded_a.status_code == 200, loaded_a.text
        assert loaded_b.status_code == 200, loaded_b.text
        assert loaded_a.content == BYTES_A
        assert loaded_b.content == BYTES_B
        assert hashlib.sha256(loaded_a.content).hexdigest() == _sha(BYTES_A)
        _logout(client, csrf)
        other = _login(client, OTHER_LOGIN, OTHER_PASSWORD)
        foreign = _open(
            client,
            other,
            project_id=PROJECT_B,
            key="synthetic-upload-open-foreign",
            name=NAME,
            payload=BYTES_A,
        )
        assert foreign.status_code == 201, foreign.text
        assert foreign.json()["data"]["artifact_created"] is True
        assert foreign.json()["data"]["artifact_id"] != artifact_id
        assert _versions(engine, artifact_id) == [
            (1, _sha(BYTES_A), revision_a),
            (2, _sha(BYTES_B), revision_b),
        ]


def test_upload_refuses_unpermitted_callers_and_wrong_bytes(
    tmp_path, engine, database_url, monkeypatch
):
    app, uploads, objects = _prepare(tmp_path, database_url, engine)
    sensitive = _sensitive_hash(engine)
    denied_name = "synthetic-upload-denied"
    with TestClient(app) as client:
        anonymous = client.post(
            f"/api/v1/projects/{PROJECT_A}/artifacts/uploads",
            headers={"Origin": ORIGIN, "Idempotency-Key": "synthetic-upload-anon"},
            json=_ticket_body(denied_name, BYTES_A),
        )
        assert anonymous.status_code == 401, anonymous.text
        assert anonymous.json()["error"]["code"] == "AUTH_REQUIRED"
        csrf = _login(client, LEAD_LOGIN, LEAD_PASSWORD)
        missing_csrf = client.post(
            f"/api/v1/projects/{PROJECT_A}/artifacts/uploads",
            headers={"Origin": ORIGIN, "Idempotency-Key": "synthetic-upload-no-csrf"},
            json=_ticket_body(NAME, BYTES_A),
        )
        assert missing_csrf.status_code == 403, missing_csrf.text
        assert missing_csrf.json()["error"]["details"]["category"] == "csrf"
        missing_origin = client.post(
            f"/api/v1/projects/{PROJECT_A}/artifacts/uploads",
            headers={CSRF_HEADER: csrf, "Idempotency-Key": "synthetic-upload-no-origin"},
            json=_ticket_body(NAME, BYTES_A),
        )
        assert missing_origin.status_code == 403, missing_origin.text
        assert missing_origin.json()["error"]["details"]["category"] == "origin"
        missing_key = client.post(
            f"/api/v1/projects/{PROJECT_A}/artifacts/uploads",
            headers={"Origin": ORIGIN, CSRF_HEADER: csrf},
            json=_ticket_body(NAME, BYTES_A),
        )
        assert missing_key.status_code == 422, missing_key.text
        assert missing_key.json()["error"]["code"] == "INVALID_SCHEMA"
        forged = dict(_ticket_body(NAME, BYTES_A))
        forged["owner_id"] = "forged-owner"
        forged_response = client.post(
            f"/api/v1/projects/{PROJECT_A}/artifacts/uploads",
            headers=_headers(csrf, "synthetic-upload-forged-owner"),
            json=forged,
        )
        assert forged_response.status_code == 422, forged_response.text
        huge = dict(_ticket_body("synthetic-upload-too-big", b"x"))
        huge["size"] = UPLOAD_BYTE_LIMIT + 1
        too_big = client.post(
            f"/api/v1/projects/{PROJECT_A}/artifacts/uploads",
            headers=_headers(csrf, "synthetic-upload-too-big"),
            json=huge,
        )
        assert too_big.status_code == 413, too_big.text
        assert too_big.json()["error"]["code"] == "UPLOAD_TOO_LARGE"
        assert _titles(engine, PROJECT_A, "synthetic-upload-too-big") == []
        _logout(client, csrf)
        team = _login(client, TEAM_LOGIN, TEAM_PASSWORD)
        denied = _open(
            client,
            team,
            project_id=PROJECT_A,
            key="synthetic-upload-team",
            name=denied_name,
            payload=BYTES_A,
        )
        assert denied.status_code == 403, denied.text
        assert denied.json()["error"]["details"]["action"] == "artifact.upload"
        assert _titles(engine, PROJECT_A, denied_name) == []
        _logout(client, team)
        other = _login(client, OTHER_LOGIN, OTHER_PASSWORD)
        hidden = _open(
            client,
            other,
            project_id=PROJECT_A,
            key="synthetic-upload-cross-project",
            name=NAME,
            payload=BYTES_A,
        )
        assert hidden.status_code == 404, hidden.text
        assert hidden.json()["error"]["code"] == "NOT_FOUND_OR_HIDDEN"
        _logout(client, other)
        csrf = _login(client, LEAD_LOGIN, LEAD_PASSWORD)
        assert len(BYTES_A) == len(BYTES_B)
        opened = _open(
            client,
            csrf,
            project_id=PROJECT_A,
            key="synthetic-upload-mismatch-open",
            name="synthetic-upload-mismatch",
            payload=BYTES_A,
        )
        assert opened.status_code == 201, opened.text
        ticket_id = opened.json()["data"]["ticket_id"]
        artifact_id = opened.json()["data"]["artifact_id"]
        staged = _put(
            client, csrf, ticket_id, key="synthetic-upload-mismatch-put", payload=BYTES_B
        )
        assert staged.status_code == 200, staged.text
        rejected = _complete(
            client,
            csrf,
            ticket_id,
            key="synthetic-upload-mismatch-complete",
            declared_hash=_sha(BYTES_A),
        )
        assert rejected.status_code == 422, rejected.text
        assert rejected.json()["error"]["details"]["category"] == "declared_hash_mismatch"
        assert _versions(engine, artifact_id) == []
        again = _complete(
            client,
            csrf,
            ticket_id,
            key="synthetic-upload-mismatch-again",
            declared_hash=_sha(BYTES_A),
        )
        assert again.status_code == 409, again.text
        assert _versions(engine, artifact_id) == []
        assert list(objects.rglob("original")) == []
        assert _sensitive_hash(engine) == sensitive
        before = _files(uploads)
        monkeypatch.setattr(upload_api, "UPLOAD_BYTE_LIMIT", 4)
        limited = _open(
            client,
            csrf,
            project_id=PROJECT_A,
            key="synthetic-upload-limit-open",
            name="synthetic-upload-limit",
            payload=b"ab",
        )
        assert limited.status_code == 201, limited.text
        limit_ticket = limited.json()["data"]["ticket_id"]
        oversized = _put(
            client, csrf, limit_ticket, key="synthetic-upload-limit-put", payload=b"12345"
        )
        assert oversized.status_code == 413, oversized.text
        assert oversized.json()["error"]["code"] == "UPLOAD_TOO_LARGE"
        assert _files(uploads) == before
