"""Persist suggestion preferences and deduplicate event or digest triggers.

The caller owns ``write_uow`` and must not commit inside it. A missing
preference behaves as manual and writes nothing. The first save inserts the
manual default at revision 1, then conditionally updates that row when
``expected_revision`` is 1. A mismatch raises and leaves the caller's
transaction to roll back. After the revision increment, queued or running
policy runs for that project and actor are cancelled in the same transaction.
Project.revision is not read or written.

Whitelist events are ``page.version_created``, ``artifact.version_registered``,
and ``test.record_confirmed``. Names starting with ``run``, ``candidate``, or
``stream`` do not queue a job. Digest polls the current UTC window only.
"""

import hashlib
import json
import logging
import math
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from sqlalchemy import inspect, select, update
from sqlalchemy.orm import Session

from sinuo.infrastructure.domain_models import AgentRun, Job
from sinuo.infrastructure.models import OutboxEvent
from sinuo.infrastructure.suggestions import SuggestionPreference, SuggestionTrigger

logger = logging.getLogger("sinuo.workers.suggestions")

DEFAULT_MODE = "manual"
DEFAULT_SCOPE = "{}"
DEFAULT_COOLDOWN_SECONDS = 0
DEFAULT_DIGEST_INTERVAL_SECONDS = 86400
DEFAULT_REVISION = 1
MODES = frozenset({"off", "manual", "event", "digest"})
WHITELIST_EVENTS = frozenset(
    {
        "page.version_created",
        "artifact.version_registered",
        "test.record_confirmed",
    }
)
IGNORED_PREFIXES = ("run", "candidate", "stream")
_OPEN_POLICY_STATUSES = ("queued", "running")
JOB_KIND = "suggestion_trigger"
OUTBOX_EVENT_TYPE = "suggestion.trigger_queued"
_PAYLOAD_KEYS = (
    "project_id",
    "actor_id",
    "preference_revision",
    "trigger_kind",
    "trigger_key",
)


class SuggestionPreferenceConflict(Exception):
    """expected_revision did not match. The caller must not catch this inside write_uow."""


@dataclass(frozen=True)
class StoredPreference:
    project_id: str
    actor_id: str
    mode: str
    scope_json: str
    cooldown_seconds: int
    digest_interval_seconds: int
    revision: int
    next_due_at: float | None
    updated_at: float


@dataclass(frozen=True)
class SuggestionTriggerResult:
    outcome: str | None
    job_id: str | None
    trigger_key: str | None
    created_job: bool


def save_suggestion_preference(
    session: Session,
    *,
    project_id: str,
    actor_id: str,
    expected_revision: int,
    mode: str,
    scope: Mapping[str, Any],
    cooldown_seconds: int,
    digest_interval_seconds: int,
    now: float,
) -> StoredPreference:
    """Insert the manual default when absent, then conditionally update it.

    After that revision increment, queued or running policy runs for this
    project and actor are cancelled in the same transaction and
    ``accepting_results`` becomes 0. An actor's manual runs stay as they are.
    A later save emits no further ``agent_runs`` UPDATE once those policy runs
    are already cancelled.
    Does not commit and does not change ``Project.revision``.
    """

    _require_identity(project_id, actor_id)
    chosen_mode = _require_mode(mode)
    scope_json = _scope_text(scope)
    cooldown = _require_int(cooldown_seconds, minimum=0, name="cooldown_seconds")
    interval = _require_int(
        digest_interval_seconds,
        minimum=1,
        name="digest_interval_seconds",
    )
    _require_timestamp(now)
    if type(expected_revision) is not int or expected_revision < 1:
        raise SuggestionPreferenceConflict(project_id)

    current = _preference(session, project_id, actor_id)
    next_due_at = None if current is None else current.next_due_at
    if current is None:
        if expected_revision != DEFAULT_REVISION:
            raise SuggestionPreferenceConflict(project_id)
        current = SuggestionPreference(
            project_id=project_id,
            actor_id=actor_id,
            mode=DEFAULT_MODE,
            scope_json=DEFAULT_SCOPE,
            cooldown_seconds=DEFAULT_COOLDOWN_SECONDS,
            digest_interval_seconds=DEFAULT_DIGEST_INTERVAL_SECONDS,
            revision=DEFAULT_REVISION,
            next_due_at=None,
            updated_at=now,
        )
        session.add(current)
        session.flush()

    changed = session.execute(
        update(SuggestionPreference)
        .where(
            SuggestionPreference.project_id == project_id,
            SuggestionPreference.actor_id == actor_id,
            SuggestionPreference.revision == expected_revision,
        )
        .values(
            mode=chosen_mode,
            scope_json=scope_json,
            cooldown_seconds=cooldown,
            digest_interval_seconds=interval,
            revision=expected_revision + 1,
            updated_at=now,
        )
        .execution_options(synchronize_session=False)
    )
    if changed.rowcount != 1:
        raise SuggestionPreferenceConflict(project_id)
    _cancel_open_policy_runs(session, project_id, actor_id)
    session.expire(current)
    return StoredPreference(
        project_id=project_id,
        actor_id=actor_id,
        mode=chosen_mode,
        scope_json=scope_json,
        cooldown_seconds=cooldown,
        digest_interval_seconds=interval,
        revision=expected_revision + 1,
        next_due_at=next_due_at,
        updated_at=now,
    )


@dataclass(frozen=True)
class PreferenceView:
    project_id: str
    actor_id: str
    mode: str
    scope_json: str
    cooldown_seconds: int
    digest_interval_seconds: int
    revision: int
    next_due_at: float | None
    updated_at: float | None
    stored: bool


def read_suggestion_preference(
    session: Session,
    *,
    project_id: str,
    actor_id: str,
) -> PreferenceView:
    """Return the stored row, or the manual default when no row exists.

    A missing row is not inserted. ``Project.revision`` is not read or written.
    """

    _require_identity(project_id, actor_id)
    current = _preference(session, project_id, actor_id)
    if current is None:
        return PreferenceView(
            project_id=project_id,
            actor_id=actor_id,
            mode=DEFAULT_MODE,
            scope_json=DEFAULT_SCOPE,
            cooldown_seconds=DEFAULT_COOLDOWN_SECONDS,
            digest_interval_seconds=DEFAULT_DIGEST_INTERVAL_SECONDS,
            revision=DEFAULT_REVISION,
            next_due_at=None,
            updated_at=None,
            stored=False,
        )
    return PreferenceView(
        project_id=current.project_id,
        actor_id=current.actor_id,
        mode=current.mode,
        scope_json=current.scope_json,
        cooldown_seconds=current.cooldown_seconds,
        digest_interval_seconds=current.digest_interval_seconds,
        revision=current.revision,
        next_due_at=current.next_due_at,
        updated_at=current.updated_at,
        stored=True,
    )


def consider_suggestion_event(
    session: Session,
    *,
    project_id: str,
    actor_id: str,
    event_id: str,
    event_type: str,
    now: float,
) -> SuggestionTriggerResult:
    """Queue one whitelist event outside cooldown, or record a suppressed trigger."""

    _require_identity(project_id, actor_id)
    _require_timestamp(now)
    if type(event_id) is not str or not event_id.strip():
        raise ValueError("event_id is rejected")
    if type(event_type) is not str or not _event_allowed(event_type):
        return _skipped()
    preference = _preference(session, project_id, actor_id)
    if preference is None or preference.mode != "event":
        return _skipped()
    return _decide(
        session,
        preference,
        trigger_kind="event",
        trigger_key=event_id,
        now=now,
        next_due_at=now + preference.cooldown_seconds,
        honor_cooldown=True,
    )


def poll_suggestion_digest(
    session: Session,
    *,
    project_id: str,
    actor_id: str,
    now: float,
) -> SuggestionTriggerResult:
    """Queue the current UTC window only. A late poll does not backfill older windows."""

    _require_identity(project_id, actor_id)
    _require_timestamp(now)
    preference = _preference(session, project_id, actor_id)
    if preference is None or preference.mode != "digest":
        return _skipped()
    start = digest_window_start(now, preference.digest_interval_seconds)
    return _decide(
        session,
        preference,
        trigger_kind="digest",
        trigger_key=str(start),
        now=now,
        next_due_at=float(start + preference.digest_interval_seconds),
        honor_cooldown=False,
    )


def digest_window_start(now: float, interval_seconds: int) -> int:
    """UTC window start in integer seconds. ``interval_seconds`` is the window length."""

    _require_timestamp(now)
    interval = _require_int(interval_seconds, minimum=1, name="digest_interval_seconds")
    seconds = math.floor(now)
    return int(seconds // interval) * interval


def _decide(
    session: Session,
    preference: SuggestionPreference,
    *,
    trigger_kind: str,
    trigger_key: str,
    now: float,
    next_due_at: float,
    honor_cooldown: bool,
) -> SuggestionTriggerResult:
    existing = _trigger(
        session,
        project_id=preference.project_id,
        actor_id=preference.actor_id,
        preference_revision=preference.revision,
        trigger_kind=trigger_kind,
        trigger_key=trigger_key,
    )
    if existing is not None:
        return SuggestionTriggerResult(
            outcome=existing.outcome,
            job_id=existing.job_id,
            trigger_key=existing.trigger_key,
            created_job=False,
        )
    if honor_cooldown and _inside_cooldown(preference, now):
        _insert_trigger(
            session,
            preference,
            trigger_kind=trigger_kind,
            trigger_key=trigger_key,
            outcome="suppressed",
            job_id=None,
            now=now,
        )
        _log(preference, trigger_kind, "suppressed")
        return SuggestionTriggerResult(
            outcome="suppressed",
            job_id=None,
            trigger_key=trigger_key,
            created_job=False,
        )
    job_id = _queue(session, preference, trigger_kind, trigger_key, now)
    _insert_trigger(
        session,
        preference,
        trigger_kind=trigger_kind,
        trigger_key=trigger_key,
        outcome="queued",
        job_id=job_id,
        now=now,
    )
    moved = session.execute(
        update(SuggestionPreference)
        .where(
            SuggestionPreference.project_id == preference.project_id,
            SuggestionPreference.actor_id == preference.actor_id,
            SuggestionPreference.revision == preference.revision,
        )
        .values(next_due_at=next_due_at)
        .execution_options(synchronize_session=False)
    )
    if moved.rowcount != 1:
        raise SuggestionPreferenceConflict(preference.project_id)
    _log(preference, trigger_kind, "queued")
    session.expire(preference)
    return SuggestionTriggerResult(
        outcome="queued",
        job_id=job_id,
        trigger_key=trigger_key,
        created_job=True,
    )


def _queue(
    session: Session,
    preference: SuggestionPreference,
    trigger_kind: str,
    trigger_key: str,
    now: float,
) -> str:
    payload = {
        key: {
            "project_id": preference.project_id,
            "actor_id": preference.actor_id,
            "preference_revision": preference.revision,
            "trigger_kind": trigger_kind,
            "trigger_key": trigger_key,
        }[key]
        for key in _PAYLOAD_KEYS
    }
    payload_text = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )
    job_id = str(uuid.uuid4())
    session.add(
        Job(
            id=job_id,
            project_id=preference.project_id,
            job_kind=JOB_KIND,
            status="queued",
            available_at=now,
            attempt_no=0,
            lease_owner=None,
            lease_until=None,
            fencing_token=0,
            input_hash=hashlib.sha256(payload_text.encode("utf-8")).hexdigest(),
            output_ref=None,
            last_error=None,
            subject_type="actor",
            subject_id=preference.actor_id,
            source_event_id=None,
            payload=payload_text,
            created_at=now,
        )
    )
    session.add(
        OutboxEvent(
            event_id=str(uuid.uuid4()),
            project_id=preference.project_id,
            event_type=OUTBOX_EVENT_TYPE,
            schema_version=1,
            payload=dict(payload),
            created_at=now,
        )
    )
    session.flush()
    return job_id


def _insert_trigger(
    session: Session,
    preference: SuggestionPreference,
    *,
    trigger_kind: str,
    trigger_key: str,
    outcome: str,
    job_id: str | None,
    now: float,
) -> None:
    session.add(
        SuggestionTrigger(
            project_id=preference.project_id,
            actor_id=preference.actor_id,
            preference_revision=preference.revision,
            trigger_kind=trigger_kind,
            trigger_key=trigger_key,
            outcome=outcome,
            job_id=job_id,
            created_at=now,
        )
    )
    session.flush()


def _open_policy_filters(project_id: str, actor_id: str):
    return (
        AgentRun.project_id == project_id,
        AgentRun.initiated_by_account_id == actor_id,
        AgentRun.origin == "policy",
        AgentRun.execution_status.in_(_OPEN_POLICY_STATUSES),
    )


def _cancel_open_policy_runs(session: Session, project_id: str, actor_id: str) -> None:
    """Cancel queued or running policy runs. Already finished runs are left alone."""

    matched = session.scalar(
        select(AgentRun.id).where(*_open_policy_filters(project_id, actor_id)).limit(1)
    )
    if matched is None:
        return
    session.execute(
        update(AgentRun)
        .where(*_open_policy_filters(project_id, actor_id))
        .values(execution_status="cancelled", accepting_results=0)
        .execution_options(synchronize_session=False)
    )
    _expire_loaded_policy_runs(session, project_id, actor_id)
    logger.info(
        "suggestion preference cancelled open policy runs project=%s actor=%s",
        project_id,
        actor_id,
    )


def _expire_loaded_policy_runs(session: Session, project_id: str, actor_id: str) -> None:
    with session.no_autoflush:
        for obj in list(session.identity_map.values()):
            if not isinstance(obj, AgentRun) or not inspect(obj).persistent:
                continue
            if obj.project_id != project_id or obj.initiated_by_account_id != actor_id:
                continue
            if obj.origin != "policy":
                continue
            session.expire(obj)


def _preference(
    session: Session,
    project_id: str,
    actor_id: str,
) -> SuggestionPreference | None:
    return session.scalar(
        select(SuggestionPreference).where(
            SuggestionPreference.project_id == project_id,
            SuggestionPreference.actor_id == actor_id,
        )
    )


def _trigger(
    session: Session,
    *,
    project_id: str,
    actor_id: str,
    preference_revision: int,
    trigger_kind: str,
    trigger_key: str,
) -> SuggestionTrigger | None:
    return session.scalar(
        select(SuggestionTrigger).where(
            SuggestionTrigger.project_id == project_id,
            SuggestionTrigger.actor_id == actor_id,
            SuggestionTrigger.preference_revision == preference_revision,
            SuggestionTrigger.trigger_kind == trigger_kind,
            SuggestionTrigger.trigger_key == trigger_key,
        )
    )


def _inside_cooldown(preference: SuggestionPreference, now: float) -> bool:
    due = preference.next_due_at
    return due is not None and now < due


def _event_allowed(event_type: str) -> bool:
    if any(event_type.startswith(prefix) for prefix in IGNORED_PREFIXES):
        return False
    return event_type in WHITELIST_EVENTS


def _skipped() -> SuggestionTriggerResult:
    return SuggestionTriggerResult(
        outcome=None,
        job_id=None,
        trigger_key=None,
        created_job=False,
    )


def _log(preference: SuggestionPreference, trigger_kind: str, outcome: str) -> None:
    logger.info(
        "suggestion trigger %s %s project=%s actor=%s revision=%s",
        trigger_kind,
        outcome,
        preference.project_id,
        preference.actor_id,
        preference.revision,
    )


def _require_identity(project_id: str, actor_id: str) -> None:
    if type(project_id) is not str or not project_id.strip():
        raise ValueError("project_id is rejected")
    if type(actor_id) is not str or not actor_id.strip():
        raise ValueError("actor_id is rejected")


def _require_mode(mode: str) -> str:
    if type(mode) is not str or mode not in MODES:
        raise ValueError("mode is rejected")
    return mode


def _require_int(value: object, *, minimum: int, name: str) -> int:
    if type(value) is not int or value < minimum:
        raise ValueError(f"{name} is rejected")
    return value


def _require_timestamp(now: float) -> None:
    if type(now) is not float or not math.isfinite(now) or now < 0:
        raise ValueError("now is rejected")


def _scope_text(scope: Mapping[str, Any]) -> str:
    if not isinstance(scope, Mapping) or isinstance(scope, (str, bytes)):
        raise ValueError("scope is rejected")
    try:
        rendered = json.dumps(
            scope,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
    except (TypeError, ValueError) as error:
        raise ValueError("scope is rejected") from error
    parsed = json.loads(rendered)
    if not isinstance(parsed, dict):
        raise ValueError("scope is rejected")
    return rendered
