"""Conditional retry wait and release for ``run_tasks``.

Each call runs inside the caller's ``write_uow`` and commits nothing itself.
Jobs have no ``retry_wait`` status; this module does not add one.

``ck_run_tasks_lease`` allows a lease only while ``status`` is ``running``.
``retry_wait``, ``failed``, and ``queued`` stay valid only when ``lease_owner``
and ``lease_until`` are both null. Fencing token and attempt number stay put;
claim is what advances them.

Finish is one conditional update of a ``running`` row. A retryable category
below the attempt cap, with the deadline still after the database clock, becomes
``retry_wait`` and stores the caller-supplied later ``available_at``. A
non-retryable category, the attempt cap, or a deadline that is not still ahead
becomes ``failed``. The cap is checked before the deadline. A row that is
already ``failed`` is returned as-is and does not emit an UPDATE; the terminal
trigger would reject a later status change.

Release is one conditional update. ``retry_wait`` becomes ``queued`` only when
``available_at`` is at or before the database clock. An earlier call leaves the
row unchanged and does not emit an UPDATE. The status predicate lets only one
of two connections change the same due row.
"""

import logging
import math
from dataclasses import dataclass

from sqlalchemy import inspect, text, update
from sqlalchemy.orm import Session

from sinuo.domain.errors import StaleLease
from sinuo.infrastructure.domain_models import RunTask

logger = logging.getLogger("sinuo.workers.run_tasks")

_NOW = text("SELECT (julianday('now') - 2440587.5) * 86400.0")
_NON_RETRYABLE_TASK_ERRORS = frozenset(
    {
        "budget_exhausted",
        "unauthorized",
        "payment_required",
        "invalid_schema",
    }
)
_RETRYABLE_TASK_ERRORS = frozenset({"rate_limited", "unavailable"})


@dataclass(frozen=True)
class TaskAttemptFinish:
    task_id: str
    status: str
    fencing_token: int
    attempt_no: int
    input_hash: str
    last_error: str | None
    available_at: float
    already_finished: bool


@dataclass(frozen=True)
class TaskRetryRelease:
    task_id: str
    status: str
    available_at: float
    fencing_token: int
    attempt_no: int
    last_error: str | None
    released: bool


def finish_task_attempt(
    session: Session,
    *,
    task_id: str,
    category: str,
    max_attempts: int,
    deadline: float,
    available_at: float,
) -> TaskAttemptFinish:
    """End one running task attempt. The caller owns write_uow.

    One conditional update matches ``status`` ``running``. Retryable failures
    below ``max_attempts`` and before ``deadline`` become ``retry_wait``, the
    lease columns clear, and ``available_at`` becomes the caller-supplied later
    time. Otherwise the task becomes ``failed`` and the lease columns clear.
    Fencing token, attempt, and input hash are not written. A failed task is
    returned as-is.
    """
    _require_task_id(task_id)
    if category not in _NON_RETRYABLE_TASK_ERRORS and category not in _RETRYABLE_TASK_ERRORS:
        raise ValueError("task error category is rejected")
    if type(max_attempts) is not int or max_attempts < 1:
        raise ValueError("max_attempts must be a positive integer")
    deadline_at = _require_moment(deadline, "deadline")
    retry_at = _require_moment(available_at, "available_at")
    _quiesce(session, task_id)
    current = _loaded_task(session, task_id)
    if current is not None and current.status == "failed":
        logger.info(
            "task finish already failed task_id=%s fencing_token=%s attempt_no=%s",
            task_id,
            current.fencing_token,
            current.attempt_no,
        )
        return _finish_view(current, already_finished=True)
    if current is None or current.status != "running":
        _reject(task_id, "finish")
    assert current is not None
    prior_token = current.fencing_token
    prior_attempt = current.attempt_no
    prior_hash = current.input_hash
    prior_available = current.available_at
    if category in _NON_RETRYABLE_TASK_ERRORS:
        target = "failed"
        error = category
        next_available = prior_available
    else:
        now = _database_now(session)
        if prior_attempt >= max_attempts:
            target = "failed"
            error = "attempts_exhausted"
            next_available = prior_available
        elif deadline_at <= now:
            target = "failed"
            error = "deadline"
            next_available = prior_available
        elif retry_at <= now:
            raise ValueError("available_at must be later than the database clock")
        else:
            target = "retry_wait"
            error = category
            next_available = retry_at
    if target == "retry_wait":
        statement = (
            update(RunTask)
            .where(RunTask.id == task_id, RunTask.status == "running")
            .values(
                status="retry_wait",
                lease_owner=None,
                lease_until=None,
                last_error=error,
                available_at=next_available,
            )
        )
    else:
        statement = (
            update(RunTask)
            .where(RunTask.id == task_id, RunTask.status == "running")
            .values(
                status="failed",
                lease_owner=None,
                lease_until=None,
                last_error=error,
            )
        )
    result = _execute(session, statement)
    _require_one(result, task_id, "finish")
    task = _refreshed_task(session, task_id)
    if (
        task.status != target
        or task.lease_owner is not None
        or task.lease_until is not None
        or task.fencing_token != prior_token
        or task.attempt_no != prior_attempt
        or task.input_hash != prior_hash
        or task.last_error != error
        or task.available_at != next_available
    ):
        _reject(task_id, "finish")
    logger.info(
        "task finish accepted task_id=%s status=%s last_error=%s fencing_token=%s attempt_no=%s",
        task_id,
        task.status,
        task.last_error,
        task.fencing_token,
        task.attempt_no,
    )
    return _finish_view(task, already_finished=False)


def release_retry_wait(session: Session, *, task_id: str) -> TaskRetryRelease:
    """Move one due ``retry_wait`` task back to ``queued``. The caller owns write_uow.

    The only write is a conditional update. Nothing is written when the row is
    not ``retry_wait`` or ``available_at`` is still after the database clock.
    """
    _require_task_id(task_id)
    _quiesce(session, task_id)
    current = _loaded_task(session, task_id)
    if current is None:
        _reject(task_id, "release")
    assert current is not None
    now = _database_now(session)
    if current.status != "retry_wait" or current.available_at > now:
        logger.info(
            "task release unchanged task_id=%s status=%s",
            task_id,
            current.status,
        )
        return _release_view(current, released=False)
    prior_token = current.fencing_token
    prior_attempt = current.attempt_no
    prior_hash = current.input_hash
    prior_available = current.available_at
    prior_error = current.last_error
    result = _execute(
        session,
        update(RunTask)
        .where(
            RunTask.id == task_id,
            RunTask.status == "retry_wait",
            RunTask.available_at <= now,
        )
        .values(status="queued"),
    )
    if result.rowcount != 1:
        raced = _loaded_task(session, task_id)
        if (
            raced is not None
            and raced.status == "queued"
            and raced.lease_owner is None
            and raced.lease_until is None
            and raced.fencing_token == prior_token
            and raced.attempt_no == prior_attempt
            and raced.input_hash == prior_hash
            and raced.available_at == prior_available
            and raced.last_error == prior_error
        ):
            logger.info("task release lost task_id=%s", task_id)
            return _release_view(raced, released=False)
        _reject(task_id, "release")
    task = _refreshed_task(session, task_id)
    if (
        task.status != "queued"
        or task.lease_owner is not None
        or task.lease_until is not None
        or task.fencing_token != prior_token
        or task.attempt_no != prior_attempt
        or task.input_hash != prior_hash
        or task.available_at != prior_available
        or task.last_error != prior_error
    ):
        _reject(task_id, "release")
    logger.info("task release accepted task_id=%s", task_id)
    return _release_view(task, released=True)


def _refreshed_task(session: Session, task_id: str) -> RunTask:
    task = _loaded_task(session, task_id)
    if task is None:
        raise StaleLease(task_id)
    return task


def _loaded_task(session: Session, task_id: str) -> RunTask | None:
    with session.no_autoflush:
        return session.get(RunTask, task_id, populate_existing=True)


def _quiesce(session: Session, task_id: str) -> None:
    """Drop a loaded task so autoflush cannot write stale lease columns."""
    with session.no_autoflush:
        for obj in list(session.identity_map.values()):
            if isinstance(obj, RunTask) and obj.id == task_id and inspect(obj).persistent:
                session.expire(obj)


def _execute(session: Session, statement):
    statement = statement.execution_options(synchronize_session=False)
    with session.no_autoflush:
        return session.execute(statement)


def _require_one(result, task_id: str, action: str) -> None:
    if result.rowcount != 1:
        _reject(task_id, action)


def _reject(task_id: str, action: str) -> None:
    logger.info("task %s rejected task_id=%s", action, task_id)
    raise StaleLease(task_id)


def _database_now(session: Session) -> float:
    value = session.scalar(_NOW)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise RuntimeError("database clock is unavailable")
    now = float(value)
    if not math.isfinite(now):
        raise RuntimeError("database clock is unavailable")
    return now


def _require_task_id(task_id: str) -> None:
    if (
        not isinstance(task_id, str)
        or task_id != task_id.strip()
        or not task_id
        or len(task_id) > 64
    ):
        raise ValueError("task_id is required")


def _require_moment(value: float, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{field} must be a finite number")
    moment = float(value)
    if not math.isfinite(moment):
        raise ValueError(f"{field} must be a finite number")
    return moment


def _finish_view(task: RunTask, *, already_finished: bool) -> TaskAttemptFinish:
    return TaskAttemptFinish(
        task.id,
        task.status,
        task.fencing_token,
        task.attempt_no,
        task.input_hash,
        task.last_error,
        task.available_at,
        already_finished,
    )


def _release_view(task: RunTask, *, released: bool) -> TaskRetryRelease:
    return TaskRetryRelease(
        task.id,
        task.status,
        task.available_at,
        task.fencing_token,
        task.attempt_no,
        task.last_error,
        released,
    )


__all__ = [
    "TaskAttemptFinish",
    "TaskRetryRelease",
    "finish_task_attempt",
    "release_retry_wait",
]
