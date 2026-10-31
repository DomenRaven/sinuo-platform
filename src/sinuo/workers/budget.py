"""Atomic model-call reservation on the existing AgentRun.budget_json text.

The document for this slice is exactly ``max_model_calls``, ``spent_model_calls``,
and ``reserved_model_calls``. Each call runs inside the caller's ``write_uow``
and commits nothing itself. A missing key or any other shape is rejected and
the stored text is not rewritten. Success is one conditional UPDATE that
matches that text. Unknown usage stays reserved; it is not recorded as zero.
"""

import json
import logging
from dataclasses import dataclass

from sqlalchemy import inspect, update
from sqlalchemy.orm import Session

from sinuo.infrastructure.domain_models import AgentRun

logger = logging.getLogger("sinuo.workers.budget")

_BUDGET_KEYS = ("max_model_calls", "spent_model_calls", "reserved_model_calls")
_OPEN_STATUSES = ("queued", "running")
_REFUSALS = frozenset({"budget_exhausted", "not_accepting"})


class BudgetConflict(Exception):
    """The stored budget text did not match, or this settle does not fit it."""


@dataclass(frozen=True)
class ModelCallReservation:
    run_id: str
    reserved: bool
    reason: str | None
    budget_json: str

    def __post_init__(self) -> None:
        if self.reserved:
            if self.reason is not None:
                raise ValueError("a reserved call has no refusal reason")
            return
        if self.reason not in _REFUSALS:
            raise ValueError("reserve refusal reason is rejected")


@dataclass(frozen=True)
class ModelCallSettlement:
    run_id: str
    pending: bool
    budget_json: str


def reserve_model_call(
    session: Session,
    run_id: str,
    estimate: int,
) -> ModelCallReservation:
    """Reserve ``estimate`` model calls with one conditional update.

    The run must be queued or running and still accepting results. When the
    remainder cannot hold ``estimate``, nothing is written and the result is
    ``reserved=False`` with reason ``budget_exhausted``.
    """
    _require_run_id(run_id)
    calls = _require_estimate(estimate)
    run = _load_run(session, run_id)
    if run is None:
        raise BudgetConflict(run_id)
    current_text = _text(run)
    document = _parse_budget(current_text)
    if not _accepting(run):
        return _reservation(run_id, False, "not_accepting", current_text)
    if _room(document) < calls:
        return _reservation(run_id, False, "budget_exhausted", current_text)
    updated = dict(document)
    updated["reserved_model_calls"] = document["reserved_model_calls"] + calls
    stored = _swap_budget(
        session,
        run_id,
        current_text,
        _dump_budget(updated),
        accepting_only=True,
    )
    if stored is not None:
        return _reservation(run_id, True, None, stored)
    return _reservation_after_miss(session, run_id, current_text, calls)


def settle_model_call(
    session: Session,
    run_id: str,
    estimate: int,
    actual: int | None,
) -> ModelCallSettlement:
    """Settle one reservation against the current budget text.

    An integer ``actual`` decreases reserved by ``estimate`` and increases
    spent by ``actual``. ``None`` leaves both counts unchanged and returns a
    pending result. A document that cannot hold the settle raises, and the
    stored text is not rewritten.
    """
    _require_run_id(run_id)
    calls = _require_estimate(estimate)
    measured = _require_actual(actual)
    run = _load_run(session, run_id)
    if run is None:
        raise BudgetConflict(run_id)
    current_text = _text(run)
    document = _parse_budget(current_text)
    if measured is None:
        if document["reserved_model_calls"] < calls:
            raise BudgetConflict(run_id)
        new_text = current_text
        pending = True
    else:
        reserved = document["reserved_model_calls"] - calls
        spent = document["spent_model_calls"] + measured
        if reserved < 0 or spent + reserved > document["max_model_calls"]:
            raise BudgetConflict(run_id)
        updated = dict(document)
        updated["reserved_model_calls"] = reserved
        updated["spent_model_calls"] = spent
        new_text = _dump_budget(updated)
        pending = False
    stored = _swap_budget(
        session,
        run_id,
        current_text,
        new_text,
        accepting_only=False,
    )
    if stored is None:
        raise BudgetConflict(run_id)
    logger.info("model call settle run_id=%s pending=%s", run_id, pending)
    return ModelCallSettlement(run_id, pending, stored)


def _reservation_after_miss(
    session: Session,
    run_id: str,
    current_text: str,
    estimate: int,
) -> ModelCallReservation:
    """The conditional update missed. Do not issue a second one."""
    observed = _load_run(session, run_id)
    if observed is None:
        raise BudgetConflict(run_id)
    observed_text = _text(observed)
    if observed_text == current_text:
        raise BudgetConflict(run_id)
    document = _parse_budget(observed_text)
    if not _accepting(observed):
        return _reservation(run_id, False, "not_accepting", observed_text)
    if _room(document) < estimate:
        return _reservation(run_id, False, "budget_exhausted", observed_text)
    raise BudgetConflict(run_id)


def _reservation(
    run_id: str,
    reserved: bool,
    reason: str | None,
    budget_json: str,
) -> ModelCallReservation:
    logger.info(
        "model call reserve run_id=%s reserved=%s reason=%s",
        run_id,
        reserved,
        reason,
    )
    return ModelCallReservation(run_id, reserved, reason, budget_json)


def _swap_budget(
    session: Session,
    run_id: str,
    current_text: str,
    new_text: str,
    *,
    accepting_only: bool,
) -> str | None:
    clauses = [
        AgentRun.id == run_id,
        AgentRun.budget_json == current_text,
    ]
    if accepting_only:
        clauses.append(AgentRun.execution_status.in_(_OPEN_STATUSES))
        clauses.append(AgentRun.accepting_results == 1)
    statement = (
        update(AgentRun)
        .where(*clauses)
        .values(budget_json=new_text)
        .execution_options(synchronize_session=False)
    )
    with session.no_autoflush:
        result = session.execute(statement)
    if result.rowcount != 1:
        return None
    stored = _budget_text(session, run_id)
    if stored != new_text:
        raise BudgetConflict(run_id)
    return stored


def _budget_text(session: Session, run_id: str) -> str:
    run = _load_run(session, run_id)
    if run is None:
        raise BudgetConflict(run_id)
    return _text(run)


def _load_run(session: Session, run_id: str) -> AgentRun | None:
    with session.no_autoflush:
        for obj in list(session.identity_map.values()):
            if isinstance(obj, AgentRun) and obj.id == run_id and inspect(obj).persistent:
                session.expire(obj)
        return session.get(AgentRun, run_id, populate_existing=True)


def _text(run: AgentRun) -> str:
    raw = run.budget_json
    if not isinstance(raw, str):
        raise ValueError("budget_json shape is rejected")
    return raw


def _accepting(run: AgentRun) -> bool:
    return run.execution_status in _OPEN_STATUSES and run.accepting_results == 1


def _parse_budget(raw: str) -> dict[str, int]:
    try:
        document = json.loads(raw)
    except json.JSONDecodeError as error:
        raise ValueError("budget_json shape is rejected") from error
    if type(document) is not dict or set(document) != set(_BUDGET_KEYS):
        raise ValueError("budget_json shape is rejected")
    parsed: dict[str, int] = {}
    for key in _BUDGET_KEYS:
        value = document[key]
        if type(value) is not int or value < 0:
            raise ValueError("budget_json shape is rejected")
        parsed[key] = value
    spent = parsed["spent_model_calls"]
    reserved = parsed["reserved_model_calls"]
    if spent + reserved > parsed["max_model_calls"]:
        raise ValueError("budget_json shape is rejected")
    return parsed


def _dump_budget(document: dict[str, int]) -> str:
    payload = {key: document[key] for key in _BUDGET_KEYS}
    return json.dumps(payload, separators=(",", ":"))


def _room(document: dict[str, int]) -> int:
    return (
        document["max_model_calls"]
        - document["spent_model_calls"]
        - document["reserved_model_calls"]
    )


def _require_run_id(run_id: str) -> None:
    if not isinstance(run_id, str) or run_id != run_id.strip() or not run_id or len(run_id) > 64:
        raise ValueError("run_id is required")


def _require_estimate(estimate: object) -> int:
    if type(estimate) is not int or estimate < 1:
        raise ValueError("estimate must be a positive integer")
    return estimate


def _require_actual(actual: object) -> int | None:
    if actual is None:
        return None
    if type(actual) is not int or actual < 0:
        raise ValueError("actual must be a non-negative integer or None")
    return actual


__all__ = [
    "BudgetConflict",
    "ModelCallReservation",
    "ModelCallSettlement",
    "reserve_model_call",
    "settle_model_call",
]
