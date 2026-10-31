"""Restart delivery after a committed outbox event has no notification row."""

from threading import Barrier, Thread
from uuid import uuid4

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from sinuo.infrastructure.database import write_uow
from sinuo.infrastructure.models import OutboxDelivery, OutboxEvent
from sinuo.workers.leases import claim_delivery, complete_delivery, recover_delivery

CONSUMER = "preview"


def commit_event(engine) -> str:
    event_id = str(uuid4())
    with write_uow(engine) as session:
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
    return event_id


def delivery_count(session: Session, event_id: str) -> int:
    counted = session.scalar(
        select(func.count())
        .select_from(OutboxDelivery)
        .where(
            OutboxDelivery.event_id == event_id,
            OutboxDelivery.consumer_name == CONSUMER,
        )
    )
    assert counted is not None
    return counted


@pytest.mark.recovery
def test_committed_event_is_recovered_claimed_and_not_reset(engine):
    event_id = commit_event(engine)
    with Session(engine) as session:
        assert session.get(OutboxEvent, event_id) is not None
        assert session.get(OutboxDelivery, (event_id, CONSUMER)) is None

    with write_uow(engine) as session:
        assert recover_delivery(session, event_id=event_id, consumer_name=CONSUMER) is True
        assert recover_delivery(session, event_id=event_id, consumer_name=CONSUMER) is False
        lease = claim_delivery(
            session,
            event_id=event_id,
            consumer_name=CONSUMER,
            owner="worker-1",
            now=0,
            ttl_seconds=10,
        )
    assert lease is not None
    with write_uow(engine) as session:
        complete_delivery(session, lease, now=1)
    with write_uow(engine) as session:
        assert recover_delivery(session, event_id=event_id, consumer_name=CONSUMER) is False
    with Session(engine) as session:
        row = session.get(OutboxDelivery, (event_id, CONSUMER))
        assert row is not None
        assert row.state == "delivered"
        assert delivery_count(session, event_id) == 1
        pending = session.scalar(
            select(func.count())
            .select_from(OutboxDelivery)
            .where(
                OutboxDelivery.event_id == event_id,
                OutboxDelivery.consumer_name == CONSUMER,
                OutboxDelivery.state == "pending",
            )
        )
        assert pending == 0


@pytest.mark.concurrency
def test_two_connections_recover_one_delivery_row(engine):
    event_id = commit_event(engine)
    start = Barrier(2)
    inserted: list[bool] = []
    errors: list[BaseException] = []

    def attempt() -> None:
        start.wait(timeout=5)
        try:
            with write_uow(engine) as session:
                inserted.append(
                    recover_delivery(session, event_id=event_id, consumer_name=CONSUMER)
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
    assert sorted(inserted) == [False, True]
    with Session(engine) as session:
        row = session.get(OutboxDelivery, (event_id, CONSUMER))
        assert row is not None
        assert row.state == "pending"
        assert row.lease_owner is None and row.lease_until is None
        assert row.fencing_token == 0 and row.attempts == 0
        assert delivery_count(session, event_id) == 1
