"""Course and project method cards.

Downgrade drops this revision's triggers and the method_cards table.
Cards are appended as new versions. This revision does not build FTS5.
"""

import sqlalchemy as sa
from alembic import op

revision = "0009_method_cards"
down_revision = "0008_ticket_expiry"
branch_labels = None
depends_on = None

_TABLE = """
CREATE TABLE method_cards (
  id VARCHAR(64) NOT NULL PRIMARY KEY,
  library_key VARCHAR(64) NOT NULL,
  project_id VARCHAR(64),
  card_key VARCHAR(64) NOT NULL,
  version_no INTEGER NOT NULL,
  name VARCHAR(200) NOT NULL,
  source_json TEXT NOT NULL,
  applicable_json TEXT NOT NULL,
  inputs_json TEXT NOT NULL,
  steps_json TEXT NOT NULL,
  outputs_json TEXT NOT NULL,
  limits_json TEXT NOT NULL,
  created_at FLOAT NOT NULL,
  CONSTRAINT fk_method_cards_project FOREIGN KEY(project_id)
    REFERENCES projects(id) ON DELETE RESTRICT ON UPDATE RESTRICT,
  CONSTRAINT uq_method_cards_version UNIQUE (library_key, card_key, version_no),
  CONSTRAINT ck_method_cards_library CHECK (
    (library_key = 'course' AND project_id IS NULL)
    OR (project_id IS NOT NULL AND library_key = project_id)
  ),
  CONSTRAINT ck_method_cards_version CHECK (version_no >= 1),
  CONSTRAINT ck_method_cards_name CHECK (length(trim(name)) > 0),
  CONSTRAINT ck_method_cards_key CHECK (length(trim(card_key)) > 0),
  CONSTRAINT ck_method_cards_source CHECK (
    json_valid(source_json) AND json_type(source_json) = 'object'
  ),
  CONSTRAINT ck_method_cards_applicable CHECK (
    json_valid(applicable_json) AND json_type(applicable_json) = 'object'
  ),
  CONSTRAINT ck_method_cards_inputs CHECK (
    json_valid(inputs_json) AND json_type(inputs_json) = 'array'
  ),
  CONSTRAINT ck_method_cards_steps CHECK (
    json_valid(steps_json) AND json_type(steps_json) = 'array'
  ),
  CONSTRAINT ck_method_cards_outputs CHECK (
    json_valid(outputs_json) AND json_type(outputs_json) = 'array'
  ),
  CONSTRAINT ck_method_cards_limits CHECK (
    json_valid(limits_json) AND json_type(limits_json) = 'array'
  )
)
"""

_UPDATE = """
CREATE TRIGGER method_cards_before_update
BEFORE UPDATE ON method_cards
BEGIN
  SELECT RAISE(ABORT, 'method_cards_immutable');
END
"""

_DELETE = """
CREATE TRIGGER method_cards_before_delete
BEFORE DELETE ON method_cards
BEGIN
  SELECT RAISE(ABORT, 'method_cards_immutable');
END
"""

_TRIGGERS = ("method_cards_before_update", "method_cards_before_delete")


def apply(connection) -> None:
    """Create method_cards on a database already at 0008. Does not commit."""

    connection.execute(sa.text(_TABLE))
    connection.execute(sa.text(_UPDATE))
    connection.execute(sa.text(_DELETE))


def remove(connection) -> None:
    for name in _TRIGGERS:
        connection.execute(sa.text(f"DROP TRIGGER IF EXISTS {name}"))
    connection.execute(sa.text("DROP TABLE IF EXISTS method_cards"))


def upgrade() -> None:
    apply(op.get_bind())


def downgrade() -> None:
    remove(op.get_bind())
