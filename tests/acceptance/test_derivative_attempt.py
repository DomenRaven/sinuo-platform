"""A derivative attempt file is written outside the job transaction.

Lease expiry is a past timestamp. Nothing sleeps.
"""

import hashlib
import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest
from sqlalchemy import update
from sqlalchemy.orm import Session

from sinuo.domain.errors import StaleLease
from sinuo.infrastructure.assets.paths import PathEscape
from sinuo.infrastructure.database import write_uow
from sinuo.infrastructure.domain_models import Job, Project
from sinuo.workers.derivative_files import (
    publish_derivative_attempt,
    write_derivative_attempt,
)
from sinuo.workers.jobs import JobPublication, claim_job

PROJECT_ID = "project-derivative-attempt"
JOB_ID = "job-derivative-attempt"
INPUT_HASH = hashlib.sha256(b"derivative-attempt-input").hexdigest()
CONSUMER = "derivative"
EFFECT = "registered"
CREATED_AT = 1_700_000_000.0
TTL = 60.0
FIRST = b"attempt-before-receipt"
SECOND = b"attempt-after-takeover"


@dataclass(frozen=True)
class SeenJob:
    status: str
    fencing_token: int
    output_ref: str | None
    lease_owner: str | None


def insert_job(engine) -> None:
    with write_uow(engine) as session:
        session.add(
            Project(
                id=PROJECT_ID,
                title="Derivative attempts",
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
                id=JOB_ID,
                project_id=PROJECT_ID,
                job_kind="derivative",
                status="queued",
                available_at=0.0,
                attempt_no=0,
                lease_owner=None,
                lease_until=None,
                fencing_token=0,
                input_hash=INPUT_HASH,
                output_ref=None,
                last_error=None,
                subject_type="artifact",
                subject_id="artifact-1",
                source_event_id=None,
                payload="{}",
                created_at=CREATED_AT,
            )
        )


def fetch_job(engine) -> SeenJob:
    with Session(engine) as session:
        row = session.get(Job, JOB_ID)
        assert row is not None
        return SeenJob(row.status, row.fencing_token, row.output_ref, row.lease_owner)


def claim(engine, owner: str):
    with write_uow(engine) as session:
        return claim_job(
            session,
            job_id=JOB_ID,
            owner=owner,
            input_hash=INPUT_HASH,
            ttl_seconds=TTL,
        )


def publish(engine, *, owner: str, fencing_token: int) -> JobPublication:
    with write_uow(engine) as session:
        return publish_derivative_attempt(
            session,
            job_id=JOB_ID,
            owner=owner,
            fencing_token=fencing_token,
            input_hash=INPUT_HASH,
            consumer_name=CONSUMER,
            effect_kind=EFFECT,
        )


def expire_lease(engine) -> None:
    """Move the deadline into the past without sleeping."""

    with write_uow(engine) as session:
        changed = session.execute(
            update(Job)
            .where(Job.id == JOB_ID, Job.status == "running")
            .values(lease_until=1.0)
            .execution_options(synchronize_session=False)
        )
        assert changed.rowcount == 1


def result_file(root: Path, fencing_token: int) -> Path:
    return root / "jobs" / JOB_ID / str(fencing_token) / "result"


@pytest.mark.integration
@pytest.mark.recovery
def test_crash_before_finalize_keeps_file_and_same_token_publishes_once(engine, tmp_path):
    root = tmp_path / "derived"
    root.mkdir()
    insert_job(engine)
    attempt = claim(engine, "worker-1")
    relative = write_derivative_attempt(root, attempt.job_id, attempt.fencing_token, FIRST)
    stored = result_file(root, attempt.fencing_token)
    assert relative == f"jobs/{JOB_ID}/{attempt.fencing_token}/result"
    assert stored.read_bytes() == FIRST
    crashed = fetch_job(engine)
    assert crashed.status == "running"
    assert crashed.output_ref is None
    assert crashed.fencing_token == attempt.fencing_token
    assert crashed.lease_owner == "worker-1"
    published = publish(engine, owner="worker-1", fencing_token=attempt.fencing_token)
    job = fetch_job(engine)
    assert published.output_ref == relative
    assert published.fencing_token == attempt.fencing_token
    assert job.status == "succeeded"
    assert job.output_ref == relative
    assert job.lease_owner is None
    assert stored.read_bytes() == FIRST
    with pytest.raises(StaleLease):
        publish(engine, owner="worker-1", fencing_token=attempt.fencing_token)
    again = fetch_job(engine)
    assert again.output_ref == relative
    assert again.fencing_token == attempt.fencing_token
    assert stored.read_bytes() == FIRST


@pytest.mark.integration
@pytest.mark.recovery
def test_old_token_cannot_replace_the_new_attempt_ref(engine, tmp_path):
    root = tmp_path / "derived"
    root.mkdir()
    insert_job(engine)
    first = claim(engine, "worker-1")
    old_relative = write_derivative_attempt(root, first.job_id, first.fencing_token, FIRST)
    old_file = result_file(root, first.fencing_token)
    assert old_file.read_bytes() == FIRST
    assert fetch_job(engine).output_ref is None
    expire_lease(engine)
    second = claim(engine, "worker-2")
    new_relative = write_derivative_attempt(root, second.job_id, second.fencing_token, SECOND)
    new_file = result_file(root, second.fencing_token)
    assert second.fencing_token != first.fencing_token
    assert new_relative == f"jobs/{JOB_ID}/{second.fencing_token}/result"
    assert new_file.read_bytes() == SECOND
    published = publish(engine, owner="worker-2", fencing_token=second.fencing_token)
    assert published.output_ref == new_relative
    with pytest.raises(StaleLease):
        publish(engine, owner="worker-1", fencing_token=first.fencing_token)
    job = fetch_job(engine)
    assert job.status == "succeeded"
    assert job.output_ref == new_relative
    assert job.output_ref != old_relative
    assert job.fencing_token == second.fencing_token
    assert old_file.read_bytes() == FIRST
    assert new_file.read_bytes() == SECOND


@pytest.mark.parametrize(
    ("job_id", "fencing_token"),
    [
        ("..", 1),
        ("../outside", 1),
        ("job/../../outside", 1),
        ("..\\outside", 1),
        ("a\\..\\b", 1),
        ("job-ok", 0),
        ("job-ok", -1),
        ("job-ok", True),
    ],
)
def test_escaping_attempt_path_writes_nothing(tmp_path, job_id: str, fencing_token: object):
    root = tmp_path / "root"
    root.mkdir()
    with pytest.raises(PathEscape):
        write_derivative_attempt(root, job_id, fencing_token, b"escape-bytes")
    assert list(root.iterdir()) == []
    assert [path for path in tmp_path.rglob("*") if path.is_file()] == []


def test_symlink_attempt_directory_writes_nothing(tmp_path):
    root = tmp_path / "root"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    link = root / "jobs"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except OSError:
        created = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link), str(outside)],
            capture_output=True,
            check=False,
        )
        if created.returncode != 0 or not link.exists():
            pytest.skip("symlink not permitted")
    with pytest.raises(PathEscape):
        write_derivative_attempt(root, "job-ok", 1, b"escape-bytes")
    assert not (outside / "job-ok").exists()
    assert [path for path in outside.rglob("*") if path.is_file()] == []
