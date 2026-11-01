"""Append a test record on one artifact version. The caller owns commit.

The record stores the path project, the signed-in actor, and the exact
artifact version id from the command. A correction inserts a new row whose
supersedes_id points at the previous row and keeps that same version id.
This command does not update or delete test records.
"""

import json
import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.orm import Session
from sqlalchemy.orm.util import identity_key

from sinuo.application.versions.pages import ProjectRevisionConflict
from sinuo.domain.auth.errors import InvalidSchema, NotFoundOrHidden, SourceRedacted
from sinuo.domain.public_contract import Action
from sinuo.domain.schema_types import DocumentSchemaError, canonical_audit_payload, canonical_json
from sinuo.infrastructure.domain_models import (
    ArtifactVersion,
    AuditEvent,
    PrivacyTombstone,
    Project,
    TestRecord,
)

_TEXT_MAX = 8000
_SOURCE_MAX = 2000
_CONDITIONS_MAX = 8000
_ID_MAX = 64
_ALIASES = frozenset({"latest", "head", "current", "newest"})
_CONDITION_FORBIDDEN = frozenset({"prompt", "api_key", "password", "token"})


class TestRecordInvalid(Exception):
    """The record is not bound to the exact version, or a correction would move it."""

    def __init__(self, category: str) -> None:
        self.category = category
        super().__init__(category)


@dataclass(frozen=True)
class TestRecordView:
    test_record_id: str
    project_id: str
    account_id: str
    artifact_version_id: str
    conditions: dict[str, Any]
    observation: str
    result: str
    source: str | None
    supersedes_id: str | None
    project_revision: int
    created_at: float
    audit_event_id: str


def create_test_record(
    session: Session,
    *,
    project_id: str,
    actor_id: str,
    artifact_version_id: str,
    conditions: object,
    observation: str,
    result: str,
    source: str | None,
    supersedes_id: str | None,
    now: float,
) -> TestRecordView:
    """Insert one test record. Does not commit and does not rewrite an existing row."""

    version_id = _object_id(artifact_version_id, alias=True)
    stored_conditions = _conditions(conditions)
    stored_observation = _text(observation)
    stored_result = _text(result)
    stored_source = _source(source)
    version = _version(session, project_id, version_id)
    previous = _previous(session, project_id, supersedes_id, version.id)
    project_revision = _advance_project_revision(session, project_id)
    record_id = str(uuid.uuid4())
    session.add(
        TestRecord(
            id=record_id,
            project_id=project_id,
            account_id=actor_id,
            artifact_version_id=version.id,
            conditions_json=stored_conditions,
            observation=stored_observation,
            result=stored_result,
            source=stored_source,
            supersedes_id=previous,
            project_revision=project_revision,
            created_at=now,
        )
    )
    session.flush()
    audit_event_id = _audit(
        session,
        project_id=project_id,
        actor_id=actor_id,
        record_id=record_id,
        artifact_version_id=version.id,
        supersedes_id=previous,
        project_revision=project_revision,
        now=now,
    )
    parsed = json.loads(stored_conditions)
    if not isinstance(parsed, dict):
        raise InvalidSchema()
    return TestRecordView(
        test_record_id=record_id,
        project_id=project_id,
        account_id=actor_id,
        artifact_version_id=version.id,
        conditions=parsed,
        observation=stored_observation,
        result=stored_result,
        source=stored_source,
        supersedes_id=previous,
        project_revision=project_revision,
        created_at=now,
        audit_event_id=audit_event_id,
    )


def _version(session: Session, project_id: str, version_id: str) -> ArtifactVersion:
    row = session.get(ArtifactVersion, version_id)
    if row is None or row.project_id != project_id:
        raise NotFoundOrHidden()
    if _tombstoned(session, project_id, "artifact_version", row.id):
        raise SourceRedacted()
    return row


def _previous(
    session: Session,
    project_id: str,
    supersedes_id: str | None,
    artifact_version_id: str,
) -> str | None:
    if supersedes_id is None:
        return None
    previous_id = _object_id(supersedes_id, alias=False)
    row = session.get(TestRecord, previous_id)
    if row is None or row.project_id != project_id:
        raise NotFoundOrHidden()
    if _tombstoned(session, project_id, "test_record", row.id):
        raise SourceRedacted()
    if row.artifact_version_id != artifact_version_id:
        raise TestRecordInvalid("artifact_version_id")
    return row.id


def _conditions(value: object) -> str:
    if not isinstance(value, dict) or not value:
        raise InvalidSchema()
    try:
        encoded = canonical_json(value, forbidden=_CONDITION_FORBIDDEN)
    except DocumentSchemaError as error:
        raise InvalidSchema() from error
    if len(encoded) > _CONDITIONS_MAX:
        raise InvalidSchema()
    return encoded


def _text(value: object) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > _TEXT_MAX:
        raise InvalidSchema()
    return value


def _source(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip() or len(value) > _SOURCE_MAX:
        raise InvalidSchema()
    return value


def _object_id(value: object, *, alias: bool) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or len(value) > _ID_MAX
        or any(char.isspace() for char in value)
    ):
        raise InvalidSchema()
    if alias and value.casefold() in _ALIASES:
        raise TestRecordInvalid("artifact_version_id")
    return value


def _tombstoned(session: Session, project_id: str, object_type: str, object_id: str) -> bool:
    found = session.scalar(
        select(PrivacyTombstone.id).where(
            PrivacyTombstone.project_id == project_id,
            PrivacyTombstone.object_type == object_type,
            PrivacyTombstone.object_id == object_id,
        )
    )
    return found is not None


def _advance_project_revision(session: Session, project_id: str) -> int:
    current = session.scalar(select(Project.revision).where(Project.id == project_id))
    if type(current) is not int:
        raise NotFoundOrHidden()
    result = session.execute(
        update(Project)
        .where(Project.id == project_id, Project.revision == current)
        .values(revision=current + 1),
        execution_options={"synchronize_session": False},
    )
    if result.rowcount != 1:
        raise ProjectRevisionConflict(project_id)
    cached = session.identity_map.get(identity_key(Project, (project_id,)))
    if cached is not None:
        session.expire(cached)
    return current + 1


def _audit(
    session: Session,
    *,
    project_id: str,
    actor_id: str,
    record_id: str,
    artifact_version_id: str,
    supersedes_id: str | None,
    project_revision: int,
    now: float,
) -> str:
    payload: dict[str, object] = {
        "test_record_id": record_id,
        "artifact_version_id": artifact_version_id,
        "project_revision": project_revision,
    }
    if supersedes_id is not None:
        payload["supersedes_id"] = supersedes_id
    audit_event_id = str(uuid.uuid4())
    session.add(
        AuditEvent(
            id=audit_event_id,
            project_id=project_id,
            actor_id=actor_id,
            command=Action.TEST_RECORD.value,
            subject_type="test_record",
            subject_id=record_id,
            project_revision=project_revision,
            payload=canonical_audit_payload(payload),
            created_at=now,
        )
    )
    session.flush()
    return audit_event_id
