"""Read a requirement node or one page version back to evidence and tests.

Page JSON may not have node kinds yet. This read does not write drafts.
Frozen kinds are ``requirement``, ``evidence``, and ``test``. A missing kind
on a requirement-node query returns no anchors and names the gap. A page
version still uses artifact versions already stored on its nodes, and the
anchors and test records that name those versions. Excerpt and test bodies
stay out of the result unless the caller may read them. ``latest`` is not
resolved to a head version.
"""

import json
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from sinuo.application.auth.authorizer import SessionAuthorizer
from sinuo.domain.auth.errors import (
    ActionForbidden,
    AuthRequired,
    InvalidSchema,
    NotFoundOrHidden,
    SourceRedacted,
)
from sinuo.domain.auth.principal import Principal
from sinuo.domain.auth.resource import ResourceRef
from sinuo.domain.public_contract import Action
from sinuo.infrastructure.domain_models import (
    EvidenceAnchor,
    PageVersion,
    PrivacyTombstone,
    TestRecord,
)

NODE_KINDS = frozenset({"requirement", "evidence", "test"})
_EDGE_KINDS = frozenset({"supports", "references", "derived_from"})
_ALIASES = frozenset({"latest", "head", "current", "newest"})
_ID_MAX = 64
_DENIED = (ActionForbidden, NotFoundOrHidden, SourceRedacted)
_KIND_GAP = "page content has no requirement, evidence, or test kind"
_SUCCESSOR_CAP = 64


@dataclass(frozen=True)
class TraceGap:
    field: str
    detail: str


@dataclass(frozen=True)
class TracedRequirement:
    node_id: str
    page_version_id: str
    evidence_anchor_ids: tuple[str, ...]


@dataclass(frozen=True)
class TracedAnchor:
    evidence_anchor_id: str
    artifact_version_id: str
    locator_type: str
    page: int | None
    region: str | dict[str, int | float] | None
    timecode: str | None
    excerpt: str | None
    excerpt_visible: bool
    supersedes_id: str | None
    requirement_node_ids: tuple[str, ...]


@dataclass(frozen=True)
class TracedTest:
    test_record_id: str
    artifact_version_id: str
    supersedes_id: str | None
    observation: str | None
    result: str | None
    source: str | None
    conditions: dict[str, object] | None
    body_visible: bool


@dataclass(frozen=True)
class RequirementTrace:
    project_id: str
    page_version_id: str | None
    node_id: str | None
    requirements: tuple[TracedRequirement, ...]
    anchors: tuple[TracedAnchor, ...]
    tests: tuple[TracedTest, ...]
    gaps: tuple[TraceGap, ...]
    pinned_artifact_version_ids: tuple[str, ...]


@dataclass(frozen=True)
class _Node:
    node_id: str
    kind: str | None
    kind_present: bool
    artifact_version_id: str | None
    anchor_ids: tuple[str, ...]


@dataclass(frozen=True)
class _Edge:
    source_node_id: str
    target_node_id: str
    kind: str | None


@dataclass(frozen=True)
class _Document:
    nodes: tuple[_Node, ...]
    edges: tuple[_Edge, ...]


def trace_requirements(
    session: Session,
    authorizer: SessionAuthorizer,
    principal: Principal,
    project_id: str,
    *,
    page_version_id: str | None = None,
    node_id: str | None = None,
) -> RequirementTrace:
    """Return evidence places and the tests pinned to those work versions.

    Does not commit. One of ``page_version_id`` or ``node_id`` is required.
    Aliases such as ``latest`` are rejected. A version the caller cannot read
    yields empty lists and no excerpt.
    """

    checked_project, checked_version, checked_node = _target(project_id, page_version_id, node_id)
    with session.no_autoflush:
        if checked_version is not None:
            return _from_page(
                session,
                authorizer,
                principal,
                checked_project,
                checked_version,
            )
        return _from_node(
            session,
            authorizer,
            principal,
            checked_project,
            checked_node or "",
        )


def _from_page(
    session: Session,
    authorizer: SessionAuthorizer,
    principal: Principal,
    project_id: str,
    page_version_id: str,
) -> RequirementTrace:
    if not _can_read_version(authorizer, principal, project_id, page_version_id):
        return _empty(project_id, page_version_id, None)
    raw = session.scalar(
        select(PageVersion.content_json).where(
            PageVersion.id == page_version_id,
            PageVersion.project_id == project_id,
        )
    )
    if not isinstance(raw, str):
        return _empty(project_id, page_version_id, None)
    document = _parse_document(raw)
    if document is None:
        return _empty(
            project_id,
            page_version_id,
            None,
            gaps=(TraceGap("content_json", page_version_id),),
        )
    return _assemble(
        session,
        authorizer,
        principal,
        project_id,
        page_version_id=page_version_id,
        node_id=None,
        document=document,
        focus=None,
    )


def _from_node(
    session: Session,
    authorizer: SessionAuthorizer,
    principal: Principal,
    project_id: str,
    node_id: str,
) -> RequirementTrace:
    version_ids = session.scalars(
        select(PageVersion.id).where(PageVersion.project_id == project_id).order_by(PageVersion.id)
    ).all()
    gaps: list[TraceGap] = []
    requirements: list[TracedRequirement] = []
    requested: dict[str, set[str]] = {}
    scan_versions: set[str] = set()
    scan_owners: dict[str, set[str]] = {}
    test_versions: set[str] = set()
    found = False
    for version_id in version_ids:
        if not _can_read_version(authorizer, principal, project_id, version_id):
            continue
        raw = session.scalar(
            select(PageVersion.content_json).where(
                PageVersion.id == version_id,
                PageVersion.project_id == project_id,
            )
        )
        if not isinstance(raw, str):
            continue
        document = _parse_document(raw)
        if document is None:
            continue
        node = next((item for item in document.nodes if item.node_id == node_id), None)
        if node is None:
            continue
        found = True
        piece = _focus_links(document, node, version_id)
        gaps.extend(piece[0])
        requirements.extend(piece[1])
        for anchor_id, owners in piece[2].items():
            requested.setdefault(anchor_id, set()).update(owners)
        for version_id_scanned in piece[3]:
            scan_versions.add(version_id_scanned)
            scan_owners.setdefault(version_id_scanned, set()).add(node_id)
        test_versions.update(piece[4])
    if not found:
        return _empty(project_id, None, node_id)
    return _finish(
        session,
        authorizer,
        principal,
        project_id,
        page_version_id=None,
        node_id=node_id,
        gaps=gaps,
        requirements=requirements,
        requested=requested,
        scan_versions=scan_versions,
        scan_owners=scan_owners,
        test_versions=test_versions,
    )


def _assemble(
    session: Session,
    authorizer: SessionAuthorizer,
    principal: Principal,
    project_id: str,
    *,
    page_version_id: str | None,
    node_id: str | None,
    document: _Document,
    focus: _Node | None,
) -> RequirementTrace:
    gaps: list[TraceGap] = []
    if focus is None and not any(node.kind in NODE_KINDS for node in document.nodes):
        gaps.append(TraceGap("node.kind", _KIND_GAP))
    requirements: list[TracedRequirement] = []
    requested: dict[str, set[str]] = {}
    scan_versions: set[str] = set()
    scan_owners: dict[str, set[str]] = {}
    test_versions: set[str] = set()
    if focus is None:
        for node in document.nodes:
            if node.artifact_version_id is not None:
                scan_versions.add(node.artifact_version_id)
                test_versions.add(node.artifact_version_id)
            for anchor_id in node.anchor_ids:
                requested.setdefault(anchor_id, set())
            if node.kind != "requirement":
                continue
            anchors, scans, tests = _requirement_links(document, node)
            if not anchors and not scans and node.artifact_version_id is None:
                gaps.append(TraceGap("evidence_anchor_id", node.node_id))
            for anchor_id in anchors:
                requested.setdefault(anchor_id, set()).add(node.node_id)
            for version_id_scanned in scans:
                scan_versions.add(version_id_scanned)
                scan_owners.setdefault(version_id_scanned, set()).add(node.node_id)
            test_versions.update(tests)
            requirements.append(TracedRequirement(node.node_id, page_version_id or "", ()))
    return _finish(
        session,
        authorizer,
        principal,
        project_id,
        page_version_id=page_version_id,
        node_id=node_id,
        gaps=gaps,
        requirements=requirements,
        requested=requested,
        scan_versions=scan_versions,
        scan_owners=scan_owners,
        test_versions=test_versions,
    )


def _focus_links(
    document: _Document,
    node: _Node,
    page_version_id: str,
) -> tuple[
    list[TraceGap],
    list[TracedRequirement],
    dict[str, set[str]],
    set[str],
    set[str],
]:
    if not node.kind_present or node.kind != "requirement":
        detail = node.node_id if node.kind is None else f"{node.node_id} is {node.kind}"
        return [TraceGap("node.kind", detail)], [], {}, set(), set()
    anchors, scans, tests = _requirement_links(document, node)
    gaps: list[TraceGap] = []
    if not anchors and not scans and node.artifact_version_id is None:
        gaps.append(TraceGap("evidence_anchor_id", node.node_id))
    requested = {anchor_id: {node.node_id} for anchor_id in anchors}
    return (
        gaps,
        [TracedRequirement(node.node_id, page_version_id, ())],
        requested,
        scans,
        tests,
    )


def _requirement_links(
    document: _Document,
    node: _Node,
) -> tuple[set[str], set[str], set[str]]:
    """Anchor ids to load, versions whose anchors count, versions whose tests count."""

    anchors = set(node.anchor_ids)
    partners = _partners(document, node.node_id)
    for partner in partners:
        anchors.update(partner.anchor_ids)
    scans: set[str] = set()
    tests: set[str] = set()
    if node.artifact_version_id is not None:
        tests.add(node.artifact_version_id)
        if not node.anchor_ids:
            scans.add(node.artifact_version_id)
    for partner in partners:
        if partner.artifact_version_id is None:
            continue
        tests.add(partner.artifact_version_id)
        if not partner.anchor_ids:
            scans.add(partner.artifact_version_id)
    return anchors, scans, tests


def _partners(document: _Document, node_id: str) -> tuple[_Node, ...]:
    nodes = {node.node_id: node for node in document.nodes}
    found: list[_Node] = []
    seen: set[str] = set()
    for edge in document.edges:
        if edge.kind not in _EDGE_KINDS:
            continue
        other_id = None
        if edge.source_node_id == node_id:
            other_id = edge.target_node_id
        elif edge.target_node_id == node_id:
            other_id = edge.source_node_id
        if other_id is None or other_id in seen:
            continue
        other = nodes.get(other_id)
        if other is None or other.kind != "evidence":
            continue
        seen.add(other_id)
        found.append(other)
    return tuple(found)


def _finish(
    session: Session,
    authorizer: SessionAuthorizer,
    principal: Principal,
    project_id: str,
    *,
    page_version_id: str | None,
    node_id: str | None,
    gaps: list[TraceGap],
    requirements: list[TracedRequirement],
    requested: dict[str, set[str]],
    scan_versions: set[str],
    scan_owners: dict[str, set[str]],
    test_versions: set[str],
) -> RequirementTrace:
    loaded, attribution = _load_anchors(session, project_id, requested)
    for anchor_id in sorted(set(requested) - set(loaded)):
        gaps.append(TraceGap("evidence_anchor_id", anchor_id))
    _expand_successors(session, project_id, loaded, attribution)
    for row in _anchors_on_versions(session, project_id, scan_versions):
        loaded.setdefault(row.id, row)
        attribution.setdefault(row.id, set())
    for row in loaded.values():
        owners = scan_owners.get(row.artifact_version_id)
        if owners:
            attribution.setdefault(row.id, set()).update(owners)
        test_versions.add(row.artifact_version_id)
    test_versions.update(scan_versions)
    filled = _fill_requirements(requirements, attribution, set(loaded))
    place_gaps = [
        TraceGap("locator.place", row.id)
        for row in loaded.values()
        if _place(row.locator_json)[:3] == (None, None, None)
    ]
    gaps.extend(place_gaps)
    hidden_versions = _tombstoned_ids(
        session,
        project_id,
        "artifact_version",
        {row.artifact_version_id for row in loaded.values()},
    )
    records = _records(session, project_id, test_versions)
    hidden_tests = _tombstoned_ids(session, project_id, "test_record", {row.id for row in records})
    tests_visible = _can_read_tests(authorizer, principal, project_id)
    anchors = tuple(
        _anchor_view(
            authorizer,
            principal,
            project_id,
            row,
            attribution.get(row.id, set()),
            hidden_versions,
        )
        for row in sorted(loaded.values(), key=lambda item: item.id)
    )
    tests = tuple(
        _test_view(row, visible=tests_visible and row.id not in hidden_tests) for row in records
    )
    return RequirementTrace(
        project_id=project_id,
        page_version_id=page_version_id,
        node_id=node_id,
        requirements=tuple(filled),
        anchors=anchors,
        tests=tests,
        gaps=_unique_gaps(gaps),
        pinned_artifact_version_ids=tuple(sorted(test_versions)),
    )


def _fill_requirements(
    requirements: list[TracedRequirement],
    attribution: dict[str, set[str]],
    loaded_ids: set[str],
) -> list[TracedRequirement]:
    filled: list[TracedRequirement] = []
    for requirement in requirements:
        ids = tuple(
            sorted(
                anchor_id
                for anchor_id, owners in attribution.items()
                if requirement.node_id in owners and anchor_id in loaded_ids
            )
        )
        filled.append(TracedRequirement(requirement.node_id, requirement.page_version_id, ids))
    filled.sort(key=lambda item: (item.page_version_id, item.node_id))
    return filled


def _load_anchors(
    session: Session,
    project_id: str,
    requested: dict[str, set[str]],
) -> tuple[dict[str, EvidenceAnchor], dict[str, set[str]]]:
    loaded: dict[str, EvidenceAnchor] = {}
    attribution = {anchor_id: set(owners) for anchor_id, owners in requested.items()}
    for row in _anchors_by_ids(session, project_id, set(requested)):
        loaded[row.id] = row
    return loaded, attribution


def _expand_successors(
    session: Session,
    project_id: str,
    loaded: dict[str, EvidenceAnchor],
    attribution: dict[str, set[str]],
) -> None:
    frontier = set(loaded)
    seen = set(loaded)
    while frontier and len(seen) < _SUCCESSOR_CAP:
        rows = session.scalars(
            select(EvidenceAnchor)
            .where(
                EvidenceAnchor.project_id == project_id,
                EvidenceAnchor.supersedes_id.in_(sorted(frontier)),
            )
            .order_by(EvidenceAnchor.id)
        ).all()
        frontier = set()
        for row in rows:
            if row.id in seen:
                continue
            seen.add(row.id)
            loaded[row.id] = row
            parent = attribution.get(row.supersedes_id or "", set())
            attribution[row.id] = set(parent)
            frontier.add(row.id)


def _anchor_view(
    authorizer: SessionAuthorizer,
    principal: Principal,
    project_id: str,
    row: EvidenceAnchor,
    owners: set[str],
    hidden_versions: set[str],
) -> TracedAnchor:
    page, region, timecode, excerpt = _place(row.locator_json)
    visible = (
        excerpt is not None
        and row.artifact_version_id not in hidden_versions
        and _can_read_anchor(authorizer, principal, project_id, row.id)
    )
    return TracedAnchor(
        evidence_anchor_id=row.id,
        artifact_version_id=row.artifact_version_id,
        locator_type=row.locator_type,
        page=page,
        region=region,
        timecode=timecode,
        excerpt=excerpt if visible else None,
        excerpt_visible=visible,
        supersedes_id=row.supersedes_id,
        requirement_node_ids=tuple(sorted(owners)),
    )


def _test_view(row: TestRecord, *, visible: bool) -> TracedTest:
    conditions = None
    if visible:
        try:
            parsed = json.loads(row.conditions_json)
        except json.JSONDecodeError:
            parsed = None
        conditions = parsed if isinstance(parsed, dict) else None
    return TracedTest(
        test_record_id=row.id,
        artifact_version_id=row.artifact_version_id,
        supersedes_id=row.supersedes_id,
        observation=row.observation if visible else None,
        result=row.result if visible else None,
        source=row.source if visible else None,
        conditions=conditions,
        body_visible=visible,
    )


def _anchors_by_ids(
    session: Session, project_id: str, anchor_ids: set[str]
) -> list[EvidenceAnchor]:
    if not anchor_ids:
        return []
    return list(
        session.scalars(
            select(EvidenceAnchor)
            .where(
                EvidenceAnchor.project_id == project_id,
                EvidenceAnchor.id.in_(sorted(anchor_ids)),
            )
            .order_by(EvidenceAnchor.id)
        ).all()
    )


def _anchors_on_versions(
    session: Session, project_id: str, version_ids: set[str]
) -> list[EvidenceAnchor]:
    if not version_ids:
        return []
    return list(
        session.scalars(
            select(EvidenceAnchor)
            .where(
                EvidenceAnchor.project_id == project_id,
                EvidenceAnchor.artifact_version_id.in_(sorted(version_ids)),
            )
            .order_by(EvidenceAnchor.id)
        ).all()
    )


def _records(session: Session, project_id: str, version_ids: set[str]) -> list[TestRecord]:
    if not version_ids:
        return []
    return list(
        session.scalars(
            select(TestRecord)
            .where(
                TestRecord.project_id == project_id,
                TestRecord.artifact_version_id.in_(sorted(version_ids)),
            )
            .order_by(TestRecord.id)
        ).all()
    )


def _tombstoned_ids(
    session: Session, project_id: str, object_type: str, object_ids: set[str]
) -> set[str]:
    if not object_ids:
        return set()
    rows = session.scalars(
        select(PrivacyTombstone.object_id).where(
            PrivacyTombstone.project_id == project_id,
            PrivacyTombstone.object_type == object_type,
            PrivacyTombstone.object_id.in_(sorted(object_ids)),
        )
    ).all()
    return set(rows)


def _can_read_version(
    authorizer: SessionAuthorizer,
    principal: Principal,
    project_id: str,
    page_version_id: str,
) -> bool:
    return _allows(
        authorizer,
        principal,
        Action.PAGE_READ,
        ResourceRef(
            project_id=project_id,
            scope_type="page_version",
            page_version_id=page_version_id,
        ),
    )


def _can_read_anchor(
    authorizer: SessionAuthorizer,
    principal: Principal,
    project_id: str,
    anchor_id: str,
) -> bool:
    return _allows(
        authorizer,
        principal,
        Action.EVIDENCE_READ,
        ResourceRef(
            project_id=project_id,
            scope_type="evidence_anchor",
            evidence_anchor_id=anchor_id,
        ),
    )


def _can_read_tests(
    authorizer: SessionAuthorizer,
    principal: Principal,
    project_id: str,
) -> bool:
    return _allows(
        authorizer,
        principal,
        Action.TEST_RECORD,
        ResourceRef(project_id=project_id, scope_type="project"),
    )


def _allows(
    authorizer: SessionAuthorizer,
    principal: Principal,
    action: Action,
    resource: ResourceRef,
) -> bool:
    try:
        authorizer.authorize(action, resource, principal)
    except AuthRequired:
        raise
    except _DENIED:
        return False
    return True


def _empty(
    project_id: str,
    page_version_id: str | None,
    node_id: str | None,
    *,
    gaps: tuple[TraceGap, ...] = (),
) -> RequirementTrace:
    return RequirementTrace(
        project_id=project_id,
        page_version_id=page_version_id,
        node_id=node_id,
        requirements=(),
        anchors=(),
        tests=(),
        gaps=gaps,
        pinned_artifact_version_ids=(),
    )


def _unique_gaps(gaps: list[TraceGap]) -> tuple[TraceGap, ...]:
    found = {(gap.field, gap.detail): gap for gap in gaps}
    return tuple(found[key] for key in sorted(found))


def _target(
    project_id: object,
    page_version_id: object,
    node_id: object,
) -> tuple[str, str | None, str | None]:
    checked_project = _query_id(project_id, "project_id")
    version_given = page_version_id is not None
    node_given = node_id is not None
    if version_given == node_given:
        raise InvalidSchema()
    if version_given:
        return checked_project, _query_id(page_version_id, "page_version_id"), None
    return checked_project, None, _query_id(node_id, "node_id")


def _query_id(value: object, field: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or len(value) > _ID_MAX
        or any(char.isspace() for char in value)
    ):
        raise InvalidSchema()
    if value.casefold() in _ALIASES:
        raise InvalidSchema()
    del field
    return value


def _stored_id(value: object) -> str | None:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or len(value) > _ID_MAX
        or any(char.isspace() for char in value)
    ):
        return None
    if value.casefold() in _ALIASES:
        return None
    return value


def _parse_document(raw: str) -> _Document | None:
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(parsed, dict):
        return None
    nodes_raw = parsed.get("nodes")
    edges_raw = parsed.get("edges")
    nodes: list[_Node] = []
    if isinstance(nodes_raw, list):
        for item in nodes_raw:
            node = _parse_node(item)
            if node is not None:
                nodes.append(node)
    edges: list[_Edge] = []
    if isinstance(edges_raw, list):
        for item in edges_raw:
            edge = _parse_edge(item)
            if edge is not None:
                edges.append(edge)
    return _Document(tuple(nodes), tuple(edges))


def _parse_node(item: object) -> _Node | None:
    if not isinstance(item, dict):
        return None
    node_id = _stored_id(item.get("node_id"))
    if node_id is None:
        return None
    raw_kind = item.get("kind") if "kind" in item else None
    kind = raw_kind if isinstance(raw_kind, str) and raw_kind in NODE_KINDS else None
    return _Node(
        node_id=node_id,
        kind=kind,
        kind_present="kind" in item,
        artifact_version_id=_stored_id(item.get("artifact_version_id")),
        anchor_ids=_parse_anchor_ids(item),
    )


def _parse_anchor_ids(item: dict[str, object]) -> tuple[str, ...]:
    found: list[str] = []
    single = _stored_id(item.get("evidence_anchor_id"))
    if single is not None:
        found.append(single)
    many = item.get("evidence_anchor_ids")
    if isinstance(many, list):
        for entry in many:
            checked = _stored_id(entry)
            if checked is not None:
                found.append(checked)
    return tuple(dict.fromkeys(found))


def _parse_edge(item: object) -> _Edge | None:
    if not isinstance(item, dict):
        return None
    source = _stored_id(item.get("source_node_id"))
    target = _stored_id(item.get("target_node_id"))
    if source is None or target is None:
        return None
    raw_kind = item.get("kind")
    kind = raw_kind if isinstance(raw_kind, str) and raw_kind in _EDGE_KINDS else None
    return _Edge(source, target, kind)


def _place(
    raw: str,
) -> tuple[int | None, str | dict[str, int | float] | None, str | None, str | None]:
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return None, None, None, None
    if not isinstance(parsed, dict):
        return None, None, None, None
    page = parsed.get("page")
    region = parsed.get("region")
    timecode = parsed.get("timecode")
    excerpt = parsed.get("excerpt")
    return (
        page if type(page) is int and page >= 1 else None,
        region if _region_ok(region) else None,
        timecode if isinstance(timecode, str) and timecode else None,
        excerpt if isinstance(excerpt, str) and excerpt else None,
    )


def _region_ok(value: object) -> bool:
    if isinstance(value, str) and value.strip() != "":
        return True
    if not isinstance(value, dict) or set(value) != {"x", "y", "width", "height"}:
        return False
    return all(type(item) in {int, float} and item == item for item in value.values())
