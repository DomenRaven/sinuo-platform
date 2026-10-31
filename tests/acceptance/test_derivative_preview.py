"""One derivative job renders a stored original outside the claim transaction.

Three phases: claim inside write_uow and commit; render and write the attempt
file with no database session open; publish or finish inside a later write_uow.
Lease expiry is a past timestamp. Nothing sleeps.
"""

import hashlib
import io
import shutil
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

import pytest
from PIL import Image
from sqlalchemy import update
from sqlalchemy.orm import Session

from sinuo.domain.errors import StaleLease
from sinuo.infrastructure.assets.objects import object_relative, read_original
from sinuo.infrastructure.database import write_uow
from sinuo.infrastructure.domain_models import Job, Project
from sinuo.workers.derivative_preview import render_derivative_preview
from sinuo.workers.jobs import claim_job, finalize_job, finish_job_attempt

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
CREATED_AT = 1_700_000_000.0
TTL = 60.0
DEADLINE = 3_900_000_000.0
RETRY_AT = 3_800_000_000.0
CONSUMER = "derivative"
EFFECT = "registered"
GENERATION_ID = "generation-preview-1"
MAX_PIXELS = 64


@dataclass(frozen=True)
class SeenJob:
    status: str
    fencing_token: int
    attempt_no: int
    output_ref: str | None
    lease_owner: str | None
    last_error: str | None


@contextmanager
def render_without_database_session():
    """Fail if rendering constructs a SQLAlchemy session."""

    created = 0
    real_init = Session.__init__

    def counting_init(self, *args, **kwargs):
        nonlocal created
        created += 1
        return real_init(self, *args, **kwargs)

    Session.__init__ = counting_init
    try:
        yield
        if created != 0:
            raise AssertionError("database session opened during render")
    finally:
        Session.__init__ = real_init


def png_bytes(size: tuple[int, int], color: tuple[int, int, int]) -> bytes:
    payload = io.BytesIO()
    image = Image.new("RGB", size, color)
    try:
        image.save(payload, format="PNG")
    finally:
        image.close()
    return payload.getvalue()


def files_under(root: Path) -> list[Path]:
    return sorted(path for path in root.rglob("*") if path.is_file())


def store_original(objects: Path, project_id: str, payload: bytes) -> str:
    digest = hashlib.sha256(payload).hexdigest()
    relative = object_relative(project_id, digest, GENERATION_ID)
    destination = objects / Path(relative)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(payload)
    return digest


def insert_job(engine, project_id: str, job_id: str, input_hash: str) -> None:
    with write_uow(engine) as session:
        session.add(
            Project(
                id=project_id,
                title="Derivative preview",
                brief=None,
                template_key=None,
                revision=1,
                access_epoch=1,
                created_at=CREATED_AT,
            )
        )
        session.flush()
        session.add(
            Job(
                id=job_id,
                project_id=project_id,
                job_kind="derivative",
                status="queued",
                available_at=0.0,
                attempt_no=0,
                lease_owner=None,
                lease_until=None,
                fencing_token=0,
                input_hash=input_hash,
                output_ref=None,
                last_error=None,
                subject_type="artifact",
                subject_id="artifact-preview",
                source_event_id=None,
                payload="{}",
                created_at=CREATED_AT,
            )
        )


def fetch_job(engine, job_id: str) -> SeenJob:
    with Session(engine) as session:
        row = session.get(Job, job_id)
        assert row is not None
        return SeenJob(
            row.status,
            row.fencing_token,
            row.attempt_no,
            row.output_ref,
            row.lease_owner,
            row.last_error,
        )


def claim(engine, job_id: str, owner: str, input_hash: str):
    with write_uow(engine) as session:
        assert session.in_transaction()
        return claim_job(
            session,
            job_id=job_id,
            owner=owner,
            input_hash=input_hash,
            ttl_seconds=TTL,
        )


def expire_lease(engine, job_id: str) -> None:
    with write_uow(engine) as session:
        changed = session.execute(
            update(Job)
            .where(Job.id == job_id, Job.status == "running")
            .values(lease_until=1.0)
            .execution_options(synchronize_session=False)
        )
        assert changed.rowcount == 1


def result_file(root: Path, job_id: str, fencing_token: int) -> Path:
    return root / "jobs" / job_id / str(fencing_token) / "result"


def assert_png(path: Path) -> None:
    payload = path.read_bytes()
    assert payload.startswith(PNG_MAGIC)
    with Image.open(path) as opened:
        assert opened.format == "PNG"


def render_outside(engine, **kwargs):
    assert engine.pool.checkedout() == 0
    with render_without_database_session():
        rendered = render_derivative_preview(**kwargs)
        assert engine.pool.checkedout() == 0
    assert engine.pool.checkedout() == 0
    return rendered


def roots(tmp_path: Path) -> tuple[Path, Path]:
    objects = tmp_path / "objects"
    derived = tmp_path / "derived"
    objects.mkdir()
    derived.mkdir()
    return objects, derived


@pytest.mark.integration
def test_small_png_publishes_attempt_png(engine, tmp_path):
    project_id = "project-derivative-png"
    job_id = "job-derivative-png"
    input_hash = hashlib.sha256(b"derivative-preview-png").hexdigest()
    objects, derived = roots(tmp_path)
    payload = png_bytes((2, 2), (12, 34, 56))
    digest = store_original(objects, project_id, payload)
    insert_job(engine, project_id, job_id, input_hash)
    phases: list[str] = []

    phases.append("claim")
    attempt = claim(engine, job_id, "worker-1", input_hash)
    phases.append("claim-committed")
    claimed = fetch_job(engine, job_id)
    assert claimed.status == "running"
    assert claimed.fencing_token == attempt.fencing_token
    assert claimed.attempt_no == attempt.attempt_no
    assert claimed.output_ref is None
    assert claimed.lease_owner == "worker-1"
    assert engine.pool.checkedout() == 0

    phases.append("render")
    rendered = render_outside(
        engine,
        objects_root=objects,
        project_id=project_id,
        content_hash=digest,
        generation_id=GENERATION_ID,
        media_type="image/png",
        derived_root=derived,
        job_id=job_id,
        fencing_token=attempt.fencing_token,
        max_pixels=MAX_PIXELS,
    )
    phases.append("render-closed")
    relative = f"jobs/{job_id}/{attempt.fencing_token}/result"
    stored = result_file(derived, job_id, attempt.fencing_token)
    assert rendered.generated is True
    assert rendered.finish_category is None
    assert rendered.state == "ready"
    assert rendered.reason == "rendered"
    assert rendered.relative_path == relative
    assert_png(stored)
    assert files_under(derived) == [stored]
    after_render = fetch_job(engine, job_id)
    assert after_render.status == "running"
    assert after_render.output_ref is None
    assert read_original(objects, project_id, digest, GENERATION_ID) == payload

    phases.append("publish")
    with write_uow(engine) as session:
        assert session.in_transaction()
        published = finalize_job(
            session,
            job_id=job_id,
            owner="worker-1",
            fencing_token=attempt.fencing_token,
            input_hash=input_hash,
            output_ref=rendered.relative_path,
            consumer_name=CONSUMER,
            effect_kind=EFFECT,
        )
    phases.append("publish-committed")
    assert phases == [
        "claim",
        "claim-committed",
        "render",
        "render-closed",
        "publish",
        "publish-committed",
    ]
    job = fetch_job(engine, job_id)
    assert published.output_ref == relative
    assert job.status == "succeeded"
    assert job.output_ref == relative
    assert job.output_ref == f"jobs/{job_id}/{attempt.fencing_token}/result"
    assert job.lease_owner is None
    assert_png(stored)
    with pytest.raises(StaleLease):
        with write_uow(engine) as session:
            finalize_job(
                session,
                job_id=job_id,
                owner="worker-1",
                fencing_token=attempt.fencing_token,
                input_hash=input_hash,
                output_ref=relative,
                consumer_name=CONSUMER,
                effect_kind=EFFECT,
            )
    again = fetch_job(engine, job_id)
    assert again.output_ref == relative
    assert again.fencing_token == attempt.fencing_token
    assert_png(stored)


@pytest.mark.integration
@pytest.mark.recovery
def test_old_token_cannot_replace_published_preview(engine, tmp_path):
    project_id = "project-derivative-takeover"
    job_id = "job-derivative-takeover"
    input_hash = hashlib.sha256(b"derivative-preview-takeover").hexdigest()
    objects, derived = roots(tmp_path)
    payload = png_bytes((2, 2), (9, 8, 7))
    digest = store_original(objects, project_id, payload)
    insert_job(engine, project_id, job_id, input_hash)

    first = claim(engine, job_id, "worker-1", input_hash)
    first_rendered = render_outside(
        engine,
        objects_root=objects,
        project_id=project_id,
        content_hash=digest,
        generation_id=GENERATION_ID,
        media_type="image/png",
        derived_root=derived,
        job_id=job_id,
        fencing_token=first.fencing_token,
        max_pixels=MAX_PIXELS,
    )
    old_relative = f"jobs/{job_id}/{first.fencing_token}/result"
    old_file = result_file(derived, job_id, first.fencing_token)
    assert first_rendered.relative_path == old_relative
    assert_png(old_file)
    assert fetch_job(engine, job_id).output_ref is None
    expire_lease(engine, job_id)

    phases: list[str] = []
    phases.append("claim")
    second = claim(engine, job_id, "worker-2", input_hash)
    phases.append("claim-committed")
    assert second.fencing_token != first.fencing_token
    assert fetch_job(engine, job_id).lease_owner == "worker-2"
    phases.append("render")
    second_rendered = render_outside(
        engine,
        objects_root=objects,
        project_id=project_id,
        content_hash=digest,
        generation_id=GENERATION_ID,
        media_type="image/png",
        derived_root=derived,
        job_id=job_id,
        fencing_token=second.fencing_token,
        max_pixels=MAX_PIXELS,
    )
    phases.append("render-closed")
    new_relative = f"jobs/{job_id}/{second.fencing_token}/result"
    new_file = result_file(derived, job_id, second.fencing_token)
    assert second_rendered.generated is True
    assert second_rendered.relative_path == new_relative
    assert_png(new_file)
    assert fetch_job(engine, job_id).output_ref is None

    phases.append("publish")
    with write_uow(engine) as session:
        published = finalize_job(
            session,
            job_id=job_id,
            owner="worker-2",
            fencing_token=second.fencing_token,
            input_hash=input_hash,
            output_ref=second_rendered.relative_path,
            consumer_name=CONSUMER,
            effect_kind=EFFECT,
        )
    phases.append("publish-committed")
    assert phases == [
        "claim",
        "claim-committed",
        "render",
        "render-closed",
        "publish",
        "publish-committed",
    ]
    assert published.output_ref == new_relative
    with pytest.raises(StaleLease):
        with write_uow(engine) as session:
            finalize_job(
                session,
                job_id=job_id,
                owner="worker-1",
                fencing_token=first.fencing_token,
                input_hash=input_hash,
                output_ref=old_relative,
                consumer_name=CONSUMER,
                effect_kind=EFFECT,
            )
    job = fetch_job(engine, job_id)
    assert job.status == "succeeded"
    assert job.output_ref == new_relative
    assert job.output_ref == f"jobs/{job_id}/{second.fencing_token}/result"
    assert job.fencing_token == second.fencing_token
    assert_png(old_file)
    assert_png(new_file)
    assert read_original(objects, project_id, digest, GENERATION_ID) == payload


@pytest.mark.integration
def test_glb_without_blender_does_not_publish(engine, tmp_path):
    assert shutil.which("blender") is None
    project_id = "project-derivative-glb"
    job_id = "job-derivative-glb"
    input_hash = hashlib.sha256(b"derivative-preview-glb").hexdigest()
    objects, derived = roots(tmp_path)
    payload = b"glb-bytes"
    digest = store_original(objects, project_id, payload)
    insert_job(engine, project_id, job_id, input_hash)

    attempt = claim(engine, job_id, "worker-1", input_hash)
    assert fetch_job(engine, job_id).status == "running"
    rendered = render_outside(
        engine,
        objects_root=objects,
        project_id=project_id,
        content_hash=digest,
        generation_id=GENERATION_ID,
        media_type="model/gltf-binary",
        derived_root=derived,
        job_id=job_id,
        fencing_token=attempt.fencing_token,
        max_pixels=MAX_PIXELS,
    )
    assert rendered.generated is False
    assert rendered.state == "unavailable"
    assert rendered.reason == "converter_not_installed"
    assert rendered.relative_path is None
    assert rendered.finish_category == "unavailable"
    assert files_under(derived) == []
    assert not result_file(derived, job_id, attempt.fencing_token).exists()
    assert fetch_job(engine, job_id).output_ref is None
    assert read_original(objects, project_id, digest, GENERATION_ID) == payload

    with write_uow(engine) as session:
        finished = finish_job_attempt(
            session,
            job_id=job_id,
            category=rendered.finish_category,
            max_attempts=attempt.attempt_no,
            deadline=DEADLINE,
            available_at=RETRY_AT,
        )
    job = fetch_job(engine, job_id)
    assert finished.status == "failed"
    assert finished.last_error == "attempts_exhausted"
    assert finished.already_finished is False
    assert job.status == "failed"
    assert job.output_ref is None
    assert job.last_error == "attempts_exhausted"
    assert job.lease_owner is None
    assert files_under(derived) == []
    assert read_original(objects, project_id, digest, GENERATION_ID) == payload


@pytest.mark.integration
@pytest.mark.parametrize(
    ("media_type", "payload", "max_pixels", "reason"),
    [
        ("image/png", None, 35, "pixel_limit"),
        ("image/svg+xml", b'<svg xmlns="http://www.w3.org/2000/svg" width="2" height="2"></svg>', 64, "svg_rejected"),
    ],
)
def test_non_retryable_preview_refusal_writes_nothing(
    engine,
    tmp_path,
    media_type: str,
    payload: bytes | None,
    max_pixels: int,
    reason: str,
):
    project_id = "project-derivative-refusal"
    job_id = "job-derivative-refusal"
    input_hash = hashlib.sha256(media_type.encode("ascii")).hexdigest()
    objects, derived = roots(tmp_path)
    body = png_bytes((6, 6), (1, 2, 3)) if payload is None else payload
    digest = store_original(objects, project_id, body)
    insert_job(engine, project_id, job_id, input_hash)
    attempt = claim(engine, job_id, "worker-1", input_hash)
    rendered = render_outside(
        engine,
        objects_root=objects,
        project_id=project_id,
        content_hash=digest,
        generation_id=GENERATION_ID,
        media_type=media_type,
        derived_root=derived,
        job_id=job_id,
        fencing_token=attempt.fencing_token,
        max_pixels=max_pixels,
    )
    assert rendered.generated is False
    assert rendered.reason == reason
    assert rendered.relative_path is None
    assert rendered.finish_category == "invalid_schema"
    assert files_under(derived) == []
    with write_uow(engine) as session:
        finished = finish_job_attempt(
            session,
            job_id=job_id,
            category=rendered.finish_category,
            max_attempts=8,
            deadline=DEADLINE,
            available_at=RETRY_AT,
        )
    job = fetch_job(engine, job_id)
    assert finished.status == "failed"
    assert finished.last_error == "invalid_schema"
    assert job.status == "failed"
    assert job.output_ref is None
    assert job.last_error == "invalid_schema"
    assert files_under(derived) == []
    assert read_original(objects, project_id, digest, GENERATION_ID) == body
