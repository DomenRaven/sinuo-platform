"""Catch up databases already stamped 0003, and add guards 0003 did not have.

0003 was edited in place before any durable database existed. This revision
still runs on a database whose stamp is 0003 so an older trigger body is
replaced. Downgrade drops only the triggers introduced here. It does not
restore the bypassable task trigger.
"""

import importlib.util
from pathlib import Path

import sqlalchemy as sa
from alembic import op

revision = "0004_repair_invariants"
down_revision = "0003_domain_identity"
branch_labels = None
depends_on = None

_REFRESH = (
    "projects_before_update",
    "stages_before_update",
    "pages_before_update",
    "page_drafts_before_update",
    "run_tasks_before_update",
)

_LOGIN_INSERT = """
CREATE TRIGGER accounts_login_name_before_insert
BEFORE INSERT ON accounts
BEGIN
  SELECT RAISE(ABORT, 'accounts_login_name')
  WHERE sinuo_login_name_canonical(NEW.login_name) IS NOT NEW.login_name;
END
"""

_LOGIN_UPDATE = """
CREATE TRIGGER accounts_login_name_before_update
BEFORE UPDATE ON accounts
BEGIN
  SELECT RAISE(ABORT, 'accounts_login_name')
  WHERE sinuo_login_name_canonical(NEW.login_name) IS NOT NEW.login_name;
END
"""

_JOBS_UPDATE = """
CREATE TRIGGER jobs_before_update
BEFORE UPDATE ON jobs
BEGIN
  SELECT RAISE(ABORT, 'jobs_terminal')
  WHERE OLD.status IN ('succeeded', 'failed', 'cancelled', 'superseded')
  AND (NEW.status IS NOT OLD.status
    OR NEW.fencing_token IS NOT OLD.fencing_token
    OR NEW.lease_owner IS NOT OLD.lease_owner
    OR NEW.lease_until IS NOT OLD.lease_until
    OR NEW.attempt_no IS NOT OLD.attempt_no);
END
"""

_NEW_TRIGGERS = (
    "accounts_login_name_before_insert",
    "accounts_login_name_before_update",
    "jobs_before_update",
)


def _prior_statements() -> dict[str, str]:
    path = Path(__file__).with_name("0003_domain_identity.py")
    spec = importlib.util.spec_from_file_location("sinuo_migration_0003", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("0003 revision cannot be loaded")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return {name: statement for name, _table, statement in module._REGISTERED}


def upgrade() -> None:
    statements = _prior_statements()
    for name in _REFRESH:
        op.execute(sa.text(f"DROP TRIGGER IF EXISTS {name}"))
        op.execute(sa.text(statements[name]))
    op.execute(sa.text(_LOGIN_INSERT))
    op.execute(sa.text(_LOGIN_UPDATE))
    op.execute(sa.text(_JOBS_UPDATE))


def downgrade() -> None:
    for name in _NEW_TRIGGERS:
        op.execute(sa.text(f"DROP TRIGGER IF EXISTS {name}"))
