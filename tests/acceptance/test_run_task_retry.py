"""Run-task retry_wait is one conditional update, then one release back to queued."""

from dataclasses import dataclass
from threading import Barrier, Thread

import pytest
from sqlalchemy import text, update
from sqlalchemy.orm import Session

from sinuo.domain.errors import StaleLease
from sinuo.domain.schema_types import JOB_STATUSES, TASK_STATUSES
from sinuo.infrastructure.database import write_uow
from sinuo.infrastructure.domain_models import Account, AgentRun, ContextSnapshot, Project, RunTask
from sinuo.workers.run_tasks import finish_task_attempt, release_retry_wait

NOW = 1_700_000_000.0
LEASE_UNTIL = 1_900_000_000.0
FUTURE_DEADLINE = 3_900_000_000.0
LATER_AVAILABLE = 3_800_000_000.0
PROJECT_ID = "synthetic-task-project"
ACCOUNT_ID = "11111111-1111-4111-8111-111111111111"
SNAPSHOT_ID = "synthetic-task-snapshot"
RUN_ID = "synthetic-task-run"
TASK_ID = "synthetic-task-1"
INPUT_HASH = "ab" * 32
PASSWORD = "$argon2id$synthetic-hash"


@dataclass(frozen=True)
class SeenTask:
    status: str
    available_at: float
    attempt_no: int
    lease_owner: str | None
    lease_until: float | None
    fencing_token: int
    input_hash: str
    output_ref: str | None
    last_error: str | None
    role: str
    run_id: str
    project_id: str
    mandatory: int


def seed_running_task(engine, *, attempt_no: int = 1) -> None:
    with write_uow(engine) as session:
        session.add(
            Project(
                id=PROJECT_ID,
                title="synthetic tasks",
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
                login_name="synthetic-task-owner",
                display_name="Synthetic",
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
            ContextSnapshot(
                id=SNAPSHOT_ID,
                project_id=PROJECT_ID,
                intent="synthetic task retry",
                template_version="synthetic-1",
                method_card_version="synthetic-1",
                authorization_scope_json="{}",
                access_epoch=1,
                exclusions_json="{}",
                index_revision=None,
                budget_json="{}",
                draft_page_id=None,
                draft_content_revision=None,
                draft_content_hash=None,
                draft_content_json=None,
                created_at=NOW,
            )
        )
        session.flush()
        session.add(
            AgentRun(
                id=RUN_ID,
                project_id=PROJECT_ID,
                initiated_by_account_id=ACCOUNT_ID,
                parent_run_id=None,
                supersedes_run_id=None,
                context_snapshot_id=SNAPSHOT_ID,
                execution_status="running",
                accepting_results=1,
                next_event_seq=0,
                budget_json="{}",
                created_at=NOW,
                closed_at=None,
            )
        )
        session.flush()
        session.add(
            RunTask(
                id=TASK_ID,
                run_id=RUN_ID,
                project_id=PROJECT_ID,
                role="evidence",
                status="running",
                mandatory=1,
                input_hash=INPUT_HASH,
                available_at=0.0,
                attempt_no=attempt_no,
                lease_owner="worker-1",
                lease_until=LEASE_UNTIL,
                fencing_token=1,
                output_ref=None,
                last_error=None,
            )
        )


def fetch_task(engine, task_id: str = TASK_ID) -> SeenTask:
    with Session(engine) as session:
        row = session.get(RunTask, task_id)
        assert row is not None
        return SeenTask(
            row.status,
            row.available_at,
            row.attempt_no,
            row.lease_owner,
            row.lease_until,
            row.fencing_token,
            row.input_hash,
            row.output_ref,
            row.last_error,
            row.role,
            row.run_id,
            row.project_id,
            row.mandatory,
        )


def raw_sqlite(session: Session):
    wrapped = session.connection().connection
    candidate = getattr(wrapped, "dbapi_connection", None)
    if candidate is None:
        candidate = wrapped
    if not hasattr(candidate, "set_trace_callback"):
        raise RuntimeError("sqlite connection is unavailable")
    return candidate


def assert_session_writes_nothing(session: Session, callback):
    baseline = session.scalar(text("SELECT total_changes()"))
    seen: list[str] = []
    raw = raw_sqlite(session)
    raw.set_trace_callback(seen.append)
    try:
        return callback()
    finally:
        raw.set_trace_callback(None)
        after = session.scalar(text("SELECT total_changes()"))
        assert after == baseline
        assert not any(statement and "UPDATE" in statement.upper() for statement in seen)


def finish(engine, **kwargs):
    with write_uow(engine) as session:
        return finish_task_attempt(session, task_id=TASK_ID, **kwargs)


def assert_failed_finish_is_quiet(engine):
    with write_uow(engine) as session:
        return assert_session_writes_nothing(
            session,
            lambda: finish_task_attempt(
                session,
                task_id=TASK_ID,
                category="rate_limited",
                max_attempts=8,
                deadline=FUTURE_DEADLINE,
                available_at=LATER_AVAILABLE,
            ),
        )


def make_due(engine) -> None:
    """Move available_at into the past. The test does not sleep."""
    with write_uow(engine) as session:
        changed = session.execute(
            update(RunTask)
            .where(RunTask.id == TASK_ID, RunTask.status == "retry_wait")
            .values(available_at=1.0)
            .execution_options(synchronize_session=False)
        )
        assert changed.rowcount == 1


def test_jobs_have_no_retry_wait_status():
    assert "retry_wait" not in JOB_STATUSES
    assert "retry_wait" in TASK_STATUSES


@pytest.mark.integration
@pytest.mark.parametrize("category", ("rate_limited", "unavailable"))
def test_retryable_running_task_waits_and_clears_the_lease(engine, category: str):
    seed_running_task(engine)
    before = fetch_task(engine)
    finished = finish(
        engine,
        category=category,
        max_attempts=8,
        deadline=FUTURE_DEADLINE,
        available_at=LATER_AVAILABLE,
    )
    task = fetch_task(engine)
    assert finished.already_finished is False
    assert finished.status == "retry_wait"
    assert finished.last_error == category
    assert finished.available_at == LATER_AVAILABLE
    assert finished.fencing_token == before.fencing_token == 1
    assert finished.attempt_no == before.attempt_no == 1
    assert finished.input_hash == INPUT_HASH
    assert task.status == "retry_wait"
    assert task.last_error == category
    assert task.available_at == LATER_AVAILABLE
    assert task.lease_owner is None
    assert task.lease_until is None
    assert task.fencing_token == 1
    assert task.attempt_no == 1
    assert task.input_hash == INPUT_HASH
    assert task.output_ref is None
    assert task.role == before.role
    assert task.run_id == before.run_id
    assert task.project_id == before.project_id
    assert task.mandatory == before.mandatory
    with write_uow(engine) as session:
        with pytest.raises(StaleLease):
            assert_session_writes_nothing(
                session,
                lambda: finish_task_attempt(
                    session,
                    task_id=TASK_ID,
                    category=category,
                    max_attempts=8,
                    deadline=FUTURE_DEADLINE,
                    available_at=LATER_AVAILABLE + 10.0,
                ),
            )
    assert fetch_task(engine) == task


@pytest.mark.integration
@pytest.mark.parametrize(
    "category",
    ("budget_exhausted", "unauthorized", "payment_required", "invalid_schema"),
)
def test_non_retryable_task_fails_and_a_second_call_does_not_update(engine, category: str):
    seed_running_task(engine)
    before = fetch_task(engine)
    finished = finish(
        engine,
        category=category,
        max_attempts=1,
        deadline=1.0,
        available_at=1.0,
    )
    task = fetch_task(engine)
    assert finished.already_finished is False
    assert finished.status == "failed"
    assert finished.last_error == category
    assert finished.available_at == 0.0
    assert finished.fencing_token == before.fencing_token
    assert finished.attempt_no == before.attempt_no
    assert task.status == "failed"
    assert task.last_error == category
    assert task.lease_owner is None
    assert task.lease_until is None
    assert task.available_at == 0.0
    assert task.fencing_token == 1
    assert task.attempt_no == 1
    assert task.input_hash == INPUT_HASH
    assert task.output_ref is None
    again = assert_failed_finish_is_quiet(engine)
    assert again.already_finished is True
    assert again.status == "failed"
    assert again.last_error == category
    assert again.available_at == 0.0
    assert fetch_task(engine) == task


@pytest.mark.integration
def test_attempt_cap_fails_before_the_deadline_and_a_second_call_does_not_update(engine):
    seed_running_task(engine)
    with write_uow(engine) as session:
        before_changes = session.scalar(text("SELECT total_changes()"))
        first = finish_task_attempt(
            session,
            task_id=TASK_ID,
            category="rate_limited",
            max_attempts=1,
            deadline=1.0,
            available_at=LATER_AVAILABLE,
        )
        after_changes = session.scalar(text("SELECT total_changes()"))
    assert after_changes == before_changes + 1
    assert first.already_finished is False
    assert first.status == "failed"
    assert first.last_error == "attempts_exhausted"
    assert first.available_at == 0.0
    assert first.fencing_token == 1
    assert first.attempt_no == 1
    task = fetch_task(engine)
    assert task.status == "failed"
    assert task.last_error == "attempts_exhausted"
    assert task.lease_owner is None
    assert task.lease_until is None
    assert task.available_at == 0.0
    assert task.status != "retry_wait"
    again = assert_failed_finish_is_quiet(engine)
    assert again.already_finished is True
    assert again.last_error == "attempts_exhausted"
    assert fetch_task(engine) == task


@pytest.mark.integration
def test_passed_deadline_fails_and_a_second_call_does_not_update(engine):
    seed_running_task(engine)
    finished = finish(
        engine,
        category="unavailable",
        max_attempts=8,
        deadline=1.0,
        available_at=LATER_AVAILABLE,
    )
    task = fetch_task(engine)
    assert finished.already_finished is False
    assert finished.status == "failed"
    assert finished.last_error == "deadline"
    assert finished.available_at == 0.0
    assert finished.attempt_no == 1
    assert task.status == "failed"
    assert task.last_error == "deadline"
    assert task.lease_owner is None
    assert task.lease_until is None
    assert task.available_at == 0.0
    assert task.fencing_token == 1
    again = assert_failed_finish_is_quiet(engine)
    assert again.already_finished is True
    assert again.last_error == "deadline"
    assert fetch_task(engine) == task


@pytest.mark.integration
def test_retryable_available_at_must_be_later_and_writes_nothing(engine):
    seed_running_task(engine)
    before = fetch_task(engine)
    assert before.status == "running"
    assert before.lease_owner == "worker-1"
    assert before.lease_until == LEASE_UNTIL
    with write_uow(engine) as session:
        with pytest.raises(ValueError, match="available_at"):
            assert_session_writes_nothing(
                session,
                lambda: finish_task_attempt(
                    session,
                    task_id=TASK_ID,
                    category="unavailable",
                    max_attempts=8,
                    deadline=FUTURE_DEADLINE,
                    available_at=1.0,
                ),
            )
    assert fetch_task(engine) == before


@pytest.mark.integration
def test_release_before_available_at_changes_nothing(engine):
    seed_running_task(engine)
    finished = finish(
        engine,
        category="rate_limited",
        max_attempts=8,
        deadline=FUTURE_DEADLINE,
        available_at=LATER_AVAILABLE,
    )
    waiting = fetch_task(engine)
    assert finished.status == "retry_wait"
    assert waiting.status == "retry_wait"
    assert waiting.available_at == LATER_AVAILABLE
    assert waiting.lease_owner is None
    assert waiting.lease_until is None
    with write_uow(engine) as session:
        released = assert_session_writes_nothing(
            session,
            lambda: release_retry_wait(session, task_id=TASK_ID),
        )
    assert released.released is False
    assert released.status == "retry_wait"
    assert released.available_at == LATER_AVAILABLE
    assert released.fencing_token == 1
    assert released.attempt_no == 1
    assert fetch_task(engine) == waiting


@pytest.mark.integration
@pytest.mark.concurrency
def test_two_threads_release_one_due_task(engine):
    seed_running_task(engine)
    finish(
        engine,
        category="unavailable",
        max_attempts=8,
        deadline=FUTURE_DEADLINE,
        available_at=LATER_AVAILABLE,
    )
    make_due(engine)
    due = fetch_task(engine)
    assert due.status == "retry_wait"
    assert due.available_at == 1.0
    assert due.lease_owner is None
    assert due.lease_until is None
    start = Barrier(2)
    released: list[bool] = []
    errors: list[BaseException] = []

    def attempt() -> None:
        start.wait(timeout=5)
        try:
            with write_uow(engine) as session:
                released.append(release_retry_wait(session, task_id=TASK_ID).released)
        except BaseException as error:
            errors.append(error)

    first = Thread(target=attempt)
    second = Thread(target=attempt)
    first.start()
    second.start()
    first.join(timeout=10)
    second.join(timeout=10)
    assert not first.is_alive()
    assert not second.is_alive()
    assert errors == []
    assert sorted(released) == [False, True]
    task = fetch_task(engine)
    assert task.status == "queued"
    assert task.available_at == 1.0
    assert task.lease_owner is None
    assert task.lease_until is None
    assert task.fencing_token == due.fencing_token == 1
    assert task.attempt_no == due.attempt_no == 1
    assert task.input_hash == INPUT_HASH
    assert task.last_error == "unavailable"
    assert task.output_ref is None
    assert task.role == due.role
    with write_uow(engine) as session:
        again = assert_session_writes_nothing(
            session,
            lambda: release_retry_wait(session, task_id=TASK_ID),
        )
    assert again.released is False
    assert again.status == "queued"
    assert fetch_task(engine) == task
