"""Suggestion preference CAS, event dedup, cooldown, and digest windows."""

import hashlib
import json
from threading import Barrier, Thread

import pytest
from sqlalchemy import event, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from sinuo.infrastructure.database import write_uow
from sinuo.infrastructure.domain_models import Account, Job, Project
from sinuo.infrastructure.models import SCHEMA_REVISION, Base, OutboxEvent
from sinuo.infrastructure.suggestions import SuggestionPreference, SuggestionTrigger
from sinuo.workers.suggestions import (
    JOB_KIND,
    OUTBOX_EVENT_TYPE,
    SuggestionPreferenceConflict,
    SuggestionTriggerResult,
    consider_suggestion_event,
    poll_suggestion_digest,
    save_suggestion_preference,
)

NOW = 1_700_000_000.0
PROJECT_ID = "synthetic-suggestion-project"
ACTOR_ID = "11111111-1111-4111-8111-111111111111"
OTHER_ACTOR_ID = "22222222-2222-4222-8222-222222222222"
PASSWORD = "$argon2id$synthetic-hash"
SCOPE = {"page_ids": ["synthetic-page"]}
SCOPE_TEXT = '{"page_ids":["synthetic-page"]}'
ARTIFACT_EVENT = "synthetic-artifact-event"
PAGE_EVENT = "synthetic-page-event"


def _project(session: Session, *, revision: int) -> None:
    session.add(
        Project(
            id=PROJECT_ID,
            title="synthetic-suggestion",
            brief=None,
            template_key=None,
            revision=revision,
            access_epoch=1,
            created_at=NOW,
        )
    )


def _account(session: Session, actor_id: str, login_name: str) -> None:
    session.add(
        Account(
            id=actor_id,
            login_name=login_name,
            display_name=login_name,
            password_hash=PASSWORD,
            status="active",
            auth_epoch=1,
            can_create_project=0,
            created_at=NOW,
            disabled_at=None,
        )
    )


def _preference(session: Session, actor_id: str = ACTOR_ID) -> SuggestionPreference | None:
    return session.scalar(
        select(SuggestionPreference).where(
            SuggestionPreference.project_id == PROJECT_ID,
            SuggestionPreference.actor_id == actor_id,
        )
    )


def _project_revision(session: Session) -> int:
    revision = session.scalar(select(Project.revision).where(Project.id == PROJECT_ID))
    assert revision is not None
    return revision


def _jobs(session: Session) -> list[Job]:
    return list(session.scalars(select(Job).where(Job.job_kind == JOB_KIND)).all())


def _triggers(session: Session) -> list[SuggestionTrigger]:
    return list(session.scalars(select(SuggestionTrigger)).all())


def _suggestion_outbox(session: Session) -> list[OutboxEvent]:
    return list(
        session.scalars(
            select(OutboxEvent).where(OutboxEvent.event_type == OUTBOX_EVENT_TYPE)
        ).all()
    )


def _save(
    session: Session,
    *,
    actor_id: str = ACTOR_ID,
    expected_revision: int,
    mode: str,
    cooldown_seconds: int = 0,
    digest_interval_seconds: int = 86400,
    now: float = NOW,
    scope: dict | None = None,
):
    return save_suggestion_preference(
        session,
        project_id=PROJECT_ID,
        actor_id=actor_id,
        expected_revision=expected_revision,
        mode=mode,
        scope={} if scope is None else scope,
        cooldown_seconds=cooldown_seconds,
        digest_interval_seconds=digest_interval_seconds,
        now=now,
    )


def _event(
    session: Session,
    *,
    event_id: str,
    event_type: str,
    now: float,
    actor_id: str = ACTOR_ID,
) -> SuggestionTriggerResult:
    return consider_suggestion_event(
        session,
        project_id=PROJECT_ID,
        actor_id=actor_id,
        event_id=event_id,
        event_type=event_type,
        now=now,
    )


def _flat(value: object) -> list[object]:
    if isinstance(value, dict):
        found: list[object] = []
        for item in value.values():
            found.extend(_flat(item))
        return found
    if isinstance(value, (list, tuple)):
        found = []
        for item in value:
            found.extend(_flat(item))
        return found
    return [value]


def _preference_writes(statements: list[tuple[str, object]]) -> list[tuple[str, object]]:
    writes = []
    for sql, params in statements:
        kind = sql.lstrip().split(None, 1)[0].upper()
        if "suggestion_preferences" in sql.lower() and kind in {"INSERT", "UPDATE"}:
            writes.append((kind, params))
    return writes


@pytest.mark.integration
def test_revision_registers_suggestion_tables(engine):
    assert SCHEMA_REVISION == "0008_ticket_expiry"
    assert {"suggestion_preferences", "suggestion_triggers"} <= set(Base.metadata.tables)
    with engine.connect() as connection:
        version = connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
        names = set(
            connection.execute(
                text("SELECT name FROM sqlite_master WHERE type = 'table'")
            ).scalars()
        )
    assert version == "0008_ticket_expiry"
    assert {"suggestion_preferences", "suggestion_triggers"} <= names


@pytest.mark.integration
def test_missing_preference_does_not_queue_a_whitelist_event(engine):
    with write_uow(engine) as session:
        _project(session, revision=7)
        _account(session, ACTOR_ID, "synthetic-suggestion-owner")
    with write_uow(engine) as session:
        result = _event(
            session,
            event_id=ARTIFACT_EVENT,
            event_type="artifact.version_registered",
            now=NOW,
        )
    assert result.created_job is False
    assert result.outcome is None
    with Session(engine) as session:
        assert _preference(session) is None
        assert _jobs(session) == []
        assert _triggers(session) == []
        assert _suggestion_outbox(session) == []
        assert _project_revision(session) == 7


@pytest.mark.integration
def test_save_inserts_manual_default_then_updates_without_project_revision(engine):
    with write_uow(engine) as session:
        _project(session, revision=7)
        _account(session, ACTOR_ID, "synthetic-suggestion-owner")
    with pytest.raises(SuggestionPreferenceConflict):
        with write_uow(engine) as session:
            _save(session, expected_revision=5, mode="event", scope=SCOPE, now=NOW)
    with Session(engine) as session:
        assert _preference(session) is None
        assert _project_revision(session) == 7

    statements: list[tuple[str, object]] = []

    def record(_conn, _cursor, statement, parameters, _context, _executemany) -> None:
        statements.append((statement, parameters))

    event.listen(engine, "before_cursor_execute", record)
    try:
        with write_uow(engine) as session:
            saved = _save(
                session,
                expected_revision=1,
                mode="event",
                scope=SCOPE,
                cooldown_seconds=30,
                digest_interval_seconds=3600,
                now=NOW,
            )
    finally:
        event.remove(engine, "before_cursor_execute", record)

    assert saved.revision == 2
    assert saved.mode == "event"
    writes = _preference_writes(statements)
    assert [kind for kind, _params in writes] == ["INSERT", "UPDATE"]
    inserted = _flat(writes[0][1])
    updated = _flat(writes[1][1])
    assert "manual" in inserted
    assert "event" not in inserted
    assert "{}" in inserted
    assert 0 in inserted
    assert 86400 in inserted
    assert 1 in inserted
    update_sql = next(
        sql for sql, _params in statements if sql.lstrip().upper().startswith("UPDATE")
    )
    assert "suggestion_preferences" in update_sql.lower()
    assert "revision" in update_sql.lower()
    assert "where" in update_sql.lower()
    assert "event" in updated
    assert SCOPE_TEXT in updated
    assert 30 in updated
    assert 3600 in updated
    assert 2 in updated
    assert 1 in updated
    assert not any(
        sql.lstrip().upper().startswith("UPDATE") and " projects" in f" {sql.lower()}"
        for sql, _params in statements
    )

    with Session(engine) as session:
        row = _preference(session)
        assert row is not None
        assert row.mode == "event"
        assert row.revision == 2
        assert row.cooldown_seconds == 30
        assert row.digest_interval_seconds == 3600
        assert row.scope_json == SCOPE_TEXT
        assert row.next_due_at is None
        assert row.updated_at == NOW
        assert _project_revision(session) == 7
        assert _jobs(session) == []

    with pytest.raises(SuggestionPreferenceConflict):
        with write_uow(engine) as session:
            _save(session, expected_revision=1, mode="off", now=NOW + 5)
    with Session(engine) as session:
        row = _preference(session)
        assert row is not None
        assert row.mode == "event"
        assert row.revision == 2
        assert row.updated_at == NOW
        assert row.cooldown_seconds == 30
        assert _project_revision(session) == 7

    with write_uow(engine) as session:
        again = _save(session, expected_revision=2, mode="event", cooldown_seconds=40, now=NOW + 9)
    assert again.revision == 3
    with Session(engine) as session:
        row = _preference(session)
        assert row is not None
        assert row.revision == 3
        assert row.cooldown_seconds == 40
        assert _project_revision(session) == 7


@pytest.mark.integration
def test_off_and_manual_create_no_suggestion_job(engine):
    with write_uow(engine) as session:
        _project(session, revision=7)
        _account(session, ACTOR_ID, "synthetic-suggestion-owner")
        _account(session, OTHER_ACTOR_ID, "synthetic-suggestion-other")
    with write_uow(engine) as session:
        _save(session, actor_id=ACTOR_ID, expected_revision=1, mode="off")
        _save(session, actor_id=OTHER_ACTOR_ID, expected_revision=1, mode="manual")
    with write_uow(engine) as session:
        for actor_id in (ACTOR_ID, OTHER_ACTOR_ID):
            event_result = _event(
                session,
                actor_id=actor_id,
                event_id=ARTIFACT_EVENT,
                event_type="artifact.version_registered",
                now=NOW + 1,
            )
            digest_result = poll_suggestion_digest(
                session,
                project_id=PROJECT_ID,
                actor_id=actor_id,
                now=NOW + 1,
            )
            assert event_result.created_job is False
            assert digest_result.created_job is False
    with Session(engine) as session:
        assert _jobs(session) == []
        assert _triggers(session) == []
        assert _suggestion_outbox(session) == []


@pytest.mark.integration
def test_event_mode_queues_once_and_a_new_session_sees_that_job(engine):
    with write_uow(engine) as session:
        _project(session, revision=7)
        _account(session, ACTOR_ID, "synthetic-suggestion-owner")
    with write_uow(engine) as session:
        _save(session, expected_revision=1, mode="event", cooldown_seconds=0)
    with write_uow(engine) as session:
        queued = _event(
            session,
            event_id=ARTIFACT_EVENT,
            event_type="artifact.version_registered",
            now=NOW + 1,
        )
    assert queued.created_job is True
    assert queued.outcome == "queued"
    assert queued.job_id is not None
    with write_uow(engine) as session:
        repeated = _event(
            session,
            event_id=ARTIFACT_EVENT,
            event_type="artifact.version_registered",
            now=NOW + 2,
        )
    assert repeated.created_job is False
    assert repeated.job_id == queued.job_id
    with write_uow(engine) as session:
        ignored = _event(
            session,
            event_id="synthetic-run-received",
            event_type="run.received",
            now=NOW + 3,
        )
    assert ignored.created_job is False
    assert ignored.outcome is None
    with Session(engine) as session:
        jobs = _jobs(session)
        triggers = _triggers(session)
        outbox = _suggestion_outbox(session)
        assert _project_revision(session) == 7
        assert [job.id for job in jobs] == [queued.job_id]
        assert jobs[0].status == "queued"
        assert jobs[0].job_kind == JOB_KIND
        assert jobs[0].lease_owner is None
        payload = json.loads(jobs[0].payload)
        assert set(payload) == {
            "project_id",
            "actor_id",
            "preference_revision",
            "trigger_kind",
            "trigger_key",
        }
        assert payload == {
            "project_id": PROJECT_ID,
            "actor_id": ACTOR_ID,
            "preference_revision": 2,
            "trigger_kind": "event",
            "trigger_key": ARTIFACT_EVENT,
        }
        assert jobs[0].input_hash == hashlib.sha256(
            jobs[0].payload.encode("utf-8")
        ).hexdigest()
        assert len(triggers) == 1
        assert triggers[0].outcome == "queued"
        assert triggers[0].job_id == queued.job_id
        assert triggers[0].trigger_key == ARTIFACT_EVENT
        assert len(outbox) == 1
        assert outbox[0].payload == payload


@pytest.mark.integration
def test_whitelist_events_queue_and_prefixed_events_do_not(engine):
    with write_uow(engine) as session:
        _project(session, revision=7)
        _account(session, ACTOR_ID, "synthetic-suggestion-owner")
    with write_uow(engine) as session:
        _save(session, expected_revision=1, mode="event", cooldown_seconds=0)
    queued: list[str] = []
    for event_type, event_id in (
        ("page.version_created", PAGE_EVENT),
        ("artifact.version_registered", ARTIFACT_EVENT),
        ("test.record_confirmed", "synthetic-test-event"),
    ):
        with write_uow(engine) as session:
            result = _event(session, event_id=event_id, event_type=event_type, now=NOW + 1)
        assert result.created_job is True
        assert result.job_id is not None
        queued.append(result.job_id)
    for event_type, event_id in (
        ("run.received", "synthetic-run-event"),
        ("candidate.ready", "synthetic-candidate-event"),
        ("stream.reset_required", "synthetic-stream-event"),
    ):
        with write_uow(engine) as session:
            result = _event(session, event_id=event_id, event_type=event_type, now=NOW + 2)
        assert result.created_job is False
        assert result.outcome is None
    with Session(engine) as session:
        assert sorted(job.id for job in _jobs(session)) == sorted(queued)
        assert len(_triggers(session)) == 3


@pytest.mark.integration
def test_different_event_inside_cooldown_is_suppressed(engine):
    with write_uow(engine) as session:
        _project(session, revision=7)
        _account(session, ACTOR_ID, "synthetic-suggestion-owner")
    with write_uow(engine) as session:
        _save(session, expected_revision=1, mode="event", cooldown_seconds=100)
    with write_uow(engine) as session:
        queued = _event(
            session,
            event_id=ARTIFACT_EVENT,
            event_type="artifact.version_registered",
            now=NOW + 10,
        )
    assert queued.created_job is True
    with write_uow(engine) as session:
        suppressed = _event(
            session,
            event_id=PAGE_EVENT,
            event_type="page.version_created",
            now=NOW + 50,
        )
    assert suppressed.created_job is False
    assert suppressed.outcome == "suppressed"
    assert suppressed.job_id is None
    with Session(engine) as session:
        jobs = _jobs(session)
        triggers = _triggers(session)
        row = _preference(session)
        assert row is not None
        assert row.next_due_at == NOW + 110
        assert [job.id for job in jobs] == [queued.job_id]
        assert sorted(item.outcome for item in triggers) == ["queued", "suppressed"]
        assert _suggestion_outbox(session)[0].payload["trigger_key"] == ARTIFACT_EVENT
        assert len(_suggestion_outbox(session)) == 1


@pytest.mark.integration
@pytest.mark.concurrency
def test_two_connections_queue_one_job_for_the_same_event(engine):
    with write_uow(engine) as session:
        _project(session, revision=7)
        _account(session, ACTOR_ID, "synthetic-suggestion-owner")
    with write_uow(engine) as session:
        _save(session, expected_revision=1, mode="event", cooldown_seconds=0)
    barrier = Barrier(2)
    results: list[SuggestionTriggerResult] = []
    errors: list[BaseException] = []

    def attempt() -> None:
        barrier.wait(timeout=5)
        try:
            with write_uow(engine) as session:
                results.append(
                    _event(
                        session,
                        event_id=ARTIFACT_EVENT,
                        event_type="artifact.version_registered",
                        now=NOW + 1,
                    )
                )
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
    assert sorted(item.created_job for item in results) == [False, True]
    assert {item.outcome for item in results} == {"queued"}
    with Session(engine) as session:
        jobs = _jobs(session)
        assert len(jobs) == 1
        assert len(_triggers(session)) == 1
        assert jobs[0].id == next(item.job_id for item in results if item.created_job)


@pytest.mark.integration
def test_late_digest_poll_queues_only_the_current_window(engine):
    with write_uow(engine) as session:
        _project(session, revision=7)
        _account(session, ACTOR_ID, "synthetic-suggestion-owner")
    with write_uow(engine) as session:
        _save(
            session,
            expected_revision=1,
            mode="digest",
            cooldown_seconds=0,
            digest_interval_seconds=100,
            now=0.0,
        )
    with write_uow(engine) as session:
        queued = poll_suggestion_digest(
            session,
            project_id=PROJECT_ID,
            actor_id=ACTOR_ID,
            now=250.0,
        )
    assert queued.created_job is True
    assert queued.trigger_key == "200"
    with write_uow(engine) as session:
        repeated = poll_suggestion_digest(
            session,
            project_id=PROJECT_ID,
            actor_id=ACTOR_ID,
            now=299.0,
        )
    assert repeated.created_job is False
    assert repeated.job_id == queued.job_id
    with Session(engine) as session:
        jobs = _jobs(session)
        triggers = _triggers(session)
        assert [job.id for job in jobs] == [queued.job_id]
        assert [item.trigger_key for item in triggers] == ["200"]
        assert "0" not in {item.trigger_key for item in triggers}
        assert "100" not in {item.trigger_key for item in triggers}
        payload = json.loads(jobs[0].payload)
        assert payload["trigger_kind"] == "digest"
        assert payload["trigger_key"] == "200"
        assert payload["preference_revision"] == 2
        assert len(_suggestion_outbox(session)) == 1


@pytest.mark.integration
def test_preference_checks_reject_illegal_rows(engine):
    with write_uow(engine) as session:
        _project(session, revision=7)
        _account(session, ACTOR_ID, "synthetic-suggestion-owner")
    illegal = (
        {"mode": "always"},
        {"revision": 0},
        {"cooldown_seconds": -1},
        {"digest_interval_seconds": 0},
        {"scope_json": "[]"},
    )
    for override in illegal:
        values = {
            "project_id": PROJECT_ID,
            "actor_id": ACTOR_ID,
            "mode": "manual",
            "scope_json": "{}",
            "cooldown_seconds": 0,
            "digest_interval_seconds": 86400,
            "revision": 1,
            "next_due_at": None,
            "updated_at": NOW,
        }
        values.update(override)
        with pytest.raises(IntegrityError):
            with write_uow(engine) as session:
                session.add(SuggestionPreference(**values))
    with Session(engine) as session:
        assert _preference(session) is None
