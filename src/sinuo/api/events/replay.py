"""Replay persisted run events after Last-Event-ID.

This module does not mount an HTTP route and does not finish a job.
Heartbeats stay in the stream so a cursor can move past them; they are not
task progress. A missing cursor asks for a snapshot. It does not dump the run.
"""

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from sqlalchemy import Table, insert, select
from sqlalchemy.orm import Session
from sqlalchemy.sql.sqltypes import JSON

from sinuo.application.auth.authorizer import SessionAuthorizer
from sinuo.domain.auth.principal import Principal
from sinuo.domain.auth.resource import ResourceRef
from sinuo.domain.public_contract import Action

HEARTBEAT_EVENT_TYPE = "stream.heartbeat"
RESET_EVENT_TYPE = "stream.reset_required"

_PROGRESS_TYPES = frozenset(
    {
        "run.received",
        "run.context_scoped",
        "run.planned",
        "run.started",
        "run.completed",
        "run.partial",
        "run.failed",
        "run.cancelled",
        "task.started",
        "task.completed",
        "task.failed",
        "task.retry_scheduled",
        "tool.started",
        "tool.completed",
        "tool.failed",
        "candidate.ready",
        "candidate.expired",
        "candidate.committed",
        "candidate.rejected",
        "access.changed",
    }
)


class EventCursorRejected(Exception):
    """Last-Event-ID is not a cursor for this run. The message has no event body."""

    def __init__(self) -> None:
        super().__init__("last event id is outside this run")


@dataclass(frozen=True)
class StreamEvent:
    """One fact to store. Writing it does not change run execution status."""

    event_id: str
    seq: int
    event_type: str
    project_id: str
    run_id: str
    payload: Mapping[str, Any]
    schema_version: int = 1
    task_id: str | None = None
    trace_id: str = "trace-synthetic"
    created_at: float = 0.0


@dataclass(frozen=True)
class ReplayEvent:
    event_id: str
    seq: int
    event_type: str
    project_id: str
    run_id: str
    schema_version: int
    payload: Mapping[str, Any]
    progress: bool
    task_id: str | None = None
    trace_id: str | None = None
    created_at: float | None = None


@dataclass(frozen=True)
class ReplayResult:
    events: tuple[ReplayEvent, ...]
    reset_required: bool = False

    @property
    def progress_events(self) -> tuple[ReplayEvent, ...]:
        return tuple(event for event in self.events if event.progress)


def is_task_progress(event_type: str) -> bool:
    """Closed run-event list. Heartbeats and reset marks are never progress."""

    if event_type in {HEARTBEAT_EVENT_TYPE, RESET_EVENT_TYPE}:
        return False
    return event_type in _PROGRESS_TYPES


def append_events(
    session: Session,
    source: Table | type[Any],
    events: Sequence[StreamEvent],
) -> None:
    """Insert rows into a caller table, or into outbox / run_events."""

    session.flush()
    table = _table(source)
    for event in events:
        session.execute(insert(table).values(**_values(table, event)))


def replay_events(
    session: Session,
    source: Table | type[Any],
    *,
    principal: Principal,
    project_id: str,
    run_id: str,
    last_event_id: str | None,
) -> ReplayResult:
    """Return this run's events after the cursor. Authorize before any body is kept."""

    SessionAuthorizer(session).authorize(
        Action.RUN_READ,
        ResourceRef(project_id=project_id, scope_type="project"),
        principal,
    )
    table = _table(source)
    columns = {column.name for column in table.columns}
    if last_event_id is None:
        cursor_seq = None
    else:
        if (
            not isinstance(last_event_id, str)
            or last_event_id == ""
            or last_event_id != last_event_id.strip()
        ):
            raise EventCursorRejected()
        matched = (
            session.execute(select(table).where(table.c.event_id == last_event_id)).mappings().all()
        )
        if len(matched) == 0:
            return _reset(project_id, run_id)
        if len(matched) != 1:
            raise EventCursorRejected()
        identity = _identity(matched[0], columns)
        if identity is None or identity[0] != project_id or identity[1] != run_id:
            raise EventCursorRejected()
        cursor_seq = identity[2]
    selected = _events_after(session, table, columns, project_id, run_id, cursor_seq)
    return ReplayResult(events=tuple(selected), reset_required=False)


def _reset(project_id: str, run_id: str) -> ReplayResult:
    frame = ReplayEvent(
        event_id=RESET_EVENT_TYPE,
        seq=0,
        event_type=RESET_EVENT_TYPE,
        project_id=project_id,
        run_id=run_id,
        schema_version=1,
        payload={},
        progress=False,
    )
    return ReplayResult(events=(frame,), reset_required=True)


def _events_after(
    session: Session,
    table: Table,
    columns: set[str],
    project_id: str,
    run_id: str,
    cursor_seq: int | None,
) -> list[ReplayEvent]:
    frames: list[ReplayEvent] = []
    seen: set[str] = set()
    rows = session.execute(select(table)).mappings()
    for row in rows:
        identity = _identity(row, columns)
        if identity is None:
            continue
        row_project, row_run, seq = identity
        if row_project != project_id or row_run != run_id:
            continue
        if cursor_seq is not None and seq <= cursor_seq:
            continue
        event_id = _text(row, "event_id")
        if event_id is None or event_id in seen:
            continue
        seen.add(event_id)
        envelope = _envelope(row, columns)
        label = _label(row, columns, envelope)
        frames.append(
            ReplayEvent(
                event_id=event_id,
                seq=seq,
                event_type=label,
                project_id=row_project,
                run_id=row_run,
                schema_version=_schema_version(row, envelope),
                payload=_body(row, envelope),
                progress=is_task_progress(label),
                task_id=_optional_text(row, columns, envelope, "task_id"),
                trace_id=_optional_text(row, columns, envelope, "trace_id"),
                created_at=_created_at(row),
            )
        )
    frames.sort(key=lambda event: (event.seq, event.event_id))
    return frames


def _values(table: Table, event: StreamEvent) -> dict[str, Any]:
    columns = {column.name for column in table.columns}
    event_id = _required_id(event.event_id, "event_id")
    project_id = _required_id(event.project_id, "project_id")
    run_id = _required_id(event.run_id, "run_id")
    event_type = _required_label(event.event_type)
    seq = _required_seq(event.seq)
    if isinstance(event.schema_version, bool) or not isinstance(event.schema_version, int):
        raise ValueError("schema_version must be a positive integer")
    if event.schema_version < 1:
        raise ValueError("schema_version must be a positive integer")
    if isinstance(event.created_at, bool) or not isinstance(event.created_at, (int, float)):
        raise ValueError("created_at must be a number")
    body = _plain_object(event.payload)
    values: dict[str, Any] = {}
    _put(values, columns, "event_id", event_id)
    _put(values, columns, "project_id", project_id)
    _put(values, columns, "run_id", run_id)
    _put(values, columns, "seq", seq)
    _put(values, columns, "event_type", event_type)
    _put(values, columns, "schema_version", event.schema_version)
    _put(values, columns, "task_id", _optional_id(event.task_id, "task_id"))
    _put(values, columns, "trace_id", _required_id(event.trace_id, "trace_id"))
    _put(values, columns, "created_at", float(event.created_at))
    if "payload" not in columns:
        raise TypeError("event table requires a payload column")
    if "seq" in columns and "run_id" in columns:
        stored: dict[str, Any] = body
    else:
        stored = {
            "schema_version": event.schema_version,
            "event_id": event_id,
            "seq": seq,
            "type": event_type,
            "project_id": project_id,
            "run_id": run_id,
            "task_id": _optional_id(event.task_id, "task_id"),
            "trace_id": _required_id(event.trace_id, "trace_id"),
            "payload": body,
        }
    values["payload"] = stored if _stores_json(table.c.payload) else _dump(stored)
    return values


def _identity(row: Mapping[str, Any], columns: set[str]) -> tuple[str, str, int] | None:
    if "seq" in columns and "run_id" in columns and "project_id" in columns:
        seq = _positive_seq(row.get("seq"))
        project_id = _text(row, "project_id")
        run_id = _text(row, "run_id")
        if seq is None or project_id is None or run_id is None:
            return None
        return project_id, run_id, seq
    envelope = _parse_object(row.get("payload"))
    if envelope is None:
        return None
    seq = _positive_seq(envelope.get("seq"))
    run_id = envelope.get("run_id")
    project_id = _text(row, "project_id")
    if project_id is None and isinstance(envelope.get("project_id"), str):
        project_id = envelope["project_id"]
    if seq is None or project_id is None or not isinstance(run_id, str) or run_id == "":
        return None
    return project_id, run_id, seq


def _envelope(row: Mapping[str, Any], columns: set[str]) -> dict[str, Any] | None:
    if "seq" in columns and "run_id" in columns:
        return None
    return _parse_object(row.get("payload"))


def _body(row: Mapping[str, Any], envelope: dict[str, Any] | None) -> dict[str, Any]:
    if envelope is None:
        parsed = _parse_object(row.get("payload"))
        if parsed is None:
            raise ValueError("event payload is not an object")
        return parsed
    inner = envelope.get("payload")
    if inner is None:
        return {}
    if not isinstance(inner, dict):
        raise ValueError("event payload is not an object")
    return dict(inner)


def _label(row: Mapping[str, Any], columns: set[str], envelope: dict[str, Any] | None) -> str:
    column_type = _text(row, "event_type") or ""
    envelope_type = ""
    if envelope is not None and "seq" not in columns and isinstance(envelope.get("type"), str):
        envelope_type = envelope["type"]
    if HEARTBEAT_EVENT_TYPE in {column_type, envelope_type}:
        return HEARTBEAT_EVENT_TYPE
    if column_type:
        return column_type
    return envelope_type


def _schema_version(row: Mapping[str, Any], envelope: dict[str, Any] | None) -> int:
    value = row.get("schema_version")
    if _positive_seq(value) is not None:
        return int(value)
    if envelope is not None:
        nested = _positive_seq(envelope.get("schema_version"))
        if nested is not None:
            return nested
    return 1


def _optional_text(
    row: Mapping[str, Any],
    columns: set[str],
    envelope: dict[str, Any] | None,
    name: str,
) -> str | None:
    if name in columns:
        return _text(row, name)
    if envelope is None:
        return None
    value = envelope.get(name)
    if isinstance(value, str) and value != "":
        return value
    return None


def _created_at(row: Mapping[str, Any]) -> float | None:
    value = row.get("created_at")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _table(source: Table | type[Any]) -> Table:
    if isinstance(source, Table):
        return source
    table = getattr(source, "__table__", None)
    if isinstance(table, Table):
        return table
    raise TypeError("event source must be a SQLAlchemy table")


def _stores_json(column: Any) -> bool:
    return isinstance(column.type, JSON) or type(column.type).__name__ == "JSON"


def _put(values: dict[str, Any], columns: set[str], name: str, value: Any) -> None:
    if name in columns and value is not None:
        values[name] = value


def _plain_object(payload: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise TypeError("event payload must be an object")
    parsed = _parse_object(_dump(payload))
    if parsed is None:
        raise TypeError("event payload must be an object")
    return parsed


def _parse_object(value: Any) -> dict[str, Any] | None:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            return None
    if isinstance(value, dict):
        return dict(value)
    return None


def _dump(value: Mapping[str, Any]) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _required_id(value: str, field: str) -> str:
    if not isinstance(value, str) or value == "" or value != value.strip() or len(value) > 64:
        raise ValueError(f"{field} must be a single token")
    return value


def _optional_id(value: str | None, field: str) -> str | None:
    if value is None:
        return None
    return _required_id(value, field)


def _required_label(value: str) -> str:
    if (
        not isinstance(value, str)
        or value.strip() == ""
        or value != value.strip()
        or len(value) > 100
    ):
        raise ValueError("event_type must be a single token")
    return value


def _required_seq(value: int) -> int:
    seq = _positive_seq(value)
    if seq is None:
        raise ValueError("seq must be a positive integer")
    return seq


def _positive_seq(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        number = value
    elif isinstance(value, float) and value.is_integer():
        number = int(value)
    else:
        return None
    if number < 1:
        return None
    return number


def _text(row: Mapping[str, Any], name: str) -> str | None:
    value = row.get(name)
    if isinstance(value, str) and value != "":
        return value
    return None
