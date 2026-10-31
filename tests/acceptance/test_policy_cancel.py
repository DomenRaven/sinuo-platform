"""Preference revision cancels that actor's open policy runs."""

import pytest
from sqlalchemy import event, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from sinuo.domain.errors import StaleLease
from sinuo.infrastructure.database import write_uow
from sinuo.infrastructure.domain_models import Account, AgentRun, ContextSnapshot, Project
from sinuo.workers.runs import publish_run
from sinuo.workers.suggestions import save_suggestion_preference

NOW = 1_700_000_000.0
PROJECT_ID = "synthetic-policy-project"
ACTOR_ID = "11111111-1111-4111-8111-111111111111"
OTHER_ACTOR_ID = "22222222-2222-4222-8222-222222222222"
SNAPSHOT_ID = "synthetic-policy-snapshot"
POLICY_RUN_ID = "synthetic-policy-run"
MANUAL_RUN_ID = "synthetic-manual-run"
OTHER_RUN_ID = "synthetic-other-policy-run"
PASSWORD = "$argon2id$synthetic-hash"
RUN_COLUMNS = (
    "id, project_id, initiated_by_account_id, parent_run_id, supersedes_run_id, "
    "context_snapshot_id, execution_status, accepting_results, next_event_seq, "
    "budget_json, created_at, closed_at"
)


def _project(session: Session) -> None:
    session.add(
        Project(
            id=PROJECT_ID,
            title="synthetic-policy",
            brief=None,
            template_key=None,
            revision=7,
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


def _snapshot(session: Session) -> None:
    session.add(
        ContextSnapshot(
            id=SNAPSHOT_ID,
            project_id=PROJECT_ID,
            intent="synthetic policy cancel",
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


def _seed(session: Session) -> None:
    _project(session)
    _account(session, ACTOR_ID, "synthetic-policy-owner")
    _account(session, OTHER_ACTOR_ID, "synthetic-policy-other")
    session.flush()
    _snapshot(session)
    session.flush()


def _add_run(
    session: Session,
    *,
    run_id: str,
    actor_id: str,
    execution_status: str,
    accepting_results: int,
    next_event_seq: int,
    origin: str | None = None,
    preference_revision: int | None = None,
) -> None:
    values: dict[str, object] = {
        "id": run_id,
        "project_id": PROJECT_ID,
        "initiated_by_account_id": actor_id,
        "parent_run_id": None,
        "supersedes_run_id": None,
        "context_snapshot_id": SNAPSHOT_ID,
        "execution_status": execution_status,
        "accepting_results": accepting_results,
        "next_event_seq": next_event_seq,
        "budget_json": "{}",
        "created_at": NOW,
        "closed_at": None,
    }
    if origin is not None:
        values["origin"] = origin
        values["preference_revision"] = preference_revision
    session.add(AgentRun(**values))


def _stored(session: Session, run_id: str) -> dict[str, object]:
    run = session.get(AgentRun, run_id)
    assert run is not None
    return {column.key: getattr(run, column.key) for column in AgentRun.__table__.columns}


def _revision(session: Session) -> int:
    project = session.get(Project, PROJECT_ID)
    assert project is not None
    return project.revision


def _save(
    session: Session,
    *,
    expected_revision: int,
    mode: str = "event",
    cooldown_seconds: int = 0,
    now: float = NOW,
):
    return save_suggestion_preference(
        session,
        project_id=PROJECT_ID,
        actor_id=ACTOR_ID,
        expected_revision=expected_revision,
        mode=mode,
        scope={},
        cooldown_seconds=cooldown_seconds,
        digest_interval_seconds=86400,
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


def _updates_touching_run(statements: list[tuple[str, object]], run_id: str) -> list[str]:
    found = []
    for sql, params in statements:
        if not sql.lstrip().upper().startswith("UPDATE"):
            continue
        if "agent_runs" in sql.lower() or run_id in sql or run_id in _flat(params):
            found.append(sql)
    return found


@pytest.mark.integration
def test_named_insert_defaults_origin_to_manual(engine):
    with write_uow(engine) as session:
        _seed(session)
        session.flush()
        session.execute(
            text(
                f"""
                INSERT INTO agent_runs ({RUN_COLUMNS})
                VALUES (
                    'synthetic-default-run', :project_id, :actor_id, NULL, NULL,
                    :snapshot_id, 'queued', 1, 0, '{{}}', :now, NULL
                )
                """
            ),
            {
                "project_id": PROJECT_ID,
                "actor_id": ACTOR_ID,
                "snapshot_id": SNAPSHOT_ID,
                "now": NOW,
            },
        )
    with Session(engine) as session:
        row = _stored(session, "synthetic-default-run")
    assert row["origin"] == "manual"
    assert row["preference_revision"] is None
    assert row["execution_status"] == "queued"
    assert row["accepting_results"] == 1


@pytest.mark.integration
def test_origin_pair_rejects_manual_revision_and_policy_without_one(engine):
    with write_uow(engine) as session:
        _seed(session)
    with pytest.raises(IntegrityError):
        with write_uow(engine) as session:
            _add_run(
                session,
                run_id="synthetic-bad-policy",
                actor_id=ACTOR_ID,
                execution_status="queued",
                accepting_results=1,
                next_event_seq=0,
                origin="policy",
                preference_revision=None,
            )
    with pytest.raises(IntegrityError):
        with write_uow(engine) as session:
            _add_run(
                session,
                run_id="synthetic-bad-manual",
                actor_id=ACTOR_ID,
                execution_status="queued",
                accepting_results=1,
                next_event_seq=0,
                origin="manual",
                preference_revision=1,
            )
    with Session(engine) as session:
        assert session.get(AgentRun, "synthetic-bad-policy") is None
        assert session.get(AgentRun, "synthetic-bad-manual") is None


@pytest.mark.integration
def test_preference_save_cancels_that_actors_queued_policy_run(engine):
    with write_uow(engine) as session:
        _seed(session)
        _add_run(
            session,
            run_id=POLICY_RUN_ID,
            actor_id=ACTOR_ID,
            execution_status="queued",
            accepting_results=1,
            next_event_seq=4,
            origin="policy",
            preference_revision=1,
        )
        _add_run(
            session,
            run_id=MANUAL_RUN_ID,
            actor_id=ACTOR_ID,
            execution_status="queued",
            accepting_results=1,
            next_event_seq=5,
        )
        _add_run(
            session,
            run_id=OTHER_RUN_ID,
            actor_id=OTHER_ACTOR_ID,
            execution_status="queued",
            accepting_results=1,
            next_event_seq=6,
            origin="policy",
            preference_revision=1,
        )
    with Session(engine) as session:
        before_policy = _stored(session, POLICY_RUN_ID)
        before_manual = _stored(session, MANUAL_RUN_ID)
        before_other = _stored(session, OTHER_RUN_ID)
        assert _revision(session) == 7
    assert before_manual["origin"] == "manual"
    assert before_manual["preference_revision"] is None
    assert before_manual["execution_status"] == "queued"
    assert before_manual["accepting_results"] == 1
    assert before_other["execution_status"] == "queued"
    assert before_other["accepting_results"] == 1
    assert before_policy["origin"] == "policy"
    assert before_policy["preference_revision"] == 1

    with write_uow(engine) as session:
        saved = _save(session, expected_revision=1, mode="event", now=NOW)
    assert saved.revision == 2

    with Session(engine) as session:
        policy = _stored(session, POLICY_RUN_ID)
        assert policy["execution_status"] == "cancelled"
        assert policy["accepting_results"] == 0
        kept = {
            key: value
            for key, value in before_policy.items()
            if key not in {"execution_status", "accepting_results"}
        }
        assert {key: policy[key] for key in kept} == kept
        assert _stored(session, MANUAL_RUN_ID) == before_manual
        assert _stored(session, OTHER_RUN_ID) == before_other
        assert _revision(session) == 7

    with pytest.raises(StaleLease):
        with write_uow(engine) as session:
            publish_run(session, run_id=POLICY_RUN_ID)
    with Session(engine) as session:
        assert _stored(session, POLICY_RUN_ID) == policy
        assert _revision(session) == 7

    statements: list[tuple[str, object]] = []

    def record(_conn, _cursor, statement, parameters, _context, _executemany) -> None:
        statements.append((statement, parameters))

    event.listen(engine, "before_cursor_execute", record)
    try:
        with write_uow(engine) as session:
            again = _save(
                session,
                expected_revision=2,
                mode="event",
                cooldown_seconds=30,
                now=NOW + 5,
            )
    finally:
        event.remove(engine, "before_cursor_execute", record)

    assert again.revision == 3
    assert _updates_touching_run(statements, POLICY_RUN_ID) == []
    with Session(engine) as session:
        assert _stored(session, POLICY_RUN_ID) == policy
        assert _stored(session, MANUAL_RUN_ID) == before_manual
        assert _stored(session, OTHER_RUN_ID) == before_other
        assert _revision(session) == 7
