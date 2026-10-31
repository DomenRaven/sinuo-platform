"""Synthetic boundary and failure cases found during independent foundation review."""

import asyncio
from dataclasses import replace
from uuid import uuid4

import pytest
from alembic import command
from conftest import alembic_config
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from sinuo.agents.contracts import InputSnapshot, SpecialistResult, SpecialistTask
from sinuo.agents.provider import DisabledModelProvider
from sinuo.agents.runner import DagRunner
from sinuo.application.idempotency import CommandResponse, CommandScope, execute_idempotent
from sinuo.infrastructure.database import write_uow
from sinuo.infrastructure.models import IdempotencyRecord, OutboxDelivery, OutboxEvent
from sinuo.workers.leases import claim_delivery, complete_delivery

SNAPSHOT = InputSnapshot("snapshot-synthetic", "project-synthetic", "hash", '{"synthetic":true}')


def delivery(session, **overrides):
    event_id = str(uuid4())
    session.add(
        OutboxEvent(
            event_id=event_id,
            project_id="project-synthetic",
            event_type="synthetic.created",
            schema_version=1,
            payload={"synthetic": True},
            created_at=0,
        )
    )
    session.flush()
    session.add(OutboxDelivery(event_id=event_id, consumer_name="synthetic", **overrides))
    session.flush()
    return event_id


@pytest.mark.recovery
@pytest.mark.parametrize("target", ["session", "connection"])
def test_inner_commit_cannot_persist_business_effect_or_receipt(engine, target):
    scope = CommandScope("project-synthetic", "actor-synthetic", "synthetic.confirm", target)

    def handler(session):
        delivery(session)
        return CommandResponse(201, {"synthetic": True})

    with pytest.raises(RuntimeError, match="owns the commit"):
        with write_uow(engine) as session:
            execute_idempotent(session, scope, {}, lambda _: None, handler)
            if target == "connection":
                session.connection().commit()
            else:
                session.commit()
    with Session(engine) as session:
        for model in (OutboxEvent, OutboxDelivery, IdempotencyRecord):
            assert session.scalar(select(func.count()).select_from(model)) == 0
    # The pooled connection remains usable after failure.
    with write_uow(engine) as session:
        delivery(session)


@pytest.mark.parametrize(
    "now,ttl",
    [
        (float("nan"), 1),
        (0, float("nan")),
        (0, float("inf")),
        (float("inf"), 1),
        (1e308, 1e308),
        (1e20, 1),
        (0, 0),
        (0, -1),
    ],
)
def test_invalid_lease_times_do_not_change_pending_delivery(engine, now, ttl):
    with write_uow(engine) as session:
        event_id = delivery(session)
    with pytest.raises(ValueError):
        with write_uow(engine) as session:
            claim_delivery(
                session,
                event_id=event_id,
                consumer_name="synthetic",
                owner="worker",
                now=now,
                ttl_seconds=ttl,
            )
    with Session(engine) as session:
        row = session.get(OutboxDelivery, (event_id, "synthetic"))
        assert row.state == "pending" and row.fencing_token == row.attempts == 0


@pytest.mark.parametrize(
    "values",
    [
        {"state": "leased", "lease_owner": "worker", "lease_until": None, "fencing_token": 1},
        {"state": "leased", "lease_owner": " ", "lease_until": 10, "fencing_token": 1},
        {
            "state": "leased",
            "lease_owner": "worker",
            "lease_until": float("inf"),
            "fencing_token": 1,
        },
        {
            "state": "leased",
            "lease_owner": "worker",
            "lease_until": float("nan"),
            "fencing_token": 1,
        },
        {"state": "leased", "lease_owner": "worker", "lease_until": 10, "fencing_token": 0},
        {"state": "pending", "lease_owner": "worker", "lease_until": 10},
        {"state": "delivered", "lease_owner": None, "lease_until": 10},
    ],
)
def test_database_rejects_inconsistent_delivery_leases(engine, values):
    with pytest.raises(IntegrityError):
        with write_uow(engine) as session:
            delivery(session, **values)


@pytest.mark.parametrize("now", [float("nan"), float("inf"), -float("inf")])
def test_completion_rejects_nonfinite_time(engine, now):
    with write_uow(engine) as session:
        event_id = delivery(session)
        lease = claim_delivery(
            session,
            event_id=event_id,
            consumer_name="synthetic",
            owner="worker",
            now=0,
            ttl_seconds=10,
        )
    with pytest.raises(ValueError):
        with write_uow(engine) as session:
            complete_delivery(session, lease, now=now)


@pytest.mark.parametrize(
    "options",
    [
        {"max_parallel": 1.5},
        {"max_parallel": True},
        {"max_specialists": 1.5},
        {"max_specialists": True},
        {"timeout_seconds": float("nan")},
        {"timeout_seconds": float("inf")},
        {"timeout_seconds": True},
        {"timeout_seconds": 0},
        {"timeout_seconds": -1},
    ],
)
def test_invalid_execution_budgets_rejected_at_construction(options):
    with pytest.raises(ValueError):
        DagRunner(DisabledModelProvider(), **options)


@pytest.mark.parametrize(
    "output",
    [
        '{"x":NaN}',
        '{"x":Infinity}',
        '{"x":-Infinity}',
        '{"x":1e999}',
        '{"x":1,"x":2}',
        '{"nested":{"x":1,"x":2}}',
        b'{"synthetic":true}',
    ],
)
def test_provider_output_must_be_strict_json(output):
    class Provider:
        async def execute(self, task, snapshot, upstream):
            result = SpecialistResult(task.task_id, "completed", "{}", role=task.role)
            return replace(result, output_json=output)

    (result,) = asyncio.run(DagRunner(Provider()).run((SpecialistTask("a", "evidence"),), SNAPSHOT))
    assert (result.status, result.error_code) == ("failed", "INVALID_OUTPUT")


@pytest.mark.concurrency
def test_ready_child_does_not_wait_for_unrelated_task():
    async def scenario():
        child_started = asyncio.Event()

        class Provider:
            async def execute(self, task, snapshot, upstream):
                if task.task_id == "b":
                    # B can finish only after C starts. C depends on A, not B.
                    await child_started.wait()
                if task.task_id == "c":
                    assert [result.task_id for result in upstream] == ["a"]
                    child_started.set()
                return SpecialistResult(task.task_id, "completed", "{}", role=task.role)

        tasks = (
            SpecialistTask("a", "evidence"),
            SpecialistTask("b", "method"),
            SpecialistTask("c", "critic", ("a",)),
        )
        return await DagRunner(Provider(), timeout_seconds=2).run(tasks, SNAPSHOT)

    results = asyncio.run(scenario())
    assert all(result.status == "completed" for result in results)


@pytest.mark.integration
def test_lease_migration_preserves_existing_valid_data(bare_engine):
    with bare_engine.begin() as connection:
        command.upgrade(alembic_config(connection), "0001_primitives")
    with write_uow(bare_engine) as session:
        event_id = delivery(session)
        lease = claim_delivery(
            session,
            event_id=event_id,
            consumer_name="synthetic",
            owner="worker",
            now=0,
            ttl_seconds=10,
        )
    with bare_engine.begin() as connection:
        command.upgrade(alembic_config(connection), "head")
        assert connection.execute(text("PRAGMA foreign_key_check")).all() == []
    with write_uow(bare_engine) as session:
        complete_delivery(session, lease, now=1)
    with Session(bare_engine) as session:
        assert session.get(OutboxDelivery, (event_id, "synthetic")).state == "delivered"
