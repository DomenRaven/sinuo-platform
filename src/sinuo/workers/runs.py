"""Conditional cancel and publish for one agent run.

Each call runs inside the caller's ``write_uow`` and commits nothing itself.
Cancel is one conditional update: only queued or running becomes cancelled, and
``accepting_results`` becomes 0. A rowcount other than 1 raises ``StaleLease``.
A run that is already cancelled is returned as-is; ``execution_status`` is not
written again.

Publish is one conditional update. It matches only while ``accepting_results``
is 1 and ``execution_status`` is not cancelled. Queued or running becomes
completed in that same update, so a concurrent cancel no longer matches; any
other non-cancelled status keeps ``execution_status`` and only stops accepting
results. ``next_event_seq`` is not written.
"""

import logging
from dataclasses import dataclass

from sqlalchemy import case, inspect, update
from sqlalchemy.orm import Session

from sinuo.domain.errors import StaleLease
from sinuo.infrastructure.domain_models import AgentRun

logger = logging.getLogger("sinuo.workers.runs")

_OPEN_STATUSES = ("queued", "running")


@dataclass(frozen=True)
class RunCancellation:
    run_id: str
    execution_status: str
    accepting_results: int
    already_cancelled: bool


@dataclass(frozen=True)
class RunPublication:
    run_id: str
    execution_status: str
    accepting_results: int


def cancel_run(session: Session, *, run_id: str) -> RunCancellation:
    """Cancel a queued or running run. The caller owns write_uow.

    The only write is a conditional update. Queued or running becomes
    cancelled and accepting_results becomes 0. A rowcount other than 1 raises
    StaleLease. When the run is already cancelled, this returns that fact and
    does not update execution_status.
    """
    _require_run_id(run_id)
    _quiesce(session, run_id)
    current = _loaded_run(session, run_id)
    if current is not None and current.execution_status == "cancelled":
        logger.info("run cancel already cancelled run_id=%s", run_id)
        return RunCancellation(
            run_id,
            current.execution_status,
            current.accepting_results,
            True,
        )
    prior_seq = None if current is None else current.next_event_seq
    result = _execute(
        session,
        update(AgentRun)
        .where(
            AgentRun.id == run_id,
            AgentRun.execution_status.in_(_OPEN_STATUSES),
        )
        .values(
            execution_status="cancelled",
            accepting_results=0,
        ),
    )
    _require_one(result, run_id, "cancel")
    run = _refreshed_run(session, run_id)
    if (
        run.execution_status != "cancelled"
        or run.accepting_results != 0
        or (prior_seq is not None and run.next_event_seq != prior_seq)
    ):
        _reject(run_id, "cancel")
    logger.info("run cancel accepted run_id=%s", run_id)
    return RunCancellation(run_id, run.execution_status, run.accepting_results, False)


def publish_run(session: Session, *, run_id: str) -> RunPublication:
    """Publish while the run still accepts results and is not cancelled.

    The only write is a conditional update. A rowcount other than 1 raises
    StaleLease. Queued or running becomes completed so cancel cannot also
    match; a later call sees accepting_results 0.
    """
    _require_run_id(run_id)
    _quiesce(session, run_id)
    result = _execute(
        session,
        update(AgentRun)
        .where(
            AgentRun.id == run_id,
            AgentRun.accepting_results == 1,
            AgentRun.execution_status != "cancelled",
        )
        .values(
            accepting_results=0,
            execution_status=case(
                (AgentRun.execution_status.in_(_OPEN_STATUSES), "completed"),
                else_=AgentRun.execution_status,
            ),
        ),
    )
    _require_one(result, run_id, "publish")
    run = _refreshed_run(session, run_id)
    if (
        run.accepting_results != 0
        or run.execution_status == "cancelled"
        or run.execution_status in _OPEN_STATUSES
    ):
        _reject(run_id, "publish")
    logger.info(
        "run publish accepted run_id=%s execution_status=%s",
        run_id,
        run.execution_status,
    )
    return RunPublication(run_id, run.execution_status, run.accepting_results)


def _loaded_run(session: Session, run_id: str) -> AgentRun | None:
    with session.no_autoflush:
        return session.get(AgentRun, run_id, populate_existing=True)


def _quiesce(session: Session, run_id: str) -> None:
    """Drop a loaded run so autoflush cannot write stale status columns."""
    with session.no_autoflush:
        for obj in list(session.identity_map.values()):
            if isinstance(obj, AgentRun) and obj.id == run_id and inspect(obj).persistent:
                session.expire(obj)


def _execute(session: Session, statement):
    statement = statement.execution_options(synchronize_session=False)
    with session.no_autoflush:
        return session.execute(statement)


def _require_one(result, run_id: str, action: str) -> None:
    if result.rowcount != 1:
        _reject(run_id, action)


def _reject(run_id: str, action: str) -> None:
    logger.info("run %s rejected run_id=%s", action, run_id)
    raise StaleLease(run_id)


def _refreshed_run(session: Session, run_id: str) -> AgentRun:
    run = _loaded_run(session, run_id)
    if run is None:
        raise StaleLease(run_id)
    return run


def _require_run_id(run_id: str) -> None:
    if not isinstance(run_id, str) or run_id != run_id.strip() or not run_id or len(run_id) > 64:
        raise ValueError("run_id is required")


__all__ = [
    "RunCancellation",
    "RunPublication",
    "cancel_run",
    "publish_run",
]
