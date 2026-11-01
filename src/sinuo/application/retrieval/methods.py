"""Read-only method lookup and exact version comparison.

Neither function calls a model or writes a business row. A card is returned
only when its question terms and stated conditions all match. Stage names
alone do not select a card. Version comparison loads the two given ids.
"""

import json
from dataclasses import dataclass

from sqlalchemy import or_, select
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
from sinuo.infrastructure.domain_models import (
    ArtifactVersion,
    Page,
    PageVersion,
    TestRecord,
)
from sinuo.infrastructure.methods import MethodCard

_ID_MAX = 64
_TEXT_MAX = 80
_LIMIT_MIN = 1
_LIMIT_MAX = 12
_CARD_SCAN_MAX = 50
_DENIED = (ActionForbidden, NotFoundOrHidden, SourceRedacted)
_KINDS = frozenset({"page_version", "artifact_version"})
_ALIASES = frozenset({"latest", "head", "current", "newest"})
_APPLICABLE_KEYS = ("question_terms", "situations", "requires", "stage_names")
_LIST_FIELDS = ("inputs_json", "steps_json", "outputs_json", "limits_json")


@dataclass(frozen=True)
class MethodCardView:
    version_id: str
    card_key: str
    version_no: int
    name: str
    source: dict[str, str]
    applicable: dict[str, tuple[str, ...]]
    inputs: tuple[str, ...]
    steps: tuple[str, ...]
    outputs: tuple[str, ...]
    limits: tuple[str, ...]


@dataclass(frozen=True)
class MethodGap:
    card_key: str
    version_id: str
    missing: tuple[str, ...]


@dataclass(frozen=True)
class LookupMethodResult:
    status: str
    cards: tuple[MethodCardView, ...]
    gaps: tuple[MethodGap, ...]
    truncated: bool


@dataclass(frozen=True)
class VersionSide:
    version_id: str
    status: str
    version_no: int | None = None
    content_hash: str | None = None
    title: str | None = None


@dataclass(frozen=True)
class NodeChange:
    node_id: str
    change: str


@dataclass(frozen=True)
class TestApplicability:
    test_id: str
    artifact_version_id: str
    bound_side: str
    other_side_status: str


@dataclass(frozen=True)
class CompareVersionsResult:
    kind: str
    left: VersionSide
    right: VersionSide
    nodes: tuple[NodeChange, ...]
    edges_added: tuple[str, ...]
    edges_removed: tuple[str, ...]
    bytes_differ: bool | None
    semantic_claim: None
    tests: tuple[TestApplicability, ...]


def lookup_method(
    session: Session,
    authorizer: SessionAuthorizer,
    principal: Principal,
    project_id: str,
    question: str,
    situation: str,
    constraints: dict[str, bool],
    limit: int,
) -> LookupMethodResult:
    """Return matching method-card versions, or the unmet conditions.

    ``limit`` is 1..12. An empty or overlong question is rejected. Cards
    whose question terms do not all occur in ``question`` are omitted.
    A higher version number is not substituted for a version that does not match.
    """

    checked_project = _require_id(project_id, "project_id")
    checked_question = _require_text(question, "question", allow_blank=False)
    checked_situation = _require_text(situation, "situation", allow_blank=True)
    checked_constraints = _require_constraints(constraints)
    bounded = _require_limit(limit)
    if not _project_is_listed(authorizer, principal, checked_project):
        return _empty_lookup()
    with session.no_autoflush:
        rows = session.scalars(
            select(MethodCard)
            .where(
                or_(
                    MethodCard.library_key == "course",
                    MethodCard.project_id == checked_project,
                )
            )
            .order_by(MethodCard.card_key, MethodCard.version_no, MethodCard.id)
            .limit(_CARD_SCAN_MAX + 1)
        ).all()
    scanned = list(rows[:_CARD_SCAN_MAX])
    scan_truncated = len(rows) > _CARD_SCAN_MAX
    matched: list[MethodCardView] = []
    gaps: list[MethodGap] = []
    for row in scanned:
        view = _view(row)
        if view is None:
            continue
        outcome, missing = _match(view, checked_question, checked_situation, checked_constraints)
        if outcome == "hit":
            matched.append(view)
        elif outcome == "gap":
            gaps.append(MethodGap(view.card_key, view.version_id, missing))
    truncated = scan_truncated or len(matched) > bounded or len(gaps) > bounded
    if matched:
        return LookupMethodResult("hit", tuple(matched[:bounded]), (), truncated)
    return LookupMethodResult("no_match", (), tuple(gaps[:bounded]), truncated)


def compare_versions(
    session: Session,
    authorizer: SessionAuthorizer,
    principal: Principal,
    project_id: str,
    left_id: str,
    right_id: str,
    kind: str,
) -> CompareVersionsResult:
    """Compare two exact version ids of one kind.

    ``latest``, ``head``, ``current`` and ``newest`` are rejected. A missing
    id stays missing. The page head is not loaded as a substitute. Byte
    differences are not described as a CAD judgment.
    """

    checked_project = _require_id(project_id, "project_id")
    checked_kind = _require_kind(kind)
    left = _require_version_id(left_id, "left_id")
    right = _require_version_id(right_id, "right_id")
    if left == right:
        raise ValueError("version_id")
    if not _project_is_listed(authorizer, principal, checked_project):
        return _hidden_compare(checked_kind, left, right)
    if checked_kind == "page_version":
        return _compare_pages(session, authorizer, principal, checked_project, left, right)
    return _compare_artifacts(session, authorizer, principal, checked_project, left, right)


def _compare_pages(
    session: Session,
    authorizer: SessionAuthorizer,
    principal: Principal,
    project_id: str,
    left_id: str,
    right_id: str,
) -> CompareVersionsResult:
    left_row = _page_row(session, authorizer, principal, project_id, left_id)
    right_row = _page_row(session, authorizer, principal, project_id, right_id)
    nodes: tuple[NodeChange, ...] = ()
    added: tuple[str, ...] = ()
    removed: tuple[str, ...] = ()
    tests: tuple[TestApplicability, ...] = ()
    if left_row.status == "visible" and right_row.status == "visible":
        left_nodes, left_edges, left_arts = _page_parts(left_row.content_json)
        right_nodes, right_edges, right_arts = _page_parts(right_row.content_json)
        nodes = _node_changes(left_nodes, right_nodes)
        added = tuple(sorted(set(right_edges) - set(left_edges)))
        removed = tuple(sorted(set(left_edges) - set(right_edges)))
        tests = _tests(session, project_id, {"left": left_arts, "right": right_arts})
    return CompareVersionsResult(
        "page_version",
        _side(left_row),
        _side(right_row),
        nodes,
        added,
        removed,
        None,
        None,
        tests,
    )


def _compare_artifacts(
    session: Session,
    authorizer: SessionAuthorizer,
    principal: Principal,
    project_id: str,
    left_id: str,
    right_id: str,
) -> CompareVersionsResult:
    left_row = _artifact_row(session, authorizer, principal, project_id, left_id)
    right_row = _artifact_row(session, authorizer, principal, project_id, right_id)
    differ: bool | None = None
    tests: tuple[TestApplicability, ...] = ()
    if left_row.status == "visible" and right_row.status == "visible":
        differ = left_row.content_hash != right_row.content_hash
        tests = _tests(
            session,
            project_id,
            {"left": frozenset({left_id}), "right": frozenset({right_id})},
        )
    return CompareVersionsResult(
        "artifact_version",
        _side(left_row),
        _side(right_row),
        (),
        (),
        (),
        differ,
        None,
        tests,
    )


@dataclass(frozen=True)
class _Loaded:
    version_id: str
    status: str
    version_no: int | None = None
    content_hash: str | None = None
    title: str | None = None
    content_json: str | None = None


def _page_row(
    session: Session,
    authorizer: SessionAuthorizer,
    principal: Principal,
    project_id: str,
    version_id: str,
) -> _Loaded:
    with session.no_autoflush:
        found = session.scalar(
            select(PageVersion.id).where(
                PageVersion.id == version_id,
                PageVersion.project_id == project_id,
            )
        )
    if found is None:
        return _Loaded(version_id, "missing")
    if not _can(
        authorizer,
        principal,
        Action.PAGE_READ,
        ResourceRef(
            project_id=project_id,
            scope_type="page_version",
            page_version_id=version_id,
        ),
    ):
        return _Loaded(version_id, "hidden")
    with session.no_autoflush:
        row = session.execute(
            select(
                PageVersion.version_no,
                PageVersion.content_hash,
                PageVersion.content_json,
                Page.title,
            )
            .join(Page, Page.id == PageVersion.page_id)
            .where(
                PageVersion.id == version_id,
                PageVersion.project_id == project_id,
                Page.project_id == project_id,
            )
        ).one()
    return _Loaded(
        version_id,
        "visible",
        row.version_no,
        row.content_hash,
        row.title,
        row.content_json,
    )


def _artifact_row(
    session: Session,
    authorizer: SessionAuthorizer,
    principal: Principal,
    project_id: str,
    version_id: str,
) -> _Loaded:
    with session.no_autoflush:
        found = session.scalar(
            select(ArtifactVersion.id).where(
                ArtifactVersion.id == version_id,
                ArtifactVersion.project_id == project_id,
            )
        )
    if found is None:
        return _Loaded(version_id, "missing")
    if not _can(
        authorizer,
        principal,
        Action.PREVIEW_READ,
        ResourceRef(
            project_id=project_id,
            scope_type="artifact_version",
            artifact_version_id=version_id,
        ),
    ):
        return _Loaded(version_id, "hidden")
    with session.no_autoflush:
        row = session.execute(
            select(
                ArtifactVersion.version_no,
                ArtifactVersion.content_hash,
            ).where(
                ArtifactVersion.id == version_id,
                ArtifactVersion.project_id == project_id,
            )
        ).one()
    return _Loaded(version_id, "visible", row.version_no, row.content_hash, None, None)


def _side(row: _Loaded) -> VersionSide:
    if row.status != "visible":
        return VersionSide(row.version_id, row.status)
    return VersionSide(row.version_id, "visible", row.version_no, row.content_hash, row.title)


def _page_parts(raw: str | None) -> tuple[dict[str, str], tuple[str, ...], frozenset[str]]:
    parsed = _object(raw)
    nodes = parsed.get("nodes") if isinstance(parsed, dict) else None
    edges = parsed.get("edges") if isinstance(parsed, dict) else None
    node_map: dict[str, str] = {}
    artifacts: set[str] = set()
    if isinstance(nodes, list):
        for node in nodes:
            if not isinstance(node, dict) or not isinstance(node.get("node_id"), str):
                continue
            node_map[node["node_id"]] = json.dumps(
                node, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            )
            artifact_id = node.get("artifact_version_id")
            if isinstance(artifact_id, str) and artifact_id:
                artifacts.add(artifact_id)
    edge_ids: list[str] = []
    if isinstance(edges, list):
        for edge in edges:
            if not isinstance(edge, dict):
                continue
            edge_id = edge.get("edge_id")
            if isinstance(edge_id, str) and edge_id:
                edge_ids.append(edge_id)
    return node_map, tuple(edge_ids), frozenset(artifacts)


def _node_changes(left: dict[str, str], right: dict[str, str]) -> tuple[NodeChange, ...]:
    changes: list[NodeChange] = []
    for node_id in sorted(set(left) - set(right)):
        changes.append(NodeChange(node_id, "removed"))
    for node_id in sorted(set(right) - set(left)):
        changes.append(NodeChange(node_id, "added"))
    for node_id in sorted(set(left) & set(right)):
        if left[node_id] != right[node_id]:
            changes.append(NodeChange(node_id, "changed"))
    return tuple(changes)


def _tests(
    session: Session,
    project_id: str,
    sides: dict[str, frozenset[str]],
) -> tuple[TestApplicability, ...]:
    wanted = set().union(*sides.values())
    if not wanted:
        return ()
    with session.no_autoflush:
        rows = session.execute(
            select(TestRecord.id, TestRecord.artifact_version_id)
            .where(
                TestRecord.project_id == project_id,
                TestRecord.artifact_version_id.in_(wanted),
            )
            .order_by(TestRecord.id)
        ).all()
    found: list[TestApplicability] = []
    for row in rows:
        owners = tuple(
            name for name, artifacts in sides.items() if row.artifact_version_id in artifacts
        )
        if owners == ("left",):
            found.append(TestApplicability(row.id, row.artifact_version_id, "left", "needs_review"))
        elif owners == ("right",):
            found.append(
                TestApplicability(row.id, row.artifact_version_id, "right", "needs_review")
            )
        elif set(owners) == {"left", "right"}:
            found.append(
                TestApplicability(row.id, row.artifact_version_id, "both", "same_version")
            )
    return tuple(found)


def _view(row: MethodCard) -> MethodCardView | None:
    source = _source(row.source_json)
    applicable = _applicable(row.applicable_json)
    lists = {name: _strings(getattr(row, name)) for name in _LIST_FIELDS}
    if source is None or applicable is None or any(item is None for item in lists.values()):
        return None
    return MethodCardView(
        row.id,
        row.card_key,
        row.version_no,
        row.name,
        source,
        applicable,
        lists["inputs_json"] or (),
        lists["steps_json"] or (),
        lists["outputs_json"] or (),
        lists["limits_json"] or (),
    )


def _match(
    view: MethodCardView,
    question: str,
    situation: str,
    constraints: dict[str, bool],
) -> tuple[str, tuple[str, ...]]:
    terms = view.applicable["question_terms"]
    if not terms or any(term not in question for term in terms):
        return "skip", ()
    missing: list[str] = []
    for item in view.applicable["situations"]:
        if item not in situation:
            missing.append(f"situation:{item}")
    for item in view.applicable["requires"]:
        if constraints.get(item) is not True:
            missing.append(f"requires:{item}")
    if missing:
        return "gap", tuple(missing)
    return "hit", ()


def _source(raw: str) -> dict[str, str] | None:
    parsed = _object(raw)
    if not isinstance(parsed, dict):
        return None
    title = parsed.get("title")
    locator = parsed.get("locator")
    if not isinstance(title, str) or not title or not isinstance(locator, str) or not locator:
        return None
    source = {"title": title, "locator": locator}
    license_name = parsed.get("license")
    if isinstance(license_name, str) and license_name:
        source["license"] = license_name
    return source


def _applicable(raw: str) -> dict[str, tuple[str, ...]] | None:
    parsed = _object(raw)
    if not isinstance(parsed, dict) or set(parsed) != set(_APPLICABLE_KEYS):
        return None
    values: dict[str, tuple[str, ...]] = {}
    for key in _APPLICABLE_KEYS:
        item = _strings_value(parsed[key])
        if item is None:
            return None
        values[key] = item
    return values


def _strings(raw: str) -> tuple[str, ...] | None:
    parsed = _object(raw)
    return _strings_value(parsed)


def _strings_value(parsed: object) -> tuple[str, ...] | None:
    if not isinstance(parsed, list) or any(
        not isinstance(item, str) or item == "" for item in parsed
    ):
        return None
    return tuple(parsed)


def _object(raw: object) -> object:
    if not isinstance(raw, str):
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return None


def _empty_lookup() -> LookupMethodResult:
    return LookupMethodResult("no_match", (), (), False)


def _hidden_compare(kind: str, left_id: str, right_id: str) -> CompareVersionsResult:
    return CompareVersionsResult(
        kind,
        VersionSide(left_id, "hidden"),
        VersionSide(right_id, "hidden"),
        (),
        (),
        (),
        None,
        None,
        (),
    )


def _require_id(value: object, field: str) -> str:
    if (
        not isinstance(value, str)
        or value.strip() == ""
        or value != value.strip()
        or len(value) > _ID_MAX
    ):
        raise ValueError(field)
    return value


def _require_version_id(value: object, field: str) -> str:
    checked = _require_id(value, field)
    if checked.casefold() in _ALIASES:
        raise ValueError(field)
    return checked


def _require_kind(kind: object) -> str:
    if kind not in _KINDS:
        raise ValueError("kind")
    return str(kind)


def _require_text(value: object, field: str, *, allow_blank: bool) -> str:
    if not isinstance(value, str) or len(value) > _TEXT_MAX:
        raise ValueError(field)
    if not allow_blank and value.strip() == "":
        raise ValueError(field)
    return value


def _require_constraints(value: object) -> dict[str, bool]:
    if type(value) is not dict:
        raise ValueError("constraints")
    checked: dict[str, bool] = {}
    for key, item in value.items():
        if not isinstance(key, str) or key.strip() == "" or len(key) > _TEXT_MAX:
            raise ValueError("constraints")
        if type(item) is not bool:
            raise ValueError("constraints")
        checked[key] = item
    return checked


def _require_limit(limit: object) -> int:
    if type(limit) is not int or limit < _LIMIT_MIN or limit > _LIMIT_MAX:
        raise ValueError("limit")
    return limit


def _project_is_listed(
    authorizer: SessionAuthorizer,
    principal: Principal,
    project_id: str,
) -> bool:
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


def _can(
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
