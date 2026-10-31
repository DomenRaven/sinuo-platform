from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from uuid import uuid4

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from sinuo.application.idempotency import CommandResponse, CommandScope, execute_idempotent
from sinuo.domain.errors import IdempotencyConflict, StaleLease
from sinuo.infrastructure.database import write_uow
from sinuo.infrastructure.models import IdempotencyRecord, OutboxDelivery, OutboxEvent
from sinuo.workers.leases import claim_delivery, complete_delivery

SCOPE = CommandScope("project-1", "actor-1", "example.confirm", "request-1")


def allow(_session):
    pass


def insert_event(session):
    event_id = str(uuid4())
    session.add(
        OutboxEvent(
            event_id=event_id,
            project_id="project-1",
            event_type="example.confirmed",
            schema_version=1,
            payload={"object_id": "object-1"},
            created_at=0,
        )
    )
    return CommandResponse(201, {"event_id": event_id})


def count(session, model):
    return session.scalar(select(func.count()).select_from(model))


@pytest.mark.recovery
def test_atomic_rollback_of_command_and_outbox(engine):
    with pytest.raises(RuntimeError):
        with write_uow(engine) as session:
            execute_idempotent(session, SCOPE, {"value": 1}, allow, insert_event)
            raise RuntimeError("failure before commit")
    with Session(engine) as session:
        assert count(session, OutboxEvent) == 0
        assert count(session, IdempotencyRecord) == 0


def test_same_key_different_body_is_rejected_and_replay_reauthorizes(engine):
    with write_uow(engine) as session:
        original = execute_idempotent(session, SCOPE, {"value": 1}, allow, insert_event)
    with write_uow(engine) as session:
        replay = execute_idempotent(session, SCOPE, {"value": 1}, allow, insert_event)
    assert replay == original
    with pytest.raises(IdempotencyConflict):
        with write_uow(engine) as session:
            execute_idempotent(session, SCOPE, {"value": 2}, allow, insert_event)

    def revoked(_session):
        raise PermissionError("revoked")

    with pytest.raises(PermissionError):
        with write_uow(engine) as session:
            execute_idempotent(session, SCOPE, {"value": 1}, revoked, insert_event)


@pytest.mark.concurrency
def test_two_connections_compete_for_one_command(engine):
    start = Barrier(2)

    def run():
        start.wait(timeout=5)
        with write_uow(engine) as session:
            return execute_idempotent(session, SCOPE, {"value": 1}, allow, insert_event)

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(run) for _ in range(2)]
        responses = [future.result(timeout=10) for future in futures]
    assert responses[0] == responses[1]
    with Session(engine) as session:
        assert count(session, OutboxEvent) == 1
        assert count(session, IdempotencyRecord) == 1


@pytest.mark.recovery
def test_expired_lease_cannot_persist_after_takeover(engine):
    with write_uow(engine) as session:
        response = insert_event(session)
        session.flush()
        event_id = response.body["event_id"]
        session.add(OutboxDelivery(event_id=event_id, consumer_name="preview"))
    with write_uow(engine) as session:
        first = claim_delivery(
            session,
            event_id=event_id,
            consumer_name="preview",
            owner="worker-1",
            now=0,
            ttl_seconds=10,
        )
    assert first is not None
    with write_uow(engine) as session:
        assert (
            claim_delivery(
                session,
                event_id=event_id,
                consumer_name="preview",
                owner="worker-2",
                now=9,
                ttl_seconds=10,
            )
            is None
        )
        second = claim_delivery(
            session,
            event_id=event_id,
            consumer_name="preview",
            owner="worker-2",
            now=10,
            ttl_seconds=10,
        )
    assert second is not None and second.fencing_token > first.fencing_token
    with pytest.raises(StaleLease):
        with write_uow(engine) as session:
            complete_delivery(session, first, now=11)
    with write_uow(engine) as session:
        complete_delivery(session, second, now=11)
    with Session(engine) as session:
        row = session.get(OutboxDelivery, (event_id, "preview"))
        assert row.state == "delivered" and row.attempts == 2
