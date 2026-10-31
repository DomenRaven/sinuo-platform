"""T05 draft. Not an installed revision. The integration owner copies it.

The first slice stores a referable original in the existing artifact_versions
table. upload_tickets is the table that slice reads and writes. stored_objects
and object_pins are the generation and pin tables from construction doc 06;
this slice does not insert them.

artifact_versions is not altered. MISSING_ARTIFACT_VERSION_COLUMNS lists what
is still absent. The object key used by the slice is

    objects/<project_id>/sha256/<sha256>/<generation_id>/original

Doc 06 also shows a sha256/<prefix>/<hash>/ shard. This draft does not add it.
"""

import sqlalchemy as sa
from alembic import op

revision = "0005_stored_objects"
down_revision = "0004_repair_invariants"
branch_labels = None
depends_on = None

OBJECT_KEY_PATTERN = "objects/<project_id>/sha256/<sha256>/<generation_id>/original"

MISSING_ARTIFACT_VERSION_COLUMNS: dict[str, str] = {
    "object_id": "Foreign key to stored_objects. This slice keeps the location in object_key.",
    "generation_id": "Stored in the object key and the outbox payload, not on the version row.",
    "original_filename": "A filename is metadata and must not become a path segment.",
    "preview_state": "A missing preview file is not a successful preview.",
    "preview_failure": "Converter errors need an explicit reason.",
    "preview_generator": "P0 image, PDF, and GLB generator name.",
    "preview_generator_version": "Generator version is still unstored.",
}

_STORED_OBJECTS = """
CREATE TABLE stored_objects (
  object_id VARCHAR(64) NOT NULL PRIMARY KEY,
  project_id VARCHAR(64) NOT NULL,
  sha256 VARCHAR(64) NOT NULL,
  generation_id VARCHAR(64) NOT NULL,
  state VARCHAR(32) NOT NULL,
  fencing_token INTEGER NOT NULL,
  byte_size INTEGER NOT NULL,
  created_at FLOAT NOT NULL,
  CONSTRAINT uq_stored_objects_generation UNIQUE (project_id, generation_id),
  CONSTRAINT fk_stored_objects_project FOREIGN KEY(project_id)
    REFERENCES projects(id) ON DELETE RESTRICT ON UPDATE RESTRICT,
  CONSTRAINT ck_stored_objects_hash CHECK (
    length(sha256) = 64 AND sha256 NOT GLOB '*[^0-9a-f]*'
  ),
  CONSTRAINT ck_stored_objects_state CHECK (
    state IN ('reserved', 'ready', 'gc_claimed', 'deleted', 'quarantined')
  ),
  CONSTRAINT ck_stored_objects_fence CHECK (fencing_token >= 0),
  CONSTRAINT ck_stored_objects_size CHECK (byte_size >= 0)
)
"""

_UPLOAD_TICKETS = """
CREATE TABLE upload_tickets (
  upload_id VARCHAR(64) NOT NULL PRIMARY KEY,
  project_id VARCHAR(64) NOT NULL,
  artifact_id VARCHAR(64) NOT NULL,
  expected_size INTEGER NOT NULL,
  declared_hash VARCHAR(64) NOT NULL,
  media_type VARCHAR(255) NOT NULL,
  license VARCHAR(200) NOT NULL,
  sensitivity VARCHAR(32) NOT NULL,
  ai_processing_allowed INTEGER NOT NULL,
  created_by_account_id VARCHAR(64) NOT NULL,
  status VARCHAR(16) NOT NULL,
  failure_reason VARCHAR(64),
  artifact_version_id VARCHAR(64),
  generation_id VARCHAR(64),
  created_at FLOAT NOT NULL,
  closed_at FLOAT,
  CONSTRAINT fk_upload_tickets_project FOREIGN KEY(project_id)
    REFERENCES projects(id) ON DELETE RESTRICT ON UPDATE RESTRICT,
  CONSTRAINT fk_upload_tickets_artifact FOREIGN KEY(project_id, artifact_id)
    REFERENCES artifacts(project_id, id) ON DELETE RESTRICT ON UPDATE RESTRICT,
  CONSTRAINT fk_upload_tickets_author FOREIGN KEY(created_by_account_id)
    REFERENCES accounts(id) ON DELETE RESTRICT ON UPDATE RESTRICT,
  CONSTRAINT fk_upload_tickets_version FOREIGN KEY(project_id, artifact_version_id)
    REFERENCES artifact_versions(project_id, id) ON DELETE RESTRICT ON UPDATE RESTRICT,
  CONSTRAINT ck_upload_tickets_size CHECK (expected_size >= 0),
  CONSTRAINT ck_upload_tickets_hash CHECK (
    length(declared_hash) = 64 AND declared_hash NOT GLOB '*[^0-9a-f]*'
  ),
  CONSTRAINT ck_upload_tickets_status CHECK (
    status IN ('open', 'rejected', 'registered')
  ),
  CONSTRAINT ck_upload_tickets_sensitivity CHECK (
    sensitivity IN ('project', 'sensitive', 'released')
  ),
  CONSTRAINT ck_upload_tickets_ai CHECK (ai_processing_allowed IN (0, 1)),
  CONSTRAINT ck_upload_tickets_media CHECK (length(trim(media_type)) > 0),
  CONSTRAINT ck_upload_tickets_license CHECK (length(trim(license)) > 0),
  CONSTRAINT ck_upload_tickets_shape CHECK (
    (
      status = 'open'
      AND artifact_version_id IS NULL
      AND generation_id IS NULL
      AND failure_reason IS NULL
      AND closed_at IS NULL
    ) OR (
      status = 'rejected'
      AND artifact_version_id IS NULL
      AND generation_id IS NULL
      AND failure_reason IS NOT NULL
      AND length(trim(failure_reason)) > 0
      AND closed_at IS NOT NULL
    ) OR (
      status = 'registered'
      AND artifact_version_id IS NOT NULL
      AND generation_id IS NOT NULL
      AND failure_reason IS NULL
      AND closed_at IS NOT NULL
    )
  )
)
"""

_OBJECT_PINS = """
CREATE TABLE object_pins (
  pin_id VARCHAR(64) NOT NULL PRIMARY KEY,
  object_id VARCHAR(64) NOT NULL,
  project_id VARCHAR(64) NOT NULL,
  upload_id VARCHAR(64),
  purpose VARCHAR(16) NOT NULL,
  expires_at FLOAT NOT NULL,
  consumed_at FLOAT,
  fencing_token INTEGER NOT NULL,
  created_at FLOAT NOT NULL,
  CONSTRAINT fk_object_pins_object FOREIGN KEY(object_id)
    REFERENCES stored_objects(object_id) ON DELETE RESTRICT ON UPDATE RESTRICT,
  CONSTRAINT fk_object_pins_project FOREIGN KEY(project_id)
    REFERENCES projects(id) ON DELETE RESTRICT ON UPDATE RESTRICT,
  CONSTRAINT ck_object_pins_purpose CHECK (
    purpose IN ('upload', 'download', 'backup')
  ),
  CONSTRAINT ck_object_pins_fence CHECK (fencing_token >= 0),
  CONSTRAINT ck_object_pins_consumed CHECK (
    consumed_at IS NULL OR consumed_at >= created_at
  )
)
"""

CREATE_SQL: tuple[str, ...] = (_STORED_OBJECTS, _UPLOAD_TICKETS, _OBJECT_PINS)
DROP_TABLES: tuple[str, ...] = ("object_pins", "upload_tickets", "stored_objects")

_UPLOAD_UPDATE = """
CREATE TRIGGER upload_tickets_before_update
BEFORE UPDATE ON upload_tickets
BEGIN
  SELECT RAISE(ABORT, 'upload_ticket_identity')
  WHERE NEW.upload_id IS NOT OLD.upload_id
    OR NEW.project_id IS NOT OLD.project_id
    OR NEW.artifact_id IS NOT OLD.artifact_id
    OR NEW.expected_size IS NOT OLD.expected_size
    OR NEW.declared_hash IS NOT OLD.declared_hash
    OR NEW.media_type IS NOT OLD.media_type
    OR NEW.license IS NOT OLD.license
    OR NEW.sensitivity IS NOT OLD.sensitivity
    OR NEW.ai_processing_allowed IS NOT OLD.ai_processing_allowed
    OR NEW.created_by_account_id IS NOT OLD.created_by_account_id
    OR NEW.created_at IS NOT OLD.created_at;
  SELECT RAISE(ABORT, 'upload_ticket_reopen')
  WHERE OLD.status IS NOT 'open';
  SELECT RAISE(ABORT, 'upload_ticket_status')
  WHERE NEW.status IS NOT 'rejected' AND NEW.status IS NOT 'registered';
END
"""

_UPLOAD_DELETE = """
CREATE TRIGGER upload_tickets_before_delete
BEFORE DELETE ON upload_tickets
BEGIN
  SELECT RAISE(ABORT, 'upload_tickets_delete_forbidden');
END
"""

_OBJECT_INSERT = """
CREATE TRIGGER stored_objects_before_insert
BEFORE INSERT ON stored_objects
BEGIN
  SELECT RAISE(ABORT, 'stored_object_initial_state')
  WHERE NEW.state IS NOT 'reserved';
END
"""

_OBJECT_UPDATE = """
CREATE TRIGGER stored_objects_before_update
BEFORE UPDATE ON stored_objects
BEGIN
  SELECT RAISE(ABORT, 'stored_object_identity')
  WHERE NEW.object_id IS NOT OLD.object_id
    OR NEW.project_id IS NOT OLD.project_id
    OR NEW.sha256 IS NOT OLD.sha256
    OR NEW.generation_id IS NOT OLD.generation_id
    OR NEW.byte_size IS NOT OLD.byte_size
    OR NEW.created_at IS NOT OLD.created_at;
  SELECT RAISE(ABORT, 'stored_object_transition')
  WHERE NOT (
    (OLD.state = 'reserved' AND NEW.state IN ('ready', 'quarantined', 'gc_claimed'))
    OR (OLD.state = 'ready' AND NEW.state IN ('ready', 'gc_claimed'))
    OR (OLD.state = 'gc_claimed' AND NEW.state IN ('gc_claimed', 'deleted'))
    OR (OLD.state = 'quarantined' AND NEW.state IN ('quarantined', 'gc_claimed', 'deleted'))
    OR (OLD.state = 'deleted' AND NEW.state = 'deleted')
  );
  SELECT RAISE(ABORT, 'stored_object_fence')
  WHERE NEW.fencing_token < OLD.fencing_token;
END
"""

_OBJECT_DELETE = """
CREATE TRIGGER stored_objects_before_delete
BEFORE DELETE ON stored_objects
BEGIN
  SELECT RAISE(ABORT, 'stored_objects_delete_forbidden');
END
"""

_PIN_INSERT = """
CREATE TRIGGER object_pins_before_insert
BEFORE INSERT ON object_pins
BEGIN
  SELECT RAISE(ABORT, 'object_pin_project')
  WHERE NOT EXISTS (
    SELECT 1 FROM stored_objects
    WHERE object_id = NEW.object_id AND project_id = NEW.project_id
  );
  SELECT RAISE(ABORT, 'object_pin_on_dead_generation')
  WHERE EXISTS (
    SELECT 1 FROM stored_objects
    WHERE object_id = NEW.object_id
      AND state IN ('gc_claimed', 'deleted')
  );
END
"""

_PIN_UPDATE = """
CREATE TRIGGER object_pins_before_update
BEFORE UPDATE ON object_pins
BEGIN
  SELECT RAISE(ABORT, 'object_pin_identity')
  WHERE NEW.pin_id IS NOT OLD.pin_id
    OR NEW.object_id IS NOT OLD.object_id
    OR NEW.project_id IS NOT OLD.project_id
    OR NEW.upload_id IS NOT OLD.upload_id
    OR NEW.purpose IS NOT OLD.purpose
    OR NEW.expires_at IS NOT OLD.expires_at
    OR NEW.fencing_token IS NOT OLD.fencing_token
    OR NEW.created_at IS NOT OLD.created_at;
  SELECT RAISE(ABORT, 'object_pin_reopen')
  WHERE OLD.consumed_at IS NOT NULL AND NEW.consumed_at IS NOT OLD.consumed_at;
END
"""

_PIN_DELETE = """
CREATE TRIGGER object_pins_before_delete
BEFORE DELETE ON object_pins
BEGIN
  SELECT RAISE(ABORT, 'object_pins_delete_forbidden');
END
"""

TRIGGER_SQL: tuple[str, ...] = (
    _UPLOAD_UPDATE,
    _UPLOAD_DELETE,
    _OBJECT_INSERT,
    _OBJECT_UPDATE,
    _OBJECT_DELETE,
    _PIN_INSERT,
    _PIN_UPDATE,
    _PIN_DELETE,
)

TRIGGER_NAMES: tuple[str, ...] = (
    "upload_tickets_before_update",
    "upload_tickets_before_delete",
    "stored_objects_before_insert",
    "stored_objects_before_update",
    "stored_objects_before_delete",
    "object_pins_before_insert",
    "object_pins_before_update",
    "object_pins_before_delete",
)


def apply(connection) -> None:
    """Create the draft tables on a database already at 0004. Does not commit."""

    for statement in CREATE_SQL:
        connection.execute(sa.text(statement))
    for statement in TRIGGER_SQL:
        connection.execute(sa.text(statement))


def remove(connection) -> None:
    for name in TRIGGER_NAMES:
        connection.execute(sa.text(f"DROP TRIGGER IF EXISTS {name}"))
    for name in DROP_TABLES:
        connection.execute(sa.text(f"DROP TABLE IF EXISTS {name}"))


def upgrade() -> None:
    apply(op.get_bind())


def downgrade() -> None:
    remove(op.get_bind())
