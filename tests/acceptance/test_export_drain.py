"""A queued export becomes downloadable only after the drain runs.

Registration stays on the existing HTTP command. Generation stays in
``run_export_job``. Download stays on ``GET /exports/{id}/content``.
"""

import hashlib
import io
import json
import uuid
import zipfile
from dataclasses import dataclass
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import text

from sinuo.api.app import create_app
from sinuo.application.exports.drain import drain_export_jobs
from sinuo.domain.public_contract import CSRF_HEADER
from sinuo.infrastructure.assets.objects import object_relative, original_key
from sinuo.infrastructure.database import write_uow
from sinuo.infrastructure.domain_models import Artifact, ArtifactVersion, EvidenceAnchor, Job
from tests.acceptance.identity_fixtures import (
    LEAD_ID,
    LEAD_LOGIN,
    LEAD_PASSWORD,
    NOW,
    PAGE_A,
    PAGE_A_TITLE,
    PROJECT_A,
    identity_settings,
    seed_identity_world,
)

ORIGIN = "http://127.0.0.1:8000"
GENERATION = "drainexport1"
ORIGINAL = b"SYNTHETIC-DRAIN-ORIGINAL"
THUMB = b"\x89PNG\r\n\x1a\nSYNTHETIC-DRAIN-THUMB"
EXCERPT = "synthetic-drain-excerpt-canary"
TITLE = "synthetic-drain-artifact-title"
LATER_AT = 4_000_000_000.0
_HASH = "a" * 64


@dataclass(frozen=True)
class Planted:
    page_version_id: str
    version_id: str
    excerpt_id: str
    later_id: str
    other_id: str
    objects: Path
    derived: Path
    exports: Path


def _login(client: TestClient) -> str:
    response = client.post(
        "/api/v1/session",
        headers={"Origin": ORIGIN},
        json={"login_name": LEAD_LOGIN, "password": LEAD_PASSWORD},
    )
    assert response.status_code == 204, response.text
    current = client.get("/api/v1/session")
    assert current.status_code == 200, current.text
    token = current.json()["data"]["csrf_token"]
    assert isinstance(token, str) and token
    return token


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


def _job_row(engine, job_id: str):
    with engine.connect() as connection:
        return connection.execute(
            text(
                """
                SELECT status, attempt_no, lease_owner, output_ref, payload
                FROM jobs WHERE id = :job_id
                """
            ),
            {"job_id": job_id},
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


def _queued_job(job_id: str, kind: str, available_at: float, subject_id: str) -> Job:
    return Job(
        id=job_id,
        project_id=PROJECT_A,
        job_kind=kind,
        status="queued",
        available_at=available_at,
        attempt_no=0,
        lease_owner=None,
        lease_until=None,
        fencing_token=0,
        input_hash=_HASH,
        output_ref=None,
        last_error=None,
        subject_type="export_snapshot",
        subject_id=subject_id,
        source_event_id=None,
        payload="{}",
        created_at=NOW,
    )


def _plant(engine, tmp_path: Path) -> Planted:
    settings = identity_settings(f"sqlite:///{(tmp_path / 'ignored.db').as_posix()}")
    seed_identity_world(engine, settings)
    objects = tmp_path / "objects"
    derived = tmp_path / "derived"
    exports = tmp_path / "exports"
    objects.mkdir()
    derived.mkdir()
    exports.mkdir()
    version_id = str(uuid.uuid4())
    excerpt_id = str(uuid.uuid4())
    later_id = "synthetic-drain-later-job"
    other_id = "synthetic-drain-other-job"
    digest, object_key = _store_original(objects, ORIGINAL)
    thumb = derived / "previews" / f"{version_id}.png"
    thumb.parent.mkdir(parents=True, exist_ok=True)
    thumb.write_bytes(THUMB)
    with write_uow(engine) as session:
        session.add(
            Artifact(
                id="synthetic-drain-artifact",
                project_id=PROJECT_A,
                title=TITLE,
                created_at=NOW,
            )
        )
        session.flush()
        session.add(
            ArtifactVersion(
                id=version_id,
                project_id=PROJECT_A,
                artifact_id="synthetic-drain-artifact",
                version_no=1,
                content_hash=digest,
                size_bytes=len(ORIGINAL),
                media_type="application/octet-stream",
                object_key=object_key,
                license="synthetic",
                sensitivity="released",
                ai_processing_allowed=0,
                project_revision=1,
                created_by_account_id=LEAD_ID,
                created_at=NOW,
            )
        )
        session.flush()
        session.add(
            EvidenceAnchor(
                id=excerpt_id,
                project_id=PROJECT_A,
                artifact_version_id=version_id,
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
        session.add(_queued_job(later_id, "export", LATER_AT, "synthetic-drain-later"))
        session.add(_queued_job(other_id, "derivative", 0.0, "synthetic-drain-other"))
        session.flush()
    return Planted(
        page_version_id=_page_version(engine),
        version_id=version_id,
        excerpt_id=excerpt_id,
        later_id=later_id,
        other_id=other_id,
        objects=objects,
        derived=derived,
        exports=exports,
    )


def _body(planted: Planted) -> dict[str, object]:
    manifest = {
        "page_version_ids": [planted.page_version_id],
        "artifact_version_ids": [planted.version_id],
        "evidence_anchor_ids": [planted.excerpt_id],
    }
    return {
        "format": "markdown",
        "include_originals": True,
        "source_manifest": manifest,
        "confirmed_scope": {
            "page_version_ids": list(manifest["page_version_ids"]),
            "artifact_version_ids": list(manifest["artifact_version_ids"]),
            "evidence_anchor_ids": list(manifest["evidence_anchor_ids"]),
        },
    }


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
    listed = {item["path"]: item for item in manifest["files"]}
    assert "manifest.json" not in listed
    for path, item in listed.items():
        assert _sha(files[path]) == item["sha256"]
        assert item["byte_size"] == len(files[path])
    assert set(files) - {"manifest.json"} == set(listed)
    return manifest


def _item(manifest: dict, kind: str, item_id: str) -> dict:
    found = [item for item in manifest["items"] if item["kind"] == kind and item["id"] == item_id]
    assert len(found) == 1
    return found[0]


def _download(client: TestClient, export_id: str) -> dict[str, bytes]:
    response = client.get(f"/api/v1/exports/{export_id}/content")
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["content-type"].startswith("application/zip")
    files = _unzip(response.content)
    _assert_file_hashes(files)
    return files


def _disk(planted: Planted, export_id: str, output_ref: str) -> dict[str, bytes]:
    folder = planted.exports / export_id / output_ref
    files: dict[str, bytes] = {}
    for path in folder.rglob("*"):
        if path.is_file():
            files[path.relative_to(folder).as_posix()] = path.read_bytes()
    return files


def _still_queued(engine, job_id: str) -> None:
    row = _job_row(engine, job_id)
    assert row.status == "queued"
    assert row.attempt_no == 0
    assert row.lease_owner is None
    assert row.output_ref is None


def _revoke(client: TestClient, engine, csrf: str) -> int:
    response = client.patch(
        f"/api/v1/projects/{PROJECT_A}/access",
        headers=_headers(csrf, "synthetic-drain-revoke"),
        json={
            "expected_access_epoch": 1,
            "subject_account_id": LEAD_ID,
            "revoke_grant_ids": [
                _grant_id(engine, "artifact.original.read"),
                _grant_id(engine, "preview.read"),
                _grant_id(engine, "evidence.read"),
                _grant_id(engine, "page.read"),
            ],
        },
    )
    assert response.status_code == 200, response.text
    return int(response.json()["data"]["access_epoch"])


def test_drain_queued_export_download_matches_manifest_and_revoke_omits(
    engine, database_url, tmp_path
):
    planted = _plant(engine, tmp_path)
    app = create_app(identity_settings(database_url))
    app.state.objects_root = planted.objects
    app.state.derived_root = planted.derived
    app.state.exports_root = planted.exports
    with TestClient(app) as client:
        csrf = _login(client)
        created = client.post(
            f"/api/v1/projects/{PROJECT_A}/exports",
            headers=_headers(csrf, "synthetic-drain-export"),
            json=_body(planted),
        )
        assert created.status_code == 202, created.text
        export_id = created.json()["data"]["export_job_id"]
        snapshot_id = created.json()["data"]["input_snapshot_id"]
        assert created.json()["data"]["status"] == "queued"
        assert created.json()["data"]["latest"] is False
        frozen = _job_row(engine, export_id).payload
        waiting = client.get(f"/api/v1/exports/{export_id}/content")
        assert waiting.status_code == 404, waiting.text
        assert waiting.json()["error"]["code"] == "NOT_FOUND_OR_HIDDEN"
        assert ORIGINAL not in waiting.content
        assert EXCERPT not in waiting.text
        runs = drain_export_jobs(
            engine,
            planted.exports,
            planted.objects,
            planted.derived,
        )
        assert len(runs) == 1
        assert runs[0].export_id == export_id
        assert runs[0].status == "succeeded"
        assert isinstance(runs[0].output_ref, str) and runs[0].output_ref
        finished = _job_row(engine, export_id)
        assert finished.status == "succeeded"
        assert finished.output_ref == runs[0].output_ref
        assert finished.payload == frozen
        _still_queued(engine, planted.later_id)
        _still_queued(engine, planted.other_id)
        packed = _download(client, export_id)
        manifest = _manifest(packed)
        blob = _joined(packed)
        report = packed["report.md"].decode("utf-8")
        assert _sha(packed["manifest.json"]) == finished.output_ref
        assert manifest["input_snapshot_id"] == snapshot_id
        assert ORIGINAL in blob
        assert b"SYNTHETIC-DRAIN-THUMB" in blob
        assert EXCERPT in report
        assert PAGE_A_TITLE in report
        assert TITLE in report
        assert "不是最新来源" in report
        assert "最新来源：否" in report
        public = _item(manifest, "artifact_version", planted.version_id)
        assert public["original"]["status"] == "included"
        assert public["original"]["sha256"] == _sha(ORIGINAL)
        assert public["thumbnail"]["status"] == "included"
        assert public["thumbnail"]["sha256"] == _sha(THUMB)
        kept = _item(manifest, "evidence_anchor", planted.excerpt_id)
        assert kept["excerpt"]["status"] == "included"
        assert _item(manifest, "page_version", planted.page_version_id)["status"] == "included"
        stored = _disk(planted, export_id, finished.output_ref)
        assert _sha(stored["manifest.json"]) == finished.output_ref
        assert _assert_file_hashes(stored)["files"] == manifest["files"]
        assert ORIGINAL in _joined(stored)
        assert not (planted.exports / planted.later_id).exists()
        assert not (planted.exports / planted.other_id).exists()
        epoch = _revoke(client, engine, csrf)
        assert epoch == 2
        withheld = _download(client, export_id)
        withheld_blob = _joined(withheld)
        withheld_report = withheld["report.md"].decode("utf-8")
        withheld_manifest = _manifest(withheld)
        assert ORIGINAL not in withheld_blob
        assert b"SYNTHETIC-DRAIN-THUMB" not in withheld_blob
        assert EXCERPT not in withheld_report
        assert PAGE_A_TITLE not in withheld_report
        assert TITLE not in withheld_report
        assert "输入授权纪元：1" in withheld_report
        assert withheld_manifest["input_snapshot_id"] == snapshot_id
        assert withheld_manifest["latest"] is False
        assert _sha(withheld["manifest.json"]) != finished.output_ref
        omitted = _item(withheld_manifest, "artifact_version", planted.version_id)
        assert omitted["original"]["status"] == "omitted"
        assert "sha256" not in omitted["original"]
        assert omitted["thumbnail"]["status"] == "omitted"
        assert "sha256" not in omitted["thumbnail"]
        excerpt = _item(withheld_manifest, "evidence_anchor", planted.excerpt_id)
        assert excerpt["excerpt"]["status"] == "omitted"
        hidden_page = _item(withheld_manifest, "page_version", planted.page_version_id)
        assert hidden_page["status"] == "omitted"
        assert f"originals/{planted.version_id}" not in withheld
        assert f"thumbnails/{planted.version_id}.png" not in withheld
        kept_on_disk = _disk(planted, export_id, finished.output_ref)
        assert ORIGINAL in _joined(kept_on_disk)
        assert _sha(kept_on_disk["manifest.json"]) == finished.output_ref
        assert _job_row(engine, export_id).payload == frozen
        assert _job_row(engine, export_id).output_ref == finished.output_ref
        again = drain_export_jobs(
            engine,
            planted.exports,
            planted.objects,
            planted.derived,
        )
        assert again == []
        assert _job_row(engine, export_id).status == "succeeded"
        _still_queued(engine, planted.later_id)
        _still_queued(engine, planted.other_id)
