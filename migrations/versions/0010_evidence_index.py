"""FTS5 trigram index of evidence excerpts.

Downgrade drops this revision's virtual table. SQLite drops that table's
shadow tables with it. Evidence anchors and original versions stay.
"""

import sqlalchemy as sa
from alembic import op

revision = "0010_evidence_index"
down_revision = "0009_method_cards"
branch_labels = None
depends_on = None

_TABLE = """
CREATE VIRTUAL TABLE evidence_excerpt_index USING fts5(
  excerpt,
  anchor_id UNINDEXED,
  project_id UNINDEXED,
  artifact_version_id UNINDEXED,
  locator_json UNINDEXED,
  excerpt_hash UNINDEXED,
  generator UNINDEXED,
  index_revision UNINDEXED,
  tokenize = 'trigram'
)
"""


def apply(connection) -> None:
    """Create the excerpt index on a database already at 0009. Does not commit."""

    connection.execute(sa.text(_TABLE))


def remove(connection) -> None:
    connection.execute(sa.text("DROP TABLE IF EXISTS evidence_excerpt_index"))


def upgrade() -> None:
    apply(op.get_bind())


def downgrade() -> None:
    remove(op.get_bind())
