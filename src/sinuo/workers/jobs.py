"""Conditional claim, renew, finalize, cancel, and finish for the existing jobs table.

The queue is ``jobs``. Each call runs inside the caller's ``write_uow`` and
commits nothing itself. The deadline is one read of SQLite's clock, compared
in the UPDATE predicate (construction doc: takeover iff ``lease_until < db_now``;
renew and finalize keep the complement). ``leases.py`` stays the outbox-delivery
helper. This module does not open a second queue or an SSE stream.

A write whose rowcount is not 1 raises ``StaleLease``. Only the current owner,
fencing token, unexpired lease, and same input hash can publish. Cancel is a
separate conditional update: only queued or running becomes cancelled, and the
lease columns are cleared. A job that is already cancelled is returned as-is;
terminal status, fencing token, lease, and attempt are not written again.

Finish ends one running attempt. Jobs have no ``retry_wait`` status. Retryable
failures return to queued at the caller-supplied later ``available_at`` while
the attempt count is below the cap and the deadline is still ahead of the
database clock. Budget exhaustion, a non-retryable category, the attempt cap,
or a passed deadline writes failed and clears the lease. Fencing token, attempt,
and input hash are not changed. Calling finish again on a failed job does not
emit an UPDATE, because the terminal trigger would abort one.
"""

import logging
import math
import re
from dataclasses import dataclass

from sqlalchemy import and_, inspect, or_, text, update
from sqlalchemy.orm import Session

from sinuo.domain.errors import StaleLease
from sinuo.infrastructure.domain_models import ConsumerReceipt, Job

logger = logging.getLogger("sinuo.workers.jobs")

_HASH = re.compile(r"[0-9a-f]{64}\Z")
_LABEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]*\Z")
_ATTEMPT_REF = re.compile(
    r"jobs/[A-Za-z0-9][A-Za-z0-9._-]{0,63}/[1-9][0-9]{0,63}/result\Z"
)
_NOW = text("SELECT (julianday('now') - 2440587.5) * 86400.0")
_NON_RETRYABLE_JOB_ERRORS = frozenset(
    {
        "budget_exhausted",
        "unauthorized",
        "payment_required",
        "invalid_schema",
    }
)
_RETRYABLE_JOB_ERRORS = frozenset({"rate_limited", "unavailable"})


class ConsumerEffectConflict(Exception):
    """This event and consumer already recorded a different effect."""


@dataclass(frozen=True)
class JobAttempt:
    job_id: str
    owner: str
    fencing_token: int
    attempt_no: int
    lease_until: float
    input_hash: str


@dataclass(frozen=True)
class JobPublication:
    job_id: str
    fencing_token: int
    attempt_no: int
    input_hash: str
    output_ref: str
    consumer_event_id: str | None


@dataclass(frozen=True)
class JobCancellation:
    job_id: str
    fencing_token: int
    attempt_no: int
    input_hash: str
    already_cancelled: bool


@dataclass(frozen=True)
class JobAttemptFinish:
    job_id: str
    status: str
    fencing_token: int
    attempt_no: int
    input_hash: str
    last_error: str | None
    available_at: float
    already_finished: bool


def claim_job(
    session: Session,
    *,
    job_id: str,
    owner: str,
    input_hash: str,
    ttl_seconds: float,
) -> JobAttempt:
    """Claim one due queued job, or a running job whose lease has expired."""
    _require_job_id(job_id)
    _require_owner(owner)
    _require_hash(input_hash)
    _quiesce(session, job_id)
    now = _database_now(session)
    expires_at = _expiry(now, ttl_seconds)
    result = _execute(
        session,
        update(Job)
        .where(
            Job.id == job_id,
            Job.input_hash == input_hash,
            Job.available_at <= now,
            or_(
                Job.status == "queued",
                and_(Job.status == "running", Job.lease_until < now),
            ),
        )
        .values(
            status="running",
            lease_owner=owner,
            lease_until=expires_at,
            fencing_token=Job.fencing_token + 1,
            attempt_no=Job.attempt_no + 1,
        ),
    )
    _require_one(result, job_id, "claim")
    job = _refreshed_job(session, job_id)
    if (
        job.status != "running"
        or job.lease_owner != owner
        or job.input_hash != input_hash
        or job.lease_until != expires_at
        or job.fencing_token < 1
        or job.attempt_no < 1
    ):
        _reject(job_id, "claim")
    logger.info(
        "job claim accepted job_id=%s owner=%s fencing_token=%s attempt_no=%s",
        job_id,
        owner,
        job.fencing_token,
        job.attempt_no,
    )
    return JobAttempt(
        job_id,
        owner,
        job.fencing_token,
        job.attempt_no,
        job.lease_until,
        job.input_hash,
    )


def renew_job(
    session: Session,
    *,
    job_id: str,
    owner: str,
    fencing_token: int,
    input_hash: str,
    ttl_seconds: float,
) -> JobAttempt:
    """Extend the lease. The caller owns write_uow; the token does not change."""
    _require_job_id(job_id)
    _require_owner(owner)
    token = _require_token(fencing_token)
    _require_hash(input_hash)
    _quiesce(session, job_id)
    now = _database_now(session)
    expires_at = _expiry(now, ttl_seconds)
    result = _execute(
        session,
        update(Job)
        .where(*_live(job_id, owner, token, input_hash, now))
        .values(lease_until=expires_at),
    )
    _require_one(result, job_id, "renew")
    job = _refreshed_job(session, job_id)
    if (
        job.status != "running"
        or job.lease_owner != owner
        or job.fencing_token != token
        or job.input_hash != input_hash
        or job.lease_until != expires_at
    ):
        _reject(job_id, "renew")
    logger.info(
        "job renew accepted job_id=%s owner=%s fencing_token=%s",
        job_id,
        owner,
        token,
    )
    return JobAttempt(
        job_id,
        owner,
        job.fencing_token,
        job.attempt_no,
        job.lease_until,
        job.input_hash,
    )


def finalize_job(
    session: Session,
    *,
    job_id: str,
    owner: str,
    fencing_token: int,
    input_hash: str,
    output_ref: str,
    consumer_name: str,
    effect_kind: str,
) -> JobPublication:
    """Publish a succeeded job. The caller owns write_uow.

    The conditional update stores the safe result reference and clears the
    lease. When the job cites a source event, the same transaction writes or
    reuses the consumer receipt. A different effect for that pair rolls back.
    """
    _require_job_id(job_id)
    _require_owner(owner)
    token = _require_token(fencing_token)
    _require_hash(input_hash)
    safe_ref = _require_output_ref(output_ref)
    consumer = _require_label(consumer_name, "consumer_name", 100)
    effect = _require_label(effect_kind, "effect_kind", 64)
    _quiesce(session, job_id)
    now = _database_now(session)
    result = _execute(
        session,
        update(Job)
        .where(*_live(job_id, owner, token, input_hash, now))
        .values(
            status="succeeded",
            lease_owner=None,
            lease_until=None,
            output_ref=safe_ref,
            last_error=None,
        ),
    )
    _require_one(result, job_id, "finalize")
    job = _refreshed_job(session, job_id)
    if (
        job.status != "succeeded"
        or job.lease_owner is not None
        or job.lease_until is not None
        or job.fencing_token != token
        or job.input_hash != input_hash
        or job.output_ref != safe_ref
    ):
        _reject(job_id, "finalize")
    event_id = _record_effect(
        session,
        job,
        consumer_name=consumer,
        effect_kind=effect,
        output_ref=safe_ref,
        created_at=now,
    )
    logger.info(
        "job finalize accepted job_id=%s owner=%s fencing_token=%s",
        job_id,
        owner,
        token,
    )
    return JobPublication(
        job_id,
        job.fencing_token,
        job.attempt_no,
        job.input_hash,
        safe_ref,
        event_id,
    )


def cancel_job(session: Session, *, job_id: str) -> JobCancellation:
    """Cancel a queued or running job. The caller owns write_uow.

    The only write is a conditional update. Queued or running becomes
    cancelled and the lease columns are cleared. A rowcount other than 1
    raises StaleLease. When the job is already cancelled, this returns that
    fact and does not update status, fencing token, lease, or attempt.
    """
    _require_job_id(job_id)
    _quiesce(session, job_id)
    current = _loaded_job(session, job_id)
    if current is not None and current.status == "cancelled":
        logger.info(
            "job cancel already cancelled job_id=%s fencing_token=%s attempt_no=%s",
            job_id,
            current.fencing_token,
            current.attempt_no,
        )
        return JobCancellation(
            job_id,
            current.fencing_token,
            current.attempt_no,
            current.input_hash,
            True,
        )
    prior_token = None if current is None else current.fencing_token
    prior_attempt = None if current is None else current.attempt_no
    prior_hash = None if current is None else current.input_hash
    result = _execute(
        session,
        update(Job)
        .where(
            Job.id == job_id,
            or_(Job.status == "queued", Job.status == "running"),
        )
        .values(
            status="cancelled",
            lease_owner=None,
            lease_until=None,
        ),
    )
    _require_one(result, job_id, "cancel")
    job = _refreshed_job(session, job_id)
    if (
        job.status != "cancelled"
        or job.lease_owner is not None
        or job.lease_until is not None
        or (prior_token is not None and job.fencing_token != prior_token)
        or (prior_attempt is not None and job.attempt_no != prior_attempt)
        or (prior_hash is not None and job.input_hash != prior_hash)
    ):
        _reject(job_id, "cancel")
    logger.info(
        "job cancel accepted job_id=%s fencing_token=%s attempt_no=%s",
        job_id,
        job.fencing_token,
        job.attempt_no,
    )
    return JobCancellation(
        job_id,
        job.fencing_token,
        job.attempt_no,
        job.input_hash,
        False,
    )


def finish_job_attempt(
    session: Session,
    *,
    job_id: str,
    category: str,
    max_attempts: int,
    deadline: float,
    available_at: float,
) -> JobAttemptFinish:
    """Finish one running attempt. The caller owns write_uow.

    Non-retryable categories and ``budget_exhausted`` fail immediately.
    ``rate_limited`` and ``unavailable`` go back to queued when ``attempt_no``
    is below ``max_attempts`` and ``deadline`` is still after the database
    clock. Otherwise the job fails with ``attempts_exhausted`` or ``deadline``.
    The attempt cap is checked first. This does not bump the fencing token or
    attempt number; ``claim_job`` does that. A failed job is returned as-is.
    """
    _require_job_id(job_id)
    if category not in _NON_RETRYABLE_JOB_ERRORS and category not in _RETRYABLE_JOB_ERRORS:
        raise ValueError("job error category is rejected")
    if type(max_attempts) is not int or max_attempts < 1:
        raise ValueError("max_attempts must be a positive integer")
    deadline_at = _require_moment(deadline, "deadline")
    retry_at = _require_moment(available_at, "available_at")
    _quiesce(session, job_id)
    current = _loaded_job(session, job_id)
    if current is not None and current.status == "failed":
        logger.info(
            "job finish already failed job_id=%s fencing_token=%s attempt_no=%s",
            job_id,
            current.fencing_token,
            current.attempt_no,
        )
        return _finish_view(current, already_finished=True)
    if current is None or current.status != "running":
        _reject(job_id, "finish")
    assert current is not None
    prior_token = current.fencing_token
    prior_attempt = current.attempt_no
    prior_hash = current.input_hash
    prior_available = current.available_at
    if category in _NON_RETRYABLE_JOB_ERRORS:
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
            target = "queued"
            error = category
            next_available = retry_at
    if target == "queued":
        statement = (
            update(Job)
            .where(Job.id == job_id, Job.status == "running")
            .values(
                status="queued",
                lease_owner=None,
                lease_until=None,
                last_error=error,
                available_at=next_available,
            )
        )
    else:
        statement = (
            update(Job)
            .where(Job.id == job_id, Job.status == "running")
            .values(
                status="failed",
                lease_owner=None,
                lease_until=None,
                last_error=error,
            )
        )
    result = _execute(session, statement)
    _require_one(result, job_id, "finish")
    job = _refreshed_job(session, job_id)
    if (
        job.status != target
        or job.lease_owner is not None
        or job.lease_until is not None
        or job.fencing_token != prior_token
        or job.attempt_no != prior_attempt
        or job.input_hash != prior_hash
        or job.last_error != error
        or job.available_at != next_available
    ):
        _reject(job_id, "finish")
    logger.info(
        "job finish accepted job_id=%s status=%s last_error=%s fencing_token=%s attempt_no=%s",
        job_id,
        job.status,
        job.last_error,
        job.fencing_token,
        job.attempt_no,
    )
    return _finish_view(job, already_finished=False)


def _loaded_job(session: Session, job_id: str) -> Job | None:
    with session.no_autoflush:
        return session.get(Job, job_id, populate_existing=True)


def _live(
    job_id: str,
    owner: str,
    fencing_token: int,
    input_hash: str,
    now: float,
):
    return (
        Job.id == job_id,
        Job.status == "running",
        Job.lease_owner == owner,
        Job.fencing_token == fencing_token,
        Job.input_hash == input_hash,
        Job.lease_until >= now,
    )


def _quiesce(session: Session, job_id: str) -> None:
    """Drop a loaded job so autoflush cannot write stale lease columns."""
    with session.no_autoflush:
        for obj in list(session.identity_map.values()):
            if isinstance(obj, Job) and obj.id == job_id and inspect(obj).persistent:
                session.expire(obj)


def _execute(session: Session, statement):
    statement = statement.execution_options(synchronize_session=False)
    with session.no_autoflush:
        return session.execute(statement)


def _require_one(result, job_id: str, action: str) -> None:
    if result.rowcount != 1:
        _reject(job_id, action)


def _reject(job_id: str, action: str) -> None:
    logger.info("job %s rejected job_id=%s", action, job_id)
    raise StaleLease(job_id)


def _refreshed_job(session: Session, job_id: str) -> Job:
    job = _loaded_job(session, job_id)
    if job is None:
        raise StaleLease(job_id)
    return job


def _record_effect(
    session: Session,
    job: Job,
    *,
    consumer_name: str,
    effect_kind: str,
    output_ref: str,
    created_at: float,
) -> str | None:
    event_id = job.source_event_id
    if event_id is None:
        return None
    existing = session.get(ConsumerReceipt, (event_id, consumer_name))
    if existing is None:
        session.add(
            ConsumerReceipt(
                event_id=event_id,
                consumer_name=consumer_name,
                project_id=job.project_id,
                effect_kind=effect_kind,
                effect_ref=output_ref,
                created_at=created_at,
            )
        )
        session.flush()
        return event_id
    if (
        existing.project_id != job.project_id
        or existing.effect_kind != effect_kind
        or existing.effect_ref != output_ref
    ):
        raise ConsumerEffectConflict(consumer_name)
    return event_id


def _database_now(session: Session) -> float:
    value = session.scalar(_NOW)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise RuntimeError("database clock is unavailable")
    now = float(value)
    if not math.isfinite(now):
        raise RuntimeError("database clock is unavailable")
    return now


def _expiry(now: float, ttl_seconds: float) -> float:
    if isinstance(ttl_seconds, bool) or not isinstance(ttl_seconds, (int, float)):
        raise ValueError("lease ttl must be a finite positive number")
    if not math.isfinite(ttl_seconds) or ttl_seconds <= 0:
        raise ValueError("lease ttl must be a finite positive number")
    expires_at = now + float(ttl_seconds)
    if not math.isfinite(expires_at) or expires_at <= now:
        raise ValueError("lease expiry is not representable")
    return expires_at


def _require_job_id(job_id: str) -> None:
    if not isinstance(job_id, str) or job_id != job_id.strip() or not job_id or len(job_id) > 64:
        raise ValueError("job_id is required")


def _require_owner(owner: str) -> None:
    if not isinstance(owner, str) or owner != owner.strip() or not owner or len(owner) > 100:
        raise ValueError("lease owner is required")


def _require_hash(input_hash: str) -> None:
    if not isinstance(input_hash, str) or _HASH.fullmatch(input_hash) is None:
        raise ValueError("input_hash must be 64 lowercase hex characters")


def _require_token(fencing_token: int) -> int:
    if type(fencing_token) is not int or fencing_token < 1:
        raise ValueError("fencing_token must be a positive integer")
    return fencing_token


def _finish_view(job: Job, *, already_finished: bool) -> JobAttemptFinish:
    return JobAttemptFinish(
        job.id,
        job.status,
        job.fencing_token,
        job.attempt_no,
        job.input_hash,
        job.last_error,
        job.available_at,
        already_finished,
    )


def _require_moment(value: float, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{field} must be a finite number")
    moment = float(value)
    if not math.isfinite(moment):
        raise ValueError(f"{field} must be a finite number")
    return moment


def _require_output_ref(value: str) -> str:
    if not isinstance(value, str) or len(value) > 200 or ".." in value:
        raise ValueError("output_ref must be a safe token")
    if _LABEL.fullmatch(value) is None and _ATTEMPT_REF.fullmatch(value) is None:
        raise ValueError("output_ref must be a safe token")
    return value


def _require_label(value: str, field: str, limit: int) -> str:
    if (
        not isinstance(value, str)
        or len(value) > limit
        or _LABEL.fullmatch(value) is None
        or ".." in value
    ):
        raise ValueError(f"{field} must be a safe token")
    return value


__all__ = [
    "ConsumerEffectConflict",
    "JobAttempt",
    "JobAttemptFinish",
    "JobCancellation",
    "JobPublication",
    "cancel_job",
    "claim_job",
    "finalize_job",
    "finish_job_attempt",
    "renew_job",
]
