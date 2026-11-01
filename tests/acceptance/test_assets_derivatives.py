"""First T05 slice: ticket, attempt bytes, hash check, original registration.

Preview converters are not invoked. A missing converter stays unavailable.
This file does not claim the rest of the T05 acceptance.
"""

import hashlib
import importlib.util
from dataclasses import asdict
from pathlib import Path
from threading import Barrier, BrokenBarrierError, Thread

import pytest
from sqlalchemy import event, func, select, text
from sqlalchemy.orm import Session

from sinuo.application.assets.download import download_original
from sinuo.application.assets.gc import (
    claim_generation,
    delete_claimed_generation,
    install_reserved_original,
    place_upload_pin,
    reserve_generation,
)
from sinuo.application.assets.orphans import discard_unregistered_original
from sinuo.application.assets.preview import (
    PreviewFormatUnsupported,
    PreviewTimedOut,
    PreviewToolMissing,
    preview_disposition,
    run_preview_converter,
)
from sinuo.application.assets.preview_access import (
    PreviewDisclosure,
    PreviewRefusal,
    read_preview_metadata,
    record_preview_metadata,
)
from sinuo.application.assets.uploads import (
    AssetRejected,
    RegistrationConflict,
    register_original,
    request_upload_ticket,
)
from sinuo.application.auth.authorizer import SessionAuthorizer
from sinuo.domain.auth.errors import ActionForbidden, NotFoundOrHidden
from sinuo.domain.auth.principal import Principal
from sinuo.domain.public_contract import Action
from sinuo.infrastructure.assets.generations import ObjectPinRow, StoredObjectRow
from sinuo.infrastructure.assets.objects import object_relative, original_key, read_original
from sinuo.infrastructure.assets.paths import PathEscape
from sinuo.infrastructure.assets.records import UploadTicketRow
from sinuo.infrastructure.assets.staging import AttemptTooLarge, write_attempt_bytes
from sinuo.infrastructure.database import write_uow
from sinuo.infrastructure.domain_models import (
    Account,
    Artifact,
    ArtifactVersion,
    Membership,
    ObjectGrant,
    Project,
)
from sinuo.infrastructure.models import OutboxEvent

ROOT = Path(__file__).resolve().parents[2]
DRAFT = ROOT / "workpool" / "handoffs" / "proposals" / "0005_stored_objects.draft.py"
NOW = 1_700_000_000.0
ACCOUNT_ID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaa1"
READER_ID = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbb1"
MEMBER_ID = "cccccccc-cccc-4ccc-8ccc-ccccccccccc1"
LOOKER_ID = "dddddddd-dddd-4ddd-8ddd-ddddddddddd1"
PROJECT_ID = "synthetic-project-asset"
ARTIFACT_ID = "synthetic-artifact-asset"
PASSWORD = "$argon2id$synthetic-hash"
PAYLOAD = b"sinuo-original-bytes"
OTHER = b"sinuo-other-bytes"


def _load_draft():
    spec = importlib.util.spec_from_file_location("draft_0005_stored_objects", DRAFT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def assets(engine):
    return engine


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


def _ticket(session: Session, *, declared_hash: str, size: int):
    return request_upload_ticket(
        session,
        project_id=PROJECT_ID,
        artifact_id=ARTIFACT_ID,
        expected_size=size,
        declared_hash=declared_hash,
        media_type="model/gltf-binary",
        license_name="synthetic-license",
        sensitivity="project",
        ai_processing_allowed=0,
        account_id=ACCOUNT_ID,
        now=NOW,
    )


@pytest.mark.integration
def test_head_includes_stored_object_tables(assets):
    draft = _load_draft()
    with assets.connect() as connection:
        version = connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
        names = set(
            connection.execute(
                text("SELECT name FROM sqlite_master WHERE type = 'table'")
            ).scalars()
        )
        ticket_columns = {
            row[1]
            for row in connection.execute(text("PRAGMA table_info(upload_tickets)"))
        }
    assert version == "0010_evidence_index"
    assert {"upload_tickets", "stored_objects", "object_pins"} <= names
    assert ticket_columns == set(UploadTicketRow.__table__.columns.keys())
    assert "object_id" in draft.MISSING_ARTIFACT_VERSION_COLUMNS
    assert "generation_id" in draft.MISSING_ARTIFACT_VERSION_COLUMNS
    assert "preview_state" in draft.MISSING_ARTIFACT_VERSION_COLUMNS


@pytest.mark.integration
def test_same_bytes_round_trip_and_do_not_invent_a_preview(assets, tmp_path):
    digest = hashlib.sha256(PAYLOAD).hexdigest()
    attempts = tmp_path / "attempts"
    objects = tmp_path / "objects"
    derived = tmp_path / "derived"
    attempts.mkdir()
    objects.mkdir()
    derived.mkdir()
    with write_uow(assets) as session:
        _seed(session)
        ticket = _ticket(session, declared_hash=digest, size=len(PAYLOAD))
    relative = f"{ticket.upload_id}/attempt-1.part"
    attempt = write_attempt_bytes(attempts, relative, PAYLOAD)
    assert attempt.read_bytes() == PAYLOAD
    with write_uow(assets) as session:
        outcome = register_original(
            session,
            upload_id=ticket.upload_id,
            temp_root=attempts,
            relative_path=relative,
            objects_root=objects,
            now=NOW + 1,
            converter_installed=False,
        )
    assert outcome.registered is True
    assert outcome.content_hash == digest
    assert outcome.byte_size == len(PAYLOAD)
    assert outcome.preview_generated is False
    assert outcome.preview_state == "unavailable"
    assert outcome.preview_reason == "converter_not_installed"
    assert outcome.object_key == (
        f"objects/{PROJECT_ID}/sha256/{digest}/{outcome.generation_id}/original"
    )
    assert not attempt.exists()
    stored = objects / PROJECT_ID / "sha256" / digest / outcome.generation_id / "original"
    assert stored.is_file()
    assert stored.read_bytes() == PAYLOAD
    assert hashlib.sha256(stored.read_bytes()).hexdigest() == digest
    assert read_original(objects, PROJECT_ID, digest, outcome.generation_id) == PAYLOAD
    preview_files = [
        path
        for path in tmp_path.rglob("*")
        if path.is_file() and path.name.startswith("preview")
    ]
    assert list(derived.iterdir()) == []
    assert preview_files == []
    with Session(assets) as session:
        version = session.get(ArtifactVersion, outcome.artifact_version_id)
        event = session.scalars(
            select(OutboxEvent).where(OutboxEvent.event_type == "artifact.version_registered")
        ).one()
        project = session.get(Project, PROJECT_ID)
        assert version is not None
        assert version.content_hash == digest
        assert version.size_bytes == len(PAYLOAD)
        assert version.object_key == outcome.object_key
        assert ".." not in version.object_key
        assert "\\" not in version.object_key
        assert not version.object_key.startswith("/")
        assert event.schema_version == 1
        assert event.project_id == PROJECT_ID
        assert set(event.payload) == {
            "project_id",
            "artifact_id",
            "artifact_version_id",
            "content_hash",
            "generation_id",
            "byte_size",
        }
        assert event.payload["content_hash"] == digest
        assert event.payload["generation_id"] == outcome.generation_id
        assert event.payload["byte_size"] == len(PAYLOAD)
        assert event.payload["artifact_version_id"] == outcome.artifact_version_id
        assert project is not None and project.revision == 2
        stored_row = session.scalar(
            select(StoredObjectRow).where(StoredObjectRow.generation_id == outcome.generation_id)
        )
        assert stored_row is not None and stored_row.state == "ready"
        pin_row = session.scalar(
            select(ObjectPinRow).where(
                ObjectPinRow.object_id == stored_row.object_id,
                ObjectPinRow.purpose == "upload",
            )
        )
        assert pin_row is not None and pin_row.consumed_at == NOW + 1
        assert version.object_key == outcome.object_key


@pytest.mark.integration
def test_wrong_declared_hash_registers_nothing(assets, tmp_path):
    declared = hashlib.sha256(OTHER).hexdigest()
    assert declared != hashlib.sha256(PAYLOAD).hexdigest()
    attempts = tmp_path / "attempts"
    objects = tmp_path / "objects"
    attempts.mkdir()
    objects.mkdir()
    with write_uow(assets) as session:
        _seed(session)
        ticket = _ticket(session, declared_hash=declared, size=len(PAYLOAD))
    relative = f"{ticket.upload_id}/attempt-1.part"
    attempt = write_attempt_bytes(attempts, relative, PAYLOAD)
    with write_uow(assets) as session:
        outcome = register_original(
            session,
            upload_id=ticket.upload_id,
            temp_root=attempts,
            relative_path=relative,
            objects_root=objects,
            now=NOW + 1,
        )
    assert outcome.registered is False
    assert outcome.reason == "declared_hash_mismatch"
    assert outcome.artifact_version_id is None
    assert outcome.object_key is None
    assert attempt.read_bytes() == PAYLOAD
    assert list(objects.rglob("original")) == []
    with write_uow(assets) as session:
        again = register_original(
            session,
            upload_id=ticket.upload_id,
            temp_root=attempts,
            relative_path=relative,
            objects_root=objects,
            now=NOW + 2,
        )
    assert again.registered is False
    assert again.reason == "ticket_closed"
    with Session(assets) as session:
        versions = session.scalar(select(func.count()).select_from(ArtifactVersion))
        events = session.scalar(
            select(func.count())
            .select_from(OutboxEvent)
            .where(OutboxEvent.event_type == "artifact.version_registered")
        )
        stored_objects = session.scalar(select(func.count()).select_from(StoredObjectRow))
        row = session.get(UploadTicketRow, ticket.upload_id)
        project = session.get(Project, PROJECT_ID)
        assert versions == 0
        assert events == 0
        assert stored_objects == 0
        assert row is not None
        assert row.status == "rejected"
        assert row.artifact_version_id is None
        assert row.generation_id is None
        assert project is not None and project.revision == 1


@pytest.mark.parametrize(
    "relative",
    [
        "../outside.bin",
        "nested/../../outside.bin",
        "a\\..\\outside.bin",
        "/outside.bin",
    ],
)
def test_path_traversal_writes_nothing(tmp_path, relative):
    root = tmp_path / "attempts"
    root.mkdir()
    with pytest.raises(PathEscape):
        write_attempt_bytes(root, relative, b"escape-bytes")
    assert not (tmp_path / "outside.bin").exists()
    written = [path for path in tmp_path.rglob("*") if path.is_file()]
    assert written == []


def test_absolute_attempt_path_writes_nothing(tmp_path):
    root = tmp_path / "attempts"
    root.mkdir()
    target = tmp_path / "absolute.bin"
    with pytest.raises(PathEscape):
        write_attempt_bytes(root, str(target), b"escape-bytes")
    assert not target.exists()
    assert [path for path in tmp_path.rglob("*") if path.is_file()] == []


def test_symlink_escape_writes_nothing(tmp_path):
    root = tmp_path / "attempts"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    link = root / "jump"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except OSError:
        import subprocess

        created = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link), str(outside)],
            capture_output=True,
            text=True,
            check=False,
        )
        if created.returncode != 0 or not link.exists():
            pytest.skip("symlink not permitted")
    with pytest.raises(PathEscape):
        write_attempt_bytes(root, "jump/secret.bin", b"escape-bytes")
    assert not (outside / "secret.bin").exists()


def test_missing_converter_does_not_fabricate_a_preview(tmp_path):
    derived = tmp_path / "derived"
    derived.mkdir()
    marker = derived / "marker"
    marker.write_bytes(b"keep")
    missing = preview_disposition("model/gltf-binary", installed=False)
    present = preview_disposition("model/gltf-binary", installed=True)
    probed = preview_disposition("model/gltf-binary")
    assert missing.state == "unavailable"
    assert missing.reason == "converter_not_installed"
    assert missing.generated is False
    assert missing.output_path is None
    assert present.generated is False
    assert present.state == "not_run"
    assert present.output_path is None
    assert probed.generated is False
    assert probed.state in {"unavailable", "not_run"}
    assert marker.read_bytes() == b"keep"
    assert list(derived.iterdir()) == [marker]


RETENTION_SECONDS = 10
STALE_BYTES = b"stale-gc-bytes"


def _principal(account_id: str) -> Principal:
    return Principal(
        account_id=account_id,
        session_id=f"synthetic-session-{account_id[:8]}",
        auth_epoch=1,
    )


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


def _membership(session: Session, membership_id: str, account_id: str) -> None:
    session.add(
        Membership(
            id=membership_id,
            project_id=PROJECT_ID,
            account_id=account_id,
            status="active",
            role_label=None,
            created_at=NOW,
            revoked_at=None,
        )
    )


def _grant(
    session: Session,
    *,
    grant_id: str,
    account_id: str,
    action: Action,
    scope_type: str,
    artifact_version_id: str | None,
) -> None:
    session.add(
        ObjectGrant(
            id=grant_id,
            project_id=PROJECT_ID,
            account_id=account_id,
            action=action.value,
            scope_type=scope_type,
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


def _no_disk_path(value: object, *roots: Path) -> None:
    rendered = repr(value)
    visible = str(value)
    for root in roots:
        absolute = str(root.resolve())
        assert absolute not in rendered
        assert str(root) not in rendered
        assert absolute not in visible
        assert str(root) not in visible
    fields = getattr(value, "__dataclass_fields__", None)
    if fields is None:
        return
    for name in fields:
        item = getattr(value, name)
        assert not isinstance(item, Path)
        if isinstance(item, str):
            assert ":\\" not in item
            assert not item.startswith("\\\\")
            assert not item.startswith("/")


def _barrier_pair(leader, follower):
    start = Barrier(2)
    release = Barrier(2)
    found: dict[str, object] = {}
    errors: list[BaseException] = []

    def guard(name: str, body, *, leads: bool) -> None:
        try:
            start.wait(timeout=5)
            if not leads:
                release.wait(timeout=5)
            found[name] = body()
            if leads:
                release.wait(timeout=5)
        except BaseException as exc:
            errors.append(exc)
            for barrier in (start, release):
                try:
                    barrier.abort()
                except Exception:
                    pass

    first = Thread(target=guard, args=("leader", leader), kwargs={"leads": True})
    second = Thread(target=guard, args=("follower", follower), kwargs={"leads": False})
    first.start()
    second.start()
    first.join(timeout=10)
    second.join(timeout=10)
    assert not first.is_alive()
    assert not second.is_alive()
    if errors:
        primary = next(
            (item for item in errors if not isinstance(item, BrokenBarrierError)),
            errors[0],
        )
        raise primary
    return found["leader"], found["follower"]


def _generation_file(root: Path, digest: str, generation_id: str) -> Path:
    relative = object_relative(PROJECT_ID, digest, generation_id)
    return root.joinpath(*relative.split("/"))


def _files_named(root: Path, name: str) -> list[Path]:
    return [path for path in root.rglob(name) if path.is_file()]


@pytest.mark.integration
def test_original_download_requires_grant_and_omits_disk_path(assets, tmp_path):
    digest = hashlib.sha256(PAYLOAD).hexdigest()
    attempts = tmp_path / "attempts"
    objects = tmp_path / "objects"
    attempts.mkdir()
    objects.mkdir()
    with write_uow(assets) as session:
        _seed(session)
        ticket = _ticket(session, declared_hash=digest, size=len(PAYLOAD))
    relative = f"{ticket.upload_id}/attempt-1.part"
    write_attempt_bytes(attempts, relative, PAYLOAD)
    with write_uow(assets) as session:
        outcome = register_original(
            session,
            upload_id=ticket.upload_id,
            temp_root=attempts,
            relative_path=relative,
            objects_root=objects,
            now=NOW + 1,
            converter_installed=False,
        )
    assert outcome.registered is True
    assert outcome.preview_generated is False
    version_id = outcome.artifact_version_id
    assert isinstance(version_id, str)
    with write_uow(assets) as session:
        _account(session, READER_ID, "synthetic-reader")
        _account(session, MEMBER_ID, "synthetic-member")
        _account(session, LOOKER_ID, "synthetic-looker")
        session.flush()
        _membership(session, "synthetic-mem-reader", READER_ID)
        _membership(session, "synthetic-mem-member", MEMBER_ID)
        _membership(session, "synthetic-mem-looker", LOOKER_ID)
        _grant(
            session,
            grant_id="synthetic-grant-reader-original",
            account_id=READER_ID,
            action=Action.ARTIFACT_ORIGINAL_READ,
            scope_type="artifact_version",
            artifact_version_id=version_id,
        )
        _grant(
            session,
            grant_id="synthetic-grant-member-read",
            account_id=MEMBER_ID,
            action=Action.PROJECT_READ,
            scope_type="project",
            artifact_version_id=None,
        )
        _grant(
            session,
            grant_id="synthetic-grant-member-preview",
            account_id=MEMBER_ID,
            action=Action.PREVIEW_READ,
            scope_type="artifact_version",
            artifact_version_id=version_id,
        )
        _grant(
            session,
            grant_id="synthetic-grant-looker-read",
            account_id=LOOKER_ID,
            action=Action.PROJECT_READ,
            scope_type="project",
            artifact_version_id=None,
        )
    absent = tmp_path / "absent-objects"
    assert not absent.exists()
    with Session(assets) as session:
        with pytest.raises(ActionForbidden) as forbidden:
            download_original(
                session,
                _principal(MEMBER_ID),
                SessionAuthorizer(session),
                project_id=PROJECT_ID,
                artifact_version_id=version_id,
                objects_root=absent,
            )
        with pytest.raises(NotFoundOrHidden) as hidden:
            download_original(
                session,
                _principal(LOOKER_ID),
                SessionAuthorizer(session),
                project_id=PROJECT_ID,
                artifact_version_id=version_id,
                objects_root=absent,
            )
        downloaded = download_original(
            session,
            _principal(READER_ID),
            SessionAuthorizer(session),
            project_id=PROJECT_ID,
            artifact_version_id=version_id,
            objects_root=objects,
        )
    assert "sinuo-original-bytes" not in repr(forbidden.value)
    assert "sinuo-original-bytes" not in repr(hidden.value)
    _no_disk_path(forbidden.value, objects, absent, tmp_path)
    _no_disk_path(hidden.value, objects, absent, tmp_path)
    assert downloaded.body == PAYLOAD
    assert downloaded.server_hash == digest
    assert downloaded.server_hash == hashlib.sha256(downloaded.body).hexdigest()
    assert downloaded.byte_size == len(downloaded.body)
    assert downloaded.artifact_version_id == version_id
    assert downloaded.media_type == "model/gltf-binary"
    assert set(asdict(downloaded)) == {
        "artifact_version_id",
        "server_hash",
        "byte_size",
        "media_type",
        "body",
    }
    _no_disk_path(downloaded, objects, attempts, tmp_path)
    assert not any(
        path.name.startswith("preview") for path in tmp_path.rglob("*") if path.is_file()
    )


@pytest.mark.integration
@pytest.mark.concurrency
def test_upload_pin_before_gc_keeps_the_generation(assets, tmp_path):
    payload = b"pinned-generation-bytes"
    digest = hashlib.sha256(payload).hexdigest()
    attempts = tmp_path / "attempts"
    objects = tmp_path / "objects"
    attempts.mkdir()
    objects.mkdir()
    with write_uow(assets) as session:
        _seed(session)
        reserved = reserve_generation(
            session,
            project_id=PROJECT_ID,
            sha256=digest,
            byte_size=len(payload),
            now=NOW - 100.0,
        )
    attempt = write_attempt_bytes(attempts, "attempt-pin.part", payload)

    def pin():
        with write_uow(assets) as session:
            return place_upload_pin(
                session,
                object_id=reserved.object_id,
                upload_id="synthetic-upload-pin",
                now=NOW,
                expires_at=NOW + 30.0,
            )

    def collect():
        with write_uow(assets) as session:
            return claim_generation(
                session,
                object_id=reserved.object_id,
                now=NOW,
                retention_seconds=RETENTION_SECONDS,
            )

    pinned, claimed = _barrier_pair(pin, collect)
    assert pinned.placed is True
    assert pinned.pin_id is not None
    assert claimed.claimed is False
    assert claimed.reason == "pinned"
    _no_disk_path(pinned, objects, attempts, tmp_path)
    _no_disk_path(claimed, objects, attempts, tmp_path)
    with Session(assets) as session:
        row = session.get(StoredObjectRow, reserved.object_id)
        pin_row = session.get(ObjectPinRow, pinned.pin_id)
        assert row is not None
        assert row.state == "reserved"
        assert row.fencing_token == 0
        assert pin_row is not None
        assert pin_row.consumed_at is None
        assert pin_row.purpose == "upload"
        assert pin_row.expires_at > NOW
    with write_uow(assets) as session:
        installed = install_reserved_original(
            session,
            object_id=reserved.object_id,
            pin_id=pinned.pin_id,
            source=attempt,
            objects_root=objects,
            now=NOW + 1.0,
        )
    assert installed.installed is True
    assert installed.object_key == original_key(PROJECT_ID, digest, reserved.generation_id)
    assert installed.reason is None
    _no_disk_path(installed, objects, attempts, tmp_path)
    assert not attempt.exists()
    stored = read_original(objects, PROJECT_ID, digest, reserved.generation_id)
    assert stored == payload
    assert hashlib.sha256(stored).hexdigest() == digest
    assert _files_named(tmp_path, "original") == [
        _generation_file(objects, digest, reserved.generation_id)
    ]
    assert not any(
        path.name.startswith("preview") for path in tmp_path.rglob("*") if path.is_file()
    )
    with Session(assets) as session:
        row = session.get(StoredObjectRow, reserved.object_id)
        pin_row = session.get(ObjectPinRow, pinned.pin_id)
        assert row is not None and row.state == "ready" and row.fencing_token == 1
        assert pin_row is not None and pin_row.consumed_at == NOW + 1.0


@pytest.mark.integration
@pytest.mark.concurrency
@pytest.mark.recovery
def test_gc_claim_before_pin_blocks_install(assets, tmp_path):
    payload = b"gc-first-generation"
    digest = hashlib.sha256(payload).hexdigest()
    attempts = tmp_path / "attempts"
    objects = tmp_path / "objects"
    attempts.mkdir()
    objects.mkdir()
    with write_uow(assets) as session:
        _seed(session)
        reserved = reserve_generation(
            session,
            project_id=PROJECT_ID,
            sha256=digest,
            byte_size=len(payload),
            now=NOW - 100.0,
        )

    attempt = write_attempt_bytes(attempts, "attempt-late.part", payload)

    def collect():
        with write_uow(assets) as session:
            return claim_generation(
                session,
                object_id=reserved.object_id,
                now=NOW,
                retention_seconds=RETENTION_SECONDS,
            )

    def late_upload():
        with write_uow(assets) as session:
            pinned = place_upload_pin(
                session,
                object_id=reserved.object_id,
                upload_id="synthetic-upload-late",
                now=NOW,
                expires_at=NOW + 30.0,
            )
            installed = install_reserved_original(
                session,
                object_id=reserved.object_id,
                pin_id=pinned.pin_id or "missing-pin",
                source=attempt,
                objects_root=objects,
                now=NOW,
            )
        return pinned, installed

    claimed, late = _barrier_pair(collect, late_upload)
    pinned, installed = late
    assert claimed.claimed is True
    assert claimed.fencing_token == 1
    assert claimed.generation_id == reserved.generation_id
    assert pinned.placed is False
    assert pinned.reason == "generation_unavailable"
    assert installed.installed is False
    assert installed.object_key is None
    assert installed.reason == "generation_unavailable"
    assert attempt.read_bytes() == payload
    assert _files_named(objects, "original") == []
    _no_disk_path(claimed, objects, attempts, tmp_path)
    _no_disk_path(installed, objects, attempts, tmp_path)
    with Session(assets) as session:
        row = session.get(StoredObjectRow, reserved.object_id)
        assert row is not None
        assert row.state == "gc_claimed"
        assert row.fencing_token == 1
    with write_uow(assets) as session:
        stale = delete_claimed_generation(
            session,
            object_id=reserved.object_id,
            fencing_token=0,
            objects_root=objects,
        )
    assert stale.deleted is False
    assert stale.reason == "stale_token"
    with write_uow(assets) as session:
        removed = delete_claimed_generation(
            session,
            object_id=reserved.object_id,
            fencing_token=1,
            objects_root=objects,
        )
    assert removed.deleted is True
    assert removed.generation_id == reserved.generation_id
    with write_uow(assets) as session:
        again_pin = place_upload_pin(
            session,
            object_id=reserved.object_id,
            upload_id="synthetic-upload-late",
            now=NOW + 1.0,
            expires_at=NOW + 30.0,
        )
        again = install_reserved_original(
            session,
            object_id=reserved.object_id,
            pin_id=again_pin.pin_id or "missing-pin",
            source=attempt,
            objects_root=objects,
            now=NOW + 1.0,
        )
    assert again_pin.placed is False
    assert again.installed is False
    assert again.object_key is None
    assert attempt.read_bytes() == payload
    assert _files_named(objects, "original") == []
    with Session(assets) as session:
        row = session.get(StoredObjectRow, reserved.object_id)
        pins = session.scalars(
            select(ObjectPinRow).where(ObjectPinRow.object_id == reserved.object_id)
        ).all()
        assert row is not None and row.state == "deleted"
        assert pins == []
    assert not any(
        path.name.startswith("preview") for path in tmp_path.rglob("*") if path.is_file()
    )


@pytest.mark.integration
@pytest.mark.recovery
def test_stale_gc_unlink_leaves_the_new_generation_download(assets, tmp_path):
    digest = hashlib.sha256(PAYLOAD).hexdigest()
    attempts = tmp_path / "attempts"
    objects = tmp_path / "objects"
    attempts.mkdir()
    objects.mkdir()
    with write_uow(assets) as session:
        _seed(session)
        ticket = _ticket(session, declared_hash=digest, size=len(PAYLOAD))
    relative = f"{ticket.upload_id}/attempt-1.part"
    write_attempt_bytes(attempts, relative, PAYLOAD)
    with write_uow(assets) as session:
        outcome = register_original(
            session,
            upload_id=ticket.upload_id,
            temp_root=attempts,
            relative_path=relative,
            objects_root=objects,
            now=NOW + 1,
            converter_installed=False,
        )
    assert outcome.registered is True
    assert outcome.preview_generated is False
    version_id = outcome.artifact_version_id
    live_generation = outcome.generation_id
    assert isinstance(version_id, str) and isinstance(live_generation, str)
    live = _generation_file(objects, digest, live_generation)
    assert live.read_bytes() == PAYLOAD
    with Session(assets) as session:
        current_row = session.scalar(
            select(StoredObjectRow).where(
                StoredObjectRow.project_id == PROJECT_ID,
                StoredObjectRow.generation_id == live_generation,
            )
        )
        assert current_row is not None and current_row.state == "ready"
        current_pin = session.scalar(
            select(ObjectPinRow).where(
                ObjectPinRow.object_id == current_row.object_id,
                ObjectPinRow.purpose == "upload",
            )
        )
        assert current_pin is not None and current_pin.consumed_at is not None
        current_object_id = current_row.object_id
        current_token = current_row.fencing_token
    with write_uow(assets) as session:
        old = reserve_generation(
            session,
            project_id=PROJECT_ID,
            sha256=digest,
            byte_size=len(STALE_BYTES),
            now=NOW - 100.0,
        )
    assert old.generation_id != live_generation
    sentinel = _generation_file(objects, digest, old.generation_id)
    sentinel.parent.mkdir(parents=True, exist_ok=True)
    sentinel.write_bytes(STALE_BYTES)
    with write_uow(assets) as session:
        blocked = claim_generation(
            session,
            object_id=current_object_id,
            now=NOW + 1.0 + float(RETENTION_SECONDS) + 1.0,
            retention_seconds=RETENTION_SECONDS,
        )
    assert blocked.claimed is False
    assert blocked.reason == "referenced"
    assert live.read_bytes() == PAYLOAD
    with write_uow(assets) as session:
        refused = delete_claimed_generation(
            session,
            object_id=current_object_id,
            fencing_token=current_token,
            objects_root=objects,
        )
    assert refused.deleted is False
    assert refused.reason == "not_claimed"
    assert live.read_bytes() == PAYLOAD
    assert sentinel.read_bytes() == STALE_BYTES
    with write_uow(assets) as session:
        claimed = claim_generation(
            session,
            object_id=old.object_id,
            now=NOW + 2,
            retention_seconds=RETENTION_SECONDS,
        )
    assert claimed.claimed is True
    assert claimed.fencing_token == 1
    assert claimed.generation_id == old.generation_id
    with write_uow(assets) as session:
        missed = delete_claimed_generation(
            session,
            object_id=old.object_id,
            fencing_token=0,
            objects_root=objects,
        )
    assert missed.deleted is False
    assert missed.reason == "stale_token"
    assert sentinel.read_bytes() == STALE_BYTES
    assert live.read_bytes() == PAYLOAD
    with write_uow(assets) as session:
        removed = delete_claimed_generation(
            session,
            object_id=old.object_id,
            fencing_token=1,
            objects_root=objects,
        )
    assert removed.deleted is True
    assert removed.generation_id == old.generation_id
    assert removed.generation_id != live_generation
    assert not sentinel.exists()
    assert live.read_bytes() == PAYLOAD
    assert hashlib.sha256(live.read_bytes()).hexdigest() == digest
    with write_uow(assets) as session:
        repeated = delete_claimed_generation(
            session,
            object_id=old.object_id,
            fencing_token=1,
            objects_root=objects,
        )
    assert repeated.deleted is True
    assert live.read_bytes() == PAYLOAD
    with write_uow(assets) as session:
        _account(session, READER_ID, "synthetic-reader")
        session.flush()
        _membership(session, "synthetic-mem-reader", READER_ID)
        _grant(
            session,
            grant_id="synthetic-grant-reader-original",
            account_id=READER_ID,
            action=Action.ARTIFACT_ORIGINAL_READ,
            scope_type="artifact_version",
            artifact_version_id=version_id,
        )
    with Session(assets) as session:
        downloaded = download_original(
            session,
            _principal(READER_ID),
            SessionAuthorizer(session),
            project_id=PROJECT_ID,
            artifact_version_id=version_id,
            objects_root=objects,
        )
    assert downloaded.body == PAYLOAD
    assert downloaded.server_hash == digest
    assert downloaded.server_hash == hashlib.sha256(downloaded.body).hexdigest()
    assert downloaded.artifact_version_id == version_id
    _no_disk_path(downloaded, objects, attempts, tmp_path)
    _no_disk_path(removed, objects, attempts, tmp_path)
    assert read_original(objects, PROJECT_ID, digest, live_generation) == PAYLOAD
    assert not any(
        path.name.startswith("preview") for path in tmp_path.rglob("*") if path.is_file()
    )


OTHER_PROJECT_ID = "synthetic-project-other"
OTHER_ARTIFACT_ID = "synthetic-artifact-other"
SAME_TITLE = "synthetic-asset-title"
BYTES_A = b"sinuo-same-name-alpha"
BYTES_B = b"sinuo-same-name-beta"


def _project_grant(
    session: Session,
    *,
    grant_id: str,
    project_id: str,
    account_id: str,
    action: Action,
) -> None:
    session.add(
        ObjectGrant(
            id=grant_id,
            project_id=project_id,
            account_id=account_id,
            action=action.value,
            scope_type="project",
            status="active",
            created_at=NOW,
            revoked_at=None,
            created_by_account_id=ACCOUNT_ID,
            stage_id=None,
            page_id=None,
            page_version_id=None,
            artifact_id=None,
            artifact_version_id=None,
            evidence_anchor_id=None,
        )
    )


def _artifact_grant(
    session: Session,
    *,
    grant_id: str,
    account_id: str,
    action: Action,
    artifact_id: str,
) -> None:
    session.add(
        ObjectGrant(
            id=grant_id,
            project_id=PROJECT_ID,
            account_id=account_id,
            action=action.value,
            scope_type="artifact",
            status="active",
            created_at=NOW,
            revoked_at=None,
            created_by_account_id=ACCOUNT_ID,
            stage_id=None,
            page_id=None,
            page_version_id=None,
            artifact_id=artifact_id,
            artifact_version_id=None,
            evidence_anchor_id=None,
        )
    )


@pytest.mark.integration
def test_same_name_new_bytes_are_two_versions_and_reuse_stays_in_project(assets, tmp_path):
    attempts = tmp_path / "attempts"
    objects = tmp_path / "objects"
    attempts.mkdir()
    objects.mkdir()
    with write_uow(assets) as session:
        _seed(session)
        session.add(
            Project(
                id=OTHER_PROJECT_ID,
                title="synthetic-other-project",
                brief=None,
                template_key=None,
                revision=1,
                access_epoch=1,
                created_at=NOW,
            )
        )
        session.flush()
        session.add(
            Artifact(
                id=OTHER_ARTIFACT_ID,
                project_id=OTHER_PROJECT_ID,
                title=SAME_TITLE,
                created_at=NOW,
            )
        )

    def store(payload: bytes, *, project_id: str, artifact_id: str, when: float):
        digest = hashlib.sha256(payload).hexdigest()
        with write_uow(assets) as session:
            ticket = request_upload_ticket(
                session,
                project_id=project_id,
                artifact_id=artifact_id,
                expected_size=len(payload),
                declared_hash=digest,
                media_type="model/gltf-binary",
                license_name="synthetic-license",
                sensitivity="project",
                ai_processing_allowed=0,
                account_id=ACCOUNT_ID,
                now=when,
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
                now=when + 1.0,
                converter_installed=False,
            )
        assert outcome.registered is True
        assert outcome.content_hash == digest
        assert outcome.byte_size == len(payload)
        assert isinstance(outcome.artifact_version_id, str)
        assert isinstance(outcome.generation_id, str)
        assert isinstance(outcome.object_key, str)
        return outcome

    first = store(BYTES_A, project_id=PROJECT_ID, artifact_id=ARTIFACT_ID, when=NOW + 10.0)
    second = store(BYTES_B, project_id=PROJECT_ID, artifact_id=ARTIFACT_ID, when=NOW + 20.0)
    third = store(BYTES_A, project_id=PROJECT_ID, artifact_id=ARTIFACT_ID, when=NOW + 30.0)
    other = store(
        BYTES_A,
        project_id=OTHER_PROJECT_ID,
        artifact_id=OTHER_ARTIFACT_ID,
        when=NOW + 40.0,
    )
    version_ids = {
        first.artifact_version_id,
        second.artifact_version_id,
        third.artifact_version_id,
        other.artifact_version_id,
    }
    assert len(version_ids) == 4
    assert first.object_key == third.object_key
    assert first.generation_id == third.generation_id
    assert first.object_key != second.object_key
    assert other.object_key != first.object_key
    assert str(other.object_key).startswith(f"objects/{OTHER_PROJECT_ID}/")
    assert str(first.object_key).startswith(f"objects/{PROJECT_ID}/")
    digest_a = hashlib.sha256(BYTES_A).hexdigest()
    digest_b = hashlib.sha256(BYTES_B).hexdigest()
    home = objects / PROJECT_ID / "sha256" / digest_a
    assert [path.name for path in home.iterdir() if path.is_dir()] == [first.generation_id]
    other_relative = object_relative(OTHER_PROJECT_ID, digest_a, str(other.generation_id))
    other_file = objects.joinpath(*other_relative.split("/"))
    first_file = _generation_file(objects, digest_a, str(first.generation_id))
    assert first_file.is_file() and other_file.is_file()
    assert not first_file.samefile(other_file)
    assert first_file.read_bytes() == BYTES_A
    assert other_file.read_bytes() == BYTES_A
    assert len(_files_named(objects, "original")) == 3
    with write_uow(assets) as session:
        _account(session, READER_ID, "synthetic-reader")
        session.flush()
        _membership(session, "synthetic-mem-reader-home", READER_ID)
        session.add(
            Membership(
                id="synthetic-mem-reader-other",
                project_id=OTHER_PROJECT_ID,
                account_id=READER_ID,
                status="active",
                role_label=None,
                created_at=NOW,
                revoked_at=None,
            )
        )
        _project_grant(
            session,
            grant_id="synthetic-grant-read-home",
            project_id=PROJECT_ID,
            account_id=READER_ID,
            action=Action.ARTIFACT_ORIGINAL_READ,
        )
        _project_grant(
            session,
            grant_id="synthetic-grant-read-other",
            project_id=OTHER_PROJECT_ID,
            account_id=READER_ID,
            action=Action.ARTIFACT_ORIGINAL_READ,
        )

    def fetch(project_id: str, version_id: str):
        with Session(assets) as session:
            return download_original(
                session,
                _principal(READER_ID),
                SessionAuthorizer(session),
                project_id=project_id,
                artifact_version_id=version_id,
                objects_root=objects,
            )

    loaded_first = fetch(PROJECT_ID, str(first.artifact_version_id))
    loaded_second = fetch(PROJECT_ID, str(second.artifact_version_id))
    loaded_third = fetch(PROJECT_ID, str(third.artifact_version_id))
    loaded_other = fetch(OTHER_PROJECT_ID, str(other.artifact_version_id))
    assert loaded_first.body == BYTES_A
    assert loaded_first.server_hash == digest_a
    assert loaded_first.server_hash == hashlib.sha256(loaded_first.body).hexdigest()
    assert loaded_second.body == BYTES_B
    assert loaded_second.server_hash == digest_b
    assert loaded_second.server_hash == hashlib.sha256(loaded_second.body).hexdigest()
    assert loaded_first.server_hash != loaded_second.server_hash
    assert loaded_third.body == BYTES_A
    assert loaded_third.server_hash == digest_a
    assert loaded_third.artifact_version_id != loaded_first.artifact_version_id
    assert loaded_other.body == BYTES_A
    assert loaded_other.server_hash == digest_a
    assert loaded_other.artifact_version_id != loaded_first.artifact_version_id
    with Session(assets) as session:
        artifact = session.get(Artifact, ARTIFACT_ID)
        other_artifact = session.get(Artifact, OTHER_ARTIFACT_ID)
        versions = session.scalars(
            select(ArtifactVersion)
            .where(ArtifactVersion.artifact_id == ARTIFACT_ID)
            .order_by(ArtifactVersion.version_no)
        ).all()
        assert artifact is not None and other_artifact is not None
        assert artifact.title == SAME_TITLE == other_artifact.title
        assert [item.version_no for item in versions] == [1, 2, 3]
        assert [item.id for item in versions] == [
            first.artifact_version_id,
            second.artifact_version_id,
            third.artifact_version_id,
        ]
        assert versions[0].object_key == versions[2].object_key
        assert versions[0].content_hash == digest_a
        assert versions[1].content_hash == digest_b
    assert not any(
        path.name.startswith("preview") for path in tmp_path.rglob("*") if path.is_file()
    )


@pytest.mark.integration
def test_byte_limit_stops_before_a_complete_attempt(assets, tmp_path):
    payload = b"bounded-attempt-bytes"
    limit = len(payload) - 1
    digest = hashlib.sha256(payload).hexdigest()
    attempts = tmp_path / "attempts"
    objects = tmp_path / "objects"
    attempts.mkdir()
    objects.mkdir()
    with pytest.raises(AttemptTooLarge) as too_big:
        write_attempt_bytes(attempts, "over/attempt.part", payload, byte_limit=limit)
    assert too_big.value.byte_limit == limit
    assert "bounded-attempt-bytes" not in str(too_big.value)
    assert not (attempts / "over").exists()
    assert [path for path in attempts.rglob("*") if path.is_file()] == []
    allowed = write_attempt_bytes(
        attempts,
        "exact/attempt.part",
        payload,
        byte_limit=len(payload),
    )
    assert allowed.read_bytes() == payload
    with write_uow(assets) as session:
        _seed(session)
    with pytest.raises(AssetRejected) as rejected:
        with write_uow(assets) as session:
            request_upload_ticket(
                session,
                project_id=PROJECT_ID,
                artifact_id=ARTIFACT_ID,
                expected_size=len(payload),
                declared_hash=digest,
                media_type="model/gltf-binary",
                license_name="synthetic-license",
                sensitivity="project",
                ai_processing_allowed=0,
                account_id=ACCOUNT_ID,
                now=NOW,
                byte_limit=limit,
            )
    assert rejected.value.reason == "byte_limit"
    with write_uow(assets) as session:
        ticket = _ticket(session, declared_hash=digest, size=len(payload))
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
            byte_limit=limit,
        )
    assert outcome.registered is False
    assert outcome.reason == "byte_limit"
    assert outcome.artifact_version_id is None
    assert outcome.object_key is None
    assert (attempts / relative).read_bytes() == payload
    assert _files_named(objects, "original") == []
    with Session(assets) as session:
        tickets = session.scalar(select(func.count()).select_from(UploadTicketRow))
        versions = session.scalar(select(func.count()).select_from(ArtifactVersion))
        stored_objects = session.scalar(select(func.count()).select_from(StoredObjectRow))
        row = session.get(UploadTicketRow, ticket.upload_id)
        assert tickets == 1
        assert versions == 0
        assert stored_objects == 0
        assert row is not None and row.status == "rejected"
        assert row.artifact_version_id is None


@pytest.mark.integration
def test_declared_length_mismatch_leaves_no_referable_original(assets, tmp_path):
    payload = b"declared-length-bytes"
    digest = hashlib.sha256(payload).hexdigest()
    declared_size = len(payload) - 1
    attempts = tmp_path / "attempts"
    objects = tmp_path / "objects"
    attempts.mkdir()
    objects.mkdir()
    with write_uow(assets) as session:
        _seed(session)
        ticket = _ticket(session, declared_hash=digest, size=declared_size)
    relative = f"{ticket.upload_id}/attempt.part"
    attempt = write_attempt_bytes(attempts, relative, payload, byte_limit=len(payload))
    assert attempt.read_bytes() == payload
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
    assert outcome.registered is False
    assert outcome.reason == "expected_size_mismatch"
    assert outcome.artifact_version_id is None
    assert outcome.object_key is None
    assert attempt.read_bytes() == payload
    assert _files_named(objects, "original") == []
    with Session(assets) as session:
        versions = session.scalar(select(func.count()).select_from(ArtifactVersion))
        stored_objects = session.scalar(select(func.count()).select_from(StoredObjectRow))
        row = session.get(UploadTicketRow, ticket.upload_id)
        assert versions == 0
        assert stored_objects == 0
        assert row is not None
        assert row.status == "rejected"
        assert row.artifact_version_id is None
        assert row.generation_id is None


@pytest.mark.integration
def test_subject_without_artifact_upload_leaves_no_referable_original(assets, tmp_path):
    payload = b"needs-upload-grant"
    digest = hashlib.sha256(payload).hexdigest()
    attempts = tmp_path / "attempts"
    objects = tmp_path / "objects"
    attempts.mkdir()
    objects.mkdir()
    with write_uow(assets) as session:
        _seed(session)
        ticket = _ticket(session, declared_hash=digest, size=len(payload))
        _account(session, MEMBER_ID, "synthetic-member")
        _account(session, LOOKER_ID, "synthetic-looker")
        session.flush()
        _membership(session, "synthetic-mem-member", MEMBER_ID)
        _membership(session, "synthetic-mem-looker", LOOKER_ID)
        _artifact_grant(
            session,
            grant_id="synthetic-grant-member-preview",
            account_id=MEMBER_ID,
            action=Action.PREVIEW_READ,
            artifact_id=ARTIFACT_ID,
        )
        _grant(
            session,
            grant_id="synthetic-grant-looker-read",
            account_id=LOOKER_ID,
            action=Action.PROJECT_READ,
            scope_type="project",
            artifact_version_id=None,
        )
    relative = f"{ticket.upload_id}/attempt.part"
    attempt = write_attempt_bytes(attempts, relative, payload, byte_limit=len(payload))
    with pytest.raises(ActionForbidden) as forbidden:
        with write_uow(assets) as session:
            register_original(
                session,
                upload_id=ticket.upload_id,
                temp_root=attempts,
                relative_path=relative,
                objects_root=objects,
                now=NOW + 1.0,
                converter_installed=False,
                principal=_principal(MEMBER_ID),
                authorizer=SessionAuthorizer(session),
            )
    with pytest.raises(NotFoundOrHidden):
        with write_uow(assets) as session:
            register_original(
                session,
                upload_id=ticket.upload_id,
                temp_root=attempts,
                relative_path=relative,
                objects_root=objects,
                now=NOW + 1.0,
                converter_installed=False,
                principal=_principal(LOOKER_ID),
                authorizer=SessionAuthorizer(session),
            )
    assert forbidden.value.action == Action.ARTIFACT_UPLOAD.value
    assert "needs-upload-grant" not in repr(forbidden.value)
    assert attempt.read_bytes() == payload
    assert _files_named(objects, "original") == []
    with Session(assets) as session:
        versions = session.scalar(select(func.count()).select_from(ArtifactVersion))
        stored_objects = session.scalar(select(func.count()).select_from(StoredObjectRow))
        row = session.get(UploadTicketRow, ticket.upload_id)
        assert versions == 0
        assert stored_objects == 0
        assert row is not None and row.status == "open"
        assert row.artifact_version_id is None
    with write_uow(assets) as session:
        _membership(session, "synthetic-mem-uploader", ACCOUNT_ID)
        _project_grant(
            session,
            grant_id="synthetic-grant-uploader",
            project_id=PROJECT_ID,
            account_id=ACCOUNT_ID,
            action=Action.ARTIFACT_UPLOAD,
        )
        _project_grant(
            session,
            grant_id="synthetic-grant-uploader-read",
            project_id=PROJECT_ID,
            account_id=ACCOUNT_ID,
            action=Action.ARTIFACT_ORIGINAL_READ,
        )
    with write_uow(assets) as session:
        outcome = register_original(
            session,
            upload_id=ticket.upload_id,
            temp_root=attempts,
            relative_path=relative,
            objects_root=objects,
            now=NOW + 2.0,
            converter_installed=False,
            principal=_principal(ACCOUNT_ID),
            authorizer=SessionAuthorizer(session),
        )
    assert outcome.registered is True
    assert outcome.content_hash == digest
    version_id = outcome.artifact_version_id
    assert isinstance(version_id, str)
    with Session(assets) as session:
        versions = session.scalar(select(func.count()).select_from(ArtifactVersion))
        downloaded = download_original(
            session,
            _principal(ACCOUNT_ID),
            SessionAuthorizer(session),
            project_id=PROJECT_ID,
            artifact_version_id=version_id,
            objects_root=objects,
        )
        assert versions == 1
    assert downloaded.body == payload
    assert downloaded.server_hash == digest
    assert downloaded.server_hash == hashlib.sha256(downloaded.body).hexdigest()
    assert downloaded.artifact_version_id == version_id
    assert not any(
        path.name.startswith("preview") for path in tmp_path.rglob("*") if path.is_file()
    )


@pytest.mark.integration
def test_mismatched_ticket_owner_leaves_the_ticket_open(assets, tmp_path):
    payload = b"ticket-owner-bytes"
    digest = hashlib.sha256(payload).hexdigest()
    attempts = tmp_path / "attempts"
    objects = tmp_path / "objects"
    attempts.mkdir()
    objects.mkdir()
    with write_uow(assets) as session:
        _seed(session)
        ticket = _ticket(session, declared_hash=digest, size=len(payload))
        _account(session, MEMBER_ID, "synthetic-member")
        session.flush()
        _membership(session, "synthetic-mem-member", MEMBER_ID)
        _project_grant(
            session,
            grant_id="synthetic-grant-member-upload",
            project_id=PROJECT_ID,
            account_id=MEMBER_ID,
            action=Action.ARTIFACT_UPLOAD,
        )
    relative = f"{ticket.upload_id}/attempt.part"
    attempt = write_attempt_bytes(attempts, relative, payload)
    with write_uow(assets) as session:
        outcome = register_original(
            session,
            upload_id=ticket.upload_id,
            temp_root=attempts,
            relative_path=relative,
            objects_root=objects,
            now=NOW + 1.0,
            converter_installed=False,
            principal=_principal(MEMBER_ID),
            authorizer=SessionAuthorizer(session),
        )
    assert outcome.registered is False
    assert outcome.reason == "ticket_owner"
    assert outcome.artifact_version_id is None
    assert outcome.object_key is None
    assert attempt.read_bytes() == payload
    assert _files_named(objects, "original") == []
    with Session(assets) as session:
        row = session.get(UploadTicketRow, ticket.upload_id)
        versions = session.scalar(select(func.count()).select_from(ArtifactVersion))
        stored_objects = session.scalar(select(func.count()).select_from(StoredObjectRow))
        assert row is not None and row.status == "open"
        assert row.artifact_version_id is None
        assert row.generation_id is None
        assert row.closed_at is None
        assert versions == 0
        assert stored_objects == 0
    with write_uow(assets) as session:
        registered = register_original(
            session,
            upload_id=ticket.upload_id,
            temp_root=attempts,
            relative_path=relative,
            objects_root=objects,
            now=NOW + 2.0,
            converter_installed=False,
        )
    assert registered.registered is True
    assert registered.content_hash == digest
    assert not attempt.exists()


@pytest.mark.integration
@pytest.mark.recovery
def test_rollback_after_install_cleans_only_the_unregistered_orphan(assets, tmp_path):
    committed_bytes = b"committed-original-bytes"
    orphan_bytes = b"rolled-back-orphan-bytes"
    committed_digest = hashlib.sha256(committed_bytes).hexdigest()
    orphan_digest = hashlib.sha256(orphan_bytes).hexdigest()
    assert committed_digest != orphan_digest
    attempts = tmp_path / "attempts"
    objects = tmp_path / "objects"
    attempts.mkdir()
    objects.mkdir()
    with write_uow(assets) as session:
        _seed(session)
        ticket = _ticket(session, declared_hash=committed_digest, size=len(committed_bytes))
    relative = f"{ticket.upload_id}/attempt.part"
    write_attempt_bytes(attempts, relative, committed_bytes)
    with write_uow(assets) as session:
        committed = register_original(
            session,
            upload_id=ticket.upload_id,
            temp_root=attempts,
            relative_path=relative,
            objects_root=objects,
            now=NOW + 1.0,
            converter_installed=False,
        )
    assert committed.registered is True
    committed_generation = committed.generation_id
    committed_version = committed.artifact_version_id
    assert isinstance(committed_generation, str)
    assert isinstance(committed_version, str)
    committed_path = _generation_file(objects, committed_digest, committed_generation)
    assert committed_path.read_bytes() == committed_bytes
    with write_uow(assets) as session:
        _account(session, READER_ID, "synthetic-reader")
        session.flush()
        _membership(session, "synthetic-mem-reader", READER_ID)
        _grant(
            session,
            grant_id="synthetic-grant-reader-original",
            account_id=READER_ID,
            action=Action.ARTIFACT_ORIGINAL_READ,
            scope_type="artifact_version",
            artifact_version_id=committed_version,
        )
    with write_uow(assets) as session:
        orphan_ticket = _ticket(session, declared_hash=orphan_digest, size=len(orphan_bytes))
    orphan_relative = f"{orphan_ticket.upload_id}/attempt.part"
    write_attempt_bytes(attempts, orphan_relative, orphan_bytes)
    orphan_generation = ""
    with pytest.raises(RuntimeError, match="^database failed$"):
        with write_uow(assets) as session:
            outcome = register_original(
                session,
                upload_id=orphan_ticket.upload_id,
                temp_root=attempts,
                relative_path=orphan_relative,
                objects_root=objects,
                now=NOW + 3.0,
                converter_installed=False,
            )
            assert outcome.registered is True
            assert isinstance(outcome.generation_id, str)
            orphan_generation = outcome.generation_id
            assert _generation_file(objects, orphan_digest, orphan_generation).read_bytes() == (
                orphan_bytes
            )
            raise RuntimeError("database failed")
    orphan_path = _generation_file(objects, orphan_digest, orphan_generation)
    assert orphan_generation != committed_generation
    assert orphan_path.read_bytes() == orphan_bytes
    assert committed_path.read_bytes() == committed_bytes
    with Session(assets) as session:
        rows = session.scalars(select(ArtifactVersion)).all()
        assert len(rows) == 1
        assert rows[0].id == committed_version
        assert rows[0].content_hash == committed_digest
    with write_uow(assets) as session:
        kept = discard_unregistered_original(
            session,
            project_id=PROJECT_ID,
            content_hash=committed_digest,
            generation_id=committed_generation,
            byte_size=len(committed_bytes),
            objects_root=objects,
            now=NOW + 4.0,
            retention_seconds=0,
        )
        removed = discard_unregistered_original(
            session,
            project_id=PROJECT_ID,
            content_hash=orphan_digest,
            generation_id=orphan_generation,
            byte_size=len(orphan_bytes),
            objects_root=objects,
            now=NOW + 4.0,
            retention_seconds=0,
        )
    assert kept.deleted is False
    assert kept.reason == "referenced"
    assert kept.generation_id == committed_generation
    assert removed.deleted is True
    assert removed.reason is None
    assert removed.generation_id == orphan_generation
    assert not orphan_path.exists()
    assert committed_path.read_bytes() == committed_bytes
    assert hashlib.sha256(committed_path.read_bytes()).hexdigest() == committed_digest
    with Session(assets) as session:
        downloaded = download_original(
            session,
            _principal(READER_ID),
            SessionAuthorizer(session),
            project_id=PROJECT_ID,
            artifact_version_id=committed_version,
            objects_root=objects,
        )
        stored = session.scalar(
            select(StoredObjectRow).where(StoredObjectRow.generation_id == committed_generation)
        )
        orphan_row = session.scalar(
            select(StoredObjectRow).where(StoredObjectRow.generation_id == orphan_generation)
        )
        committed_pin = None
        committed_version_row = session.get(ArtifactVersion, committed_version)
        if stored is not None:
            committed_pin = session.scalar(
                select(ObjectPinRow).where(
                    ObjectPinRow.object_id == stored.object_id,
                    ObjectPinRow.purpose == "upload",
                )
            )
    assert downloaded.body == committed_bytes
    assert downloaded.server_hash == committed_digest
    assert downloaded.server_hash == hashlib.sha256(downloaded.body).hexdigest()
    assert downloaded.artifact_version_id == committed_version
    assert stored is not None and stored.state == "ready"
    assert committed_pin is not None and committed_pin.consumed_at is not None
    assert committed_version_row is not None
    assert committed_version_row.object_key == original_key(
        PROJECT_ID,
        committed_digest,
        committed_generation,
    )
    assert orphan_row is not None and orphan_row.state == "deleted"
    assert not any(
        path.name.startswith("preview") for path in tmp_path.rglob("*") if path.is_file()
    )


def _placeholder(work: Path, label: bytes) -> None:
    (work / "preview.png").write_bytes(label)


def _crash_converter(payload: bytes, work: Path) -> None:
    assert payload == PAYLOAD
    _placeholder(work, b"placeholder-crash")
    raise RuntimeError("synthetic-crash-detail")


def _timeout_converter(payload: bytes, work: Path) -> None:
    assert payload == PAYLOAD
    _placeholder(work, b"placeholder-timeout")
    raise PreviewTimedOut()


def _unsupported_converter(payload: bytes, work: Path) -> None:
    assert payload == PAYLOAD
    _placeholder(work, b"placeholder-unsupported")
    raise PreviewFormatUnsupported()


def _missing_tool_converter(payload: bytes, work: Path) -> None:
    assert payload == PAYLOAD
    _placeholder(work, b"placeholder-missing-tool")
    raise PreviewToolMissing()


@pytest.mark.integration
def test_injected_converter_failures_leave_the_original_downloadable(assets, tmp_path):
    digest = hashlib.sha256(PAYLOAD).hexdigest()
    attempts = tmp_path / "attempts"
    objects = tmp_path / "objects"
    derived = tmp_path / "derived"
    attempts.mkdir()
    objects.mkdir()
    derived.mkdir()
    with write_uow(assets) as session:
        _seed(session)
        ticket = _ticket(session, declared_hash=digest, size=len(PAYLOAD))
    relative = f"{ticket.upload_id}/attempt.part"
    write_attempt_bytes(attempts, relative, PAYLOAD)
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
    assert isinstance(version_id, str) and isinstance(generation_id, str)
    stored = _generation_file(objects, digest, generation_id)
    assert stored.read_bytes() == PAYLOAD
    converters = (
        (_crash_converter, "failed", "converter_crashed"),
        (_timeout_converter, "timed_out", "converter_timeout"),
        (_unsupported_converter, "unsupported", "format_unsupported"),
        (_missing_tool_converter, "unavailable", "converter_not_installed"),
    )
    results = []
    for converter, state, reason in converters:
        disposition = run_preview_converter(PAYLOAD, derived_root=derived, converter=converter)
        assert disposition.state == state
        assert disposition.reason == reason
        assert disposition.generated is False
        assert disposition.output_path is None
        assert "synthetic-crash-detail" not in repr(disposition)
        assert b"placeholder" not in repr(disposition).encode()
        results.append(disposition)
    assert len({item.state for item in results}) == 4
    assert len({item.reason for item in results}) == 4
    assert len({(item.state, item.reason) for item in results}) == 4
    assert stored.read_bytes() == PAYLOAD
    assert hashlib.sha256(stored.read_bytes()).hexdigest() == digest
    assert [path for path in derived.rglob("*") if path.is_file()] == []
    assert not any(
        path.name.startswith("preview") for path in tmp_path.rglob("*") if path.is_file()
    )
    with write_uow(assets) as session:
        _account(session, READER_ID, "synthetic-reader")
        session.flush()
        _membership(session, "synthetic-mem-reader", READER_ID)
        _grant(
            session,
            grant_id="synthetic-grant-reader-original",
            account_id=READER_ID,
            action=Action.ARTIFACT_ORIGINAL_READ,
            scope_type="artifact_version",
            artifact_version_id=version_id,
        )
    with Session(assets) as session:
        downloaded = download_original(
            session,
            _principal(READER_ID),
            SessionAuthorizer(session),
            project_id=PROJECT_ID,
            artifact_version_id=version_id,
            objects_root=objects,
        )
    assert downloaded.body == PAYLOAD
    assert downloaded.server_hash == digest
    assert downloaded.server_hash == hashlib.sha256(downloaded.body).hexdigest()
    assert downloaded.byte_size == len(PAYLOAD)
    assert downloaded.artifact_version_id == version_id
    _no_disk_path(downloaded, objects, attempts, derived, tmp_path)
    for item in results:
        _no_disk_path(item, objects, attempts, derived, tmp_path)


OLD_PREVIEW_NAME = "synthetic-old-original.glb"
NEW_PREVIEW_NAME = "synthetic-new-original.glb"
OLD_VERSION_BYTES = b"old-artifact-version-bytes"
NEW_VERSION_BYTES = b"new-artifact-version-bytes"


@pytest.mark.integration
def test_preview_metadata_stops_at_the_new_version_grant(assets, tmp_path):
    attempts = tmp_path / "attempts"
    objects = tmp_path / "objects"
    derived = tmp_path / "derived"
    registry = tmp_path / "metadata"
    attempts.mkdir()
    objects.mkdir()
    derived.mkdir()
    registry.mkdir()
    with write_uow(assets) as session:
        _seed(session)

    def store(payload: bytes, when: float):
        digest = hashlib.sha256(payload).hexdigest()
        with write_uow(assets) as session:
            ticket = request_upload_ticket(
                session,
                project_id=PROJECT_ID,
                artifact_id=ARTIFACT_ID,
                expected_size=len(payload),
                declared_hash=digest,
                media_type="model/gltf-binary",
                license_name="synthetic-license",
                sensitivity="project",
                ai_processing_allowed=0,
                account_id=ACCOUNT_ID,
                now=when,
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
                now=when + 1.0,
                converter_installed=False,
            )
        assert outcome.registered is True
        assert outcome.preview_generated is False
        assert isinstance(outcome.artifact_version_id, str)
        assert isinstance(outcome.object_key, str)
        assert isinstance(outcome.generation_id, str)
        return outcome

    def crash_new(payload: bytes, work: Path) -> None:
        assert payload == NEW_VERSION_BYTES
        _placeholder(work, b"placeholder-new-version")
        raise RuntimeError("synthetic-crash-detail")

    old = store(OLD_VERSION_BYTES, NOW + 10.0)
    new = store(NEW_VERSION_BYTES, NOW + 20.0)
    assert old.artifact_version_id != new.artifact_version_id
    assert old.object_key != new.object_key
    failed = run_preview_converter(
        NEW_VERSION_BYTES,
        derived_root=derived,
        converter=crash_new,
    )
    assert failed.state == "failed"
    assert failed.reason == "converter_crashed"
    assert failed.generated is False
    record_preview_metadata(
        registry,
        artifact_version_id=str(old.artifact_version_id),
        original_filename=OLD_PREVIEW_NAME,
        disposition=preview_disposition("model/gltf-binary", installed=False),
    )
    record_preview_metadata(
        registry,
        artifact_version_id=str(new.artifact_version_id),
        original_filename=NEW_PREVIEW_NAME,
        disposition=failed,
    )
    with write_uow(assets) as session:
        _account(session, READER_ID, "synthetic-reader")
        _account(session, MEMBER_ID, "synthetic-member")
        _account(session, LOOKER_ID, "synthetic-old-holder")
        session.flush()
        _membership(session, "synthetic-mem-reader", READER_ID)
        _membership(session, "synthetic-mem-member", MEMBER_ID)
        _membership(session, "synthetic-mem-looker", LOOKER_ID)
        _grant(
            session,
            grant_id="synthetic-grant-reader-preview",
            account_id=READER_ID,
            action=Action.PREVIEW_READ,
            scope_type="artifact_version",
            artifact_version_id=str(new.artifact_version_id),
        )
        _grant(
            session,
            grant_id="synthetic-grant-member-original",
            account_id=MEMBER_ID,
            action=Action.ARTIFACT_ORIGINAL_READ,
            scope_type="artifact_version",
            artifact_version_id=str(new.artifact_version_id),
        )
        _grant(
            session,
            grant_id="synthetic-grant-looker-preview-old",
            account_id=LOOKER_ID,
            action=Action.PREVIEW_READ,
            scope_type="artifact_version",
            artifact_version_id=str(old.artifact_version_id),
        )
        _grant(
            session,
            grant_id="synthetic-grant-looker-original-old",
            account_id=LOOKER_ID,
            action=Action.ARTIFACT_ORIGINAL_READ,
            scope_type="artifact_version",
            artifact_version_id=str(old.artifact_version_id),
        )
    with Session(assets) as session:
        authorizer = SessionAuthorizer(session)
        forbidden = read_preview_metadata(
            session,
            _principal(MEMBER_ID),
            authorizer,
            project_id=PROJECT_ID,
            artifact_version_id=str(new.artifact_version_id),
            registry=registry,
        )
        hidden = read_preview_metadata(
            session,
            _principal(LOOKER_ID),
            authorizer,
            project_id=PROJECT_ID,
            artifact_version_id=str(new.artifact_version_id),
            registry=registry,
        )
        opened = read_preview_metadata(
            session,
            _principal(READER_ID),
            authorizer,
            project_id=PROJECT_ID,
            artifact_version_id=str(new.artifact_version_id),
            registry=registry,
        )
    assert isinstance(forbidden, PreviewRefusal)
    assert isinstance(hidden, PreviewRefusal)
    assert forbidden.decision == "forbidden"
    assert hidden.decision == "hidden"
    secret_bits = (
        OLD_PREVIEW_NAME,
        NEW_PREVIEW_NAME,
        str(old.object_key),
        str(new.object_key),
        "converter_crashed",
        "converter_not_installed",
        "synthetic-crash-detail",
    )
    for refused in (forbidden, hidden):
        assert set(asdict(refused)) == {"decision"}
        rendered = repr(refused)
        for secret in secret_bits:
            assert secret not in rendered
            assert secret not in str(refused)
        assert not hasattr(refused, "original_filename")
        assert not hasattr(refused, "object_key")
        assert not hasattr(refused, "storage_path")
        assert not hasattr(refused, "preview_state")
        assert not hasattr(refused, "preview_reason")
        assert not hasattr(refused, "body")
        _no_disk_path(refused, objects, attempts, derived, registry, tmp_path)
    assert isinstance(opened, PreviewDisclosure)
    assert opened.artifact_version_id == new.artifact_version_id
    assert opened.original_filename == NEW_PREVIEW_NAME
    assert opened.object_key == new.object_key
    assert opened.preview_state == "failed"
    assert opened.preview_reason == "converter_crashed"
    assert opened.generated is False
    assert opened.original_filename != OLD_PREVIEW_NAME
    assert opened.object_key != old.object_key
    _no_disk_path(opened, objects, attempts, derived, registry, tmp_path)
    assert [path for path in derived.rglob("*") if path.is_file()] == []
    assert not any(
        path.name.startswith("preview") for path in tmp_path.rglob("*") if path.is_file()
    )


CLAIMED_OLD_BYTES = b"claimed-generation-bytes"
REUPLOADED_BYTES = b"reuploaded-after-gc-claim"


@pytest.mark.integration
@pytest.mark.recovery
def test_gc_claimed_generation_reuploads_on_a_new_generation(assets, tmp_path):
    old_digest = hashlib.sha256(CLAIMED_OLD_BYTES).hexdigest()
    new_digest = hashlib.sha256(REUPLOADED_BYTES).hexdigest()
    assert old_digest != new_digest
    attempts = tmp_path / "attempts"
    objects = tmp_path / "objects"
    attempts.mkdir()
    objects.mkdir()
    with write_uow(assets) as session:
        _seed(session)
        reserved = reserve_generation(
            session,
            project_id=PROJECT_ID,
            sha256=old_digest,
            byte_size=len(CLAIMED_OLD_BYTES),
            now=NOW - 100.0,
        )
    old_path = _generation_file(objects, old_digest, reserved.generation_id)
    old_path.parent.mkdir(parents=True)
    old_path.write_bytes(CLAIMED_OLD_BYTES)
    with write_uow(assets) as session:
        claimed = claim_generation(
            session,
            object_id=reserved.object_id,
            now=NOW,
            retention_seconds=RETENTION_SECONDS,
        )
    assert claimed.claimed is True
    assert claimed.generation_id == reserved.generation_id
    assert claimed.fencing_token == 1
    assert old_path.read_bytes() == CLAIMED_OLD_BYTES
    with write_uow(assets) as session:
        ticket = request_upload_ticket(
            session,
            project_id=PROJECT_ID,
            artifact_id=ARTIFACT_ID,
            expected_size=len(REUPLOADED_BYTES),
            declared_hash=new_digest,
            media_type="model/gltf-binary",
            license_name="synthetic-license",
            sensitivity="project",
            ai_processing_allowed=0,
            account_id=ACCOUNT_ID,
            now=NOW + 1.0,
        )
    relative = f"{ticket.upload_id}/attempt.part"
    write_attempt_bytes(attempts, relative, REUPLOADED_BYTES)
    with write_uow(assets) as session:
        outcome = register_original(
            session,
            upload_id=ticket.upload_id,
            temp_root=attempts,
            relative_path=relative,
            objects_root=objects,
            now=NOW + 2.0,
            converter_installed=False,
        )
    assert outcome.registered is True
    assert outcome.preview_generated is False
    assert outcome.content_hash == new_digest
    assert outcome.generation_id != reserved.generation_id
    assert outcome.generation_id != claimed.generation_id
    version_id = outcome.artifact_version_id
    generation_id = outcome.generation_id
    assert isinstance(version_id, str) and isinstance(generation_id, str)
    fresh = _generation_file(objects, new_digest, generation_id)
    assert fresh.read_bytes() == REUPLOADED_BYTES
    assert old_path.read_bytes() == CLAIMED_OLD_BYTES
    assert not fresh.samefile(old_path)
    with Session(assets) as session:
        row = session.get(StoredObjectRow, reserved.object_id)
        assert row is not None
        assert row.state == "gc_claimed"
        assert row.generation_id == reserved.generation_id
    with write_uow(assets) as session:
        _account(session, READER_ID, "synthetic-reader")
        session.flush()
        _membership(session, "synthetic-mem-reader", READER_ID)
        _grant(
            session,
            grant_id="synthetic-grant-reader-original",
            account_id=READER_ID,
            action=Action.ARTIFACT_ORIGINAL_READ,
            scope_type="artifact_version",
            artifact_version_id=version_id,
        )
    with Session(assets) as session:
        downloaded = download_original(
            session,
            _principal(READER_ID),
            SessionAuthorizer(session),
            project_id=PROJECT_ID,
            artifact_version_id=version_id,
            objects_root=objects,
        )
    assert downloaded.body == REUPLOADED_BYTES
    assert downloaded.body != CLAIMED_OLD_BYTES
    assert downloaded.server_hash == new_digest
    assert downloaded.server_hash == hashlib.sha256(downloaded.body).hexdigest()
    assert downloaded.server_hash == hashlib.sha256(REUPLOADED_BYTES).hexdigest()
    assert downloaded.artifact_version_id == version_id
    assert old_path.read_bytes() == CLAIMED_OLD_BYTES
    _no_disk_path(downloaded, objects, attempts, tmp_path)
    _no_disk_path(outcome, objects, attempts, tmp_path)
    assert not any(
        path.name.startswith("preview") for path in tmp_path.rglob("*") if path.is_file()
    )


HOSTILE_NAME = 'C:\\secret\\store\\..\\"quoted"\r\nleaf.html'
HEADER_BYTES = b"header-download-bytes"
SAFE_NAME = "quotedleaf.html"
CONTENT_PATH = "/api/v1/artifact-versions/{artifact_version_id}/content"


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        (HOSTILE_NAME, SAFE_NAME),
        ("C:\\secret\\store\\file.txt", "file.txt"),
        ("/var/objects/original", "original"),
        ('say"hi\n.txt', "sayhi.txt"),
        ("", "original"),
        ("..", "original"),
        ("   ", "original"),
        ("..\\..\\passwd\r\n.html", "passwd.html"),
    ],
)
def test_safe_download_filename_drops_path_quotes_and_breaks(source, expected):
    from sinuo.application.assets.download import safe_download_filename

    safe = safe_download_filename(source)
    assert safe == expected
    assert "/" not in safe
    assert "\\" not in safe
    assert '"' not in safe
    assert "\r" not in safe
    assert "\n" not in safe


def test_download_headers_attach_active_and_plain_types():
    from sinuo.application.assets.download import download_response_headers, is_active_content

    assert is_active_content("text/html") is True
    assert is_active_content("image/svg+xml") is True
    assert is_active_content("model/gltf-binary") is False
    active = download_response_headers(HOSTILE_NAME, "text/html")
    plain = download_response_headers("model.glb", "model/gltf-binary")
    for headers in (active, plain):
        disposition = headers["Content-Disposition"]
        assert headers["X-Content-Type-Options"] == "nosniff"
        assert headers["Cache-Control"] == "no-store"
        assert disposition.startswith("attachment;")
        assert "inline" not in disposition.lower()
        assert "\r" not in disposition
        assert "\n" not in disposition
        assert ".." not in disposition
        assert "C:\\secret\\store" not in disposition
    assert 'filename="quotedleaf.html"' in active["Content-Disposition"]
    assert active["Content-Type"] == "text/html"
    assert plain["Content-Type"] == "model/gltf-binary"
    assert 'filename="model.glb"' in plain["Content-Disposition"]


@pytest.mark.integration
def test_mounted_download_route_sets_nosniff_and_attachment(assets, database_url, tmp_path):
    import time

    from fastapi.testclient import TestClient

    from sinuo.api.app import create_app
    from sinuo.application.assets.download import safe_download_filename
    from sinuo.application.auth.sessions import issue_session
    from sinuo.domain.public_contract import session_cookie_name
    from tests.acceptance.identity_fixtures import identity_settings

    digest = hashlib.sha256(HEADER_BYTES).hexdigest()
    attempts = tmp_path / "attempts"
    objects = tmp_path / "objects"
    attempts.mkdir()
    objects.mkdir()
    with write_uow(assets) as session:
        _seed(session)
        ticket = request_upload_ticket(
            session,
            project_id=PROJECT_ID,
            artifact_id=ARTIFACT_ID,
            expected_size=len(HEADER_BYTES),
            declared_hash=digest,
            media_type="text/html",
            license_name="synthetic-license",
            sensitivity="project",
            ai_processing_allowed=0,
            account_id=ACCOUNT_ID,
            now=NOW,
        )
    relative = f"{ticket.upload_id}/attempt-1.part"
    write_attempt_bytes(attempts, relative, HEADER_BYTES)
    with write_uow(assets) as session:
        outcome = register_original(
            session,
            upload_id=ticket.upload_id,
            temp_root=attempts,
            relative_path=relative,
            objects_root=objects,
            now=NOW + 1,
            converter_installed=False,
        )
    assert outcome.registered is True
    assert outcome.preview_generated is False
    version_id = outcome.artifact_version_id
    assert isinstance(version_id, str)
    assert len(HOSTILE_NAME) <= 200
    assert safe_download_filename(HOSTILE_NAME) == SAFE_NAME
    with write_uow(assets) as session:
        artifact = session.get(Artifact, ARTIFACT_ID)
        assert artifact is not None
        artifact.title = HOSTILE_NAME
        _account(session, READER_ID, "synthetic-reader")
        _account(session, MEMBER_ID, "synthetic-member")
        _account(session, LOOKER_ID, "synthetic-looker")
        session.flush()
        _membership(session, "synthetic-mem-reader", READER_ID)
        _membership(session, "synthetic-mem-member", MEMBER_ID)
        _membership(session, "synthetic-mem-looker", LOOKER_ID)
        _grant(
            session,
            grant_id="synthetic-grant-reader-original",
            account_id=READER_ID,
            action=Action.ARTIFACT_ORIGINAL_READ,
            scope_type="artifact_version",
            artifact_version_id=version_id,
        )
        _grant(
            session,
            grant_id="synthetic-grant-member-preview",
            account_id=MEMBER_ID,
            action=Action.PREVIEW_READ,
            scope_type="artifact_version",
            artifact_version_id=version_id,
        )
        _grant(
            session,
            grant_id="synthetic-grant-looker-read",
            account_id=LOOKER_ID,
            action=Action.PROJECT_READ,
            scope_type="project",
            artifact_version_id=None,
        )

    settings = identity_settings(database_url)
    app = create_app(settings)
    assert CONTENT_PATH in app.openapi()["paths"]
    app.state.objects_root = objects
    cookie = session_cookie_name(settings.public_url)

    def token_for(account_id: str) -> str:
        with write_uow(assets) as session:
            account = session.get(Account, account_id)
            assert account is not None
            return issue_session(session, account, settings, time.time()).token

    reader_token = token_for(READER_ID)
    member_token = token_for(MEMBER_ID)
    looker_token = token_for(LOOKER_ID)
    url = f"/api/v1/artifact-versions/{version_id}/content"
    disk_bits = (
        str(objects.resolve()),
        str(attempts.resolve()),
        str(tmp_path.resolve()),
        "C:\\secret\\store",
    )

    def assert_path_hidden(response) -> None:
        text = response.content.decode("latin-1")
        for value in response.headers.values():
            text = text + "\n" + value
        for secret in disk_bits:
            assert secret not in text
        assert "\r" not in response.headers.get("content-disposition", "")
        assert "\n" not in response.headers.get("content-disposition", "")

    with TestClient(app) as client:
        client.cookies.set(cookie, reader_token)
        allowed = client.get(url, params={"filename": '..\\..\\passwd\r\n.html'})
        assert allowed.status_code == 200
        assert allowed.content == HEADER_BYTES
        assert allowed.headers["x-content-type-options"] == "nosniff"
        assert allowed.headers["cache-control"] == "no-store"
        assert allowed.headers["content-type"] == "text/html"
        disposition = allowed.headers["content-disposition"]
        assert disposition.startswith("attachment;")
        assert "inline" not in disposition.lower()
        marker = 'filename="'
        start = disposition.index(marker) + len(marker)
        end = disposition.index('"', start)
        assert disposition[start:end] == SAFE_NAME
        assert "passwd" not in disposition
        assert "%0a" not in disposition.lower()
        assert "%0d" not in disposition.lower()
        assert_path_hidden(allowed)
        client.cookies.set(cookie, member_token)
        forbidden = client.get(url)
        assert forbidden.status_code == 403
        assert forbidden.json()["error"]["code"] == "ACTION_FORBIDDEN"
        assert HEADER_BYTES not in forbidden.content
        assert SAFE_NAME not in forbidden.text
        assert "content-disposition" not in forbidden.headers
        assert_path_hidden(forbidden)
        client.cookies.set(cookie, looker_token)
        hidden = client.get(url)
        assert hidden.status_code == 404
        assert hidden.json()["error"]["code"] == "NOT_FOUND_OR_HIDDEN"
        assert HEADER_BYTES not in hidden.content
        assert SAFE_NAME not in hidden.text
        assert "content-disposition" not in hidden.headers
        assert_path_hidden(hidden)
        client.cookies.clear()
        anonymous = client.get(url)
        assert anonymous.status_code == 401
        assert anonymous.json()["error"]["code"] == "AUTH_REQUIRED"
        assert HEADER_BYTES not in anonymous.content
        assert SAFE_NAME not in anonymous.text
        assert_path_hidden(anonymous)
    assert not any(
        path.name.startswith("preview") for path in tmp_path.rglob("*") if path.is_file()
    )


@pytest.mark.integration
def test_reuse_consume_requires_the_captured_fencing_token(assets, tmp_path):
    payload = b"sinuo-reuse-fence-bytes"
    digest = hashlib.sha256(payload).hexdigest()
    attempts = tmp_path / "attempts"
    objects = tmp_path / "objects"
    attempts.mkdir()
    objects.mkdir()
    with write_uow(assets) as session:
        _seed(session)

    def open_ticket(when: float) -> str:
        with write_uow(assets) as session:
            ticket = request_upload_ticket(
                session,
                project_id=PROJECT_ID,
                artifact_id=ARTIFACT_ID,
                expected_size=len(payload),
                declared_hash=digest,
                media_type="model/gltf-binary",
                license_name="synthetic-license",
                sensitivity="project",
                ai_processing_allowed=0,
                account_id=ACCOUNT_ID,
                now=when,
            )
        return ticket.upload_id

    def register(upload_id: str, when: float):
        relative = f"{upload_id}/attempt.part"
        write_attempt_bytes(attempts, relative, payload)
        with write_uow(assets) as session:
            return register_original(
                session,
                upload_id=upload_id,
                temp_root=attempts,
                relative_path=relative,
                objects_root=objects,
                now=when,
                converter_installed=False,
            )

    first_id = open_ticket(NOW)
    first = register(first_id, NOW + 1.0)
    assert first.registered is True
    assert isinstance(first.generation_id, str)
    assert isinstance(first.artifact_version_id, str)
    reuse_id = open_ticket(NOW + 2.0)
    reused = register(reuse_id, NOW + 3.0)
    assert reused.registered is True
    assert reused.generation_id == first.generation_id
    assert reused.object_key == first.object_key
    assert reused.artifact_version_id != first.artifact_version_id
    assert reused.content_hash == digest
    stored_file = _generation_file(objects, digest, str(first.generation_id))
    assert stored_file.is_file()
    assert stored_file.read_bytes() == payload
    assert hashlib.sha256(stored_file.read_bytes()).hexdigest() == digest
    assert _files_named(objects, "original") == [stored_file]
    with Session(assets) as session:
        ready = session.scalars(
            select(StoredObjectRow).where(StoredObjectRow.project_id == PROJECT_ID)
        ).one()
        reuse_pin = session.scalars(
            select(ObjectPinRow).where(ObjectPinRow.upload_id == reuse_id)
        ).one()
        assert ready.state == "ready"
        assert reuse_pin.consumed_at == NOW + 3.0
        assert reuse_pin.fencing_token == ready.fencing_token
        assert session.scalar(select(func.count()).select_from(ArtifactVersion)) == 2

    # object_pins_before_update rejects fencing_token changes. Suspend it so
    # this test can move the placed pin's token and show the consume predicate
    # rolls the new version back. The trigger is restored before returning.
    with write_uow(assets) as session:
        connection = session.connection()
        trigger_sql = connection.exec_driver_sql(
            "SELECT sql FROM sqlite_master WHERE type = 'trigger' "
            "AND name = 'object_pins_before_update'"
        ).scalar()
        assert isinstance(trigger_sql, str) and "object_pin_identity" in trigger_sql
        connection.exec_driver_sql("DROP TRIGGER object_pins_before_update")
    mismatch_id = open_ticket(NOW + 4.0)
    relative = f"{mismatch_id}/attempt.part"
    write_attempt_bytes(attempts, relative, payload)
    shifted = {"done": False}

    def shift_placed_pin(session, _flush_context) -> None:
        if shifted["done"]:
            return
        connection = session.connection()
        found = connection.exec_driver_sql(
            "SELECT pin_id, fencing_token FROM object_pins "
            "WHERE upload_id = ? AND consumed_at IS NULL",
            (mismatch_id,),
        )
        try:
            row = found.fetchone()
        finally:
            found.close()
        if row is None:
            return
        shifted["done"] = True
        connection.exec_driver_sql(
            "UPDATE object_pins SET fencing_token = ? WHERE pin_id = ?",
            (int(row[1]) + 1, row[0]),
        )

    try:
        with pytest.raises(RegistrationConflict):
            with write_uow(assets) as session:
                event.listen(session, "after_flush_postexec", shift_placed_pin)
                try:
                    register_original(
                        session,
                        upload_id=mismatch_id,
                        temp_root=attempts,
                        relative_path=relative,
                        objects_root=objects,
                        now=NOW + 5.0,
                        converter_installed=False,
                    )
                finally:
                    event.remove(session, "after_flush_postexec", shift_placed_pin)
        assert shifted["done"] is True
        assert _files_named(objects, "original") == [stored_file]
        with Session(assets) as session:
            versions = set(
                session.scalars(
                    select(ArtifactVersion.id).where(ArtifactVersion.project_id == PROJECT_ID)
                ).all()
            )
            ticket = session.get(UploadTicketRow, mismatch_id)
            pins = session.scalars(
                select(ObjectPinRow).where(ObjectPinRow.upload_id == mismatch_id)
            ).all()
            assert versions == {first.artifact_version_id, reused.artifact_version_id}
            assert ticket is not None
            assert ticket.status == "open"
            assert ticket.artifact_version_id is None
            assert pins == [] or all(pin.consumed_at is None for pin in pins)
            assert all(pin.consumed_at is None for pin in pins)
    finally:
        with write_uow(assets) as session:
            session.connection().exec_driver_sql(trigger_sql)
