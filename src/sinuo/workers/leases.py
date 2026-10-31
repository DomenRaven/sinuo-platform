import math
from dataclasses import dataclass

from sqlalchemy import and_, or_, update
from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.orm import Session

from sinuo.domain.errors import StaleLease
from sinuo.infrastructure.models import OutboxDelivery


@dataclass(frozen=True)
class Lease:
    event_id: str
    consumer_name: str
    owner: str
    fencing_token: int
    expires_at: float


def claim_delivery(
    session: Session,
    *,
    event_id: str,
    consumer_name: str,
    owner: str,
    now: float,
    ttl_seconds: float,
) -> Lease | None:
    expires_at = now + ttl_seconds
    if (
        not math.isfinite(now)
        or not math.isfinite(ttl_seconds)
        or ttl_seconds <= 0
        or not math.isfinite(expires_at)
        or expires_at <= now
        or not owner.strip()
    ):
        raise ValueError("Lease owner and a finite, representable expiry are required")
    statement = (
        update(OutboxDelivery)
        .where(
            OutboxDelivery.event_id == event_id,
            OutboxDelivery.consumer_name == consumer_name,
            or_(
                OutboxDelivery.state == "pending",
                and_(OutboxDelivery.state == "leased", OutboxDelivery.lease_until <= now),
            ),
        )
        .values(
            state="leased",
            lease_owner=owner,
            fencing_token=OutboxDelivery.fencing_token + 1,
            attempts=OutboxDelivery.attempts + 1,
            lease_until=expires_at,
        )
        .returning(OutboxDelivery.fencing_token)
    )
    token = session.execute(statement).scalar_one_or_none()
    if token is None:
        return None
    return Lease(event_id, consumer_name, owner, token, expires_at)


def complete_delivery(session: Session, lease: Lease, *, now: float) -> None:
    """Persist the consumer effect and completion in the same caller-owned transaction."""
    if not math.isfinite(now):
        raise ValueError("Completion time must be finite")
    result = session.execute(
        update(OutboxDelivery)
        .where(
            OutboxDelivery.event_id == lease.event_id,
            OutboxDelivery.consumer_name == lease.consumer_name,
            OutboxDelivery.state == "leased",
            OutboxDelivery.lease_owner == lease.owner,
            OutboxDelivery.fencing_token == lease.fencing_token,
            OutboxDelivery.lease_until > now,
        )
        .values(state="delivered", lease_owner=None, lease_until=None)
    )
    if result.rowcount != 1:
        raise StaleLease(lease.event_id)


def recover_delivery(session: Session, *, event_id: str, consumer_name: str) -> bool:
    """Insert one pending delivery when that consumer has no row. Does not commit.

    The caller owns the transaction. A second insert of the same pair does nothing,
    so a delivered row stays delivered.
    """
    inserted = session.execute(
        insert(OutboxDelivery)
        .values(
            event_id=event_id,
            consumer_name=consumer_name,
            state="pending",
            lease_owner=None,
            fencing_token=0,
            lease_until=None,
            attempts=0,
        )
        .on_conflict_do_nothing(index_elements=["event_id", "consumer_name"])
        .returning(OutboxDelivery.event_id)
    ).scalar_one_or_none()
    return inserted is not None
