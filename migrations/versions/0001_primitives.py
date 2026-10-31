"""Foundation only: command idempotency, outbox events and fenced delivery leases."""

import sqlalchemy as sa
from alembic import op

revision = "0001_primitives"
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "idempotency_records",
        sa.Column("project_id", sa.String(64), primary_key=True),
        sa.Column("actor_id", sa.String(64), primary_key=True),
        sa.Column("command", sa.String(100), primary_key=True),
        sa.Column("request_key", sa.String(128), primary_key=True),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column("response_status", sa.Integer, nullable=False),
        sa.Column("response_json", sa.JSON, nullable=False),
        sa.Column("created_at", sa.Float, nullable=False),
    )
    op.create_table(
        "outbox_events",
        sa.Column("event_id", sa.String(64), primary_key=True),
        sa.Column("project_id", sa.String(64), nullable=False),
        sa.Column("event_type", sa.String(100), nullable=False),
        sa.Column("schema_version", sa.Integer, nullable=False),
        sa.Column("payload", sa.JSON, nullable=False),
        sa.Column("created_at", sa.Float, nullable=False),
    )
    op.create_index("ix_outbox_events_project_id", "outbox_events", ["project_id"])
    op.create_table(
        "outbox_deliveries",
        sa.Column(
            "event_id", sa.String(64), sa.ForeignKey("outbox_events.event_id"), primary_key=True
        ),
        sa.Column("consumer_name", sa.String(100), primary_key=True),
        sa.Column("state", sa.String(16), nullable=False),
        sa.Column("lease_owner", sa.String(100), nullable=True),
        sa.Column("fencing_token", sa.Integer, nullable=False),
        sa.Column("lease_until", sa.Float, nullable=True),
        sa.Column("attempts", sa.Integer, nullable=False),
        sa.CheckConstraint("state IN ('pending', 'leased', 'delivered')", name="ck_delivery_state"),
        sa.CheckConstraint("fencing_token >= 0 AND attempts >= 0", name="ck_delivery_counters"),
    )


def downgrade():
    op.drop_table("outbox_deliveries")
    op.drop_index("ix_outbox_events_project_id", table_name="outbox_events")
    op.drop_table("outbox_events")
    op.drop_table("idempotency_records")
