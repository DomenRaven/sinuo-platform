"""Agent run origin and the preference revision that started a policy run.

Downgrade drops this revision's two triggers, then those two columns.
Existing rows default to manual.
"""

import sqlalchemy as sa
from alembic import op

revision = "0007_run_origin"
down_revision = "0006_suggestion_preferences"
branch_labels = None
depends_on = None

_ORIGIN = """
ALTER TABLE agent_runs ADD COLUMN origin VARCHAR(16) NOT NULL DEFAULT 'manual'
"""

_PREFERENCE_REVISION = """
ALTER TABLE agent_runs ADD COLUMN preference_revision INTEGER
"""

# SQLite cannot DROP COLUMN while a CHECK still names it. These triggers are
# removed before the columns on downgrade. manual stays null; policy is >= 1.
_INSERT = """
CREATE TRIGGER agent_runs_origin_before_insert
BEFORE INSERT ON agent_runs
BEGIN
  SELECT RAISE(ABORT, 'agent_runs_origin')
  WHERE NOT (
    (NEW.origin = 'manual' AND NEW.preference_revision IS NULL)
    OR (
      NEW.origin = 'policy'
      AND NEW.preference_revision IS NOT NULL
      AND NEW.preference_revision >= 1
    )
  );
END
"""

_UPDATE = """
CREATE TRIGGER agent_runs_origin_before_update
BEFORE UPDATE ON agent_runs
BEGIN
  SELECT RAISE(ABORT, 'agent_runs_origin')
  WHERE NOT (
    (NEW.origin = 'manual' AND NEW.preference_revision IS NULL)
    OR (
      NEW.origin = 'policy'
      AND NEW.preference_revision IS NOT NULL
      AND NEW.preference_revision >= 1
    )
  );
END
"""

_TRIGGER_NAMES = (
    "agent_runs_origin_before_insert",
    "agent_runs_origin_before_update",
)


def _column_names(connection) -> set[str]:
    rows = connection.execute(sa.text("PRAGMA table_info(agent_runs)"))
    return {row[1] for row in rows}


def apply(connection) -> None:
    """Add missing columns and the origin triggers. Does not commit.

    Revision 0003 creates ``agent_runs`` from the current ORM, so a fresh
    upgrade already has both columns. A database stamped 0006 does not.
    """

    present = _column_names(connection)
    if "origin" not in present:
        connection.execute(sa.text(_ORIGIN))
    if "preference_revision" not in present:
        connection.execute(sa.text(_PREFERENCE_REVISION))
    for name in _TRIGGER_NAMES:
        connection.execute(sa.text(f"DROP TRIGGER IF EXISTS {name}"))
    connection.execute(sa.text(_INSERT))
    connection.execute(sa.text(_UPDATE))


def remove(connection) -> None:
    for name in _TRIGGER_NAMES:
        connection.execute(sa.text(f"DROP TRIGGER IF EXISTS {name}"))
    present = _column_names(connection)
    if "preference_revision" in present:
        connection.execute(sa.text("ALTER TABLE agent_runs DROP COLUMN preference_revision"))
    if "origin" in present:
        connection.execute(sa.text("ALTER TABLE agent_runs DROP COLUMN origin"))


def upgrade() -> None:
    apply(op.get_bind())


def downgrade() -> None:
    remove(op.get_bind())
