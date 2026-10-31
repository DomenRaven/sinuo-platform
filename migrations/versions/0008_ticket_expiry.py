"""Nullable expiry on upload tickets.

Downgrade drops this revision's expires_at column.
A null value means the ticket does not expire.
"""

import sqlalchemy as sa
from alembic import op

revision = "0008_ticket_expiry"
down_revision = "0007_run_origin"
branch_labels = None
depends_on = None

_EXPIRES_AT = """
ALTER TABLE upload_tickets ADD COLUMN expires_at FLOAT
"""


def _column_names(connection) -> set[str]:
    rows = connection.execute(sa.text("PRAGMA table_info(upload_tickets)"))
    return {row[1] for row in rows}


def apply(connection) -> None:
    """Add expires_at when it is missing. Does not commit.

    Revision 0005 creates ``upload_tickets`` without this column. Existing
    rows stay null.
    """

    if "expires_at" not in _column_names(connection):
        connection.execute(sa.text(_EXPIRES_AT))


def remove(connection) -> None:
    if "expires_at" in _column_names(connection):
        connection.execute(sa.text("ALTER TABLE upload_tickets DROP COLUMN expires_at"))


def upgrade() -> None:
    apply(op.get_bind())


def downgrade() -> None:
    remove(op.get_bind())
