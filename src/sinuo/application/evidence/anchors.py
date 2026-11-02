"""Append an evidence anchor on one artifact version. The caller owns commit.

The anchor stores that version id, a locator that contains the excerpt, and a
page, region, or timecode. A correction inserts a new row whose supersedes_id
points at the previous row. This command does not update or delete anchors.
"""

import hashlib
import json
import uuid
from collections.abc import Mapping
from dataclasses import dataclass

from sqlalchemy import select, update
from sqlalchemy.orm import Session
from sqlalchemy.orm.util import identity_key

from sinuo.application.versions.pages import ProjectRevisionConflict
from sinuo.domain.auth.errors import InvalidSchema, NotFoundOrHidden, SourceRedacted
from sinuo.domain.public_contract import Action
from sinuo.domain.schema_types import SENSITIVITY_LEVELS, VISIBILITY_LEVELS, canonical_audit_payload
from sinuo.infrastructure.domain_models import (
    ArtifactVersion,
    AuditEvent,
    EvidenceAnchor,
    PrivacyTombstone,
    Project,
)

_EXCERPT_MAX = 4000
_EXTRACTOR_MAX = 64
_ID_MAX = 64
_PAGE_MAX = 100_000
_REGION_MAX = 200
_TIMECODE_MAX = 32
_OFFSET_MAX = 10_000_000
_SCHEMA_VERSION = 1
_EXTRACTOR_VERSION = "manual-1"
_ALIASES = frozenset({"latest", "head", "current", "newest"})
_PLACE_KEYS = ("page", "region", "timecode")
_LOCATOR_KEYS = frozenset({"excerpt", "page", "region", "timecode", "start", "end"})


class AnchorInvalid(Exception):
    """The locator does not place an excerpt, or a correction would retarget it."""

    def __init__(self, category: str) -> None:
        self.category = category
        super().__init__(category)


@dataclass(frozen=True)
class AnchorView:
    evidence_anchor_id: str
    project_id: str
    artifact_version_id: str
    locator_type: str
    locator: dict[str, object]
    excerpt_hash: str
    extractor_version: str
    visibility: str
    sensitivity: str
    supersedes_id: str | None
    schema_version: int
    created_by_account_id: str
    created_at: float
    audit_event_id: str
    project_revision: int


def create_anchor(
    session: Session,
    *,
    project_id: str,
    actor_id: str,
    artifact_version_id: str,
    excerpt: str,
    locator: object,
    supersedes_id: str | None,
    visibility: str | None,
    extractor_version: str | None,
    now: float,
) -> AnchorView:
    """Insert one anchor. Does not commit and does not rewrite an existing row."""

    version_id = _object_id(artifact_version_id, alias=True)
    stored_excerpt = _excerpt(excerpt)
    locator_type, locator_json = _locator_document(stored_excerpt, locator)
    version = _version(session, project_id, version_id)
    previous = _previous(session, project_id, supersedes_id, version.id)
    chosen_visibility = _visibility(visibility, version.sensitivity)
    chosen_extractor = _extractor(extractor_version)
    project_revision = _advance_project_revision(session, project_id)
    anchor_id = str(uuid.uuid4())
    digest = hashlib.sha256(stored_excerpt.encode("utf-8")).hexdigest()
    session.add(
        EvidenceAnchor(
            id=anchor_id,
            project_id=project_id,
            artifact_version_id=version.id,
            locator_type=locator_type,
            locator_json=locator_json,
            excerpt_hash=digest,
            extractor_version=chosen_extractor,
            visibility=chosen_visibility,
            sensitivity=version.sensitivity,
            supersedes_id=previous,
            schema_version=_SCHEMA_VERSION,
            created_by_account_id=actor_id,
            created_at=now,
        )
    )
    session.flush()
    audit_event_id = _audit(
        session,
        project_id=project_id,
        actor_id=actor_id,
        anchor_id=anchor_id,
        artifact_version_id=version.id,
        locator_type=locator_type,
        excerpt_hash=digest,
        supersedes_id=previous,
        project_revision=project_revision,
        now=now,
    )
    parsed = json.loads(locator_json)
    if not isinstance(parsed, dict):
        raise InvalidSchema()
    return AnchorView(
        evidence_anchor_id=anchor_id,
        project_id=project_id,
        artifact_version_id=version.id,
        locator_type=locator_type,
        locator=parsed,
        excerpt_hash=digest,
        extractor_version=chosen_extractor,
        visibility=chosen_visibility,
        sensitivity=version.sensitivity,
        supersedes_id=previous,
        schema_version=_SCHEMA_VERSION,
        created_by_account_id=actor_id,
        created_at=now,
        audit_event_id=audit_event_id,
        project_revision=project_revision,
    )


def _version(session: Session, project_id: str, version_id: str) -> ArtifactVersion:
    row = session.get(ArtifactVersion, version_id)
    if row is None or row.project_id != project_id:
        raise NotFoundOrHidden()
    if row.sensitivity not in SENSITIVITY_LEVELS:
        raise InvalidSchema()
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
    row = session.get(EvidenceAnchor, previous_id)
    if row is None or row.project_id != project_id:
        raise NotFoundOrHidden()
    if _tombstoned(session, project_id, "evidence_anchor", row.id):
        raise SourceRedacted()
    if row.artifact_version_id != artifact_version_id:
        raise AnchorInvalid("artifact_version_id")
    return row.id


def _locator_document(excerpt: str, locator: object) -> tuple[str, str]:
    """Keep the excerpt inside the locator. Search reads that key back out."""

    if not isinstance(locator, dict):
        raise InvalidSchema()
    if set(locator) - _LOCATOR_KEYS:
        raise InvalidSchema()
    if "excerpt" in locator and locator["excerpt"] != excerpt:
        raise AnchorInvalid("excerpt")
    places = [key for key in _PLACE_KEYS if key in locator]
    if len(places) != 1:
        raise AnchorInvalid("locator")
    place = places[0]
    stored: dict[str, object] = {"excerpt": excerpt}
    if place == "page":
        stored["page"] = _page_number(locator["page"])
        locator_type = "page"
    elif place == "region":
        stored["region"] = _region(locator["region"])
        locator_type = "region"
    else:
        stored["timecode"] = _timecode(locator["timecode"])
        locator_type = "timecode"
    span = _span(locator)
    if span is not None:
        stored["start"] = span[0]
        stored["end"] = span[1]
    encoded = json.dumps(
        stored,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return locator_type, encoded


def _page_number(value: object) -> int:
    if type(value) is not int:
        raise InvalidSchema()
    if value < 1 or value > _PAGE_MAX:
        raise AnchorInvalid("page")
    return value


def _region(value: object) -> str | dict[str, int | float]:
    if isinstance(value, str):
        if not value.strip() or value != value.strip() or len(value) > _REGION_MAX:
            raise AnchorInvalid("region")
        return value
    if not isinstance(value, dict) or set(value) != {"x", "y", "width", "height"}:
        raise AnchorInvalid("region")
    box = {key: _coordinate(value[key]) for key in ("x", "y", "width", "height")}
    if box["width"] <= 0 or box["height"] <= 0:
        raise AnchorInvalid("region")
    return box


def _coordinate(value: object) -> int | float:
    if type(value) is int:
        return value
    if type(value) is float and value == value and abs(value) != float("inf"):
        return value
    raise InvalidSchema()


def _timecode(value: object) -> str:
    if not isinstance(value, str):
        raise InvalidSchema()
    if (
        not value
        or value != value.strip()
        or len(value) > _TIMECODE_MAX
        or ":" not in value
        or any(char.isspace() for char in value)
    ):
        raise AnchorInvalid("timecode")
    return value


def _span(locator: Mapping[str, object]) -> tuple[int, int] | None:
    has_start = "start" in locator
    has_end = "end" in locator
    if not has_start and not has_end:
        return None
    if not has_start or not has_end:
        raise InvalidSchema()
    start = locator["start"]
    end = locator["end"]
    if type(start) is not int or type(end) is not int:
        raise InvalidSchema()
    if start < 0 or end < start or end > _OFFSET_MAX:
        raise InvalidSchema()
    return start, end


def _excerpt(value: object) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > _EXCERPT_MAX:
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
        raise AnchorInvalid("artifact_version_id")
    return value


def _visibility(value: str | None, sensitivity: str) -> str:
    if value is None:
        if sensitivity == "sensitive":
            return "restricted"
        return "project"
    if value not in VISIBILITY_LEVELS:
        raise InvalidSchema()
    if sensitivity == "sensitive" and value != "restricted":
        raise AnchorInvalid("visibility")
    return value


def _extractor(value: str | None) -> str:
    if value is None:
        return _EXTRACTOR_VERSION
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or len(value) > _EXTRACTOR_MAX
    ):
        raise InvalidSchema()
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
    anchor_id: str,
    artifact_version_id: str,
    locator_type: str,
    excerpt_hash: str,
    supersedes_id: str | None,
    project_revision: int,
    now: float,
) -> str:
    payload: dict[str, object] = {
        "evidence_anchor_id": anchor_id,
        "artifact_version_id": artifact_version_id,
        "locator_type": locator_type,
        "excerpt_hash": excerpt_hash,
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
            command=Action.EVIDENCE_CREATE.value,
            subject_type="evidence_anchor",
            subject_id=anchor_id,
            project_revision=project_revision,
            payload=canonical_audit_payload(payload),
            created_at=now,
        )
    )
    session.flush()
    return audit_event_id
