"""Reject unclaimable leases; preserve valid foundation data on upgrade."""

import sqlalchemy as sa
from alembic import op

revision = "0002_lease_invariants"
down_revision = "0001_primitives"
branch_labels = None
depends_on = None

LEASE_CONSISTENCY = """
(state = 'leased' AND lease_owner IS NOT NULL AND length(trim(lease_owner)) > 0
AND lease_until IS NOT NULL AND lease_until BETWEEN -1.7976931348623157e308
AND 1.7976931348623157e308 AND fencing_token > 0)
OR (state IN ('pending', 'delivered') AND lease_owner IS NULL AND lease_until IS NULL)"""


def upgrade():
    # Fail before replacing the table if a legacy malformed row exists. Repair must
    # be deliberate; the migration cannot silently reassign an active delivery.
    invalid = (
        op.get_bind()
        .execute(sa.text(f"SELECT count(*) FROM outbox_deliveries WHERE NOT ({LEASE_CONSISTENCY})"))
        .scalar_one()
    )
    if invalid:
        raise RuntimeError("Invalid legacy leases; inspect delivery state before migration")
    with op.batch_alter_table("outbox_deliveries") as batch:
        batch.create_check_constraint("ck_delivery_lease_consistency", LEASE_CONSISTENCY)


def downgrade():
    with op.batch_alter_table("outbox_deliveries") as batch:
        batch.drop_constraint("ck_delivery_lease_consistency", type_="check")
