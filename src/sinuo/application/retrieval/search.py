"""Authorized search over the evidence excerpt index.

Search reads. It does not call a model and does not write. Rebuild reads
evidence anchors and inserts missing index rows only. unicode61 is not used:
it matches a whole Chinese phrase and misses a piece inside that phrase.
Queries of three or more characters use the SQLite trigram tokenizer. Shorter
queries use an escaped substring match on the already authorized index rows.
"""

import json
from dataclasses import dataclass

from sqlalchemy import select, text
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
_VERSION_MAX = 64
_TRIGRAM_MIN = 3
_DENIED = (ActionForbidden, NotFoundOrHidden, SourceRedacted)
_ALIASES = frozenset({"latest", "head", "current", "newest"})

EVIDENCE_INDEX = "evidence_excerpt_index"
_GENERATOR = "evidence-anchor"
_INDEX_REVISION = "trigram-1"

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
    *,
    artifact_version_id: str | None = None,
) -> SearchEvidenceResult:
    """Search one project's authorized indexed excerpts.

    The current ``evidence.read`` set is calculated first. Trigram or
    substring matching then runs only inside that set. ``artifact_version_id``
    limits the result to that exact original version. ``latest``, ``head``,
    ``current`` and ``newest`` are rejected. Omitting the version does not
    retarget an older excerpt onto a newer original.
    """

    checked = _require_query(query)
    bounded = _require_limit(limit)
    version_filter = _optional_version(artifact_version_id)
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
        if version_filter is not None:
            if version_filter not in version_ids:
                return _empty()
            version_ids = [version_filter]
        anchor_rows = session.execute(
            select(EvidenceAnchor.id, EvidenceAnchor.artifact_version_id)
            .where(EvidenceAnchor.project_id == project_id)
            .order_by(EvidenceAnchor.id)
        ).all()
        if version_filter is not None:
            anchor_rows = [
                row for row in anchor_rows if row.artifact_version_id == version_filter
            ]
        authorized_ids: list[str] = []
        authorized_versions: set[str] = set()
        anchored_versions: set[str] = set()
        for row in anchor_rows:
            anchored_versions.add(row.artifact_version_id)
            if _can_read_anchor(authorizer, principal, project_id, row.id):
                authorized_ids.append(row.id)
                authorized_versions.add(row.artifact_version_id)
        indexed_rows = _indexed_rows(session, authorized_ids)
        indexed_ids = [row.anchor_id for row in indexed_rows]
        indexed_versions = {row.artifact_version_id for row in indexed_rows}
        matched = _match_index(session, indexed_ids, checked)
    visible = set(version_ids)
    ready = [item for item in _ready_hits(matched) if item[0] in visible]
    matched_versions = {version_id for version_id, _locator in ready}
    hits = tuple(
        EvidenceHit(version_id, locator) for version_id, locator in ready[:bounded]
    )
    versions: list[VersionSearchStatus] = []
    counts = {STATUS_HIT: 0, STATUS_NO_HIT: 0, STATUS_NOT_INDEXED: 0}
    for version_id in version_ids:
        if version_id in matched_versions:
            status = STATUS_HIT
        elif version_id in indexed_versions:
            status = STATUS_NO_HIT
        elif version_id in authorized_versions:
            status = STATUS_NOT_INDEXED
        elif version_id in anchored_versions:
            continue
        else:
            status = STATUS_NOT_INDEXED
        versions.append(VersionSearchStatus(version_id, status))
        counts[status] += 1
    return SearchEvidenceResult(hits, tuple(versions), counts, len(ready) > bounded)


def rebuild_evidence_index(session: Session) -> int:
    """Insert index rows for anchors that do not have one yet.

    Reads evidence anchors, then writes the excerpt index. Does not update or
    delete anchors, original versions, or an index row already stored for an
    anchor. The version id written is that anchor's ``artifact_version_id``.
    Returns the number of index rows inserted.
    """

    existing = set(session.scalars(text(f"SELECT anchor_id FROM {EVIDENCE_INDEX}")).all())
    anchors = session.execute(
        select(
            EvidenceAnchor.id,
            EvidenceAnchor.project_id,
            EvidenceAnchor.artifact_version_id,
            EvidenceAnchor.locator_json,
            EvidenceAnchor.excerpt_hash,
        ).order_by(EvidenceAnchor.id)
    ).all()
    inserted = 0
    for anchor in anchors:
        if anchor.id in existing:
            continue
        excerpt = _excerpt_text(anchor.locator_json)
        if excerpt is None:
            continue
        session.execute(
            text(
                f"""
                INSERT INTO {EVIDENCE_INDEX} (
                  excerpt, anchor_id, project_id, artifact_version_id,
                  locator_json, excerpt_hash, generator, index_revision
                ) VALUES (
                  :excerpt, :anchor_id, :project_id, :artifact_version_id,
                  :locator_json, :excerpt_hash, :generator, :index_revision
                )
                """
            ),
            {
                "excerpt": excerpt,
                "anchor_id": anchor.id,
                "project_id": anchor.project_id,
                "artifact_version_id": anchor.artifact_version_id,
                "locator_json": anchor.locator_json,
                "excerpt_hash": anchor.excerpt_hash,
                "generator": _GENERATOR,
                "index_revision": _INDEX_REVISION,
            },
        )
        existing.add(anchor.id)
        inserted += 1
    return inserted


def _require_query(query: object) -> str:
    if not isinstance(query, str) or len(query) > _QUERY_MAX or query.strip() == "":
        raise ValueError("query")
    return query


def _require_limit(limit: object) -> int:
    if type(limit) is not int or limit < _LIMIT_MIN or limit > _LIMIT_MAX:
        raise ValueError("limit")
    return limit


def _optional_version(value: object) -> str | None:
    if value is None:
        return None
    if (
        not isinstance(value, str)
        or value.strip() == ""
        or value != value.strip()
        or len(value) > _VERSION_MAX
    ):
        raise ValueError("artifact_version_id")
    if value.casefold() in _ALIASES:
        raise ValueError("artifact_version_id")
    return value


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


def _indexed_rows(session: Session, anchor_ids: list[str]):
    if not anchor_ids:
        return []
    clause, params = _placeholders(anchor_ids)
    return session.execute(
        text(
            f"""
            SELECT anchor_id, artifact_version_id
            FROM {EVIDENCE_INDEX}
            WHERE anchor_id IN ({clause})
            ORDER BY artifact_version_id, anchor_id
            """
        ),
        params,
    ).all()


def _match_index(session: Session, anchor_ids: list[str], query: str):
    if not anchor_ids:
        return []
    if len(query) >= _TRIGRAM_MIN:
        return _match_trigram(session, anchor_ids, query)
    return _match_substring(session, anchor_ids, query)


def _match_trigram(session: Session, anchor_ids: list[str], query: str):
    clause, params = _placeholders(anchor_ids)
    params["query"] = '"' + query.replace('"', '""') + '"'
    return session.execute(
        text(
            f"""
            SELECT artifact_version_id, locator_json
            FROM {EVIDENCE_INDEX}
            WHERE anchor_id IN ({clause})
              AND {EVIDENCE_INDEX} MATCH :query
            ORDER BY artifact_version_id, anchor_id
            """
        ),
        params,
    ).all()


def _match_substring(session: Session, anchor_ids: list[str], query: str):
    clause, params = _placeholders(anchor_ids)
    params["pattern"] = _like_pattern(query)
    return session.execute(
        text(
            f"""
            SELECT artifact_version_id, locator_json
            FROM {EVIDENCE_INDEX}
            WHERE anchor_id IN ({clause})
              AND excerpt LIKE :pattern ESCAPE '\\'
            ORDER BY artifact_version_id, anchor_id
            """
        ),
        params,
    ).all()


def _placeholders(values: list[str]) -> tuple[str, dict[str, str]]:
    params = {f"anchor_{index}": value for index, value in enumerate(values)}
    clause = ", ".join(f":anchor_{index}" for index in range(len(values)))
    return clause, params


def _like_pattern(query: str) -> str:
    escaped = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def _ready_hits(rows) -> list[tuple[str, dict[str, object]]]:
    ready: list[tuple[str, dict[str, object]]] = []
    for row in rows:
        locator = _locator(row.locator_json)
        if locator is None:
            continue
        ready.append((row.artifact_version_id, locator))
    return ready


def _excerpt_text(raw: str) -> str | None:
    parsed = _locator(raw)
    if parsed is None:
        return None
    excerpt = parsed.get("excerpt")
    if not isinstance(excerpt, str):
        return None
    return excerpt


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
