"""Suggestion preferences and their deduplicated trigger records.

Downgrade drops only the two tables created here.
"""

import sqlalchemy as sa
from alembic import op

revision = "0006_suggestion_preferences"
down_revision = "0005_stored_objects"
branch_labels = None
depends_on = None

_PREFERENCES = """
CREATE TABLE suggestion_preferences (
  project_id VARCHAR(64) NOT NULL,
  actor_id VARCHAR(64) NOT NULL,
  mode VARCHAR(16) NOT NULL,
  scope_json TEXT NOT NULL,
  cooldown_seconds INTEGER NOT NULL,
  digest_interval_seconds INTEGER NOT NULL,
  revision INTEGER NOT NULL,
  next_due_at FLOAT,
  updated_at FLOAT NOT NULL,
  PRIMARY KEY (project_id, actor_id),
  CONSTRAINT fk_suggestion_preferences_project FOREIGN KEY(project_id)
    REFERENCES projects(id) ON DELETE RESTRICT ON UPDATE RESTRICT,
  CONSTRAINT fk_suggestion_preferences_actor FOREIGN KEY(actor_id)
    REFERENCES accounts(id) ON DELETE RESTRICT ON UPDATE RESTRICT,
  CONSTRAINT ck_suggestion_preferences_mode CHECK (
    mode IN ('off', 'manual', 'event', 'digest')
  ),
  CONSTRAINT ck_suggestion_preferences_revision CHECK (revision >= 1),
  CONSTRAINT ck_suggestion_preferences_cooldown CHECK (cooldown_seconds >= 0),
  CONSTRAINT ck_suggestion_preferences_interval CHECK (digest_interval_seconds > 0),
  CONSTRAINT ck_suggestion_preferences_scope CHECK (
    json_valid(scope_json) AND json_type(scope_json) = 'object'
  )
)
"""

_TRIGGERS = """
CREATE TABLE suggestion_triggers (
  project_id VARCHAR(64) NOT NULL,
  actor_id VARCHAR(64) NOT NULL,
  preference_revision INTEGER NOT NULL,
  trigger_kind VARCHAR(16) NOT NULL,
  trigger_key VARCHAR(200) NOT NULL,
  outcome VARCHAR(16) NOT NULL,
  job_id VARCHAR(64),
  created_at FLOAT NOT NULL,
  PRIMARY KEY (project_id, actor_id, preference_revision, trigger_kind, trigger_key),
  CONSTRAINT fk_suggestion_triggers_preference FOREIGN KEY(project_id, actor_id)
    REFERENCES suggestion_preferences(project_id, actor_id)
    ON DELETE RESTRICT ON UPDATE RESTRICT,
  CONSTRAINT fk_suggestion_triggers_job FOREIGN KEY(job_id)
    REFERENCES jobs(id) ON DELETE RESTRICT ON UPDATE RESTRICT,
  CONSTRAINT ck_suggestion_triggers_kind CHECK (
    trigger_kind IN ('event', 'digest')
  ),
  CONSTRAINT ck_suggestion_triggers_outcome CHECK (
    outcome IN ('queued', 'coalesced', 'suppressed')
  ),
  CONSTRAINT ck_suggestion_triggers_job CHECK (
    (outcome = 'queued' AND job_id IS NOT NULL)
    OR (outcome IN ('coalesced', 'suppressed') AND job_id IS NULL)
  ),
  CONSTRAINT ck_suggestion_triggers_revision CHECK (preference_revision >= 1),
  CONSTRAINT ck_suggestion_triggers_key CHECK (length(trim(trigger_key)) > 0)
)
"""

CREATE_SQL: tuple[str, ...] = (_PREFERENCES, _TRIGGERS)
DROP_TABLES: tuple[str, ...] = ("suggestion_triggers", "suggestion_preferences")


def apply(connection) -> None:
    """Create the two tables on a database already at 0005. Does not commit."""

    for statement in CREATE_SQL:
        connection.execute(sa.text(statement))


def remove(connection) -> None:
    for name in DROP_TABLES:
        connection.execute(sa.text(f"DROP TABLE IF EXISTS {name}"))


def upgrade() -> None:
    apply(op.get_bind())


def downgrade() -> None:
    remove(op.get_bind())
