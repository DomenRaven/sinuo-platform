"""Preview rendering after original registration has committed.

``write_uow`` is used to register. ``render_registered_preview`` runs after
that block exits. This file does not claim a new Alembic head.
"""

import hashlib
import io
from pathlib import Path

import pytest
from PIL import Image
from sqlalchemy.orm import Session

from sinuo.application.assets.download import download_original
from sinuo.application.assets.registered_preview import render_registered_preview
from sinuo.application.assets.uploads import register_original, request_upload_ticket
from sinuo.application.auth.authorizer import SessionAuthorizer
from sinuo.domain.auth.principal import Principal
from sinuo.domain.public_contract import Action
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

NOW = 1_700_000_000.0
ACCOUNT_ID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaa1"
READER_ID = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbb1"
PROJECT_ID = "synthetic-project-asset"
ARTIFACT_ID = "synthetic-artifact-asset"
PASSWORD = "$argon2id$synthetic-hash"


@pytest.fixture
def assets(engine):
    return engine


def _png_bytes(size: tuple[int, int], color: tuple[int, int, int]) -> bytes:
    payload = io.BytesIO()
    image = Image.new("RGB", size, color)
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


def _principal(account_id: str) -> Principal:
    return Principal(
        account_id=account_id,
        session_id=f"synthetic-session-{account_id[:8]}",
        auth_epoch=1,
    )


def _register(assets, tmp_path: Path, payload: bytes, media_type: str):
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
            media_type=media_type,
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
        )
        assert outcome.registered is True
        assert outcome.preview_generated is False
        assert outcome.preview_state == "not_run"
        assert outcome.preview_reason == "converter_not_invoked"
        assert isinstance(outcome.artifact_version_id, str)
        assert isinstance(outcome.generation_id, str)
        assert outcome.content_hash == digest
        session.add(
            Account(
                id=READER_ID,
                login_name="synthetic-reader",
                display_name="synthetic-reader",
                password_hash=PASSWORD,
                status="active",
                auth_epoch=1,
                can_create_project=0,
                created_at=NOW,
                disabled_at=None,
            )
        )
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
            ObjectGrant(
                id="synthetic-grant-reader-original",
                project_id=PROJECT_ID,
                account_id=READER_ID,
                action=Action.ARTIFACT_ORIGINAL_READ.value,
                scope_type="artifact_version",
                status="active",
                created_at=NOW,
                revoked_at=None,
                created_by_account_id=ACCOUNT_ID,
                stage_id=None,
                page_id=None,
                page_version_id=None,
                artifact_id=None,
                artifact_version_id=outcome.artifact_version_id,
                evidence_anchor_id=None,
            )
        )
    stored = read_original(objects, PROJECT_ID, digest, outcome.generation_id)
    assert stored == payload
    assert _files(derived) == []
    return outcome, objects, derived, digest


def _download(assets, objects: Path, version_id: str):
    with Session(assets) as session:
        return download_original(
            session,
            _principal(READER_ID),
            SessionAuthorizer(session),
            project_id=PROJECT_ID,
            artifact_version_id=version_id,
            objects_root=objects,
        )


@pytest.mark.integration
def test_registered_png_renders_after_commit(assets, tmp_path):
    payload = _png_bytes((2, 2), (12, 34, 56))
    outcome, objects, derived, digest = _register(assets, tmp_path, payload, "image/png")
    version_id = outcome.artifact_version_id
    generation_id = outcome.generation_id
    assert isinstance(version_id, str)
    assert isinstance(generation_id, str)
    assert _files(derived) == []

    result = render_registered_preview(
        objects_root=objects,
        project_id=PROJECT_ID,
        content_hash=digest,
        generation_id=generation_id,
        media_type="image/png",
        derived_root=derived,
        relative_path="preview.png",
        max_pixels=4,
    )

    preview = derived / "preview.png"
    rendered = preview.read_bytes()
    assert result.generated is True
    assert result.state == "ready"
    assert result.reason == "rendered"
    assert result.generator_name == "Pillow"
    assert result.relative_path == "preview.png"
    assert rendered.startswith(b"\x89PNG\r\n\x1a\n")
    assert _files(derived) == [preview]
    with Image.open(preview) as opened:
        assert opened.format == "PNG"
        assert opened.size == (2, 2)
        assert opened.getpixel((0, 0)) == (12, 34, 56)
    assert read_original(objects, PROJECT_ID, digest, generation_id) == payload
    downloaded = _download(assets, objects, version_id)
    assert downloaded.body == payload
    assert downloaded.server_hash == digest
    assert downloaded.server_hash == hashlib.sha256(downloaded.body).hexdigest()
    assert downloaded.byte_size == len(payload)
    assert downloaded.media_type == "image/png"


@pytest.mark.integration
def test_oversized_registered_png_leaves_the_original_downloadable(assets, tmp_path):
    payload = _png_bytes((6, 6), (1, 2, 3))
    outcome, objects, derived, digest = _register(assets, tmp_path, payload, "image/png")
    version_id = outcome.artifact_version_id
    generation_id = outcome.generation_id
    assert isinstance(version_id, str)
    assert isinstance(generation_id, str)

    result = render_registered_preview(
        objects_root=objects,
        project_id=PROJECT_ID,
        content_hash=digest,
        generation_id=generation_id,
        media_type="image/png",
        derived_root=derived,
        relative_path="preview.png",
        max_pixels=35,
    )

    assert result.generated is False
    assert result.state == "unsupported"
    assert result.reason == "pixel_limit"
    assert result.relative_path is None
    assert _files(derived) == []
    assert not (derived / "preview.png").exists()
    assert read_original(objects, PROJECT_ID, digest, generation_id) == payload
    downloaded = _download(assets, objects, version_id)
    assert downloaded.body == payload
    assert downloaded.server_hash == digest
    assert downloaded.server_hash == hashlib.sha256(downloaded.body).hexdigest()
    assert downloaded.byte_size == len(payload)
    assert downloaded.artifact_version_id == version_id
