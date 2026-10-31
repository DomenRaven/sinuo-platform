"""Authorized preview HTTP after registration has committed.

The route is called through ``create_app``. ``write_uow`` is only the existing
registration helper. Rendering stays outside that transaction. This file does
not claim a new Alembic head.
"""

import hashlib
import inspect
import io
import time
from pathlib import Path

import PIL
import pytest
from fastapi.testclient import TestClient
from PIL import Image
from sqlalchemy.orm import Session

from sinuo.api.app import create_app
from sinuo.api.assets.routes import get_artifact_version_preview, post_artifact_version_preview
from sinuo.application.assets.uploads import register_original, request_upload_ticket
from sinuo.application.auth.sessions import issue_session
from sinuo.domain.public_contract import Action, session_cookie_name
from sinuo.infrastructure.assets.objects import read_original
from sinuo.infrastructure.assets.staging import write_attempt_bytes
from sinuo.infrastructure.database import write_uow
from sinuo.infrastructure.domain_models import (
    Account,
    Artifact,
    Membership,
    ObjectGrant,
    Project,
)
from tests.acceptance.identity_fixtures import identity_settings

NOW = 1_700_000_000.0
ACCOUNT_ID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaa1"
READER_ID = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbb1"
MEMBER_ID = "cccccccc-cccc-4ccc-8ccc-ccccccccccc1"
PROJECT_ID = "synthetic-project-asset"
ARTIFACT_ID = "synthetic-artifact-asset"
PASSWORD = "$argon2id$synthetic-hash"
PREVIEW_PATH = "/api/v1/artifact-versions/{artifact_version_id}/preview"
HOSTILE = {
    "relative_path": "../outside.png",
    "path": "C:\\secret\\store\\preview.png",
    "max_pixels": 1,
    "objects_root": "C:\\secret\\store",
    "derived_root": "C:\\secret\\derived",
}


@pytest.fixture
def assets(engine):
    return engine


def _png_bytes() -> bytes:
    payload = io.BytesIO()
    image = Image.new("RGB", (2, 2), (12, 34, 56))
    try:
        image.save(payload, format="PNG")
    finally:
        image.close()
    return payload.getvalue()


def _files(root: Path) -> list[Path]:
    return sorted(path for path in root.rglob("*") if path.is_file())


def _seed(session: Session) -> None:
    session.add(
        Project(
            id=PROJECT_ID,
            title="synthetic-asset-project",
            brief=None,
            template_key=None,
            revision=1,
            access_epoch=1,
            created_at=NOW,
        )
    )
    session.add(
        Account(
            id=ACCOUNT_ID,
            login_name="synthetic-uploader",
            display_name="synthetic-uploader",
            password_hash=PASSWORD,
            status="active",
            auth_epoch=1,
            can_create_project=1,
            created_at=NOW,
            disabled_at=None,
        )
    )
    session.flush()
    session.add(
        Artifact(
            id=ARTIFACT_ID,
            project_id=PROJECT_ID,
            title="synthetic-asset-title",
            created_at=NOW,
        )
    )
    session.flush()


def _account(session: Session, account_id: str, login_name: str) -> None:
    session.add(
        Account(
            id=account_id,
            login_name=login_name,
            display_name=login_name,
            password_hash=PASSWORD,
            status="active",
            auth_epoch=1,
            can_create_project=0,
            created_at=NOW,
            disabled_at=None,
        )
    )


def _grant(
    session: Session,
    *,
    grant_id: str,
    account_id: str,
    action: Action,
    artifact_version_id: str,
) -> None:
    session.add(
        ObjectGrant(
            id=grant_id,
            project_id=PROJECT_ID,
            account_id=account_id,
            action=action.value,
            scope_type="artifact_version",
            status="active",
            created_at=NOW,
            revoked_at=None,
            created_by_account_id=ACCOUNT_ID,
            stage_id=None,
            page_id=None,
            page_version_id=None,
            artifact_id=None,
            artifact_version_id=artifact_version_id,
            evidence_anchor_id=None,
        )
    )


def _visible(response) -> str:
    text = response.content.decode("latin-1")
    for value in response.headers.values():
        text = text + "\n" + value
    return text


def _assert_hidden(response, secrets: tuple[str, ...]) -> None:
    text = _visible(response)
    for secret in secrets:
        assert secret not in text
        assert secret.replace("\\", "\\\\") not in text


def _assert_render_outside_write() -> None:
    source = inspect.getsource(post_artifact_version_preview)
    assert "write_uow" not in source
    in_session = False
    rendered_outside = False
    for line in source.splitlines():
        stripped = line.strip()
        if stripped.startswith("with Session("):
            in_session = True
            continue
        if in_session and stripped.startswith("except "):
            in_session = False
        if "render_registered_preview(" in stripped and not in_session:
            rendered_outside = True
    assert rendered_outside


def _assert_get_reads_only() -> None:
    source = inspect.getsource(get_artifact_version_preview)
    assert "write_uow" not in source
    assert "render_registered_preview" not in source
    assert "render_original_preview" not in source


@pytest.mark.integration
def test_preview_route_renders_outside_the_write_transaction(assets, database_url, tmp_path):
    _assert_render_outside_write()
    _assert_get_reads_only()
    payload = _png_bytes()
    digest = hashlib.sha256(payload).hexdigest()
    attempts = tmp_path / "attempts"
    objects = tmp_path / "objects"
    derived = tmp_path / "derived"
    attempts.mkdir()
    objects.mkdir()
    derived.mkdir()
    with write_uow(assets) as session:
        _seed(session)
        ticket = request_upload_ticket(
            session,
            project_id=PROJECT_ID,
            artifact_id=ARTIFACT_ID,
            expected_size=len(payload),
            declared_hash=digest,
            media_type="image/png",
            license_name="synthetic-license",
            sensitivity="project",
            ai_processing_allowed=0,
            account_id=ACCOUNT_ID,
            now=NOW,
        )
    relative = f"{ticket.upload_id}/attempt.part"
    write_attempt_bytes(attempts, relative, payload)
    with write_uow(assets) as session:
        outcome = register_original(
            session,
            upload_id=ticket.upload_id,
            temp_root=attempts,
            relative_path=relative,
            objects_root=objects,
            now=NOW + 1.0,
            converter_installed=False,
        )
        assert outcome.registered is True
        assert outcome.preview_generated is False
        version_id = outcome.artifact_version_id
        generation_id = outcome.generation_id
        assert isinstance(version_id, str)
        assert isinstance(generation_id, str)
        _account(session, READER_ID, "synthetic-reader")
        _account(session, MEMBER_ID, "synthetic-member")
        session.flush()
        session.add(
            Membership(
                id="synthetic-mem-reader",
                project_id=PROJECT_ID,
                account_id=READER_ID,
                status="active",
                role_label=None,
                created_at=NOW,
                revoked_at=None,
            )
        )
        session.add(
            Membership(
                id="synthetic-mem-member",
                project_id=PROJECT_ID,
                account_id=MEMBER_ID,
                status="active",
                role_label=None,
                created_at=NOW,
                revoked_at=None,
            )
        )
        _grant(
            session,
            grant_id="synthetic-grant-reader-preview",
            account_id=READER_ID,
            action=Action.PREVIEW_READ,
            artifact_version_id=version_id,
        )
        _grant(
            session,
            grant_id="synthetic-grant-member-original",
            account_id=MEMBER_ID,
            action=Action.ARTIFACT_ORIGINAL_READ,
            artifact_version_id=version_id,
        )
    assert read_original(objects, PROJECT_ID, digest, generation_id) == payload
    assert _files(derived) == []

    settings = identity_settings(database_url)
    app = create_app(settings)
    assert PREVIEW_PATH in app.openapi()["paths"]
    assert "post" in app.openapi()["paths"][PREVIEW_PATH]
    assert "get" in app.openapi()["paths"][PREVIEW_PATH]
    cookie = session_cookie_name(settings.public_url)

    def token_for(account_id: str) -> str:
        with write_uow(assets) as session:
            account = session.get(Account, account_id)
            assert account is not None
            return issue_session(session, account, settings, time.time()).token

    reader_token = token_for(READER_ID)
    member_token = token_for(MEMBER_ID)
    url = f"/api/v1/artifact-versions/{version_id}/preview"
    secrets = (
        str(objects.resolve()),
        str(derived.resolve()),
        str(tmp_path.resolve()),
        str(attempts.resolve()),
        "C:\\secret\\store",
        "C:\\secret\\derived",
        "outside.png",
        "max_pixels",
        "relative_path",
    )

    def assert_no_preview_file() -> None:
        assert _files(derived) == []
        assert list(tmp_path.rglob("*.png")) == []
        assert not (tmp_path / "outside.png").exists()

    with TestClient(app) as client:
        app.state.objects_root = objects
        app.state.derived_root = derived
        anonymous = client.post(url, json=HOSTILE)
        assert anonymous.status_code == 401
        assert anonymous.json()["error"]["code"] == "AUTH_REQUIRED"
        assert "Pillow" not in anonymous.text
        assert "generator_name" not in anonymous.text
        assert "generator_version" not in anonymous.text
        assert payload not in anonymous.content
        _assert_hidden(anonymous, secrets)
        assert_no_preview_file()

        client.cookies.set(cookie, member_token)
        denied = client.post(url, json=HOSTILE)
        assert denied.status_code == 403
        assert denied.json()["error"]["code"] == "ACTION_FORBIDDEN"
        assert denied.json()["error"]["details"]["action"] == "preview.read"
        assert "Pillow" not in denied.text
        assert "generator_name" not in denied.text
        assert "generator_version" not in denied.text
        assert "rendered" not in denied.text
        assert payload not in denied.content
        _assert_hidden(denied, secrets)
        assert_no_preview_file()

        client.cookies.set(cookie, reader_token)
        app.state.objects_root = None
        app.state.derived_root = None
        missing = client.post(url, json=HOSTILE)
        assert missing.status_code == 200
        assert missing.headers["cache-control"] == "no-store"
        assert missing.headers["x-content-type-options"] == "nosniff"
        assert missing.json() == {
            "generated": False,
            "state": "unavailable",
            "reason": "storage_not_configured",
            "generator_name": "",
            "generator_version": "",
        }
        assert "Pillow" not in missing.text
        _assert_hidden(missing, secrets)
        assert_no_preview_file()

        app.state.objects_root = objects
        objects_only = client.post(url, json=HOSTILE)
        assert objects_only.status_code == 200
        assert objects_only.json()["generated"] is False
        assert objects_only.json()["state"] == "unavailable"
        assert "Pillow" not in objects_only.text
        _assert_hidden(objects_only, secrets)
        assert_no_preview_file()

        app.state.derived_root = derived
        title = "synthetic-asset-title"
        absent = client.get(url)
        assert absent.status_code == 404
        assert absent.json()["error"]["code"] == "NOT_FOUND_OR_HIDDEN"
        assert absent.headers["x-content-type-options"] == "nosniff"
        assert b"\x89PNG" not in absent.content
        assert title not in _visible(absent)
        _assert_hidden(absent, secrets)
        assert_no_preview_file()
        hidden = client.get("/api/v1/artifact-versions/missing-version-id/preview")
        assert hidden.status_code == 404
        assert hidden.json()["error"]["code"] == "NOT_FOUND_OR_HIDDEN"
        assert b"\x89PNG" not in hidden.content
        assert title not in _visible(hidden)
        _assert_hidden(hidden, secrets)
        assert_no_preview_file()
        assert not (derived / "previews" / "missing-version-id.png").exists()

        allowed = client.post(url, json=HOSTILE)
        assert allowed.status_code == 200
        assert allowed.headers["cache-control"] == "no-store"
        assert allowed.headers["x-content-type-options"] == "nosniff"
        assert set(allowed.json()) == {
            "generated",
            "state",
            "reason",
            "generator_name",
            "generator_version",
        }
        assert allowed.json()["generated"] is True
        assert allowed.json()["state"] == "ready"
        assert allowed.json()["reason"] == "rendered"
        assert allowed.json()["generator_name"] == "Pillow"
        assert allowed.json()["generator_version"] == PIL.__version__
        assert payload not in allowed.content
        assert b"\x89PNG" not in allowed.content
        _assert_hidden(allowed, secrets)

        preview_path = derived / "previews" / f"{version_id}.png"
        written = preview_path.read_bytes()
        fetched = client.get(url)
        assert fetched.status_code == 200
        assert fetched.headers["cache-control"] == "no-store"
        assert fetched.headers["x-content-type-options"] == "nosniff"
        assert fetched.headers["content-type"].split(";")[0] == "image/png"
        assert fetched.headers["content-disposition"] == 'inline; filename="preview.png"'
        assert "synthetic-asset-title" not in fetched.headers["content-disposition"]
        assert fetched.content.startswith(b"\x89PNG\r\n\x1a\n")
        assert fetched.content == written
        assert title not in _visible(fetched)
        _assert_hidden(fetched, secrets)
        assert preview_path.read_bytes() == written
        assert _files(derived) == [preview_path]

        client.cookies.set(cookie, member_token)
        denied_get = client.get(url)
        assert denied_get.status_code == 403
        assert denied_get.json()["error"]["code"] == "ACTION_FORBIDDEN"
        assert denied_get.json()["error"]["details"]["action"] == "preview.read"
        assert b"\x89PNG" not in denied_get.content
        assert title not in _visible(denied_get)
        _assert_hidden(denied_get, secrets)
        assert preview_path.read_bytes() == written
        assert _files(derived) == [preview_path]

        client.cookies.clear()
        anonymous_get = client.get(url)
        assert anonymous_get.status_code == 401
        assert anonymous_get.json()["error"]["code"] == "AUTH_REQUIRED"
        assert b"\x89PNG" not in anonymous_get.content
        assert title not in _visible(anonymous_get)
        _assert_hidden(anonymous_get, secrets)
        assert preview_path.read_bytes() == written
        assert _files(derived) == [preview_path]

    preview = derived / "previews" / f"{version_id}.png"
    assert _files(derived) == [preview]
    rendered = preview.read_bytes()
    assert rendered.startswith(b"\x89PNG\r\n\x1a\n")
    with Image.open(preview) as opened:
        assert opened.format == "PNG"
        assert opened.size == (2, 2)
        assert opened.getpixel((0, 0)) == (12, 34, 56)
    assert read_original(objects, PROJECT_ID, digest, generation_id) == payload
    assert not (tmp_path / "outside.png").exists()
    assert not (tmp_path / "escaped").exists()
