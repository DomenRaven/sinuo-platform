"""Acceptance for job claim, renew, finalize, cancel, finish, and event replay.

Claim tests do not open an event stream. Replay tests call replay_events.
HTTP SSE is mounted on create_app. Lease expiry is written into the past; nothing sleeps.
Budget reservation matches the stored budget_json text. Finish does not add retry_wait.
"""

import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from threading import Barrier

import pytest
from sqlalchemy import (
    Column,
    Float,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    UniqueConstraint,
    select,
    text,
    update,
)
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from sinuo.api.events import (
    HEARTBEAT_EVENT_TYPE,
    RESET_EVENT_TYPE,
    EventCursorRejected,
    StreamEvent,
    append_events,
    is_task_progress,
    replay_events,
)
from sinuo.domain.auth.errors import ActionForbidden, AuthRequired, NotFoundOrHidden
from sinuo.domain.auth.principal import Principal
from sinuo.domain.errors import StaleLease
from sinuo.infrastructure.database import write_uow
from sinuo.infrastructure.domain_models import (
    Account,
    AgentRun,
    ConsumerReceipt,
    ContextSnapshot,
    Job,
    Membership,
    ObjectGrant,
    Project,
    RunEvent,
)
from sinuo.infrastructure.models import OutboxEvent
from sinuo.workers.budget import BudgetConflict, reserve_model_call, settle_model_call
from sinuo.workers.jobs import (
    ConsumerEffectConflict,
    JobAttempt,
    JobAttemptFinish,
    JobCancellation,
    JobPublication,
    cancel_job,
    claim_job,
    finalize_job,
    finish_job_attempt,
    renew_job,
)

PROJECT_ID = "project-durable"
JOB_ID = "job-durable"
EVENT_ID = "event-durable"
INPUT_HASH = hashlib.sha256(b"durable-job-input").hexdigest()
OTHER_HASH = hashlib.sha256(b"other-durable-input").hexdigest()
OUTPUT_REF = "derivative-result-a"
OTHER_REF = "derivative-result-b"
CONSUMER = "derivative"
EFFECT = "registered"
CREATED_AT = 1_700_000_000.0
TTL = 60.0


@dataclass(frozen=True)
class SeenJob:
    status: str
    attempt_no: int
    fencing_token: int
    lease_owner: str | None
    lease_until: float | None
    input_hash: str
    output_ref: str | None
    last_error: str | None


@dataclass(frozen=True)
class SeenReceipt:
    event_id: str
    consumer_name: str
    project_id: str
    effect_kind: str
    effect_ref: str


def insert_job(
    engine,
    *,
    job_id: str = JOB_ID,
    event_id: str | None = EVENT_ID,
    available_at: float = 0.0,
    input_hash: str = INPUT_HASH,
) -> None:
    with write_uow(engine) as session:
        if session.get(Project, PROJECT_ID) is None:
            session.add(
                Project(
                    id=PROJECT_ID,
                    title="Durable jobs",
                    brief=None,
                    template_key=None,
                    revision=1,
                    access_epoch=1,
                    created_at=CREATED_AT,
                )
            )
        if event_id is not None and session.get(OutboxEvent, event_id) is None:
            session.add(
                OutboxEvent(
                    event_id=event_id,
                    project_id=PROJECT_ID,
                    event_type="artifact.version_registered",
                    schema_version=1,
                    payload={
                        "project_id": PROJECT_ID,
                        "artifact_id": "artifact-1",
                        "artifact_version_id": "artifact-version-1",
                        "content_hash": input_hash,
                        "generation_id": "generation-1",
                        "byte_size": 4,
                    },
                    created_at=CREATED_AT,
                )
            )
        session.flush()
        session.add(
            Job(
                id=job_id,
                project_id=PROJECT_ID,
                job_kind="derivative",
                status="queued",
                available_at=available_at,
                attempt_no=0,
                lease_owner=None,
                lease_until=None,
                fencing_token=0,
                input_hash=input_hash,
                output_ref=None,
                last_error=None,
                subject_type="artifact",
                subject_id="artifact-1",
                source_event_id=event_id,
                payload="{}",
                created_at=CREATED_AT,
            )
        )


def fetch_job(engine, job_id: str = JOB_ID) -> SeenJob:
    with Session(engine) as session:
        row = session.get(Job, job_id)
        assert row is not None
        return SeenJob(
            row.status,
            row.attempt_no,
            row.fencing_token,
            row.lease_owner,
            row.lease_until,
            row.input_hash,
            row.output_ref,
            row.last_error,
        )


def fetch_receipts(engine) -> list[SeenReceipt]:
    with Session(engine) as session:
        rows = session.scalars(select(ConsumerReceipt)).all()
        return [
            SeenReceipt(
                row.event_id,
                row.consumer_name,
                row.project_id,
                row.effect_kind,
                row.effect_ref,
            )
            for row in rows
        ]


def claim(
    engine,
    *,
    owner: str = "worker-1",
    job_id: str = JOB_ID,
    input_hash: str = INPUT_HASH,
    ttl_seconds: float = TTL,
) -> JobAttempt:
    with write_uow(engine) as session:
        return claim_job(
            session,
            job_id=job_id,
            owner=owner,
            input_hash=input_hash,
            ttl_seconds=ttl_seconds,
        )


def renew(
    engine,
    *,
    owner: str,
    fencing_token: int,
    job_id: str = JOB_ID,
    input_hash: str = INPUT_HASH,
    ttl_seconds: float = TTL,
) -> JobAttempt:
    with write_uow(engine) as session:
        return renew_job(
            session,
            job_id=job_id,
            owner=owner,
            fencing_token=fencing_token,
            input_hash=input_hash,
            ttl_seconds=ttl_seconds,
        )


def finalize(
    engine,
    *,
    owner: str,
    fencing_token: int,
    job_id: str = JOB_ID,
    input_hash: str = INPUT_HASH,
    output_ref: str = OUTPUT_REF,
):
    with write_uow(engine) as session:
        return finalize_job(
            session,
            job_id=job_id,
            owner=owner,
            fencing_token=fencing_token,
            input_hash=input_hash,
            output_ref=output_ref,
            consumer_name=CONSUMER,
            effect_kind=EFFECT,
        )


def expire_lease(engine, job_id: str = JOB_ID) -> None:
    """Move the deadline into the past without sleeping."""
    with write_uow(engine) as session:
        changed = session.execute(
            update(Job)
            .where(Job.id == job_id, Job.status == "running")
            .values(lease_until=1.0)
            .execution_options(synchronize_session=False)
        )
        assert changed.rowcount == 1


def jobs_trigger_sql(engine) -> str:
    with engine.connect() as connection:
        statement = connection.execute(
            text(
                "SELECT sql FROM sqlite_master "
                "WHERE type = 'trigger' AND name = 'jobs_before_update'"
            )
        ).scalar_one()
    assert isinstance(statement, str)
    return statement


def insert_receipt(
    engine,
    *,
    event_id: str = EVENT_ID,
    effect_ref: str = OUTPUT_REF,
) -> None:
    with write_uow(engine) as session:
        session.add(
            ConsumerReceipt(
                event_id=event_id,
                consumer_name=CONSUMER,
                project_id=PROJECT_ID,
                effect_kind=EFFECT,
                effect_ref=effect_ref,
                created_at=CREATED_AT,
            )
        )


@pytest.mark.integration
def test_claim_queued_job_bumps_fence_and_attempt(engine):
    insert_job(engine)
    attempt = claim(engine, owner="worker-1")
    job = fetch_job(engine)
    assert attempt.fencing_token == 1
    assert attempt.attempt_no == 1
    assert attempt.owner == "worker-1"
    assert job.status == "running"
    assert job.fencing_token == 1
    assert job.attempt_no == 1
    assert job.lease_owner == "worker-1"
    assert job.input_hash == INPUT_HASH
    assert job.lease_until == attempt.lease_until
    assert 1_600_000_000 < attempt.lease_until < 4_000_000_000


@pytest.mark.integration
def test_loaded_job_keeps_claim_through_commit(engine):
    insert_job(engine)
    with write_uow(engine) as session:
        loaded = session.get(Job, JOB_ID)
        assert loaded is not None
        loaded.last_error = "stale local edit"
        attempt = claim_job(
            session,
            job_id=JOB_ID,
            owner="worker-1",
            input_hash=INPUT_HASH,
            ttl_seconds=TTL,
        )
        assert loaded.status == "running"
        assert loaded.fencing_token == attempt.fencing_token == 1
        assert loaded.lease_owner == "worker-1"
        assert loaded.last_error is None
    job = fetch_job(engine)
    assert job.status == "running"
    assert job.fencing_token == 1
    assert job.attempt_no == 1
    assert job.last_error is None


@pytest.mark.integration
def test_claim_fails_unless_one_row_changes(engine):
    insert_job(engine, job_id="job-hash")
    with pytest.raises(StaleLease, match="job-hash"):
        claim(engine, job_id="job-hash", input_hash=OTHER_HASH)
    missed = fetch_job(engine, "job-hash")
    assert missed.status == "queued"
    assert missed.fencing_token == 0
    assert missed.attempt_no == 0
    assert missed.lease_owner is None

    insert_job(engine, job_id="job-future", available_at=1e12)
    with pytest.raises(StaleLease, match="job-future"):
        claim(engine, job_id="job-future")
    future = fetch_job(engine, "job-future")
    assert future.status == "queued"
    assert future.fencing_token == 0
    assert future.attempt_no == 0

    insert_job(engine, job_id="job-live")
    held = claim(engine, job_id="job-live", owner="worker-1")
    with pytest.raises(StaleLease, match="job-live"):
        claim(engine, job_id="job-live", owner="worker-2")
    live = fetch_job(engine, "job-live")
    assert live.status == "running"
    assert live.lease_owner == "worker-1"
    assert live.fencing_token == held.fencing_token == 1
    assert live.attempt_no == 1

    with pytest.raises(StaleLease, match="job-missing"):
        claim(engine, job_id="job-missing")


@pytest.mark.integration
@pytest.mark.recovery
def test_expired_running_job_can_be_claimed(engine):
    insert_job(engine)
    first = claim(engine, owner="worker-1")
    expire_lease(engine)
    with pytest.raises(StaleLease):
        claim(engine, owner="worker-2", input_hash=OTHER_HASH)
    unchanged = fetch_job(engine)
    assert unchanged.status == "running"
    assert unchanged.lease_owner == "worker-1"
    assert unchanged.fencing_token == first.fencing_token
    assert unchanged.attempt_no == first.attempt_no
    assert unchanged.lease_until == 1.0
    second = claim(engine, owner="worker-2")
    assert second.fencing_token == first.fencing_token + 1
    assert second.attempt_no == first.attempt_no + 1
    job = fetch_job(engine)
    assert job.status == "running"
    assert job.lease_owner == "worker-2"
    assert job.fencing_token == second.fencing_token
    assert job.attempt_no == second.attempt_no
    assert job.lease_until == second.lease_until
    assert job.lease_until > 1.0


@pytest.mark.integration
@pytest.mark.concurrency
def test_two_connections_only_one_claim_succeeds(engine):
    insert_job(engine)
    start = Barrier(2)

    def run(owner: str) -> JobAttempt:
        start.wait(timeout=5)
        with write_uow(engine) as session:
            return claim_job(
                session,
                job_id=JOB_ID,
                owner=owner,
                input_hash=INPUT_HASH,
                ttl_seconds=TTL,
            )

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(run, "worker-1"), pool.submit(run, "worker-2")]
        outcomes: list[JobAttempt | None] = []
        for future in futures:
            try:
                outcomes.append(future.result(timeout=10))
            except StaleLease:
                outcomes.append(None)
    won = [item for item in outcomes if item is not None]
    assert len(outcomes) == 2
    assert len(won) == 1
    job = fetch_job(engine)
    assert job.status == "running"
    assert job.fencing_token == 1
    assert job.attempt_no == 1
    assert job.lease_owner == won[0].owner
    assert won[0].fencing_token == 1
    assert won[0].attempt_no == 1


@pytest.mark.integration
def test_renew_only_current_owner_and_token(engine):
    insert_job(engine)
    first = claim(engine, owner="worker-1")
    with pytest.raises(StaleLease):
        renew(engine, owner="worker-2", fencing_token=first.fencing_token)
    with pytest.raises(StaleLease):
        renew(engine, owner="worker-1", fencing_token=first.fencing_token + 1)
    with pytest.raises(StaleLease):
        renew(engine, owner="worker-1", fencing_token=first.fencing_token, input_hash=OTHER_HASH)
    held = fetch_job(engine)
    assert held.lease_until == first.lease_until
    assert held.fencing_token == first.fencing_token
    assert held.attempt_no == first.attempt_no
    assert held.lease_owner == "worker-1"
    renewed = renew(
        engine,
        owner="worker-1",
        fencing_token=first.fencing_token,
        ttl_seconds=TTL + 30,
    )
    assert renewed.fencing_token == first.fencing_token
    assert renewed.attempt_no == first.attempt_no
    assert renewed.lease_until > first.lease_until
    expire_lease(engine)
    with pytest.raises(StaleLease):
        renew(engine, owner="worker-1", fencing_token=first.fencing_token)
    expired = fetch_job(engine)
    assert expired.status == "running"
    assert expired.lease_owner == "worker-1"
    assert expired.lease_until == 1.0
    assert expired.fencing_token == first.fencing_token
    assert expired.attempt_no == first.attempt_no


@pytest.mark.integration
@pytest.mark.recovery
def test_finalize_requires_live_lease_and_writes_one_receipt(engine):
    insert_job(engine)
    first = claim(engine, owner="worker-1")
    expire_lease(engine)
    with pytest.raises(StaleLease):
        finalize(engine, owner="worker-1", fencing_token=first.fencing_token)
    assert fetch_receipts(engine) == []
    assert fetch_job(engine).status == "running"
    second = claim(engine, owner="worker-2")
    with pytest.raises(StaleLease):
        renew(engine, owner="worker-1", fencing_token=first.fencing_token)
    with pytest.raises(StaleLease):
        finalize(engine, owner="worker-1", fencing_token=first.fencing_token)
    with pytest.raises(StaleLease):
        finalize(engine, owner="worker-1", fencing_token=second.fencing_token)
    with pytest.raises(StaleLease):
        finalize(
            engine,
            owner="worker-2",
            fencing_token=second.fencing_token,
            input_hash=OTHER_HASH,
        )
    still = fetch_job(engine)
    assert still.status == "running"
    assert still.output_ref is None
    assert still.lease_owner == "worker-2"
    assert still.fencing_token == second.fencing_token
    assert fetch_receipts(engine) == []
    published = finalize(engine, owner="worker-2", fencing_token=second.fencing_token)
    job = fetch_job(engine)
    assert published.consumer_event_id == EVENT_ID
    assert published.output_ref == OUTPUT_REF
    assert published.fencing_token == second.fencing_token
    assert job.status == "succeeded"
    assert job.output_ref == OUTPUT_REF
    assert job.lease_owner is None
    assert job.lease_until is None
    assert job.fencing_token == second.fencing_token
    assert job.attempt_no == second.attempt_no
    assert job.input_hash == INPUT_HASH
    assert job.last_error is None
    receipts = fetch_receipts(engine)
    assert receipts == [SeenReceipt(EVENT_ID, CONSUMER, PROJECT_ID, EFFECT, OUTPUT_REF)]
    with pytest.raises(StaleLease):
        finalize(engine, owner="worker-2", fencing_token=second.fencing_token)
    with pytest.raises(StaleLease):
        finalize(engine, owner="worker-1", fencing_token=first.fencing_token)
    assert fetch_job(engine).status == "succeeded"
    assert fetch_job(engine).fencing_token == second.fencing_token
    assert fetch_receipts(engine) == receipts


@pytest.mark.integration
def test_finalize_without_source_event_stores_output_ref(engine):
    insert_job(engine, job_id="job-plain", event_id=None)
    attempt = claim(engine, job_id="job-plain")
    published = finalize(
        engine,
        owner="worker-1",
        fencing_token=attempt.fencing_token,
        job_id="job-plain",
    )
    job = fetch_job(engine, "job-plain")
    assert published.consumer_event_id is None
    assert job.status == "succeeded"
    assert job.output_ref == OUTPUT_REF
    assert job.lease_owner is None
    assert fetch_receipts(engine) == []


@pytest.mark.integration
@pytest.mark.recovery
def test_same_consumer_effect_is_reused(engine):
    insert_job(engine)
    attempt = claim(engine)
    insert_receipt(engine, effect_ref=OUTPUT_REF)
    published = finalize(engine, owner="worker-1", fencing_token=attempt.fencing_token)
    assert published.consumer_event_id == EVENT_ID
    assert fetch_job(engine).status == "succeeded"
    assert fetch_job(engine).output_ref == OUTPUT_REF
    assert fetch_receipts(engine) == [
        SeenReceipt(EVENT_ID, CONSUMER, PROJECT_ID, EFFECT, OUTPUT_REF)
    ]


@pytest.mark.integration
@pytest.mark.recovery
def test_different_consumer_effect_rolls_back_finalize(engine):
    insert_job(engine)
    attempt = claim(engine)
    insert_receipt(engine, effect_ref=OTHER_REF)
    with pytest.raises(ConsumerEffectConflict, match=CONSUMER):
        finalize(engine, owner="worker-1", fencing_token=attempt.fencing_token)
    job = fetch_job(engine)
    assert job.status == "running"
    assert job.output_ref is None
    assert job.lease_owner == "worker-1"
    assert job.fencing_token == attempt.fencing_token
    assert job.lease_until == attempt.lease_until
    assert fetch_receipts(engine) == [
        SeenReceipt(EVENT_ID, CONSUMER, PROJECT_ID, EFFECT, OTHER_REF)
    ]


@pytest.mark.integration
def test_terminal_job_trigger_rejects_return_to_running(engine):
    insert_job(engine)
    attempt = claim(engine)
    finalize(engine, owner="worker-1", fencing_token=attempt.fencing_token)
    before = jobs_trigger_sql(engine)
    assert "jobs_terminal" in before
    assert "DROP TRIGGER" not in before
    with pytest.raises(StaleLease):
        claim(engine, owner="worker-2")
    with pytest.raises(IntegrityError) as raised:
        with write_uow(engine) as session:
            session.execute(
                update(Job)
                .where(Job.id == JOB_ID)
                .values(
                    status="running",
                    lease_owner="stale-worker",
                    lease_until=4_000_000_000.0,
                    fencing_token=Job.fencing_token + 1,
                )
                .execution_options(synchronize_session=False)
            )
    detail = f"{raised.value} {getattr(raised.value, 'orig', '')}"
    assert "jobs_terminal" in detail
    after = jobs_trigger_sql(engine)
    assert after == before
    job = fetch_job(engine)
    assert job.status == "succeeded"
    assert job.lease_owner is None
    assert job.lease_until is None
    assert job.fencing_token == attempt.fencing_token
    assert job.attempt_no == attempt.attempt_no
    assert job.output_ref == OUTPUT_REF


SSE_ACCOUNT = "11111111-1111-4111-8111-111111111111"
SSE_OTHER = "22222222-2222-4222-8222-222222222222"
SSE_PROJECT = "project-sse"
SSE_OTHER_PROJECT = "project-sse-other"
SSE_RUN = "run-sse"
SSE_OTHER_RUN = "run-sse-other"
SSE_FOREIGN_RUN = "run-sse-foreign"
SSE_MEMBER = "membership-sse"
SSE_GRANT = "grant-sse-read"
SSE_SESSION = "sse-session"
LOCAL_BODY = "synthetic-local-event-body"
FOREIGN_BODY = "synthetic-foreign-event-body"
OTHER_RUN_BODY = "synthetic-other-run-event-body"
HEARTBEAT_BODY = "synthetic-heartbeat-body"
TASK_BODY = "synthetic-task-started"
STREAM_KINDS = ("table", "outbox", "run")


def reader() -> Principal:
    return Principal(SSE_ACCOUNT, SSE_SESSION, 1)


def stream_event(
    event_id: str,
    seq: int,
    event_type: str,
    payload: dict[str, str],
    *,
    project_id: str = SSE_PROJECT,
    run_id: str = SSE_RUN,
) -> StreamEvent:
    return StreamEvent(
        event_id=event_id,
        seq=seq,
        event_type=event_type,
        project_id=project_id,
        run_id=run_id,
        payload=payload,
        trace_id="trace-sse",
        created_at=CREATED_AT + seq,
    )


def stream_source(engine, kind: str) -> Table | type:
    if kind == "table":
        table = Table(
            "synthetic_stream_events",
            MetaData(),
            Column("event_id", String(64), primary_key=True),
            Column("project_id", String(64), nullable=False),
            Column("run_id", String(64), nullable=False),
            Column("seq", Integer, nullable=False),
            Column("event_type", String(100), nullable=False),
            Column("schema_version", Integer, nullable=False),
            Column("task_id", String(64)),
            Column("trace_id", String(64), nullable=False),
            Column("payload", Text, nullable=False),
            Column("created_at", Float, nullable=False),
            UniqueConstraint("run_id", "seq", name="uq_synthetic_stream_seq"),
        )
        table.create(engine)
        return table
    if kind == "outbox":
        return OutboxEvent
    if kind == "run":
        return RunEvent
    raise AssertionError(kind)


def ensure_project(session: Session, project_id: str) -> None:
    if session.get(Project, project_id) is None:
        session.add(
            Project(
                id=project_id,
                title="synthetic sse",
                brief=None,
                template_key=None,
                revision=1,
                access_epoch=1,
                created_at=CREATED_AT,
            )
        )
        session.flush()


def ensure_account(session: Session, account_id: str, login_name: str) -> None:
    if session.get(Account, account_id) is not None:
        return
    session.add(
        Account(
            id=account_id,
            login_name=login_name,
            display_name=login_name,
            password_hash="$argon2id$synthetic-hash",
            status="active",
            auth_epoch=1,
            can_create_project=0,
            created_at=CREATED_AT,
            disabled_at=None,
        )
    )
    session.flush()


def insert_run(session: Session, project_id: str, run_id: str, snapshot_id: str) -> None:
    ensure_project(session, project_id)
    session.add(
        ContextSnapshot(
            id=snapshot_id,
            project_id=project_id,
            intent="synthetic replay",
            template_version="synthetic-1",
            method_card_version="synthetic-1",
            authorization_scope_json="{}",
            access_epoch=1,
            exclusions_json="[]",
            index_revision=None,
            budget_json="{}",
            draft_page_id=None,
            draft_content_revision=None,
            draft_content_hash=None,
            draft_content_json=None,
            created_at=CREATED_AT,
        )
    )
    session.flush()
    session.add(
        AgentRun(
            id=run_id,
            project_id=project_id,
            initiated_by_account_id=SSE_ACCOUNT,
            parent_run_id=None,
            supersedes_run_id=None,
            context_snapshot_id=snapshot_id,
            execution_status="queued",
            accepting_results=1,
            next_event_seq=0,
            budget_json="{}",
            created_at=CREATED_AT,
            closed_at=None,
        )
    )


def seed_reader(session: Session, *, grant: bool) -> None:
    ensure_account(session, SSE_ACCOUNT, "synthetic-sse-reader")
    ensure_project(session, SSE_PROJECT)
    if session.get(Membership, SSE_MEMBER) is None:
        session.add(
            Membership(
                id=SSE_MEMBER,
                project_id=SSE_PROJECT,
                account_id=SSE_ACCOUNT,
                status="active",
                role_label=None,
                created_at=CREATED_AT,
                revoked_at=None,
            )
        )
    if grant and session.get(ObjectGrant, SSE_GRANT) is None:
        session.add(read_grant(SSE_GRANT))


def read_grant(grant_id: str) -> ObjectGrant:
    return ObjectGrant(
        id=grant_id,
        project_id=SSE_PROJECT,
        account_id=SSE_ACCOUNT,
        action="run.read",
        scope_type="project",
        status="active",
        created_at=CREATED_AT,
        revoked_at=None,
        created_by_account_id=SSE_ACCOUNT,
        stage_id=None,
        page_id=None,
        page_version_id=None,
        artifact_id=None,
        artifact_version_id=None,
        evidence_anchor_id=None,
    )


def open_stream(engine, kind: str, *, grant: bool) -> Table | type:
    source = stream_source(engine, kind)
    with write_uow(engine) as session:
        seed_reader(session, grant=grant)
        if kind == "run":
            insert_run(session, SSE_PROJECT, SSE_RUN, "snapshot-sse")
    return source


def write_events(
    engine,
    source,
    events: list[StreamEvent],
    *,
    runs: tuple[tuple[str, str, str], ...] = (),
) -> None:
    with write_uow(engine) as session:
        for project_id, run_id, snapshot_id in runs:
            insert_run(session, project_id, run_id, snapshot_id)
        append_events(session, source, events)


def play(engine, source, principal: Principal, last_event_id: str | None):
    with Session(engine) as session:
        return replay_events(
            session,
            source,
            principal=principal,
            project_id=SSE_PROJECT,
            run_id=SSE_RUN,
            last_event_id=last_event_id,
        )


def source_table(source: Table | type) -> Table:
    if isinstance(source, Table):
        return source
    table = source.__table__
    assert isinstance(table, Table)
    return table


def event_count(engine, source) -> int:
    table = source_table(source)
    with Session(engine) as session:
        return len(session.execute(select(table.c.event_id)).all())


def stored_types(engine, source) -> set[str]:
    table = source_table(source)
    with Session(engine) as session:
        return set(session.execute(select(table.c.event_type)).scalars())


def payload_text(engine, source) -> str:
    table = source_table(source)
    with Session(engine) as session:
        rows = session.execute(select(table.c.payload)).scalars().all()
    return repr(rows)


def rendered_error(error: BaseException) -> str:
    return " ".join(
        [
            type(error).__name__,
            str(error),
            repr(error),
            repr(error.args),
            repr(getattr(error, "__dict__", {})),
            repr(error.__cause__),
            repr(error.__context__),
            repr(getattr(error, "action", None)),
        ]
    )


def assert_secrets_absent(blob: object, *secrets: str) -> None:
    rendered = blob if isinstance(blob, str) else repr(blob)
    for secret in secrets:
        assert secret not in rendered


def assert_runs_still_queued(engine) -> None:
    with Session(engine) as session:
        runs = session.scalars(select(AgentRun)).all()
        assert runs
        assert {row.execution_status for row in runs} == {"queued"}
        assert {row.next_event_seq for row in runs} == {0}
        assert {row.closed_at for row in runs} == {None}


@pytest.mark.integration
@pytest.mark.parametrize("kind", STREAM_KINDS)
def test_replay_returns_later_events_in_seq_order(engine, kind):
    source = open_stream(engine, kind, grant=True)
    write_events(
        engine,
        source,
        [
            stream_event("sse-4", 4, "task.started", {"note": TASK_BODY}),
            stream_event(
                "sse-2",
                2,
                HEARTBEAT_EVENT_TYPE,
                {"note": HEARTBEAT_BODY, "status": "completed"},
            ),
            stream_event("sse-1", 1, "run.received", {"note": LOCAL_BODY}),
        ],
    )
    before = event_count(engine, source)
    assert is_task_progress(HEARTBEAT_EVENT_TYPE) is False
    assert is_task_progress(RESET_EVENT_TYPE) is False
    assert is_task_progress("run.received") is True
    first = play(engine, source, reader(), None)
    assert [event.seq for event in first.events] == [1, 2, 4]
    assert [event.event_id for event in first.events] == ["sse-1", "sse-2", "sse-4"]
    assert first.reset_required is False
    assert [event.event_id for event in first.progress_events] == ["sse-1", "sse-4"]
    assert first.events[0].payload == {"note": LOCAL_BODY}
    assert first.events[1].event_type == HEARTBEAT_EVENT_TYPE
    assert first.events[1].progress is False
    assert first.events[1].payload["status"] == "completed"
    assert first.events[1].payload["note"] == HEARTBEAT_BODY
    assert "run.completed" not in [event.event_type for event in first.events]
    again = play(engine, source, reader(), None)
    assert again == first
    assert event_count(engine, source) == before
    after_first = play(engine, source, reader(), "sse-1")
    assert [event.event_id for event in after_first.events] == ["sse-2", "sse-4"]
    assert [event.event_id for event in after_first.progress_events] == ["sse-4"]
    tail = play(engine, source, reader(), "sse-4")
    assert tail.events == ()
    assert tail.reset_required is False
    reset = play(engine, source, reader(), "missing-sse-cursor")
    assert reset.reset_required is True
    assert [event.event_type for event in reset.events] == [RESET_EVENT_TYPE]
    assert reset.events[0].payload == {}
    assert reset.events[0].progress is False
    assert reset.progress_events == ()
    assert_secrets_absent(reset, LOCAL_BODY, HEARTBEAT_BODY, TASK_BODY)
    with pytest.raises(EventCursorRejected) as blank:
        play(engine, source, reader(), "")
    assert_secrets_absent(rendered_error(blank.value), LOCAL_BODY, HEARTBEAT_BODY, TASK_BODY)
    with pytest.raises(EventCursorRejected) as padded:
        play(engine, source, reader(), " sse-1")
    assert_secrets_absent(rendered_error(padded.value), LOCAL_BODY, HEARTBEAT_BODY, TASK_BODY)
    assert event_count(engine, source) == before
    assert "run.completed" not in stored_types(engine, source)
    if kind == "run":
        assert_runs_still_queued(engine)
    else:
        with Session(engine) as session:
            assert session.scalars(select(AgentRun)).all() == []


@pytest.mark.integration
@pytest.mark.parametrize("kind", STREAM_KINDS)
def test_foreign_last_event_id_does_not_return_other_body(engine, kind):
    source = open_stream(engine, kind, grant=True)
    extra = ()
    if kind == "run":
        extra = (
            (SSE_PROJECT, SSE_OTHER_RUN, "snapshot-sse-other"),
            (SSE_OTHER_PROJECT, SSE_FOREIGN_RUN, "snapshot-sse-foreign"),
        )
    write_events(
        engine,
        source,
        [
            stream_event("sse-local", 1, "run.received", {"note": LOCAL_BODY}),
            stream_event(
                "sse-other-run",
                1,
                "task.started",
                {"note": OTHER_RUN_BODY},
                run_id=SSE_OTHER_RUN,
            ),
            stream_event(
                "sse-foreign",
                1,
                "task.started",
                {"note": FOREIGN_BODY},
                project_id=SSE_OTHER_PROJECT,
                run_id=SSE_FOREIGN_RUN,
            ),
        ],
        runs=extra,
    )
    visible = play(engine, source, reader(), None)
    assert [event.event_id for event in visible.events] == ["sse-local"]
    assert visible.events[0].payload == {"note": LOCAL_BODY}
    assert_secrets_absent(visible, FOREIGN_BODY, OTHER_RUN_BODY)
    before = event_count(engine, source)
    with pytest.raises(EventCursorRejected) as foreign:
        play(engine, source, reader(), "sse-foreign")
    assert_secrets_absent(rendered_error(foreign.value), FOREIGN_BODY, OTHER_RUN_BODY, LOCAL_BODY)
    with pytest.raises(EventCursorRejected) as other_run:
        play(engine, source, reader(), "sse-other-run")
    assert_secrets_absent(rendered_error(other_run.value), FOREIGN_BODY, OTHER_RUN_BODY, LOCAL_BODY)
    with write_uow(engine) as session:
        ensure_account(session, SSE_OTHER, "synthetic-sse-other")
    outsider = Principal(SSE_OTHER, "sse-session-other", 1)
    with pytest.raises(NotFoundOrHidden) as hidden:
        play(engine, source, outsider, "sse-foreign")
    assert_secrets_absent(rendered_error(hidden.value), FOREIGN_BODY, OTHER_RUN_BODY, LOCAL_BODY)
    assert event_count(engine, source) == before
    assert LOCAL_BODY in payload_text(engine, source)
    assert FOREIGN_BODY in payload_text(engine, source)
    assert "run.completed" not in stored_types(engine, source)
    if kind == "run":
        assert_runs_still_queued(engine)


@pytest.mark.integration
@pytest.mark.parametrize("kind", STREAM_KINDS)
def test_replay_checks_current_permission_before_each_read(engine, kind):
    source = open_stream(engine, kind, grant=False)
    write_events(
        engine,
        source,
        [stream_event("sse-local", 1, "run.received", {"note": LOCAL_BODY})],
    )
    with pytest.raises(ActionForbidden) as forbidden:
        play(engine, source, reader(), None)
    assert_secrets_absent(rendered_error(forbidden.value), LOCAL_BODY)
    with write_uow(engine) as session:
        session.add(read_grant(SSE_GRANT))
    stale = Principal(SSE_ACCOUNT, SSE_SESSION, 2)
    with pytest.raises(AuthRequired) as expired:
        play(engine, source, stale, "sse-local")
    assert_secrets_absent(rendered_error(expired.value), LOCAL_BODY)
    allowed = play(engine, source, reader(), None)
    assert allowed.events[0].payload == {"note": LOCAL_BODY}
    assert allowed.progress_events[0].event_type == "run.received"
    with write_uow(engine) as session:
        grant = session.get(ObjectGrant, SSE_GRANT)
        assert grant is not None
        grant.status = "revoked"
        grant.revoked_at = CREATED_AT + 1
    with pytest.raises(ActionForbidden) as revoked_grant:
        play(engine, source, reader(), "sse-local")
    assert_secrets_absent(rendered_error(revoked_grant.value), LOCAL_BODY)
    with write_uow(engine) as session:
        membership = session.get(Membership, SSE_MEMBER)
        assert membership is not None
        membership.status = "revoked"
        membership.revoked_at = CREATED_AT + 1
    with pytest.raises(NotFoundOrHidden) as revoked_member:
        play(engine, source, reader(), "sse-local")
    assert_secrets_absent(rendered_error(revoked_member.value), LOCAL_BODY)
    with pytest.raises(NotFoundOrHidden) as missing:
        play(engine, source, reader(), "missing-sse-cursor")
    assert_secrets_absent(rendered_error(missing.value), LOCAL_BODY)
    assert LOCAL_BODY in payload_text(engine, source)
    assert "run.completed" not in stored_types(engine, source)
    if kind == "run":
        assert_runs_still_queued(engine)


SEQ_BODY = "synthetic-seq-body"
SEQ_RUN_A = "run-seq-a"
SEQ_RUN_B = "run-seq-b"


def take_run_seq(
    session: Session,
    *,
    run_id: str,
    expected_seq: int,
    event_id: str,
    event_type: str = "run.received",
    payload: dict[str, str] | None = None,
) -> int:
    from sinuo.api.events.sequence import allocate_run_event

    return allocate_run_event(
        session,
        project_id=SSE_PROJECT,
        run_id=run_id,
        expected_seq=expected_seq,
        event_id=event_id,
        event_type=event_type,
        payload={"note": SEQ_BODY} if payload is None else payload,
        trace_id="trace-seq",
        created_at=CREATED_AT + expected_seq,
    )


def run_seq_view(engine, run_id: str) -> tuple[int, list[int], set[str], set[str], str]:
    with Session(engine) as session:
        run = session.get(AgentRun, run_id)
        assert run is not None
        rows = session.execute(
            select(RunEvent.event_id, RunEvent.seq, RunEvent.event_type)
            .where(RunEvent.run_id == run_id)
            .order_by(RunEvent.seq)
        ).all()
        return (
            run.next_event_seq,
            [row.seq for row in rows],
            {row.event_id for row in rows},
            {row.event_type for row in rows},
            run.budget_json,
        )


def assert_contiguous_seqs(engine, run_id: str, expected_next: int) -> None:
    nxt, seqs, event_ids, event_types, _budget = run_seq_view(engine, run_id)
    assert nxt == expected_next
    assert seqs == list(range(1, expected_next + 1))
    assert len(event_ids) == expected_next
    assert HEARTBEAT_EVENT_TYPE not in event_types


@pytest.mark.integration
@pytest.mark.concurrency
@pytest.mark.recovery
def test_two_connections_do_not_share_or_skip_run_event_seq(engine):
    from sinuo.api.events.sequence import EventSequenceConflict, HeartbeatNotSequenced

    with write_uow(engine) as session:
        seed_reader(session, grant=False)
        insert_run(session, SSE_PROJECT, SSE_RUN, "snapshot-seq")
    assert_contiguous_seqs(engine, SSE_RUN, 0)
    start = Barrier(2)

    def race(expected: int, left_id: str, right_id: str) -> tuple[str, int]:
        def run(event_id: str) -> tuple[str, int]:
            start.wait(timeout=5)
            with write_uow(engine) as session:
                seq = take_run_seq(
                    session,
                    run_id=SSE_RUN,
                    expected_seq=expected,
                    event_id=event_id,
                )
                return event_id, seq

        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(run, left_id), pool.submit(run, right_id)]
            outcomes: list[tuple[str, int] | None] = []
            for future in futures:
                try:
                    outcomes.append(future.result(timeout=10))
                except EventSequenceConflict as error:
                    assert_secrets_absent(rendered_error(error), SEQ_BODY, HEARTBEAT_BODY)
                    outcomes.append(None)
        won = [item for item in outcomes if item is not None]
        assert len(outcomes) == 2
        assert outcomes.count(None) == 1
        assert len(won) == 1
        assert won[0][1] == expected + 1
        return won[0]

    first = race(0, "seq-race-a", "seq-race-b")
    _nxt, seqs, event_ids, _types, _budget = run_seq_view(engine, SSE_RUN)
    assert seqs == [1]
    assert first[0] in {"seq-race-a", "seq-race-b"}
    assert first[1] == 1
    assert first[0] in event_ids
    assert ({"seq-race-a", "seq-race-b"} - {first[0]}).isdisjoint(event_ids)
    assert_contiguous_seqs(engine, SSE_RUN, 1)

    second = race(1, "seq-race-c", "seq-race-d")
    assert second[1] == 2
    assert first[1] != second[1]
    assert_contiguous_seqs(engine, SSE_RUN, 2)
    _nxt, _seqs, event_ids, _types, _budget = run_seq_view(engine, SSE_RUN)
    assert second[0] in event_ids
    assert ({"seq-race-c", "seq-race-d"} - {second[0]}).isdisjoint(event_ids)

    with write_uow(engine) as session:
        with pytest.raises(HeartbeatNotSequenced) as heartbeat:
            take_run_seq(
                session,
                run_id=SSE_RUN,
                expected_seq=2,
                event_id="seq-heartbeat",
                event_type=HEARTBEAT_EVENT_TYPE,
                payload={"note": HEARTBEAT_BODY},
            )
        assert_secrets_absent(rendered_error(heartbeat.value), SEQ_BODY, HEARTBEAT_BODY)
    assert_contiguous_seqs(engine, SSE_RUN, 2)
    _nxt, _seqs, event_ids, event_types, _budget = run_seq_view(engine, SSE_RUN)
    assert "seq-heartbeat" not in event_ids
    assert HEARTBEAT_EVENT_TYPE not in event_types

    class SeqRollback(Exception):
        pass

    with pytest.raises(SeqRollback):
        with write_uow(engine) as session:
            rolled = take_run_seq(
                session,
                run_id=SSE_RUN,
                expected_seq=2,
                event_id="seq-rollback",
                event_type="task.started",
            )
            assert rolled == 3
            raise SeqRollback()
    assert_contiguous_seqs(engine, SSE_RUN, 2)
    _nxt, _seqs, event_ids, _types, _budget = run_seq_view(engine, SSE_RUN)
    assert "seq-rollback" not in event_ids

    with write_uow(engine) as session:
        continued = take_run_seq(
            session,
            run_id=SSE_RUN,
            expected_seq=2,
            event_id="seq-continue",
            event_type="task.started",
        )
    assert continued == 3
    assert_contiguous_seqs(engine, SSE_RUN, 3)
    _nxt, seqs, event_ids, event_types, _budget = run_seq_view(engine, SSE_RUN)
    assert seqs == [1, 2, 3]
    assert "seq-continue" in event_ids
    assert event_types <= {"run.received", "task.started"}
    with Session(engine) as session:
        project = session.get(Project, SSE_PROJECT)
        run = session.get(AgentRun, SSE_RUN)
        stored = session.execute(select(RunEvent.payload)).scalars().all()
    assert project is not None and run is not None
    assert project.revision == 1
    assert project.access_epoch == 1
    assert run.execution_status == "queued"
    assert run.accepting_results == 1
    assert run.closed_at is None
    assert run.next_event_seq == 3
    assert SEQ_BODY in repr(stored)
    assert HEARTBEAT_BODY not in repr(stored)


@pytest.mark.integration
@pytest.mark.recovery
def test_each_run_sequence_starts_at_one_and_ignores_heartbeats(engine):
    from sinuo.api.events.sequence import EventSequenceConflict, HeartbeatNotSequenced

    with write_uow(engine) as session:
        seed_reader(session, grant=False)
        insert_run(session, SSE_PROJECT, SEQ_RUN_A, "snapshot-seq-a")
        insert_run(session, SSE_PROJECT, SEQ_RUN_B, "snapshot-seq-b")

    with write_uow(engine) as session:
        with pytest.raises(HeartbeatNotSequenced) as heartbeat:
            take_run_seq(
                session,
                run_id=SEQ_RUN_A,
                expected_seq=0,
                event_id="seq-beat-a",
                event_type=HEARTBEAT_EVENT_TYPE,
                payload={"note": HEARTBEAT_BODY},
            )
        assert_secrets_absent(rendered_error(heartbeat.value), HEARTBEAT_BODY, SEQ_BODY)
        first = take_run_seq(session, run_id=SEQ_RUN_A, expected_seq=0, event_id="seq-a1")
        second = take_run_seq(
            session,
            run_id=SEQ_RUN_A,
            expected_seq=1,
            event_id="seq-a2",
            event_type="task.started",
        )
        other = take_run_seq(session, run_id=SEQ_RUN_B, expected_seq=0, event_id="seq-b1")
        with pytest.raises(EventSequenceConflict) as stale:
            take_run_seq(session, run_id=SEQ_RUN_A, expected_seq=0, event_id="seq-a-stale")
        assert_secrets_absent(rendered_error(stale.value), SEQ_BODY, HEARTBEAT_BODY)
        assert first == 1
        assert second == 2
        assert other == 1

    assert_contiguous_seqs(engine, SEQ_RUN_A, 2)
    assert_contiguous_seqs(engine, SEQ_RUN_B, 1)
    _nxt, _seqs, event_ids, event_types, budget = run_seq_view(engine, SEQ_RUN_A)
    assert "seq-beat-a" not in event_ids
    assert "seq-a-stale" not in event_ids
    assert HEARTBEAT_EVENT_TYPE not in event_types
    assert budget == "{}"

    with pytest.raises(EventSequenceConflict) as missing:
        with write_uow(engine) as session:
            take_run_seq(
                session,
                run_id="run-seq-missing",
                expected_seq=0,
                event_id="seq-missing",
            )
    assert_secrets_absent(rendered_error(missing.value), SEQ_BODY, HEARTBEAT_BODY)
    assert_contiguous_seqs(engine, SEQ_RUN_A, 2)
    assert_contiguous_seqs(engine, SEQ_RUN_B, 1)

    with write_uow(engine) as session:
        loaded = session.get(AgentRun, SEQ_RUN_A)
        assert loaded is not None
        assert loaded.next_event_seq == 2
        loaded.budget_json = '{"kept":1}'
        third = take_run_seq(session, run_id=SEQ_RUN_A, expected_seq=2, event_id="seq-a3")
        assert third == 3
        assert loaded.budget_json == '{"kept":1}'

    assert_contiguous_seqs(engine, SEQ_RUN_A, 3)
    assert_contiguous_seqs(engine, SEQ_RUN_B, 1)
    _nxt, seqs, event_ids, _types, budget = run_seq_view(engine, SEQ_RUN_A)
    assert seqs == [1, 2, 3]
    assert "seq-a3" in event_ids
    assert budget == '{"kept":1}'
    with Session(engine) as session:
        project = session.get(Project, SSE_PROJECT)
        run_a = session.get(AgentRun, SEQ_RUN_A)
        run_b = session.get(AgentRun, SEQ_RUN_B)
        assert session.get(RunEvent, "seq-missing") is None
        assert session.get(RunEvent, "seq-beat-a") is None
    assert project is not None and run_a is not None and run_b is not None
    assert project.revision == 1
    assert project.access_epoch == 1
    assert run_a.execution_status == "queued"
    assert run_a.accepting_results == 1
    assert run_a.closed_at is None
    assert run_a.next_event_seq == 3
    assert run_b.execution_status == "queued"
    assert run_b.next_event_seq == 1
    assert run_b.closed_at is None


NEXT_BODY = "synthetic-http-next-body"
LATER_BODY = "synthetic-http-later-body"
EVENTS_URL = f"/api/v1/agent-runs/{SSE_RUN}/events"


def http_events_app(engine, database_url):
    from fastapi import FastAPI

    from sinuo.api.events.routes import router as events_router
    from tests.acceptance.identity_fixtures import identity_settings

    app = FastAPI()
    app.state.engine = engine
    app.state.settings = identity_settings(database_url)
    app.include_router(events_router)
    return app


def persist_sequenced(
    session: Session,
    *,
    project_id: str,
    run_id: str,
    expected_seq: int,
    event_id: str,
    event_type: str,
    note: str,
) -> int:
    from sinuo.api.events.sequence import allocate_run_event

    return allocate_run_event(
        session,
        project_id=project_id,
        run_id=run_id,
        expected_seq=expected_seq,
        event_id=event_id,
        event_type=event_type,
        payload={"note": note},
        trace_id="trace-http",
        created_at=CREATED_AT + expected_seq + 1,
    )


def reader_token(session: Session, settings) -> str:
    import time

    from sinuo.application.auth.sessions import issue_session

    account = session.get(Account, SSE_ACCOUNT)
    assert account is not None
    return issue_session(session, account, settings, time.time()).token


def cookie_name(app) -> str:
    from sinuo.domain.public_contract import session_cookie_name

    return session_cookie_name(app.state.settings.public_url)


def parse_frames(text: str) -> list[dict]:
    import json

    frames = []
    for block in text.split("\n\n"):
        if not block.strip():
            continue
        data = ""
        for line in block.split("\n"):
            if line.startswith("data:"):
                data = line[5:].lstrip()
        frames.append(json.loads(data))
    return frames


def assert_event_text_hidden(response, *secrets: str) -> None:
    assert "text/event-stream" not in response.headers.get("content-type", "")
    assert response.headers.get("cache-control") == "no-store"
    assert_secrets_absent(response.text, *secrets)


def pull_sse(app, token: str, run_id: str, *, on_first_body) -> dict:
    import anyio

    path = f"/api/v1/agent-runs/{run_id}/events"
    headers = [(b"cookie", f"{cookie_name(app)}={token}".encode())]
    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.4"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "headers": headers,
        "client": ("127.0.0.1", 50000),
        "server": ("127.0.0.1", 8000),
        "root_path": "",
    }
    captured: dict = {"status": None, "headers": [], "chunks": []}

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        if message["type"] == "http.response.start":
            captured["status"] = message["status"]
            captured["headers"] = message.get("headers", [])
            return
        chunk = message.get("body") or b""
        if not chunk:
            return
        captured["chunks"].append(chunk)
        if len(captured["chunks"]) == 1:
            on_first_body()

    async def drive():
        await app(scope, receive, send)

    anyio.run(drive)
    return captured


def header_value(raw_headers, name: str) -> str:
    for key, value in raw_headers:
        if key.decode().lower() == name:
            return value.decode()
    return ""


@pytest.mark.integration
def test_sse_route_returns_persisted_events_in_order(engine, database_url):
    from fastapi.testclient import TestClient

    app = http_events_app(engine, database_url)
    with write_uow(engine) as session:
        seed_reader(session, grant=True)
        insert_run(session, SSE_PROJECT, SSE_RUN, "snapshot-http-order")
        assert persist_sequenced(
            session,
            project_id=SSE_PROJECT,
            run_id=SSE_RUN,
            expected_seq=0,
            event_id="order-received",
            event_type="run.received",
            note=LOCAL_BODY,
        ) == 1
        assert persist_sequenced(
            session,
            project_id=SSE_PROJECT,
            run_id=SSE_RUN,
            expected_seq=1,
            event_id="order-task",
            event_type="task.started",
            note=TASK_BODY,
        ) == 2
        append_events(
            session,
            RunEvent,
            [
                stream_event(
                    "order-beat",
                    3,
                    HEARTBEAT_EVENT_TYPE,
                    {"note": HEARTBEAT_BODY, "status": "completed"},
                )
            ],
        )
        token = reader_token(session, app.state.settings)
    with TestClient(app) as client:
        client.cookies.set(cookie_name(app), token)
        response = client.get(EVENTS_URL)
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        assert response.headers["cache-control"] == "no-store"
        frames = parse_frames(response.text)
        assert [frame["seq"] for frame in frames] == [1, 2, 3]
        assert [frame["event_id"] for frame in frames] == [
            "order-received",
            "order-task",
            "order-beat",
        ]
        assert [frame["type"] for frame in frames] == [
            "run.received",
            "task.started",
            HEARTBEAT_EVENT_TYPE,
        ]
        assert [frame["progress"] for frame in frames] == [True, True, False]
        assert frames[0]["payload"] == {"note": LOCAL_BODY}
        assert frames[2]["payload"]["note"] == HEARTBEAT_BODY
        assert frames[2]["payload"]["status"] == "completed"
        assert frames[2]["type"] != "run.completed"
        resumed = client.get(EVENTS_URL, headers={"Last-Event-ID": "order-received"})
        assert [frame["event_id"] for frame in parse_frames(resumed.text)] == [
            "order-task",
            "order-beat",
        ]
        assert parse_frames(resumed.text)[1]["progress"] is False
        reset = client.get(EVENTS_URL, headers={"Last-Event-ID": "missing-http-cursor"})
        reset_frames = parse_frames(reset.text)
        assert [frame["type"] for frame in reset_frames] == [RESET_EVENT_TYPE]
        assert reset_frames[0]["progress"] is False
        assert reset_frames[0]["payload"] == {}
        assert_secrets_absent(reset.text, LOCAL_BODY, TASK_BODY, HEARTBEAT_BODY)
        tail = client.get(EVENTS_URL, headers={"Last-Event-ID": "order-beat"})
        assert tail.status_code == 200
        assert tail.text == ""
    with Session(engine) as session:
        project = session.get(Project, SSE_PROJECT)
        run = session.get(AgentRun, SSE_RUN)
    assert project is not None and run is not None
    assert project.revision == 1
    assert project.access_epoch == 1
    assert run.execution_status == "queued"
    assert run.next_event_seq == 2
    assert run.closed_at is None
    assert HEARTBEAT_BODY in payload_text(engine, RunEvent)


@pytest.mark.integration
def test_sse_route_hides_event_text_without_current_read(engine, database_url):
    from fastapi.testclient import TestClient

    secrets = (LOCAL_BODY, OTHER_RUN_BODY, FOREIGN_BODY)
    app = http_events_app(engine, database_url)
    with write_uow(engine) as session:
        seed_reader(session, grant=False)
        insert_run(session, SSE_PROJECT, SSE_RUN, "snapshot-http-deny")
        insert_run(session, SSE_PROJECT, SSE_OTHER_RUN, "snapshot-http-other")
        insert_run(session, SSE_OTHER_PROJECT, SSE_FOREIGN_RUN, "snapshot-http-foreign")
        persist_sequenced(
            session,
            project_id=SSE_PROJECT,
            run_id=SSE_RUN,
            expected_seq=0,
            event_id="deny-local",
            event_type="run.received",
            note=LOCAL_BODY,
        )
        persist_sequenced(
            session,
            project_id=SSE_PROJECT,
            run_id=SSE_OTHER_RUN,
            expected_seq=0,
            event_id="deny-other",
            event_type="task.started",
            note=OTHER_RUN_BODY,
        )
        persist_sequenced(
            session,
            project_id=SSE_OTHER_PROJECT,
            run_id=SSE_FOREIGN_RUN,
            expected_seq=0,
            event_id="deny-foreign",
            event_type="task.started",
            note=FOREIGN_BODY,
        )
        token = reader_token(session, app.state.settings)
    with TestClient(app) as client:
        missing_cookie = client.get(EVENTS_URL)
        assert missing_cookie.status_code == 401
        assert missing_cookie.json()["error"]["code"] == "AUTH_REQUIRED"
        assert_event_text_hidden(missing_cookie, *secrets)
        client.cookies.set(cookie_name(app), token)
        forbidden = client.get(EVENTS_URL)
        assert forbidden.status_code == 403
        assert forbidden.json()["error"]["code"] == "ACTION_FORBIDDEN"
        assert forbidden.json()["error"]["details"] == {"action": "run.read"}
        assert_event_text_hidden(forbidden, *secrets)
        with write_uow(engine) as session:
            session.add(read_grant(SSE_GRANT))
        other_run = client.get(EVENTS_URL, headers={"Last-Event-ID": "deny-other"})
        foreign = client.get(EVENTS_URL, headers={"Last-Event-ID": "deny-foreign"})
        blank = client.get(EVENTS_URL, headers={"Last-Event-ID": ""})
        padded = client.get(EVENTS_URL, headers={"Last-Event-ID": " deny-local"})
        for rejected in (other_run, foreign, blank, padded):
            assert rejected.status_code == 422
            error = rejected.json()["error"]
            assert error["code"] == "INVALID_SCHEMA"
            assert error["message"] == "Last-Event-ID is outside this run."
            assert error["details"] == {"category": "last_event_id"}
            assert error["retryable"] is False
            assert error["request_id"]
            assert_event_text_hidden(rejected, *secrets)
        hidden_run = client.get("/api/v1/agent-runs/run-http-missing/events")
        assert hidden_run.status_code == 404
        assert hidden_run.json()["error"]["code"] == "NOT_FOUND_OR_HIDDEN"
        assert_event_text_hidden(hidden_run, *secrets)
        with write_uow(engine) as session:
            grant = session.get(ObjectGrant, SSE_GRANT)
            assert grant is not None
            grant.status = "revoked"
            grant.revoked_at = CREATED_AT + 1
        revoked = client.get(EVENTS_URL)
        assert revoked.status_code == 403
        assert revoked.json()["error"]["code"] == "ACTION_FORBIDDEN"
        assert_event_text_hidden(revoked, *secrets)
        with write_uow(engine) as session:
            membership = session.get(Membership, SSE_MEMBER)
            assert membership is not None
            membership.status = "revoked"
            membership.revoked_at = CREATED_AT + 1
        hidden = client.get(EVENTS_URL)
        assert hidden.status_code == 404
        assert hidden.json()["error"]["code"] == "NOT_FOUND_OR_HIDDEN"
        assert_event_text_hidden(hidden, *secrets)
    stored = payload_text(engine, RunEvent)
    assert LOCAL_BODY in stored
    assert OTHER_RUN_BODY in stored
    assert FOREIGN_BODY in stored


@pytest.mark.integration
def test_sse_route_checks_permission_before_the_next_frame(engine, database_url):
    import time

    from fastapi.testclient import TestClient

    app = http_events_app(engine, database_url)
    with write_uow(engine) as session:
        seed_reader(session, grant=True)
        insert_run(session, SSE_PROJECT, SSE_RUN, "snapshot-http-live")
        persist_sequenced(
            session,
            project_id=SSE_PROJECT,
            run_id=SSE_RUN,
            expected_seq=0,
            event_id="live-1",
            event_type="run.received",
            note=LOCAL_BODY,
        )
        persist_sequenced(
            session,
            project_id=SSE_PROJECT,
            run_id=SSE_RUN,
            expected_seq=1,
            event_id="live-2",
            event_type="task.started",
            note=NEXT_BODY,
        )
        persist_sequenced(
            session,
            project_id=SSE_PROJECT,
            run_id=SSE_RUN,
            expected_seq=2,
            event_id="live-3",
            event_type="task.completed",
            note=LATER_BODY,
        )
        token = reader_token(session, app.state.settings)

    def revoke_grant() -> None:
        with write_uow(engine) as session:
            grant = session.get(ObjectGrant, SSE_GRANT)
            assert grant is not None
            grant.status = "revoked"
            grant.revoked_at = time.time()

    pulled = pull_sse(app, token, SSE_RUN, on_first_body=revoke_grant)
    text = b"".join(pulled["chunks"]).decode()
    first, _, rest = text.partition("\n\n")
    assert pulled["status"] == 200
    assert header_value(pulled["headers"], "content-type").startswith("text/event-stream")
    assert header_value(pulled["headers"], "cache-control") == "no-store"
    opened = parse_frames(first + "\n\n")
    assert opened[0]["event_id"] == "live-1"
    assert opened[0]["progress"] is True
    assert opened[0]["payload"]["note"] == LOCAL_BODY
    assert NEXT_BODY not in first
    assert LATER_BODY not in first
    assert sum(1 for line in text.splitlines() if line.startswith("data:")) == 2
    errors = parse_frames(rest)
    assert [frame["error"]["code"] for frame in errors] == ["ACTION_FORBIDDEN"]
    assert_secrets_absent(rest, NEXT_BODY, LATER_BODY)
    assert_secrets_absent(text, NEXT_BODY, LATER_BODY)
    with TestClient(app) as client:
        client.cookies.set(cookie_name(app), token)
        again = client.get(EVENTS_URL)
    assert again.status_code == 403
    assert again.json()["error"]["code"] == "ACTION_FORBIDDEN"
    assert_event_text_hidden(again, LOCAL_BODY, NEXT_BODY, LATER_BODY)
    stored = payload_text(engine, RunEvent)
    assert LOCAL_BODY in stored
    assert NEXT_BODY in stored
    assert LATER_BODY in stored
    with Session(engine) as session:
        run = session.get(AgentRun, SSE_RUN)
    assert run is not None
    assert run.execution_status == "queued"
    assert run.next_event_seq == 3
    assert run.closed_at is None


@pytest.mark.integration
def test_create_app_mounts_agent_run_events(engine, database_url):
    from fastapi.testclient import TestClient

    from sinuo.api.app import create_app
    from tests.acceptance.identity_fixtures import identity_settings

    app = create_app(identity_settings(database_url))
    assert "/api/v1/agent-runs/{run_id}/events" in app.openapi()["paths"]
    with TestClient(app) as client:
        response = client.get("/api/v1/agent-runs/missing-run/events")
    assert response.status_code == 401
    assert "payload" not in response.text
    assert engine is not None


def cancel(engine, *, job_id: str = JOB_ID) -> JobCancellation:
    with write_uow(engine) as session:
        return cancel_job(session, job_id=job_id)


def insert_terminal_job(engine, job_id: str, status: str) -> None:
    with write_uow(engine) as session:
        session.add(
            Job(
                id=job_id,
                project_id=PROJECT_ID,
                job_kind="derivative",
                status=status,
                available_at=0.0,
                attempt_no=2,
                lease_owner=None,
                lease_until=None,
                fencing_token=4,
                input_hash=INPUT_HASH,
                output_ref=None,
                last_error=None,
                subject_type="artifact",
                subject_id="artifact-1",
                source_event_id=None,
                payload="{}",
                created_at=CREATED_AT,
            )
        )


def raw_sqlite(session: Session):
    wrapped = session.connection().connection
    candidate = getattr(wrapped, "dbapi_connection", None)
    if candidate is None:
        candidate = wrapped
    if not hasattr(candidate, "set_trace_callback"):
        raise RuntimeError("sqlite connection is unavailable")
    return candidate


@pytest.mark.integration
def test_cancel_queued_or_running_clears_lease(engine):
    insert_job(engine, job_id="job-queued")
    queued = cancel(engine, job_id="job-queued")
    queued_job = fetch_job(engine, "job-queued")
    assert queued.already_cancelled is False
    assert queued.fencing_token == 0
    assert queued.attempt_no == 0
    assert queued_job.status == "cancelled"
    assert queued_job.lease_owner is None
    assert queued_job.lease_until is None
    assert queued_job.fencing_token == 0
    assert queued_job.attempt_no == 0
    assert queued_job.output_ref is None
    insert_job(engine)
    attempt = claim(engine, owner="worker-1")
    running = cancel(engine)
    running_job = fetch_job(engine)
    assert running.already_cancelled is False
    assert running.fencing_token == attempt.fencing_token
    assert running.attempt_no == attempt.attempt_no
    assert running_job.status == "cancelled"
    assert running_job.lease_owner is None
    assert running_job.lease_until is None
    assert running_job.fencing_token == attempt.fencing_token
    assert running_job.attempt_no == attempt.attempt_no
    assert running_job.input_hash == INPUT_HASH
    assert fetch_receipts(engine) == []


@pytest.mark.integration
def test_repeat_cancel_returns_without_updating_terminal_fields(engine):
    insert_job(engine)
    attempt = claim(engine, owner="worker-1")
    first = cancel(engine)
    assert first.already_cancelled is False
    before = fetch_job(engine)
    with write_uow(engine) as session:
        baseline = session.scalar(text("SELECT total_changes()"))
        seen: list[str] = []
        raw = raw_sqlite(session)
        raw.set_trace_callback(seen.append)
        try:
            again = cancel_job(session, job_id=JOB_ID)
        finally:
            raw.set_trace_callback(None)
        after = session.scalar(text("SELECT total_changes()"))
    assert after == baseline
    assert not any(statement and "UPDATE" in statement.upper() for statement in seen)
    assert again.already_cancelled is True
    assert again.fencing_token == attempt.fencing_token
    assert again.attempt_no == attempt.attempt_no
    assert again.input_hash == INPUT_HASH
    job = fetch_job(engine)
    assert job == before
    assert job.status == "cancelled"
    assert job.lease_owner is None
    assert job.lease_until is None
    assert job.fencing_token == attempt.fencing_token
    assert job.attempt_no == attempt.attempt_no


@pytest.mark.integration
def test_cancel_rejects_unless_one_row_changes(engine):
    insert_job(engine)
    attempt = claim(engine)
    finalize(engine, owner="worker-1", fencing_token=attempt.fencing_token)
    insert_terminal_job(engine, "job-failed", "failed")
    insert_terminal_job(engine, "job-superseded", "superseded")
    succeeded = fetch_job(engine)
    failed = fetch_job(engine, "job-failed")
    superseded = fetch_job(engine, "job-superseded")
    with pytest.raises(StaleLease):
        cancel(engine)
    with pytest.raises(StaleLease):
        cancel(engine, job_id="job-failed")
    with pytest.raises(StaleLease):
        cancel(engine, job_id="job-superseded")
    with pytest.raises(StaleLease):
        cancel(engine, job_id="job-missing")
    assert fetch_job(engine) == succeeded
    assert fetch_job(engine, "job-failed") == failed
    assert fetch_job(engine, "job-superseded") == superseded
    assert succeeded.status == "succeeded"
    assert succeeded.fencing_token == attempt.fencing_token
    assert failed.status == "failed"
    assert failed.fencing_token == 4
    assert failed.attempt_no == 2
    assert failed.lease_owner is None
    assert failed.lease_until is None
    assert superseded.status == "superseded"
    assert superseded.fencing_token == 4
    assert superseded.attempt_no == 2
    assert superseded.lease_owner is None
    assert superseded.lease_until is None


@pytest.mark.integration
def test_old_fence_cannot_finalize_or_renew_after_cancel(engine):
    insert_job(engine)
    attempt = claim(engine, owner="worker-1")
    cancelled = cancel(engine)
    assert cancelled.already_cancelled is False
    assert cancelled.fencing_token == attempt.fencing_token
    with pytest.raises(StaleLease):
        renew(engine, owner="worker-1", fencing_token=attempt.fencing_token)
    with pytest.raises(StaleLease):
        finalize(engine, owner="worker-1", fencing_token=attempt.fencing_token)
    job = fetch_job(engine)
    assert job.status == "cancelled"
    assert job.lease_owner is None
    assert job.lease_until is None
    assert job.fencing_token == attempt.fencing_token
    assert job.attempt_no == attempt.attempt_no
    assert job.output_ref is None
    assert job.last_error is None
    assert fetch_receipts(engine) == []
    with pytest.raises(StaleLease):
        claim(engine, owner="worker-2")
    assert fetch_job(engine) == job


@pytest.mark.integration
@pytest.mark.concurrency
def test_two_connections_only_one_of_finalize_or_cancel_succeeds(engine):
    insert_job(engine)
    attempt = claim(engine, owner="worker-1")
    start = Barrier(2)

    def run_finalize() -> JobPublication:
        start.wait(timeout=5)
        with write_uow(engine) as session:
            return finalize_job(
                session,
                job_id=JOB_ID,
                owner="worker-1",
                fencing_token=attempt.fencing_token,
                input_hash=INPUT_HASH,
                output_ref=OUTPUT_REF,
                consumer_name=CONSUMER,
                effect_kind=EFFECT,
            )

    def run_cancel() -> JobCancellation:
        start.wait(timeout=5)
        with write_uow(engine) as session:
            return cancel_job(session, job_id=JOB_ID)

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(run_finalize), pool.submit(run_cancel)]
        won: list[JobPublication | JobCancellation] = []
        lost = 0
        for future in futures:
            try:
                won.append(future.result(timeout=10))
            except StaleLease:
                lost += 1
    assert len(won) == 1
    assert lost == 1
    job = fetch_job(engine)
    assert job.lease_owner is None
    assert job.lease_until is None
    assert job.fencing_token == attempt.fencing_token
    assert job.attempt_no == attempt.attempt_no
    winner = won[0]
    if isinstance(winner, JobCancellation):
        assert winner.already_cancelled is False
        assert winner.fencing_token == attempt.fencing_token
        assert job.status == "cancelled"
        assert job.output_ref is None
        assert fetch_receipts(engine) == []
    else:
        assert isinstance(winner, JobPublication)
        assert winner.fencing_token == attempt.fencing_token
        assert job.status == "succeeded"
        assert job.output_ref == OUTPUT_REF
        assert fetch_receipts(engine) == [
            SeenReceipt(EVENT_ID, CONSUMER, PROJECT_ID, EFFECT, OUTPUT_REF)
        ]


@dataclass(frozen=True)
class SeenRunGate:
    execution_status: str
    accepting_results: int
    next_event_seq: int
    closed_at: float | None
    created_at: float


def insert_gate_run(
    engine,
    run_id: str,
    *,
    execution_status: str = "queued",
    accepting_results: int = 1,
    next_event_seq: int = 0,
) -> None:
    snapshot_id = f"snapshot-{run_id}"
    with write_uow(engine) as session:
        seed_reader(session, grant=False)
        session.add(
            ContextSnapshot(
                id=snapshot_id,
                project_id=SSE_PROJECT,
                intent="synthetic replay",
                template_version="synthetic-1",
                method_card_version="synthetic-1",
                authorization_scope_json="{}",
                access_epoch=1,
                exclusions_json="[]",
                index_revision=None,
                budget_json="{}",
                draft_page_id=None,
                draft_content_revision=None,
                draft_content_hash=None,
                draft_content_json=None,
                created_at=CREATED_AT,
            )
        )
        session.flush()
        session.add(
            AgentRun(
                id=run_id,
                project_id=SSE_PROJECT,
                initiated_by_account_id=SSE_ACCOUNT,
                parent_run_id=None,
                supersedes_run_id=None,
                context_snapshot_id=snapshot_id,
                execution_status=execution_status,
                accepting_results=accepting_results,
                next_event_seq=next_event_seq,
                budget_json="{}",
                created_at=CREATED_AT,
                closed_at=None,
            )
        )


def fetch_run_gate(engine, run_id: str) -> SeenRunGate:
    with Session(engine) as session:
        row = session.get(AgentRun, run_id)
        assert row is not None
        return SeenRunGate(
            row.execution_status,
            row.accepting_results,
            row.next_event_seq,
            row.closed_at,
            row.created_at,
        )


def cancel_open_run(engine, run_id: str):
    from sinuo.workers.runs import cancel_run

    with write_uow(engine) as session:
        return cancel_run(session, run_id=run_id)


def publish_open_run(engine, run_id: str):
    from sinuo.workers.runs import publish_run

    with write_uow(engine) as session:
        return publish_run(session, run_id=run_id)


@pytest.mark.integration
def test_cancel_run_closes_status_and_rejects_late_publish(engine):
    insert_gate_run(engine, "run-cancel-queued", next_event_seq=4)
    queued = cancel_open_run(engine, "run-cancel-queued")
    queued_run = fetch_run_gate(engine, "run-cancel-queued")
    assert queued.already_cancelled is False
    assert queued.execution_status == "cancelled"
    assert queued.accepting_results == 0
    assert queued_run.execution_status == "cancelled"
    assert queued_run.accepting_results == 0
    assert queued_run.next_event_seq == 4
    assert queued_run.closed_at is None
    assert queued_run.created_at == CREATED_AT
    with pytest.raises(StaleLease):
        publish_open_run(engine, "run-cancel-queued")
    assert fetch_run_gate(engine, "run-cancel-queued") == queued_run

    insert_gate_run(engine, "run-cancel-running", execution_status="running", next_event_seq=4)
    running = cancel_open_run(engine, "run-cancel-running")
    running_run = fetch_run_gate(engine, "run-cancel-running")
    assert running.already_cancelled is False
    assert running.execution_status == "cancelled"
    assert running.accepting_results == 0
    assert running_run.execution_status == "cancelled"
    assert running_run.accepting_results == 0
    assert running_run.next_event_seq == 4

    insert_gate_run(
        engine,
        "run-closed-running",
        execution_status="running",
        accepting_results=0,
        next_event_seq=2,
    )
    closed_before = fetch_run_gate(engine, "run-closed-running")
    with pytest.raises(StaleLease):
        publish_open_run(engine, "run-closed-running")
    assert fetch_run_gate(engine, "run-closed-running") == closed_before
    closed = cancel_open_run(engine, "run-closed-running")
    closed_run = fetch_run_gate(engine, "run-closed-running")
    assert closed.already_cancelled is False
    assert closed.execution_status == "cancelled"
    assert closed.accepting_results == 0
    assert closed_run.execution_status == "cancelled"
    assert closed_run.accepting_results == 0
    assert closed_run.next_event_seq == 2

    insert_gate_run(
        engine,
        "run-completed",
        execution_status="completed",
        accepting_results=1,
        next_event_seq=3,
    )
    completed_before = fetch_run_gate(engine, "run-completed")
    with pytest.raises(StaleLease):
        cancel_open_run(engine, "run-completed")
    assert fetch_run_gate(engine, "run-completed") == completed_before
    published = publish_open_run(engine, "run-completed")
    published_run = fetch_run_gate(engine, "run-completed")
    assert published.execution_status == "completed"
    assert published.accepting_results == 0
    assert published_run.execution_status == "completed"
    assert published_run.accepting_results == 0
    assert published_run.next_event_seq == 3
    with pytest.raises(StaleLease):
        publish_open_run(engine, "run-completed")
    assert fetch_run_gate(engine, "run-completed") == published_run

    insert_gate_run(engine, "run-failed", execution_status="failed", accepting_results=1)
    failed = publish_open_run(engine, "run-failed")
    failed_run = fetch_run_gate(engine, "run-failed")
    assert failed.execution_status == "failed"
    assert failed.accepting_results == 0
    assert failed_run.execution_status == "failed"
    assert failed_run.accepting_results == 0
    assert failed_run.next_event_seq == 0
    with pytest.raises(StaleLease):
        cancel_open_run(engine, "run-failed")
    assert fetch_run_gate(engine, "run-failed") == failed_run

    insert_gate_run(
        engine,
        "run-already-cancelled",
        execution_status="cancelled",
        accepting_results=1,
        next_event_seq=5,
    )
    cancelled_before = fetch_run_gate(engine, "run-already-cancelled")
    with pytest.raises(StaleLease):
        publish_open_run(engine, "run-already-cancelled")
    assert fetch_run_gate(engine, "run-already-cancelled") == cancelled_before
    with pytest.raises(StaleLease):
        cancel_open_run(engine, "run-missing")


@pytest.mark.integration
def test_repeat_cancel_run_does_not_rewrite_execution_status(engine):
    from sinuo.workers.runs import cancel_run

    insert_gate_run(engine, "run-repeat-cancel", execution_status="running", next_event_seq=4)
    first = cancel_open_run(engine, "run-repeat-cancel")
    assert first.already_cancelled is False
    assert first.execution_status == "cancelled"
    assert first.accepting_results == 0
    before = fetch_run_gate(engine, "run-repeat-cancel")
    assert before.execution_status == "cancelled"
    assert before.accepting_results == 0
    with write_uow(engine) as session:
        baseline = session.scalar(text("SELECT total_changes()"))
        seen: list[str] = []
        raw = raw_sqlite(session)
        raw.set_trace_callback(seen.append)
        try:
            again = cancel_run(session, run_id="run-repeat-cancel")
        finally:
            raw.set_trace_callback(None)
        after = session.scalar(text("SELECT total_changes()"))
    assert after == baseline
    assert not any(statement and "UPDATE" in statement.upper() for statement in seen)
    assert again.already_cancelled is True
    assert again.execution_status == "cancelled"
    assert again.accepting_results == 0
    assert fetch_run_gate(engine, "run-repeat-cancel") == before


@pytest.mark.integration
def test_publish_run_seals_open_run_and_blocks_cancel(engine):
    insert_gate_run(engine, "run-publish-queued", next_event_seq=4)
    queued = publish_open_run(engine, "run-publish-queued")
    queued_run = fetch_run_gate(engine, "run-publish-queued")
    assert queued.execution_status == "completed"
    assert queued.accepting_results == 0
    assert queued_run.execution_status == "completed"
    assert queued_run.accepting_results == 0
    assert queued_run.next_event_seq == 4
    assert queued_run.closed_at is None
    with pytest.raises(StaleLease):
        publish_open_run(engine, "run-publish-queued")
    with pytest.raises(StaleLease):
        cancel_open_run(engine, "run-publish-queued")
    assert fetch_run_gate(engine, "run-publish-queued") == queued_run

    insert_gate_run(engine, "run-publish-running", execution_status="running", next_event_seq=6)
    running = publish_open_run(engine, "run-publish-running")
    running_run = fetch_run_gate(engine, "run-publish-running")
    assert running.execution_status == "completed"
    assert running.accepting_results == 0
    assert running_run.execution_status == "completed"
    assert running_run.accepting_results == 0
    assert running_run.next_event_seq == 6
    with pytest.raises(StaleLease):
        cancel_open_run(engine, "run-publish-running")
    assert fetch_run_gate(engine, "run-publish-running") == running_run


@pytest.mark.integration
@pytest.mark.concurrency
def test_two_connections_only_one_of_publish_or_cancel_succeeds(engine):
    from sinuo.workers.runs import RunCancellation, RunPublication, cancel_run, publish_run

    run_id = "run-publish-cancel-race"
    insert_gate_run(engine, run_id, execution_status="running", next_event_seq=4)
    start = Barrier(2)

    def run_publish() -> RunPublication:
        start.wait(timeout=5)
        with write_uow(engine) as session:
            return publish_run(session, run_id=run_id)

    def run_cancel() -> RunCancellation:
        start.wait(timeout=5)
        with write_uow(engine) as session:
            return cancel_run(session, run_id=run_id)

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(run_publish), pool.submit(run_cancel)]
        won: list[RunPublication | RunCancellation] = []
        lost = 0
        for future in futures:
            try:
                won.append(future.result(timeout=10))
            except StaleLease:
                lost += 1
    assert len(won) == 1
    assert lost == 1
    seen = fetch_run_gate(engine, run_id)
    assert seen.accepting_results == 0
    assert seen.next_event_seq == 4
    assert seen.closed_at is None
    assert seen.created_at == CREATED_AT
    winner = won[0]
    if isinstance(winner, RunCancellation):
        assert winner.already_cancelled is False
        assert winner.execution_status == "cancelled"
        assert winner.accepting_results == 0
        assert seen.execution_status == "cancelled"
    else:
        assert isinstance(winner, RunPublication)
        assert winner.execution_status == "completed"
        assert winner.accepting_results == 0
        assert seen.execution_status == "completed"


FUTURE_DEADLINE = 3_900_000_000.0
LATER_AVAILABLE = 3_800_000_000.0
INVALID_BUDGETS = (
    "{}",
    '{"kept":1}',
    '{"max_model_calls":1,"spent_model_calls":0}',
    '{"max_model_calls":1,"spent_model_calls":0,"reserved_model_calls":0,"kept":1}',
    '{"max_model_calls":1,"spent_model_calls":-1,"reserved_model_calls":0}',
    '{"max_model_calls":true,"spent_model_calls":0,"reserved_model_calls":0}',
    '{"max_model_calls":1.0,"spent_model_calls":0,"reserved_model_calls":0}',
    '{"max_model_calls":1,"spent_model_calls":1,"reserved_model_calls":1}',
    "[]",
    "null",
)


def model_budget(max_calls: int, spent: int, reserved: int) -> str:
    return json.dumps(
        {
            "max_model_calls": max_calls,
            "spent_model_calls": spent,
            "reserved_model_calls": reserved,
        },
        separators=(",", ":"),
    )


def insert_budget_run(
    engine,
    run_id: str,
    budget_json: str,
    *,
    execution_status: str = "queued",
    accepting_results: int = 1,
) -> None:
    insert_gate_run(
        engine,
        run_id,
        execution_status=execution_status,
        accepting_results=accepting_results,
    )
    with write_uow(engine) as session:
        run = session.get(AgentRun, run_id)
        assert run is not None
        assert run.budget_json == "{}"
        run.budget_json = budget_json


def fetch_available_at(engine, job_id: str = JOB_ID) -> float:
    with Session(engine) as session:
        row = session.get(Job, job_id)
        assert row is not None
        return row.available_at


def fetch_budget_text(engine, run_id: str) -> str:
    with Session(engine) as session:
        run = session.get(AgentRun, run_id)
        assert run is not None
        return run.budget_json


def fetch_snapshot_budget(engine, run_id: str) -> str:
    with Session(engine) as session:
        run = session.get(AgentRun, run_id)
        assert run is not None
        snapshot = session.get(ContextSnapshot, run.context_snapshot_id)
        assert snapshot is not None
        return snapshot.budget_json


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


def finish_attempt(
    engine,
    *,
    job_id: str = JOB_ID,
    category: str,
    max_attempts: int,
    deadline: float,
    available_at: float,
) -> JobAttemptFinish:
    with write_uow(engine) as session:
        return finish_job_attempt(
            session,
            job_id=job_id,
            category=category,
            max_attempts=max_attempts,
            deadline=deadline,
            available_at=available_at,
        )


def assert_failed_finish_is_quiet(engine, job_id: str = JOB_ID) -> JobAttemptFinish:
    with write_uow(engine) as session:
        return assert_session_writes_nothing(
            session,
            lambda: finish_job_attempt(
                session,
                job_id=job_id,
                category="rate_limited",
                max_attempts=8,
                deadline=FUTURE_DEADLINE,
                available_at=LATER_AVAILABLE,
            ),
        )


def assert_budget_gate(engine, run_id: str, *, status: str, accepting: int) -> None:
    assert fetch_run_gate(engine, run_id) == SeenRunGate(
        status,
        accepting,
        0,
        None,
        CREATED_AT,
    )
    assert fetch_snapshot_budget(engine, run_id) == "{}"


@pytest.mark.integration
def test_reserve_and_settle_move_model_call_counts(engine):
    run_id = "run-budget-open"
    insert_budget_run(engine, run_id, model_budget(4, 1, 0), execution_status="running")
    with write_uow(engine) as session:
        reserved = reserve_model_call(session, run_id, 2)
    assert reserved.reserved is True
    assert reserved.reason is None
    assert reserved.budget_json == model_budget(4, 1, 2)
    assert fetch_budget_text(engine, run_id) == model_budget(4, 1, 2)
    assert_budget_gate(engine, run_id, status="running", accepting=1)
    with write_uow(engine) as session:
        settled = settle_model_call(session, run_id, 2, 1)
    assert settled.pending is False
    assert settled.budget_json == model_budget(4, 2, 0)
    assert fetch_budget_text(engine, run_id) == model_budget(4, 2, 0)
    assert_budget_gate(engine, run_id, status="running", accepting=1)
    with write_uow(engine) as session:
        again = reserve_model_call(session, run_id, 2)
    assert again.reserved is True
    assert fetch_budget_text(engine, run_id) == model_budget(4, 2, 2)
    with write_uow(engine) as session:
        blocked = assert_session_writes_nothing(
            session,
            lambda: reserve_model_call(session, run_id, 1),
        )
    assert blocked.reserved is False
    assert blocked.reason == "budget_exhausted"
    assert blocked.budget_json == model_budget(4, 2, 2)
    assert fetch_budget_text(engine, run_id) == model_budget(4, 2, 2)
    assert_budget_gate(engine, run_id, status="running", accepting=1)


@pytest.mark.integration
def test_reserve_matches_stored_budget_text(engine):
    run_id = "run-budget-spaced"
    stored = '{"reserved_model_calls": 0, "spent_model_calls": 0, "max_model_calls": 3}'
    insert_budget_run(engine, run_id, stored)
    with write_uow(engine) as session:
        reserved = reserve_model_call(session, run_id, 1)
    assert reserved.reserved is True
    assert reserved.budget_json == model_budget(3, 0, 1)
    assert fetch_budget_text(engine, run_id) == model_budget(3, 0, 1)
    assert fetch_budget_text(engine, run_id) != stored
    assert_budget_gate(engine, run_id, status="queued", accepting=1)


@pytest.mark.integration
def test_unknown_actual_stays_reserved(engine):
    run_id = "run-budget-pending"
    stored = model_budget(3, 0, 1)
    insert_budget_run(engine, run_id, stored)
    with write_uow(engine) as session:
        before_changes = session.scalar(text("SELECT total_changes()"))
        seen: list[str] = []
        raw = raw_sqlite(session)
        raw.set_trace_callback(seen.append)
        try:
            pending = settle_model_call(session, run_id, 1, None)
        finally:
            raw.set_trace_callback(None)
        after_changes = session.scalar(text("SELECT total_changes()"))
    updates = [item for item in seen if item and "UPDATE" in item.upper()]
    # The trace callback repeats the parent UPDATE once per trigger step.
    assert after_changes == before_changes + 1
    assert len(set(updates)) == 1
    assert pending.pending is True
    assert pending.budget_json == stored
    assert fetch_budget_text(engine, run_id) == stored
    loaded = json.loads(stored)
    assert loaded["spent_model_calls"] == 0
    assert loaded["reserved_model_calls"] == 1
    assert_budget_gate(engine, run_id, status="queued", accepting=1)


@pytest.mark.integration
def test_known_zero_actual_releases_the_reservation(engine):
    run_id = "run-budget-zero"
    insert_budget_run(engine, run_id, model_budget(3, 1, 2))
    with write_uow(engine) as session:
        settled = settle_model_call(session, run_id, 2, 0)
    assert settled.pending is False
    assert settled.budget_json == model_budget(3, 1, 0)
    assert fetch_budget_text(engine, run_id) == model_budget(3, 1, 0)
    assert_budget_gate(engine, run_id, status="queued", accepting=1)


@pytest.mark.integration
def test_settle_that_does_not_fit_leaves_the_text(engine):
    run_id = "run-budget-fit"
    stored = model_budget(3, 2, 1)
    insert_budget_run(engine, run_id, stored)
    with write_uow(engine) as session:
        with pytest.raises(BudgetConflict):
            assert_session_writes_nothing(
                session,
                lambda: settle_model_call(session, run_id, 2, 0),
            )
        with pytest.raises(BudgetConflict):
            assert_session_writes_nothing(
                session,
                lambda: settle_model_call(session, run_id, 1, 2),
            )
        with pytest.raises(BudgetConflict):
            assert_session_writes_nothing(
                session,
                lambda: settle_model_call(session, run_id, 2, None),
            )
        with pytest.raises(ValueError, match="actual"):
            assert_session_writes_nothing(
                session,
                lambda: settle_model_call(session, run_id, 1, True),
            )
    assert fetch_budget_text(engine, run_id) == stored
    assert_budget_gate(engine, run_id, status="queued", accepting=1)


@pytest.mark.integration
def test_rejected_budget_shapes_are_not_rewritten(engine):
    for index, sample in enumerate(INVALID_BUDGETS):
        run_id = f"run-budget-shape-{index}"
        if sample == "{}":
            insert_gate_run(engine, run_id)
        else:
            insert_budget_run(engine, run_id, sample)
        with write_uow(engine) as session:
            with pytest.raises(ValueError, match="budget_json shape is rejected"):
                assert_session_writes_nothing(
                    session,
                    lambda run_id=run_id: reserve_model_call(session, run_id, 1),
                )
            with pytest.raises(ValueError, match="budget_json shape is rejected"):
                assert_session_writes_nothing(
                    session,
                    lambda run_id=run_id: settle_model_call(session, run_id, 1, 1),
                )
            with pytest.raises(ValueError, match="budget_json shape is rejected"):
                assert_session_writes_nothing(
                    session,
                    lambda run_id=run_id: settle_model_call(session, run_id, 1, None),
                )
        assert fetch_budget_text(engine, run_id) == sample
        assert_budget_gate(engine, run_id, status="queued", accepting=1)


@pytest.mark.integration
def test_reserve_rejects_bad_estimate_and_exhausted_budget(engine):
    run_id = "run-budget-empty"
    stored = model_budget(1, 1, 0)
    insert_budget_run(engine, run_id, stored)
    with write_uow(engine) as session:
        for bad in (0, -1, True, 1.5):
            with pytest.raises(ValueError, match="estimate"):
                assert_session_writes_nothing(
                    session,
                    lambda bad=bad: reserve_model_call(session, run_id, bad),
                )
        blocked = assert_session_writes_nothing(
            session,
            lambda: reserve_model_call(session, run_id, 1),
        )
    assert blocked.reserved is False
    assert blocked.reason == "budget_exhausted"
    assert blocked.budget_json == stored
    assert fetch_budget_text(engine, run_id) == stored
    assert_budget_gate(engine, run_id, status="queued", accepting=1)


@pytest.mark.integration
def test_reserve_skips_runs_that_are_not_accepting(engine):
    stored = model_budget(3, 0, 0)
    cases = (
        ("run-budget-cancelled", "cancelled", 0),
        ("run-budget-shut", "running", 0),
        ("run-budget-done", "completed", 1),
    )
    for run_id, status, accepting in cases:
        insert_budget_run(
            engine,
            run_id,
            stored,
            execution_status=status,
            accepting_results=accepting,
        )
        with write_uow(engine) as session:
            result = assert_session_writes_nothing(
                session,
                lambda run_id=run_id: reserve_model_call(session, run_id, 1),
            )
        assert result.reserved is False
        assert result.reason == "not_accepting"
        assert result.budget_json == stored
        assert fetch_budget_text(engine, run_id) == stored
        assert_budget_gate(engine, run_id, status=status, accepting=accepting)


@pytest.mark.integration
def test_settle_records_usage_when_run_is_not_accepting(engine):
    run_id = "run-budget-late"
    insert_budget_run(
        engine,
        run_id,
        model_budget(2, 0, 1),
        execution_status="cancelled",
        accepting_results=0,
    )
    with write_uow(engine) as session:
        settled = settle_model_call(session, run_id, 1, 1)
    assert settled.pending is False
    assert settled.budget_json == model_budget(2, 1, 0)
    assert fetch_budget_text(engine, run_id) == model_budget(2, 1, 0)
    assert_budget_gate(engine, run_id, status="cancelled", accepting=0)


@pytest.mark.integration
def test_missing_run_budget_is_a_conflict(engine):
    with write_uow(engine) as session:
        with pytest.raises(BudgetConflict):
            assert_session_writes_nothing(
                session,
                lambda: reserve_model_call(session, "run-budget-missing", 1),
            )
        with pytest.raises(BudgetConflict):
            assert_session_writes_nothing(
                session,
                lambda: settle_model_call(session, "run-budget-missing", 1, 1),
            )


@pytest.mark.integration
@pytest.mark.concurrency
def test_two_connections_only_one_reserves_the_last_model_call(engine):
    run_id = "run-budget-race"
    insert_budget_run(engine, run_id, model_budget(1, 0, 0))
    start = Barrier(2)

    def run() -> object:
        start.wait(timeout=5)
        with write_uow(engine) as session:
            return reserve_model_call(session, run_id, 1)

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(run), pool.submit(run)]
        outcomes = [future.result(timeout=10) for future in futures]
    won = [item for item in outcomes if item.reserved]
    lost = [item for item in outcomes if not item.reserved]
    assert len(outcomes) == 2
    assert len(won) == 1
    assert len(lost) == 1
    assert won[0].reason is None
    assert lost[0].reason == "budget_exhausted"
    stored = fetch_budget_text(engine, run_id)
    assert stored == won[0].budget_json == lost[0].budget_json
    assert json.loads(stored) == {
        "max_model_calls": 1,
        "spent_model_calls": 0,
        "reserved_model_calls": 1,
    }
    assert_budget_gate(engine, run_id, status="queued", accepting=1)


@pytest.mark.integration
@pytest.mark.concurrency
def test_two_connections_one_settle_applies(engine):
    run_id = "run-settle-race"
    insert_budget_run(engine, run_id, model_budget(2, 0, 1))
    start = Barrier(2)

    def run() -> object:
        start.wait(timeout=5)
        with write_uow(engine) as session:
            return settle_model_call(session, run_id, 1, 1)

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(run), pool.submit(run)]
        won = []
        lost = 0
        for future in futures:
            try:
                won.append(future.result(timeout=10))
            except BudgetConflict:
                lost += 1
    assert len(won) == 1
    assert lost == 1
    assert won[0].pending is False
    stored = fetch_budget_text(engine, run_id)
    assert stored == won[0].budget_json
    assert json.loads(stored) == {
        "max_model_calls": 2,
        "spent_model_calls": 1,
        "reserved_model_calls": 0,
    }
    assert_budget_gate(engine, run_id, status="queued", accepting=1)


@pytest.mark.integration
@pytest.mark.parametrize(
    "category",
    ("budget_exhausted", "unauthorized", "payment_required", "invalid_schema"),
)
def test_non_retryable_job_error_fails_once(engine, category: str):
    insert_job(engine)
    attempt = claim(engine, owner="worker-1")
    finished = finish_attempt(
        engine,
        category=category,
        max_attempts=8,
        deadline=FUTURE_DEADLINE,
        available_at=LATER_AVAILABLE,
    )
    job = fetch_job(engine)
    assert finished.already_finished is False
    assert finished.status == "failed"
    assert finished.last_error == category
    assert finished.fencing_token == attempt.fencing_token == 1
    assert finished.attempt_no == attempt.attempt_no == 1
    assert finished.input_hash == INPUT_HASH
    assert finished.available_at == 0.0
    assert job.status == "failed"
    assert job.last_error == category
    assert job.lease_owner is None
    assert job.lease_until is None
    assert job.fencing_token == 1
    assert job.attempt_no == 1
    assert job.input_hash == INPUT_HASH
    assert fetch_available_at(engine) == 0.0
    assert job.output_ref is None
    assert fetch_receipts(engine) == []
    again = assert_failed_finish_is_quiet(engine)
    assert again.already_finished is True
    assert again.status == "failed"
    assert again.last_error == category
    assert fetch_job(engine) == job


@pytest.mark.integration
@pytest.mark.parametrize("category", ("rate_limited", "unavailable"))
def test_retryable_job_returns_to_queued_until_later(engine, category: str):
    insert_job(engine)
    attempt = claim(engine, owner="worker-1")
    finished = finish_attempt(
        engine,
        category=category,
        max_attempts=8,
        deadline=FUTURE_DEADLINE,
        available_at=LATER_AVAILABLE,
    )
    job = fetch_job(engine)
    assert finished.already_finished is False
    assert finished.status == "queued"
    assert finished.last_error == category
    assert finished.available_at == LATER_AVAILABLE
    assert finished.fencing_token == attempt.fencing_token
    assert finished.attempt_no == attempt.attempt_no
    assert finished.input_hash == INPUT_HASH
    assert job.status == "queued"
    assert job.last_error == category
    assert fetch_available_at(engine) == LATER_AVAILABLE
    assert job.lease_owner is None
    assert job.lease_until is None
    assert job.fencing_token == attempt.fencing_token
    assert job.attempt_no == attempt.attempt_no
    assert job.input_hash == INPUT_HASH
    assert job.output_ref is None
    assert job.status != "retry_wait"
    assert fetch_receipts(engine) == []
    with pytest.raises(StaleLease):
        claim(engine, owner="worker-2")
    assert fetch_job(engine) == job
    with pytest.raises(StaleLease):
        finish_attempt(
            engine,
            category=category,
            max_attempts=8,
            deadline=FUTURE_DEADLINE,
            available_at=LATER_AVAILABLE,
        )
    assert fetch_job(engine) == job


@pytest.mark.integration
def test_retryable_job_fails_when_deadline_has_passed(engine):
    insert_job(engine)
    attempt = claim(engine, owner="worker-1")
    finished = finish_attempt(
        engine,
        category="unavailable",
        max_attempts=8,
        deadline=1.0,
        available_at=LATER_AVAILABLE,
    )
    job = fetch_job(engine)
    assert finished.already_finished is False
    assert finished.status == "failed"
    assert finished.last_error == "deadline"
    assert finished.fencing_token == attempt.fencing_token
    assert finished.attempt_no == attempt.attempt_no
    assert finished.available_at == 0.0
    assert job.status == "failed"
    assert job.last_error == "deadline"
    assert job.lease_owner is None
    assert job.lease_until is None
    assert fetch_available_at(engine) == 0.0
    assert job.fencing_token == attempt.fencing_token
    assert job.attempt_no == attempt.attempt_no
    again = assert_failed_finish_is_quiet(engine)
    assert again.already_finished is True
    assert again.last_error == "deadline"
    assert fetch_job(engine) == job


@pytest.mark.integration
def test_failed_job_past_attempt_cap_does_not_return_to_queued(engine):
    insert_job(engine)
    attempt = claim(engine, owner="worker-1")
    assert attempt.attempt_no == 1
    with write_uow(engine) as session:
        before = session.scalar(text("SELECT total_changes()"))
        first = finish_job_attempt(
            session,
            job_id=JOB_ID,
            category="rate_limited",
            max_attempts=1,
            deadline=FUTURE_DEADLINE,
            available_at=LATER_AVAILABLE,
        )
        after_first = session.scalar(text("SELECT total_changes()"))

        def repeat() -> list[JobAttemptFinish]:
            found = []
            pairs = (
                ("unavailable", LATER_AVAILABLE + 1.0),
                ("rate_limited", LATER_AVAILABLE + 2.0),
                ("budget_exhausted", LATER_AVAILABLE + 3.0),
            )
            for category, later in pairs:
                found.append(
                    finish_job_attempt(
                        session,
                        job_id=JOB_ID,
                        category=category,
                        max_attempts=5,
                        deadline=FUTURE_DEADLINE,
                        available_at=later,
                    )
                )
            return found

        repeats = assert_session_writes_nothing(session, repeat)
    assert after_first == before + 1
    assert first.already_finished is False
    assert first.status == "failed"
    assert first.last_error == "attempts_exhausted"
    assert first.fencing_token == attempt.fencing_token
    assert first.attempt_no == attempt.attempt_no
    assert first.input_hash == INPUT_HASH
    assert first.available_at == 0.0
    assert len(repeats) == 3
    assert all(item.already_finished for item in repeats)
    assert all(item.status == "failed" for item in repeats)
    assert all(item.last_error == "attempts_exhausted" for item in repeats)
    assert all(item.available_at == 0.0 for item in repeats)
    job = fetch_job(engine)
    assert job.status == "failed"
    assert job.last_error == "attempts_exhausted"
    assert fetch_available_at(engine) == 0.0
    assert job.lease_owner is None
    assert job.lease_until is None
    assert job.fencing_token == attempt.fencing_token
    assert job.attempt_no == attempt.attempt_no
    assert job.input_hash == INPUT_HASH
    assert job.output_ref is None
    assert fetch_receipts(engine) == []


@pytest.mark.integration
def test_already_failed_job_is_not_rewritten(engine):
    insert_job(engine)
    insert_terminal_job(engine, "job-failed", "failed")
    before = fetch_job(engine, "job-failed")
    again = assert_failed_finish_is_quiet(engine, "job-failed")
    assert again.already_finished is True
    assert again.status == "failed"
    assert again.last_error is None
    assert again.fencing_token == 4
    assert again.attempt_no == 2
    assert fetch_job(engine, "job-failed") == before
    assert before.status == "failed"
    assert before.lease_owner is None
    assert before.lease_until is None


@pytest.mark.integration
def test_finish_rejects_bad_arguments_without_update(engine):
    insert_job(engine)
    attempt = claim(engine, owner="worker-1")
    cases = (
        {
            "category": "retry_wait",
            "max_attempts": 3,
            "deadline": FUTURE_DEADLINE,
            "available_at": LATER_AVAILABLE,
        },
        {
            "category": "rate_limited",
            "max_attempts": 0,
            "deadline": FUTURE_DEADLINE,
            "available_at": LATER_AVAILABLE,
        },
        {
            "category": "rate_limited",
            "max_attempts": True,
            "deadline": FUTURE_DEADLINE,
            "available_at": LATER_AVAILABLE,
        },
        {
            "category": "rate_limited",
            "max_attempts": 3,
            "deadline": float("nan"),
            "available_at": LATER_AVAILABLE,
        },
        {
            "category": "rate_limited",
            "max_attempts": 3,
            "deadline": FUTURE_DEADLINE,
            "available_at": 1.0,
        },
    )
    for case in cases:
        with write_uow(engine) as session:
            with pytest.raises(ValueError):
                assert_session_writes_nothing(
                    session,
                    lambda case=case: finish_job_attempt(session, job_id=JOB_ID, **case),
                )
    job = fetch_job(engine)
    assert job.status == "running"
    assert job.lease_owner == "worker-1"
    assert job.lease_until == attempt.lease_until
    assert job.fencing_token == attempt.fencing_token
    assert job.attempt_no == attempt.attempt_no
    assert job.input_hash == INPUT_HASH
    assert job.last_error is None
    assert fetch_available_at(engine) == 0.0
