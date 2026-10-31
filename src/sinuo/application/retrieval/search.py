"""Bounded keyword search over authorized evidence excerpts.

Short queries match the excerpt string inside locator_json. This module does
not call a model and does not insert or update business rows.
"""

import json
from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from sinuo.application.auth.authorizer import SessionAuthorizer
from sinuo.domain.auth.errors import (
    ActionForbidden,
    AuthRequired,
    NotFoundOrHidden,
    SourceRedacted,
)
from sinuo.domain.auth.principal import Principal
from sinuo.domain.auth.resource import ResourceRef
from sinuo.domain.public_contract import Action
from sinuo.infrastructure.domain_models import ArtifactVersion, EvidenceAnchor

_QUERY_MAX = 40
_LIMIT_MIN = 1
_LIMIT_MAX = 20
_DENIED = (ActionForbidden, NotFoundOrHidden, SourceRedacted)

STATUS_HIT = "hit"
STATUS_NO_HIT = "no_hit"
STATUS_NOT_INDEXED = "not_indexed"


@dataclass(frozen=True)
class EvidenceHit:
    artifact_version_id: str
    locator: dict[str, object]


@dataclass(frozen=True)
class VersionSearchStatus:
    artifact_version_id: str
    status: str


@dataclass(frozen=True)
class SearchEvidenceResult:
    hits: tuple[EvidenceHit, ...]
    versions: tuple[VersionSearchStatus, ...]
    counts: dict[str, int]
    truncated: bool


def search_evidence(
    session: Session,
    authorizer: SessionAuthorizer,
    principal: Principal,
    project_id: str,
    query: str,
    limit: int,
) -> SearchEvidenceResult:
    """Search one project's authorized excerpts. Other projects stay out."""

    checked = _require_query(query)
    bounded = _require_limit(limit)
    if not _project_is_listed(authorizer, principal, project_id):
        return _empty()
    with session.no_autoflush:
        version_ids = list(
            session.scalars(
                select(ArtifactVersion.id)
                .where(ArtifactVersion.project_id == project_id)
                .order_by(ArtifactVersion.id)
            ).all()
        )
        anchor_rows = session.execute(
            select(EvidenceAnchor.id, EvidenceAnchor.artifact_version_id)
            .where(EvidenceAnchor.project_id == project_id)
            .order_by(EvidenceAnchor.id)
        ).all()
        authorized_ids: list[str] = []
        authorized_versions: set[str] = set()
        anchored_versions: set[str] = set()
        for row in anchor_rows:
            anchored_versions.add(row.artifact_version_id)
            if _can_read_anchor(authorizer, principal, project_id, row.id):
                authorized_ids.append(row.id)
                authorized_versions.add(row.artifact_version_id)
        matched = _match_excerpts(session, project_id, authorized_ids, checked)
    ready = _ready_hits(matched)
    matched_versions = {version_id for version_id, _locator in ready}
    hits = tuple(
        EvidenceHit(version_id, locator) for version_id, locator in ready[:bounded]
    )
    versions: list[VersionSearchStatus] = []
    counts = {STATUS_HIT: 0, STATUS_NO_HIT: 0, STATUS_NOT_INDEXED: 0}
    for version_id in version_ids:
        if version_id in matched_versions:
            status = STATUS_HIT
        elif version_id in authorized_versions:
            status = STATUS_NO_HIT
        elif version_id in anchored_versions:
            continue
        else:
            status = STATUS_NOT_INDEXED
        versions.append(VersionSearchStatus(version_id, status))
        counts[status] += 1
    return SearchEvidenceResult(hits, tuple(versions), counts, len(ready) > bounded)


def _require_query(query: object) -> str:
    if not isinstance(query, str) or len(query) > _QUERY_MAX or query.strip() == "":
        raise ValueError("query")
    return query


def _require_limit(limit: object) -> int:
    if type(limit) is not int or limit < _LIMIT_MIN or limit > _LIMIT_MAX:
        raise ValueError("limit")
    return limit


def _empty() -> SearchEvidenceResult:
    return SearchEvidenceResult(
        (),
        (),
        {STATUS_HIT: 0, STATUS_NO_HIT: 0, STATUS_NOT_INDEXED: 0},
        False,
    )


def _project_is_listed(
    authorizer: SessionAuthorizer,
    principal: Principal,
    project_id: str,
) -> bool:
    """Hide a project the principal is not in. A member without project.read continues."""

    try:
        authorizer.authorize(
            Action.PROJECT_READ,
            ResourceRef(project_id=project_id, scope_type="project"),
            principal,
        )
    except AuthRequired:
        raise
    except ActionForbidden:
        return True
    except _DENIED:
        return False
    return True


def _can_read_anchor(
    authorizer: SessionAuthorizer,
    principal: Principal,
    project_id: str,
    anchor_id: str,
) -> bool:
    try:
        authorizer.authorize(
            Action.EVIDENCE_READ,
            ResourceRef(
                project_id=project_id,
                scope_type="evidence_anchor",
                evidence_anchor_id=anchor_id,
            ),
            principal,
        )
    except AuthRequired:
        raise
    except _DENIED:
        return False
    return True


def _match_excerpts(session: Session, project_id: str, anchor_ids: list[str], query: str):
    if not anchor_ids:
        return []
    excerpt = func.json_extract(EvidenceAnchor.locator_json, "$.excerpt")
    statement = (
        select(
            EvidenceAnchor.artifact_version_id,
            EvidenceAnchor.locator_json,
        )
        .where(
            EvidenceAnchor.project_id == project_id,
            EvidenceAnchor.id.in_(anchor_ids),
            func.json_type(EvidenceAnchor.locator_json, "$.excerpt") == "text",
            excerpt.contains(query, autoescape=True),
        )
        .order_by(EvidenceAnchor.artifact_version_id, EvidenceAnchor.id)
    )
    return session.execute(statement).all()


def _ready_hits(rows) -> list[tuple[str, dict[str, object]]]:
    ready: list[tuple[str, dict[str, object]]] = []
    for row in rows:
        locator = _locator(row.locator_json)
        if locator is None:
            continue
        ready.append((row.artifact_version_id, locator))
    return ready


def _locator(raw: str) -> dict[str, object] | None:
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(parsed, dict):
        return None
    excerpt = parsed.get("excerpt")
    if not isinstance(excerpt, str):
        return None
    return parsed
