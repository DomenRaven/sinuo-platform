"""Export snapshots obey current rights, and one start key registers one job.

These tests cover T10 acceptance items 2 and 3. Review opinions stay in
test_review_opinions.py. Later tests cover backup and restore.
"""

import hashlib
import io
import json
import shutil
import sqlite3
import uuid
import zipfile
from dataclasses import dataclass
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import Session

from sinuo.api.app import create_app
from sinuo.application.assets.download import download_original
from sinuo.application.auth.authorizer import SessionAuthorizer
from sinuo.application.exports.package import run_export_job
from sinuo.application.recovery import (
    FAILURE_CORRUPT,
    FAILURE_INCOMPATIBLE_VERSION,
    FAILURE_MISSING_ORIGINAL,
    backup_snapshot,
    restore_snapshot,
)
from sinuo.application.recovery.snapshot import (
    DETAIL_CORRUPT,
    DETAIL_INCOMPATIBLE_VERSION,
    DETAIL_MISSING_ORIGINAL,
)
from sinuo.application.versions.branches import branch_from_version
from sinuo.domain.auth.errors import SourceRedacted
from sinuo.domain.auth.principal import Principal
from sinuo.domain.public_contract import CSRF_HEADER
from sinuo.domain.schema_types import canonical_audit_payload, canonical_document, document_hash
from sinuo.infrastructure.assets.objects import object_relative, original_key
from sinuo.infrastructure.database import build_engine, write_uow
from sinuo.infrastructure.domain_models import (
    Artifact,
    ArtifactVersion,
    AuditEvent,
    Decision,
    EvidenceAnchor,
    Job,
    ObjectGrant,
    Page,
    PageVersion,
    PrivacyTombstone,
)
from tests.acceptance.identity_fixtures import (
    CITATION,
    LEAD_ID,
    LEAD_LOGIN,
    LEAD_PASSWORD,
    NOW,
    OTHER_LOGIN,
    OTHER_PASSWORD,
    PAGE_A,
    PAGE_A_TITLE,
    PAGE_B_TITLE,
    PROJECT_A,
    REVIEWER_LOGIN,
    REVIEWER_PASSWORD,
    SENSITIVE_TITLE,
    TEAM_ID,
    TEAM_LOGIN,
    TEAM_PASSWORD,
    identity_settings,
    seed_identity_world,
)

ORIGIN = "http://127.0.0.1:8000"
GENERATION = "exportgen1"
PUBLIC_ORIGINAL = b"SYNTHETIC-PUBLIC-ORIGINAL"
SECRET_ORIGINAL = b"SYNTHETIC-SECRET-ORIGINAL"
TOMB_ORIGINAL = b"SYNTHETIC-TOMB-ORIGINAL"
MISSING_PLAIN = b"SYNTHETIC-MISSING-ORIGINAL"
PUBLIC_THUMB = b"\x89PNG\r\n\x1a\nSYNTHETIC-PUBLIC-THUMB"
SECRET_THUMB = b"\x89PNG\r\n\x1a\nSYNTHETIC-SECRET-THUMB"
EXCERPT = "synthetic-export-excerpt-canary"
PUBLIC_TITLE = "synthetic-export-public-title"
SECRET_TITLE = "synthetic-export-secret-title"
TOMB_TITLE = "synthetic-export-tomb-title"
MISSING_TITLE = "synthetic-export-missing-title"
LATER_NODE = "synthetic-export-later-node"


@dataclass(frozen=True)
class Planted:
    page_version_id: str
    public_version_id: str
    secret_version_id: str
    tomb_version_id: str
    missing_version_id: str
    excerpt_id: str
    foreign_evidence_id: str
    objects: Path
    derived: Path
    exports: Path


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


def _page_version(engine) -> str:
    with engine.connect() as connection:
        value = connection.execute(
            text("SELECT id FROM page_versions WHERE page_id = :page_id"),
            {"page_id": PAGE_A},
        ).scalar_one()
    return str(value)


def _jobs(engine) -> int:
    with engine.connect() as connection:
        value = connection.execute(
            text(
                """
                SELECT count(*) FROM jobs
                WHERE project_id = :project_id AND job_kind = 'export'
                """
            ),
            {"project_id": PROJECT_A},
        ).scalar_one()
    return int(value)


def _job_row(engine, export_id: str):
    with engine.connect() as connection:
        return connection.execute(
            text(
                """
                SELECT status, last_error, output_ref, payload
                FROM jobs WHERE id = :export_id
                """
            ),
            {"export_id": export_id},
        ).one()


def _grant_id(engine, action: str) -> str:
    with engine.connect() as connection:
        value = connection.execute(
            text(
                """
                SELECT id FROM object_grants
                WHERE project_id = :project_id AND account_id = :account_id
                  AND status = 'active' AND scope_type = 'project' AND action = :action
                """
            ),
            {"project_id": PROJECT_A, "account_id": LEAD_ID, "action": action},
        ).scalar_one()
    return str(value)


def _store_original(root: Path, body: bytes) -> tuple[str, str]:
    digest = _sha(body)
    relative = object_relative(PROJECT_A, digest, GENERATION)
    path = root.joinpath(*relative.split("/"))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(body)
    return digest, original_key(PROJECT_A, digest, GENERATION)


def _declared_original(body: bytes) -> tuple[str, str]:
    digest = _sha(body)
    return digest, original_key(PROJECT_A, digest, GENERATION)


def _store_thumb(root: Path, version_id: str, body: bytes) -> None:
    path = root / "previews" / f"{version_id}.png"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(body)


def _artifact(artifact_id: str, title: str) -> Artifact:
    return Artifact(id=artifact_id, project_id=PROJECT_A, title=title, created_at=NOW)


def _version(
    version_id: str,
    artifact_id: str,
    *,
    content_hash: str,
    size_bytes: int,
    object_key: str,
    sensitivity: str,
) -> ArtifactVersion:
    return ArtifactVersion(
        id=version_id,
        project_id=PROJECT_A,
        artifact_id=artifact_id,
        version_no=1,
        content_hash=content_hash,
        size_bytes=size_bytes,
        media_type="application/octet-stream",
        object_key=object_key,
        license="synthetic",
        sensitivity=sensitivity,
        ai_processing_allowed=0,
        project_revision=1,
        created_by_account_id=LEAD_ID,
        created_at=NOW,
    )


def _plant(engine, tmp_path: Path) -> Planted:
    settings = identity_settings(f"sqlite:///{(tmp_path / 'ignored.db').as_posix()}")
    world = seed_identity_world(engine, settings)
    objects = tmp_path / "objects"
    derived = tmp_path / "derived"
    exports = tmp_path / "exports"
    objects.mkdir()
    derived.mkdir()
    exports.mkdir()
    public_id = str(uuid.uuid4())
    secret_id = str(uuid.uuid4())
    tomb_id = str(uuid.uuid4())
    missing_id = str(uuid.uuid4())
    excerpt_id = str(uuid.uuid4())
    public_hash, public_key = _store_original(objects, PUBLIC_ORIGINAL)
    secret_hash, secret_key = _store_original(objects, SECRET_ORIGINAL)
    tomb_hash, tomb_key = _store_original(objects, TOMB_ORIGINAL)
    missing_hash, missing_key = _declared_original(MISSING_PLAIN)
    _store_thumb(derived, public_id, PUBLIC_THUMB)
    _store_thumb(derived, secret_id, SECRET_THUMB)
    with write_uow(engine) as session:
        session.add_all(
            [
                _artifact("synthetic-export-public", PUBLIC_TITLE),
                _artifact("synthetic-export-secret", SECRET_TITLE),
                _artifact("synthetic-export-tomb", TOMB_TITLE),
                _artifact("synthetic-export-missing", MISSING_TITLE),
            ]
        )
        session.flush()
        session.add_all(
            [
                _version(
                    public_id,
                    "synthetic-export-public",
                    content_hash=public_hash,
                    size_bytes=len(PUBLIC_ORIGINAL),
                    object_key=public_key,
                    sensitivity="released",
                ),
                _version(
                    secret_id,
                    "synthetic-export-secret",
                    content_hash=secret_hash,
                    size_bytes=len(SECRET_ORIGINAL),
                    object_key=secret_key,
                    sensitivity="sensitive",
                ),
                _version(
                    tomb_id,
                    "synthetic-export-tomb",
                    content_hash=tomb_hash,
                    size_bytes=len(TOMB_ORIGINAL),
                    object_key=tomb_key,
                    sensitivity="released",
                ),
                _version(
                    missing_id,
                    "synthetic-export-missing",
                    content_hash=missing_hash,
                    size_bytes=len(MISSING_PLAIN),
                    object_key=missing_key,
                    sensitivity="released",
                ),
            ]
        )
        session.flush()
        session.add(
            EvidenceAnchor(
                id=excerpt_id,
                project_id=PROJECT_A,
                artifact_version_id=secret_id,
                locator_type="text_range",
                locator_json=json.dumps({"marker": EXCERPT}, separators=(",", ":")),
                excerpt_hash=_sha(EXCERPT.encode("utf-8")),
                extractor_version="synthetic-1",
                visibility="restricted",
                sensitivity="sensitive",
                supersedes_id=None,
                schema_version=1,
                created_by_account_id=LEAD_ID,
                created_at=NOW,
            )
        )
        audit_id = str(uuid.uuid4())
        session.add(
            AuditEvent(
                id=audit_id,
                project_id=PROJECT_A,
                actor_id=LEAD_ID,
                command="privacy.delete",
                subject_type="artifact_version",
                subject_id=tomb_id,
                project_revision=None,
                payload=canonical_audit_payload({"object_id": tomb_id}),
                created_at=NOW,
            )
        )
        session.flush()
        session.add(
            PrivacyTombstone(
                id=str(uuid.uuid4()),
                project_id=PROJECT_A,
                object_type="artifact_version",
                object_id=tomb_id,
                reason_category="policy",
                access_epoch=1,
                audit_event_id=audit_id,
                created_at=NOW,
            )
        )
        session.flush()
    return Planted(
        page_version_id=_page_version(engine),
        public_version_id=public_id,
        secret_version_id=secret_id,
        tomb_version_id=tomb_id,
        missing_version_id=missing_id,
        excerpt_id=excerpt_id,
        foreign_evidence_id=world.foreign_evidence_id,
        objects=objects,
        derived=derived,
        exports=exports,
    )


def _body(planted: Planted, *, include_originals: bool, full: bool) -> dict[str, object]:
    versions = [planted.public_version_id]
    anchors = [planted.excerpt_id]
    if full:
        versions = [
            planted.public_version_id,
            planted.secret_version_id,
            planted.tomb_version_id,
            planted.missing_version_id,
        ]
        anchors = [planted.excerpt_id, planted.foreign_evidence_id]
    manifest = {
        "page_version_ids": [planted.page_version_id],
        "artifact_version_ids": versions,
        "evidence_anchor_ids": anchors,
    }
    return {
        "format": "markdown",
        "include_originals": include_originals,
        "source_manifest": manifest,
        "confirmed_scope": {
            "page_version_ids": list(manifest["page_version_ids"]),
            "artifact_version_ids": list(manifest["artifact_version_ids"]),
            "evidence_anchor_ids": list(manifest["evidence_anchor_ids"]),
        },
    }


def _post(
    client: TestClient,
    csrf: str,
    planted: Planted,
    *,
    key: str,
    include_originals: bool,
    full: bool,
):
    return client.post(
        f"/api/v1/projects/{PROJECT_A}/exports",
        headers=_headers(csrf, key),
        json=_body(planted, include_originals=include_originals, full=full),
    )


def _bind(app, planted: Planted) -> None:
    app.state.objects_root = planted.objects
    app.state.derived_root = planted.derived
    app.state.exports_root = planted.exports


def _run(engine, planted: Planted, export_id: str):
    return run_export_job(
        engine,
        export_id,
        exports_root=planted.exports,
        objects_root=planted.objects,
        derived_root=planted.derived,
    )


def _unzip(payload: bytes) -> dict[str, bytes]:
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        return {name: archive.read(name) for name in archive.namelist()}


def _joined(files: dict[str, bytes]) -> bytes:
    return b"".join(files[name] for name in sorted(files))


def _manifest(files: dict[str, bytes]) -> dict:
    return json.loads(files["manifest.json"].decode("utf-8"))


def _assert_file_hashes(files: dict[str, bytes]) -> dict:
    manifest = _manifest(files)
    assert manifest["latest"] is False
    listed = {item["path"]: item["sha256"] for item in manifest["files"]}
    assert "manifest.json" not in listed
    for path, digest in listed.items():
        assert _sha(files[path]) == digest
        matched = next(item for item in manifest["files"] if item["path"] == path)
        assert matched["byte_size"] == len(files[path])
    assert set(files) - {"manifest.json"} == set(listed)
    return manifest


def _disk(planted: Planted, export_id: str, output_ref: str) -> dict[str, bytes]:
    folder = planted.exports / export_id / output_ref
    files: dict[str, bytes] = {}
    for path in folder.rglob("*"):
        if path.is_file():
            files[path.relative_to(folder).as_posix()] = path.read_bytes()
    return files


def _item(manifest: dict, kind: str, item_id: str) -> dict:
    found = [item for item in manifest["items"] if item["kind"] == kind and item["id"] == item_id]
    assert len(found) == 1
    return found[0]


def _content(client: TestClient, export_id: str) -> dict[str, bytes]:
    response = client.get(f"/api/v1/exports/{export_id}/content")
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    files = _unzip(response.content)
    _assert_file_hashes(files)
    return files


def _app(database_url: str, planted: Planted):
    app = create_app(identity_settings(database_url))
    _bind(app, planted)
    return app


def test_export_scope_originals_thumbnails_and_excerpts_follow_acl(engine, database_url, tmp_path):
    planted = _plant(engine, tmp_path)
    app = _app(database_url, planted)
    full = _body(planted, include_originals=True, full=True)
    with TestClient(app) as client:
        reviewer = _login(client, REVIEWER_LOGIN, REVIEWER_PASSWORD)
        denied = client.post(
            f"/api/v1/projects/{PROJECT_A}/exports",
            headers=_headers(reviewer, "synthetic-export-reviewer"),
            json=full,
        )
        assert denied.status_code == 403, denied.text
        assert denied.json()["error"]["code"] == "ACTION_FORBIDDEN"
        assert denied.json()["error"]["details"]["action"] == "report.export"
        _logout(client, reviewer)
        team = _login(client, TEAM_LOGIN, TEAM_PASSWORD)
        team_denied = client.post(
            f"/api/v1/projects/{PROJECT_A}/exports",
            headers=_headers(team, "synthetic-export-team"),
            json=full,
        )
        assert team_denied.status_code == 403, team_denied.text
        assert team_denied.json()["error"]["details"]["action"] == "report.export"
        _logout(client, team)
        outsider = _login(client, OTHER_LOGIN, OTHER_PASSWORD)
        hidden = client.post(
            f"/api/v1/projects/{PROJECT_A}/exports",
            headers=_headers(outsider, "synthetic-export-outsider"),
            json=full,
        )
        assert hidden.status_code == 404, hidden.text
        assert hidden.json()["error"]["code"] == "NOT_FOUND_OR_HIDDEN"
        _logout(client, outsider)
        assert _jobs(engine) == 0
        csrf = _login(client, LEAD_LOGIN, LEAD_PASSWORD)
        optional = _post(
            client,
            csrf,
            planted,
            key="synthetic-export-optional",
            include_originals=False,
            full=False,
        )
        assert optional.status_code == 202, optional.text
        assert optional.json()["data"]["latest"] is False
        optional_id = optional.json()["data"]["export_job_id"]
        assert _run(engine, planted, optional_id).status == "succeeded"
        skipped = _content(client, optional_id)
        skipped_manifest = _manifest(skipped)
        public_original = _item(skipped_manifest, "artifact_version", planted.public_version_id)
        assert public_original["original"]["status"] == "not_requested"
        assert "sha256" not in public_original["original"]
        assert public_original["thumbnail"]["status"] == "included"
        excerpt = _item(skipped_manifest, "evidence_anchor", planted.excerpt_id)
        assert excerpt["excerpt"]["status"] == "included"
        skipped_bytes = _joined(skipped)
        assert PUBLIC_ORIGINAL not in skipped_bytes
        assert b"SYNTHETIC-PUBLIC-THUMB" in skipped_bytes
        assert EXCERPT.encode("utf-8") in skipped_bytes
        accepted = _post(
            client,
            csrf,
            planted,
            key="synthetic-export-full",
            include_originals=True,
            full=True,
        )
        assert accepted.status_code == 202, accepted.text
        export_id = accepted.json()["data"]["export_job_id"]
        assert accepted.json()["data"]["status"] == "queued"
        assert accepted.json()["data"]["latest"] is False
        pages = accepted.json()["data"]["source_manifest"]["page_version_ids"]
        assert pages == [planted.page_version_id]
        assert _run(engine, planted, export_id).status == "succeeded"
        packed = _content(client, export_id)
        manifest = _manifest(packed)
        blob = _joined(packed)
        report = packed["report.md"].decode("utf-8")
        assert "不是最新来源" in report
        assert "最新来源：否" in report
        assert "已墓碑" in report
        assert "缺失" in report
        assert PAGE_A_TITLE in report
        assert PUBLIC_TITLE in report
        assert SECRET_TITLE in report
        assert TOMB_TITLE not in report
        assert MISSING_TITLE not in report
        assert SENSITIVE_TITLE not in report
        assert PAGE_B_TITLE not in report
        assert CITATION not in report
        assert EXCERPT in report
        assert PUBLIC_ORIGINAL in blob
        assert SECRET_ORIGINAL in blob
        assert b"SYNTHETIC-PUBLIC-THUMB" in blob
        assert b"SYNTHETIC-SECRET-THUMB" not in blob
        assert TOMB_ORIGINAL not in blob
        assert MISSING_PLAIN not in blob
        assert f"originals/{planted.tomb_version_id}" not in packed
        assert f"originals/{planted.missing_version_id}" not in packed
        assert f"thumbnails/{planted.secret_version_id}.png" not in packed
        public = _item(manifest, "artifact_version", planted.public_version_id)
        secret = _item(manifest, "artifact_version", planted.secret_version_id)
        tomb = _item(manifest, "artifact_version", planted.tomb_version_id)
        missing = _item(manifest, "artifact_version", planted.missing_version_id)
        assert public["original"]["status"] == "included"
        assert public["original"]["sha256"] == _sha(PUBLIC_ORIGINAL)
        assert public["thumbnail"]["status"] == "included"
        assert secret["original"]["status"] == "included"
        assert secret["thumbnail"]["status"] == "omitted"
        assert "sha256" not in secret["thumbnail"]
        assert tomb["original"]["status"] == "tombstoned"
        assert "sha256" not in tomb["original"]
        assert missing["original"]["status"] == "missing"
        assert "sha256" not in missing["original"]
        assert _sha(MISSING_PLAIN) not in json.dumps(manifest["files"])
        kept = _item(manifest, "evidence_anchor", planted.excerpt_id)
        assert kept["excerpt"]["status"] == "included"
        foreign = _item(manifest, "evidence_anchor", planted.foreign_evidence_id)
        assert foreign["excerpt"]["status"] == "omitted"
        assert _item(manifest, "page_version", planted.page_version_id)["status"] == "included"
        frozen = json.loads(_job_row(engine, export_id).payload)
        assert frozen["latest"] is False
        assert frozen["permission_scope"]["original_read"][planted.secret_version_id] == "allow"
        assert frozen["permission_scope"]["preview_read"][planted.secret_version_id] == "forbidden"
        assert frozen["permission_scope"]["evidence_read"][planted.foreign_evidence_id] == "hidden"
        _logout(client, csrf)
        other = _login(client, OTHER_LOGIN, OTHER_PASSWORD)
        unseen = client.get(f"/api/v1/exports/{export_id}")
        assert unseen.status_code == 404, unseen.text
        assert EXCERPT not in unseen.text
        _logout(client, other)
    assert _jobs(engine) == 2


def test_revoke_during_export_refuses_restricted_download(engine, database_url, tmp_path):
    planted = _plant(engine, tmp_path)
    app = _app(database_url, planted)
    with TestClient(app) as client:
        csrf = _login(client, LEAD_LOGIN, LEAD_PASSWORD)
        first = _post(
            client,
            csrf,
            planted,
            key="synthetic-export-before-revoke",
            include_originals=True,
            full=True,
        )
        assert first.status_code == 202, first.text
        first_id = first.json()["data"]["export_job_id"]
        snapshot_id = first.json()["data"]["input_snapshot_id"]
        finished = _run(engine, planted, first_id)
        assert finished.status == "succeeded"
        assert finished.output_ref is not None
        before = _content(client, first_id)
        assert PUBLIC_ORIGINAL in _joined(before)
        assert SECRET_ORIGINAL in _joined(before)
        assert b"SYNTHETIC-PUBLIC-THUMB" in _joined(before)
        assert EXCERPT.encode("utf-8") in _joined(before)
        second = _post(
            client,
            csrf,
            planted,
            key="synthetic-export-while-queued",
            include_originals=True,
            full=True,
        )
        assert second.status_code == 202, second.text
        second_id = second.json()["data"]["export_job_id"]
        queued_payload = _job_row(engine, second_id).payload
        queued = json.loads(queued_payload)
        assert queued["permission_scope"]["original_read"][planted.public_version_id] == "allow"
        assert queued["permission_scope"]["preview_read"][planted.public_version_id] == "allow"
        assert queued["permission_scope"]["evidence_read"][planted.excerpt_id] == "allow"
        assert queued["access_epoch"] == 1
        epoch = _revoke(
            client,
            csrf,
            1,
            [
                _grant_id(engine, "artifact.original.read"),
                _grant_id(engine, "preview.read"),
                _grant_id(engine, "evidence.read"),
                _grant_id(engine, "page.read"),
            ],
            "synthetic-export-revoke-content",
        )
        assert epoch == 2
        withheld = _content(client, first_id)
        withheld_blob = _joined(withheld)
        withheld_report = withheld["report.md"].decode("utf-8")
        assert PUBLIC_ORIGINAL not in withheld_blob
        assert SECRET_ORIGINAL not in withheld_blob
        assert b"SYNTHETIC-PUBLIC-THUMB" not in withheld_blob
        assert b"SYNTHETIC-SECRET-THUMB" not in withheld_blob
        assert EXCERPT.encode("utf-8") not in withheld_blob
        assert PAGE_A_TITLE not in withheld_report
        assert "输入授权纪元：1" in withheld_report
        assert _manifest(withheld)["input_snapshot_id"] == snapshot_id
        assert _manifest(withheld)["latest"] is False
        withheld_public = _item(
            _manifest(withheld),
            "artifact_version",
            planted.public_version_id,
        )
        assert withheld_public["original"]["status"] == "omitted"
        stored = _joined(_disk(planted, first_id, finished.output_ref))
        assert PUBLIC_ORIGINAL in stored
        generated = _run(engine, planted, second_id)
        assert generated.status == "succeeded"
        assert generated.output_ref is not None
        assert _job_row(engine, second_id).payload == queued_payload
        second_disk = _joined(_disk(planted, second_id, generated.output_ref))
        second_files = _content(client, second_id)
        assert PUBLIC_ORIGINAL not in second_disk
        assert SECRET_ORIGINAL not in second_disk
        assert EXCERPT.encode("utf-8") not in second_disk
        assert b"SYNTHETIC-PUBLIC-THUMB" not in second_disk
        assert PUBLIC_ORIGINAL not in _joined(second_files)
        public = _item(_manifest(second_files), "artifact_version", planted.public_version_id)
        assert public["original"]["status"] == "omitted"
        third = _post(
            client,
            csrf,
            planted,
            key="synthetic-export-after-content-revoke",
            include_originals=True,
            full=False,
        )
        assert third.status_code == 202, third.text
        third_id = third.json()["data"]["export_job_id"]
        third_payload = _job_row(engine, third_id).payload
        assert json.loads(third_payload)["permission_scope"]["report_export"] == "allow"
        epoch = _revoke(
            client,
            csrf,
            epoch,
            [_grant_id(engine, "report.export")],
            "synthetic-export-revoke-report",
        )
        assert epoch == 3
        failed = _run(engine, planted, third_id)
        assert failed.status == "failed"
        assert failed.output_ref is None
        row = _job_row(engine, third_id)
        assert row.status == "failed"
        assert row.last_error == "unauthorized"
        assert row.output_ref is None
        assert row.payload == third_payload
        assert not (planted.exports / third_id).exists()
        for export_id in (first_id, second_id, third_id):
            blocked = client.get(f"/api/v1/exports/{export_id}/content")
            assert blocked.status_code == 403, blocked.text
            assert blocked.json()["error"]["code"] == "ACTION_FORBIDDEN"
            assert EXCERPT not in blocked.text
            assert PUBLIC_ORIGINAL not in blocked.content
            status = client.get(f"/api/v1/exports/{export_id}")
            assert status.status_code == 403, status.text
        again = _post(
            client,
            csrf,
            planted,
            key="synthetic-export-after-report-revoke",
            include_originals=True,
            full=False,
        )
        assert again.status_code == 403, again.text
        assert _jobs(engine) == 3


def _revoke(client: TestClient, csrf: str, epoch: int, grant_ids: list[str], key: str) -> int:
    response = client.patch(
        f"/api/v1/projects/{PROJECT_A}/access",
        headers=_headers(csrf, key),
        json={
            "expected_access_epoch": epoch,
            "subject_account_id": LEAD_ID,
            "revoke_grant_ids": grant_ids,
        },
    )
    assert response.status_code == 200, response.text
    return int(response.json()["data"]["access_epoch"])


def test_same_export_key_registers_one_job_and_file_hashes_match(engine, database_url, tmp_path):
    planted = _plant(engine, tmp_path)
    app = _app(database_url, planted)
    with TestClient(app) as client:
        csrf = _login(client, LEAD_LOGIN, LEAD_PASSWORD)
        rejected = client.post(
            f"/api/v1/projects/{PROJECT_A}/exports",
            headers=_headers(csrf, "synthetic-export-docx"),
            json={**_body(planted, include_originals=True, full=False), "format": "docx"},
        )
        assert rejected.status_code == 422, rejected.text
        assert rejected.json()["error"]["code"] == "INVALID_SCHEMA"
        assert _jobs(engine) == 0
        created = _post(
            client,
            csrf,
            planted,
            key="synthetic-export-retry",
            include_originals=True,
            full=False,
        )
        assert created.status_code == 202, created.text
        export_id = created.json()["data"]["export_job_id"]
        snapshot_id = created.json()["data"]["input_snapshot_id"]
        assert created.json()["meta"]["idempotency_replayed"] is False
        assert created.json()["data"]["latest"] is False
        replayed = _post(
            client,
            csrf,
            planted,
            key="synthetic-export-retry",
            include_originals=True,
            full=False,
        )
        assert replayed.status_code == 202, replayed.text
        assert replayed.json()["data"]["export_job_id"] == export_id
        assert replayed.json()["data"]["input_snapshot_id"] == snapshot_id
        assert replayed.json()["meta"]["idempotency_replayed"] is True
        assert _jobs(engine) == 1
        conflict = _post(
            client,
            csrf,
            planted,
            key="synthetic-export-retry",
            include_originals=False,
            full=False,
        )
        assert conflict.status_code == 409, conflict.text
        assert conflict.json()["error"]["code"] == "IDEMPOTENCY_KEY_REUSED"
        assert _jobs(engine) == 1
        finished = _run(engine, planted, export_id)
        assert finished.status == "succeeded"
        assert finished.output_ref is not None
        disk = _disk(planted, export_id, finished.output_ref)
        disk_manifest = _assert_file_hashes(disk)
        assert _sha(disk["manifest.json"]) == finished.output_ref
        downloaded = _content(client, export_id)
        download_manifest = _assert_file_hashes(downloaded)
        assert download_manifest["input_snapshot_id"] == snapshot_id
        assert disk_manifest["input_snapshot_id"] == snapshot_id
        assert _sha(downloaded[f"originals/{planted.public_version_id}"]) == _sha(PUBLIC_ORIGINAL)
        assert _run(engine, planted, export_id).output_ref == finished.output_ref
        replayed_again = _post(
            client,
            csrf,
            planted,
            key="synthetic-export-retry",
            include_originals=True,
            full=False,
        )
        assert replayed_again.status_code == 202, replayed_again.text
        assert replayed_again.json()["data"]["export_job_id"] == export_id
        assert _jobs(engine) == 1
        payload_before = _job_row(engine, export_id).payload
        later_id = str(uuid.uuid4())
        later = {"schema_version": 1, "nodes": [{"node_id": LATER_NODE}], "edges": []}
        with write_uow(engine) as session:
            session.add(
                PageVersion(
                    id=later_id,
                    project_id=PROJECT_A,
                    page_id=PAGE_A,
                    version_no=2,
                    parent_version_id=planted.page_version_id,
                    content_json=canonical_document(later),
                    content_hash=document_hash(later),
                    schema_version=1,
                    project_revision=1,
                    created_by_account_id=LEAD_ID,
                    created_at=NOW + 5,
                )
            )
            session.flush()
            page = session.get(Page, PAGE_A)
            assert page is not None
            page.head_version_id = later_id
        assert _job_row(engine, export_id).payload == payload_before
        current = client.get(f"/api/v1/exports/{export_id}")
        assert current.status_code == 200, current.text
        viewed = current.json()["data"]
        assert viewed["latest"] is False
        assert viewed["input_snapshot_id"] == snapshot_id
        assert viewed["access_epoch_at_input"] == 1
        assert viewed["source_manifest"]["page_version_ids"] == [planted.page_version_id]
        assert later_id not in current.text
        assert LATER_NODE not in current.text
        refreshed = _content(client, export_id)
        refreshed_text = _joined(refreshed).decode("utf-8", "ignore")
        assert later_id not in refreshed_text
        assert LATER_NODE not in refreshed_text
        assert planted.page_version_id in refreshed_text
        assert _manifest(refreshed)["latest"] is False
        assert _manifest(refreshed)["input_snapshot_id"] == snapshot_id
        stored_report = _disk(planted, export_id, finished.output_ref)["report.md"]
        assert LATER_NODE not in stored_report.decode("utf-8")


@dataclass(frozen=True)
class BackupWorld:
    source: Path
    objects: Path
    backup_dir: Path
    relative: str
    object_key: str
    body: bytes
    content_hash: str
    version_id: str
    decision_id: str
    history_id: str
    parent_id: str
    edge_id: str
    job_id: str


def _database_path(engine) -> Path:
    database = engine.url.database
    if not isinstance(database, str) or database == "":
        raise RuntimeError("backup test requires a file database")
    return Path(database)


def _rows(path: Path, sql: str) -> list[tuple[object, ...]]:
    connection = sqlite3.connect(path)
    try:
        return list(connection.execute(sql).fetchall())
    finally:
        connection.close()


def _fingerprint(path: Path) -> str:
    queries = (
        "SELECT id, version_no, parent_version_id, content_hash FROM page_versions ORDER BY id",
        "SELECT id, source_version_id, target_version_id, kind FROM page_edges ORDER BY id",
        "SELECT id, body, page_version_id, project_revision FROM decisions ORDER BY id",
        "SELECT id, content_hash, size_bytes, object_key FROM artifact_versions ORDER BY id",
        "SELECT id, status, revoked_at, action, account_id FROM object_grants ORDER BY id",
        "SELECT id, object_type, object_id FROM privacy_tombstones ORDER BY id",
        "SELECT id, status, lease_owner, job_kind FROM jobs ORDER BY id",
        "SELECT id, revoked_at IS NULL FROM sessions ORDER BY id",
    )
    joined = "\n".join(repr(_rows(path, query)) for query in queries)
    return hashlib.sha256(joined.encode()).hexdigest()


def _backup_world(engine, tmp_path: Path) -> BackupWorld:
    settings = identity_settings(f"sqlite:///{(tmp_path / 'ignored.db').as_posix()}")
    seed_identity_world(engine, settings)
    objects = tmp_path / "objects"
    objects.mkdir()
    body = b"SYNTHETIC-BACKUP-ORIGINAL"
    generation = "backupgen1"
    digest = _sha(body)
    relative = object_relative(PROJECT_A, digest, generation)
    stored = objects.joinpath(*relative.split("/"))
    stored.parent.mkdir(parents=True, exist_ok=True)
    stored.write_bytes(body)
    key = original_key(PROJECT_A, digest, generation)
    version_id = str(uuid.uuid4())
    decision_id = str(uuid.uuid4())
    history_id = str(uuid.uuid4())
    job_id = str(uuid.uuid4())
    excerpt = "synthetic-backup-excerpt"
    history = {"schema_version": 1, "nodes": [{"node_id": "synthetic-backup-history"}], "edges": []}
    with engine.connect() as connection:
        parent_id = connection.execute(
            text("SELECT head_version_id FROM pages WHERE id = :page_id"),
            {"page_id": PAGE_A},
        ).scalar_one()
    with write_uow(engine) as session:
        branched = branch_from_version(
            session,
            project_id=PROJECT_A,
            source_version_id=str(parent_id),
            actor_id=LEAD_ID,
            title="synthetic-backup-branch",
            now=NOW + 1,
        )
        session.add(
            PageVersion(
                id=history_id,
                project_id=PROJECT_A,
                page_id=PAGE_A,
                version_no=2,
                parent_version_id=str(parent_id),
                content_json=canonical_document(history),
                content_hash=document_hash(history),
                schema_version=1,
                project_revision=branched.project_revision,
                created_by_account_id=LEAD_ID,
                created_at=NOW + 2,
            )
        )
        session.flush()
        page = session.get(Page, PAGE_A)
        assert page is not None
        page.head_version_id = history_id
        session.add(
            Decision(
                id=decision_id,
                project_id=PROJECT_A,
                account_id=LEAD_ID,
                body="synthetic-backup-decision",
                reason="synthetic-backup-reason",
                page_version_id=history_id,
                artifact_version_id=None,
                stage_id=None,
                supersedes_id=None,
                project_revision=branched.project_revision,
                created_at=NOW + 2,
            )
        )
        session.add(
            Artifact(
                id="synthetic-backup-artifact",
                project_id=PROJECT_A,
                title="synthetic-backup-artifact",
                created_at=NOW + 2,
            )
        )
        session.flush()
        session.add(
            ArtifactVersion(
                id=version_id,
                project_id=PROJECT_A,
                artifact_id="synthetic-backup-artifact",
                version_no=1,
                content_hash=digest,
                size_bytes=len(body),
                media_type="application/octet-stream",
                object_key=key,
                license="synthetic",
                sensitivity="released",
                ai_processing_allowed=0,
                project_revision=branched.project_revision,
                created_by_account_id=LEAD_ID,
                created_at=NOW + 2,
            )
        )
        session.add(
            EvidenceAnchor(
                id=str(uuid.uuid4()),
                project_id=PROJECT_A,
                artifact_version_id=version_id,
                locator_type="text_range",
                locator_json=json.dumps({"excerpt": excerpt}, separators=(",", ":")),
                excerpt_hash=_sha(excerpt.encode("utf-8")),
                extractor_version="synthetic-1",
                visibility="project",
                sensitivity="project",
                supersedes_id=None,
                schema_version=1,
                created_by_account_id=LEAD_ID,
                created_at=NOW + 2,
            )
        )
        session.add(
            Job(
                id=job_id,
                project_id=PROJECT_A,
                job_kind="index",
                status="running",
                available_at=NOW,
                attempt_no=1,
                lease_owner="synthetic-old-owner",
                lease_until=NOW + 100,
                fencing_token=1,
                input_hash=_sha(b"synthetic-backup-job"),
                output_ref=None,
                last_error=None,
                subject_type="page",
                subject_id=PAGE_A,
                source_event_id=None,
                payload="{}",
                created_at=NOW,
            )
        )
    preview = tmp_path / "derived" / "previews" / f"{version_id}.png"
    preview.parent.mkdir(parents=True)
    preview.write_bytes(b"\x89PNG\r\n\x1a\nSYNTHETIC-BACKUP-PREVIEW")
    backup_dir = tmp_path / "backup"
    backup_snapshot(
        source_database=_database_path(engine),
        objects_root=objects,
        backup_dir=backup_dir,
    )
    return BackupWorld(
        source=_database_path(engine),
        objects=objects,
        backup_dir=backup_dir,
        relative=relative,
        object_key=key,
        body=body,
        content_hash=digest,
        version_id=version_id,
        decision_id=decision_id,
        history_id=history_id,
        parent_id=str(parent_id),
        edge_id=branched.edge_id,
        job_id=job_id,
    )


_HISTORY_SQL = """
SELECT id, page_id, version_no, parent_version_id, content_hash
FROM page_versions ORDER BY id
"""
_BRANCH_SQL = """
SELECT id, source_version_id, target_version_id, kind
FROM page_edges ORDER BY id
"""
_DECISION_SQL = """
SELECT id, body, reason, page_version_id, project_revision
FROM decisions ORDER BY id
"""
_HASH_SQL = """
SELECT id, content_hash, size_bytes, object_key
FROM artifact_versions ORDER BY id
"""
_GRANT_SQL = """
SELECT id, account_id, action, scope_type, status, revoked_at
FROM object_grants ORDER BY id
"""


def test_isolated_restore_matches_history_branch_decision_hash_and_permissions(engine, tmp_path):
    world = _backup_world(engine, tmp_path)
    manifest = json.loads((world.backup_dir / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["backup_method"] == "sqlite_online_backup"
    assert manifest["source_journal_mode"] == "wal"
    assert manifest["schema_revision"] == "0010_evidence_index"
    lone = tmp_path / "lone-snapshot"
    lone.mkdir()
    shutil.copyfile(world.backup_dir / "database.sqlite", lone / "database.sqlite")
    connection = sqlite3.connect(lone / "database.sqlite")
    try:
        assert connection.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        found = connection.execute(
            "SELECT id FROM decisions WHERE id = ?",
            (world.decision_id,),
        ).fetchone()
    finally:
        connection.close()
    assert found == (world.decision_id,)
    assert not (world.backup_dir / "database.sqlite-wal").exists()
    before = _fingerprint(world.source)
    dest = tmp_path / "restored"
    restored = restore_snapshot(
        backup_dir=world.backup_dir,
        destination=dest,
        live_database=world.source,
    )
    assert restored.succeeded, restored.report
    assert restored.opened is True
    assert restored.failure is None
    assert _fingerprint(world.source) == before
    database = dest / "sinuo.db"
    assert database.resolve() != world.source.resolve()
    assert _rows(world.source, _HISTORY_SQL) == _rows(database, _HISTORY_SQL)
    assert _rows(world.source, _BRANCH_SQL) == _rows(database, _BRANCH_SQL)
    assert _rows(world.source, _DECISION_SQL) == _rows(database, _DECISION_SQL)
    assert _rows(world.source, _HASH_SQL) == _rows(database, _HASH_SQL)
    assert _rows(world.source, _GRANT_SQL) == _rows(database, _GRANT_SQL)
    history = [row for row in _rows(database, _HISTORY_SQL) if row[0] == world.history_id]
    assert history == [(world.history_id, PAGE_A, 2, world.parent_id, history[0][4])]
    branches = _rows(database, _BRANCH_SQL)
    assert any(row[0] == world.edge_id and row[3] == "branches_from" for row in branches)
    decisions = _rows(database, _DECISION_SQL)
    matched = [row for row in decisions if row[0] == world.decision_id]
    assert matched and matched[0][1] == "synthetic-backup-decision"
    original = dest / "objects" / world.relative
    assert original.read_bytes() == world.body
    assert _sha(original.read_bytes()) == world.content_hash
    report = restored.report
    assert report["checks"] == {
        "page_history": "match",
        "branches": "match",
        "decisions": "match",
        "artifact_hashes": "match",
        "permissions": "match",
    }
    assert "originals" in report["restored"]
    assert report["rebuilt"] == ["evidence_index"]
    assert "evidence_index" not in report["restored"]
    assert "previews" in report["not_restored"]
    assert report["derivatives"]["previews"]["restored"] is False
    assert report["derivatives"]["previews"]["state"] == "pending_rebuild"
    assert report["derivatives"]["evidence_index"]["state"] == "rebuilt"
    assert report["derivatives"]["evidence_index"]["restored"] is False
    assert report["derivatives"]["evidence_index"]["rows_inserted"] >= 1
    assert "尚未重建" in report["summary"]
    assert "已恢复业务与原件" in report["summary"]
    assert b"SYNTHETIC-BACKUP-PREVIEW" not in (dest / "recovery_report.json").read_bytes()
    assert not any(path.name.endswith(".png") for path in dest.rglob("*"))
    assert _one(world.source, world.job_id) == ("running", "synthetic-old-owner", "index")
    assert _one(database, world.job_id) == ("queued", None, "index")
    restored_sessions = _rows(database, "SELECT revoked_at IS NOT NULL FROM sessions")
    source_sessions = _rows(world.source, "SELECT revoked_at IS NOT NULL FROM sessions")
    assert all(row[0] == 1 for row in restored_sessions)
    assert any(row[0] == 0 for row in source_sessions)
    assert restored.elapsed_seconds >= 0
    assert manifest["elapsed_seconds"] >= 0


def _one(path: Path, job_id: str) -> tuple[object, ...]:
    connection = sqlite3.connect(path)
    try:
        return connection.execute(
            "SELECT status, lease_owner, job_kind FROM jobs WHERE id = ?",
            (job_id,),
        ).fetchone()
    finally:
        connection.close()


def test_corrupt_backup_fails(engine, tmp_path):
    world = _backup_world(engine, tmp_path)
    before = _fingerprint(world.source)
    database = world.backup_dir / "database.sqlite"
    blob = database.read_bytes()
    database.write_bytes(blob[:-1] + bytes([blob[-1] ^ 0xFF]))
    dest = tmp_path / "restored-corrupt"
    restored = restore_snapshot(
        backup_dir=world.backup_dir,
        destination=dest,
        live_database=world.source,
    )
    assert restored.succeeded is False
    assert restored.opened is False
    assert restored.failure == FAILURE_CORRUPT
    assert restored.report["detail"] == DETAIL_CORRUPT
    assert restored.report["fully_automatic"] is False
    assert restored.failure != FAILURE_MISSING_ORIGINAL
    assert restored.failure != FAILURE_INCOMPATIBLE_VERSION
    assert not dest.exists()
    assert _fingerprint(world.source) == before


def test_missing_original_fails(engine, tmp_path):
    world = _backup_world(engine, tmp_path)
    before = _fingerprint(world.source)
    packed = world.backup_dir / "originals" / world.relative
    assert packed.is_file()
    packed.unlink()
    dest = tmp_path / "restored-missing"
    restored = restore_snapshot(
        backup_dir=world.backup_dir,
        destination=dest,
        live_database=world.source,
    )
    assert restored.succeeded is False
    assert restored.opened is False
    assert restored.failure == FAILURE_MISSING_ORIGINAL
    assert restored.report["detail"] == DETAIL_MISSING_ORIGINAL
    assert restored.report["fully_automatic"] is False
    assert restored.failure != FAILURE_CORRUPT
    assert restored.failure != FAILURE_INCOMPATIBLE_VERSION
    assert not dest.exists()
    assert _fingerprint(world.source) == before


def test_incompatible_version_fails(engine, tmp_path):
    world = _backup_world(engine, tmp_path)
    before = _fingerprint(world.source)
    manifest_path = world.backup_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["schema_revision"] = "0009_method_cards"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    dest = tmp_path / "restored-version"
    restored = restore_snapshot(
        backup_dir=world.backup_dir,
        destination=dest,
        live_database=world.source,
    )
    assert restored.succeeded is False
    assert restored.opened is False
    assert restored.failure == FAILURE_INCOMPATIBLE_VERSION
    assert restored.report["detail"] == DETAIL_INCOMPATIBLE_VERSION
    assert restored.report["fully_automatic"] is False
    assert restored.failure != FAILURE_CORRUPT
    assert restored.failure != FAILURE_MISSING_ORIGINAL
    assert not dest.exists()
    assert _fingerprint(world.source) == before


def test_old_backup_does_not_revive_tombstone_or_revoked_grant(engine, tmp_path):
    world = _backup_world(engine, tmp_path)
    with engine.connect() as connection:
        grant_id = connection.execute(
            text(
                """
                SELECT id FROM object_grants
                WHERE project_id = :project_id AND account_id = :account_id
                  AND status = 'active'
                ORDER BY id LIMIT 1
                """
            ),
            {"project_id": PROJECT_A, "account_id": TEAM_ID},
        ).scalar_one()
    audit_id = str(uuid.uuid4())
    tombstone_id = str(uuid.uuid4())
    with write_uow(engine) as session:
        session.add(
            AuditEvent(
                id=audit_id,
                project_id=PROJECT_A,
                actor_id=LEAD_ID,
                command="privacy.delete",
                subject_type="artifact_version",
                subject_id=world.version_id,
                project_revision=None,
                payload=canonical_audit_payload(
                    {"object_type": "artifact_version", "object_id": world.version_id}
                ),
                created_at=NOW + 30,
            )
        )
        session.add(
            PrivacyTombstone(
                id=tombstone_id,
                project_id=PROJECT_A,
                object_type="artifact_version",
                object_id=world.version_id,
                reason_category="subject_request",
                access_epoch=1,
                audit_event_id=audit_id,
                created_at=NOW + 30,
            )
        )
        grant = session.get(ObjectGrant, str(grant_id))
        assert grant is not None
        grant.status = "revoked"
        grant.revoked_at = NOW + 30
    before = _fingerprint(world.source)
    dest = tmp_path / "restored-tombstone"
    restored = restore_snapshot(
        backup_dir=world.backup_dir,
        destination=dest,
        live_database=world.source,
    )
    assert restored.succeeded, restored.report
    assert restored.opened is True
    assert restored.report["tombstones_applied"] == 1
    assert restored.report["revocations_applied"] >= 1
    assert world.object_key in restored.report["originals_withheld"]
    assert world.object_key not in restored.report["originals_copied"]
    assert "originals" not in restored.report["restored"]
    assert "已删除的原件没有放回隔离目录" in restored.report["summary"]
    assert _fingerprint(world.source) == before
    database = dest / "sinuo.db"
    assert _rows(world.source, _HISTORY_SQL) == _rows(database, _HISTORY_SQL)
    assert _rows(world.source, _BRANCH_SQL) == _rows(database, _BRANCH_SQL)
    assert _rows(world.source, _DECISION_SQL) == _rows(database, _DECISION_SQL)
    assert _rows(world.source, _HASH_SQL) == _rows(database, _HASH_SQL)
    restored_grant = _rows(
        database,
        "SELECT status, revoked_at FROM object_grants WHERE id = '%s'" % str(grant_id),
    )
    assert restored_grant[0][0] == "revoked"
    assert restored_grant[0][1] is not None
    tombstones = _rows(
        database,
        "SELECT object_id FROM privacy_tombstones WHERE object_type = 'artifact_version'",
    )
    assert (world.version_id,) in tombstones
    assert not (dest / "objects" / world.relative).exists()
    assert world.body not in (dest / "recovery_report.json").read_bytes()
    opened = build_engine(f"sqlite:///{database.as_posix()}")
    try:
        with Session(opened) as session:
            with pytest.raises(SourceRedacted):
                download_original(
                    session,
                    Principal(LEAD_ID, "synthetic-backup-session", 1),
                    SessionAuthorizer(session),
                    project_id=PROJECT_A,
                    artifact_version_id=world.version_id,
                    objects_root=dest / "objects",
                )
    finally:
        opened.dispose()
