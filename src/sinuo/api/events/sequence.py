"""Allocate one ``run_events.seq`` inside the caller's open transaction.

The allocation is one conditional update, the same shape as the domain
contract::

    UPDATE agent_runs
    SET next_event_seq = next_event_seq + 1
    WHERE id = :run_id AND next_event_seq = :expected;

``next_event_seq`` starts at 0, so the first committed fact is seq 1. The
inserted row uses that new value. A miss raises and inserts nothing; the
caller rolls the unit back, so a lost attempt does not leave a hole.

Heartbeats are not run facts. This function rejects them before any write.
It does not commit, sleep, or mount an HTTP route.
"""

import json
import math
from collections.abc import Mapping
from typing import Any

from sqlalchemy import insert, inspect, update
from sqlalchemy.orm import Session

from sinuo.api.events.replay import HEARTBEAT_EVENT_TYPE
from sinuo.infrastructure.domain_models import AgentRun, RunEvent


class EventSequenceConflict(Exception):
    """``next_event_seq`` was not ``expected_seq``. This call inserts no row."""

    def __init__(self) -> None:
        super().__init__("run event sequence was not at the expected value")


class HeartbeatNotSequenced(Exception):
    """A heartbeat keeps the connection and does not consume ``next_event_seq``."""

    def __init__(self) -> None:
        super().__init__("heartbeat does not take a run event sequence")


def allocate_run_event(
    session: Session,
    *,
    project_id: str,
    run_id: str,
    expected_seq: int,
    event_id: str,
    event_type: str,
    payload: Mapping[str, Any],
    trace_id: str,
    created_at: float,
    schema_version: int = 1,
    task_id: str | None = None,
    context_snapshot_id: str | None = None,
) -> int:
    """Increment one run's counter and insert that seq. Does not commit."""

    event_type = _token(event_type, "event_type", 100)
    if event_type == HEARTBEAT_EVENT_TYPE:
        raise HeartbeatNotSequenced()
    project_id = _token(project_id, "project_id", 64)
    run_id = _token(run_id, "run_id", 64)
    event_id = _token(event_id, "event_id", 64)
    trace_id = _token(trace_id, "trace_id", 64)
    expected_seq = _expected_seq(expected_seq)
    schema_version = _schema_version(schema_version)
    created_at = _timestamp(created_at)
    task_id = _optional_token(task_id, "task_id", 64)
    context_snapshot_id = _optional_token(context_snapshot_id, "context_snapshot_id", 64)
    stored_payload = _payload(payload)
    assigned = expected_seq + 1

    _unload_cached_seq(session, run_id)
    session.flush()
    changed = session.execute(
        update(AgentRun)
        .where(
            AgentRun.id == run_id,
            AgentRun.project_id == project_id,
            AgentRun.next_event_seq == expected_seq,
        )
        .values(next_event_seq=AgentRun.next_event_seq + 1)
        .execution_options(synchronize_session=False)
    )
    if changed.rowcount != 1:
        raise EventSequenceConflict()
    _unload_cached_seq(session, run_id)

    values: dict[str, Any] = {
        "event_id": event_id,
        "project_id": project_id,
        "run_id": run_id,
        "seq": assigned,
        "event_type": event_type,
        "schema_version": schema_version,
        "trace_id": trace_id,
        "payload": stored_payload,
        "created_at": created_at,
    }
    if task_id is not None:
        values["task_id"] = task_id
    if context_snapshot_id is not None:
        values["context_snapshot_id"] = context_snapshot_id
    session.execute(insert(RunEvent).values(**values))
    return assigned


def _unload_cached_seq(session: Session, run_id: str) -> None:
    """Drop a loaded counter so flush cannot write a stale ``next_event_seq``."""

    with session.no_autoflush:
        for obj in list(session.identity_map.values()):
            if isinstance(obj, AgentRun) and obj.id == run_id and inspect(obj).persistent:
                session.expire(obj, ["next_event_seq"])


def _token(value: object, field: str, limit: int) -> str:
    if not isinstance(value, str) or value == "" or value != value.strip() or len(value) > limit:
        raise ValueError(f"{field} must be a single token")
    return value


def _optional_token(value: object, field: str, limit: int) -> str | None:
    if value is None:
        return None
    return _token(value, field, limit)


def _expected_seq(value: object) -> int:
    if type(value) is not int or value < 0:
        raise ValueError("expected_seq must be a non-negative integer")
    return value


def _schema_version(value: object) -> int:
    if type(value) is not int or value < 1:
        raise ValueError("schema_version must be a positive integer")
    return value


def _timestamp(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError("created_at must be a finite number")
    return float(value)


def _payload(value: object) -> str:
    if not isinstance(value, Mapping) or isinstance(value, str):
        raise TypeError("event payload must be an object")
    body = dict(value)
    encoded = json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    parsed = json.loads(encoded)
    if not isinstance(parsed, dict):
        raise TypeError("event payload must be an object")
    return encoded
