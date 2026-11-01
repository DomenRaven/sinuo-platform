from typing import Any

from sqlalchemy import JSON, CheckConstraint, Float, ForeignKey, Integer, String
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

SCHEMA_REVISION = "0010_evidence_index"


class Base(DeclarativeBase):
    pass


class IdempotencyRecord(Base):
    __tablename__ = "idempotency_records"
    project_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    actor_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    command: Mapped[str] = mapped_column(String(100), primary_key=True)
    request_key: Mapped[str] = mapped_column(String(128), primary_key=True)
    request_hash: Mapped[str] = mapped_column(String(64))
    response_status: Mapped[int] = mapped_column(Integer)
    response_json: Mapped[dict[str, Any]] = mapped_column(JSON)
    created_at: Mapped[float] = mapped_column(Float)


class OutboxEvent(Base):
    __tablename__ = "outbox_events"
    event_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    project_id: Mapped[str] = mapped_column(String(64), index=True)
    event_type: Mapped[str] = mapped_column(String(100))
    schema_version: Mapped[int] = mapped_column(Integer)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON)
    created_at: Mapped[float] = mapped_column(Float)


class OutboxDelivery(Base):
    __tablename__ = "outbox_deliveries"
    __table_args__ = (
        CheckConstraint("state IN ('pending', 'leased', 'delivered')", name="ck_delivery_state"),
        CheckConstraint("fencing_token >= 0 AND attempts >= 0", name="ck_delivery_counters"),
        CheckConstraint(
            "(state = 'leased' AND lease_owner IS NOT NULL "
            "AND length(trim(lease_owner)) > 0 AND lease_until IS NOT NULL "
            "AND lease_until BETWEEN -1.7976931348623157e308 AND 1.7976931348623157e308 "
            "AND fencing_token > 0) OR (state IN ('pending', 'delivered') "
            "AND lease_owner IS NULL AND lease_until IS NULL)",
            name="ck_delivery_lease_consistency",
        ),
    )
    event_id: Mapped[str] = mapped_column(ForeignKey("outbox_events.event_id"), primary_key=True)
    consumer_name: Mapped[str] = mapped_column(String(100), primary_key=True)
    state: Mapped[str] = mapped_column(String(16), default="pending")
    lease_owner: Mapped[str | None] = mapped_column(String(100), nullable=True)
    fencing_token: Mapped[int] = mapped_column(Integer, default=0)
    lease_until: Mapped[float | None] = mapped_column(Float, nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)


import sinuo.infrastructure.domain_models as _domain_models  # noqa: E402

# domain_models rejects tables outside its downgrade list. That check runs
# while this module is still loading when domain_models is imported first.
# Register after the check so readiness sees the later tables on the normal path.
if hasattr(_domain_models, "DOWNGRADE_ORDER"):
    import sinuo.infrastructure.methods  # noqa: E402, F401
    import sinuo.infrastructure.suggestions  # noqa: E402, F401
