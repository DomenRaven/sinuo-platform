"""Read-only neighborhood over stored page lineage edges.

The only edge kinds in ``page_edges`` are ``LINEAGE_KINDS``. This walk does
not invent supports or contradicts edges, does not call a model, and does
not insert or update business rows.
"""

from dataclasses import dataclass

from sqlalchemy import select
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
from sinuo.domain.schema_types import LINEAGE_KINDS
from sinuo.infrastructure.domain_models import PageEdge

_DEPTH_MAX = 2
_NODES_MIN = 1
_NODES_MAX = 30
_ID_MAX = 64
_DENIED = (ActionForbidden, NotFoundOrHidden, SourceRedacted)
_KINDS = frozenset(LINEAGE_KINDS)


@dataclass(frozen=True)
class GraphNode:
    page_version_id: str


@dataclass(frozen=True)
class GraphEdge:
    edge_id: str
    kind: str
    source_version_id: str
    target_version_id: str


@dataclass(frozen=True)
class GraphNeighborhood:
    nodes: tuple[GraphNode, ...]
    edges: tuple[GraphEdge, ...]
    truncated: bool


def get_graph_neighborhood(
    session: Session,
    authorizer: SessionAuthorizer,
    principal: Principal,
    project_id: str,
    start_version_id: str,
    kinds: tuple[str, ...],
    depth: int,
    max_nodes: int,
) -> GraphNeighborhood:
    """Return authorized lineage around one page version.

    ``depth`` above 2 and ``max_nodes`` outside 1..30 are rejected. A node
    the caller cannot ``page.read`` is omitted, and the walk does not pass
    through it. Further authorized nodes that do not fit are omitted and
    ``truncated`` is true.
    """

    checked_project = _require_id(project_id, "project_id")
    checked_start = _require_id(start_version_id, "start_version_id")
    checked_kinds = _require_kinds(kinds)
    checked_depth = _require_depth(depth)
    checked_limit = _require_max_nodes(max_nodes)
    if not _project_is_listed(authorizer, principal, checked_project):
        return _empty()
    readable = _Readable(authorizer, principal, checked_project)
    if not readable.allows(checked_start):
        return _empty()
    rows = session.execute(
        select(
            PageEdge.id,
            PageEdge.kind,
            PageEdge.source_version_id,
            PageEdge.target_version_id,
        )
        .where(
            PageEdge.project_id == checked_project,
            PageEdge.kind.in_(checked_kinds),
        )
        .order_by(PageEdge.id)
    ).all()
    adjacency = _adjacency(rows)
    nodes = [checked_start]
    seen = {checked_start}
    layer = [checked_start]
    truncated = False
    for _hop in range(checked_depth):
        nxt: list[str] = []
        for node_id in layer:
            for other in adjacency.get(node_id, ()):
                if other in seen or not readable.allows(other):
                    continue
                if len(nodes) >= checked_limit:
                    truncated = True
                    continue
                seen.add(other)
                nodes.append(other)
                nxt.append(other)
        layer = nxt
        if truncated:
            break
    if not truncated:
        for node_id in layer:
            if any(
                other not in seen and readable.allows(other)
                for other in adjacency.get(node_id, ())
            ):
                truncated = True
                break
    kept = tuple(
        GraphEdge(row.id, row.kind, row.source_version_id, row.target_version_id)
        for row in rows
        if row.source_version_id in seen and row.target_version_id in seen
    )
    return GraphNeighborhood(
        tuple(GraphNode(node_id) for node_id in nodes),
        kept,
        truncated,
    )


def _adjacency(rows) -> dict[str, tuple[str, ...]]:
    ordered: dict[str, list[str]] = {}
    for row in rows:
        _append(ordered, row.source_version_id, row.target_version_id)
        _append(ordered, row.target_version_id, row.source_version_id)
    return {key: tuple(value) for key, value in ordered.items()}


def _append(ordered: dict[str, list[str]], origin: str, other: str) -> None:
    current = ordered.setdefault(origin, [])
    if other not in current:
        current.append(other)


def _empty() -> GraphNeighborhood:
    return GraphNeighborhood((), (), False)


def _require_id(value: object, field: str) -> str:
    if not isinstance(value, str) or value.strip() == "" or len(value) > _ID_MAX:
        raise ValueError(field)
    return value


def _require_kinds(kinds: object) -> tuple[str, ...]:
    if (
        type(kinds) is not tuple
        or not kinds
        or len(set(kinds)) != len(kinds)
        or any(type(kind) is not str or kind not in _KINDS for kind in kinds)
    ):
        raise ValueError("kinds")
    return kinds


def _require_depth(depth: object) -> int:
    if type(depth) is not int or depth < 0 or depth > _DEPTH_MAX:
        raise ValueError("depth")
    return depth


def _require_max_nodes(max_nodes: object) -> int:
    if type(max_nodes) is not int or max_nodes < _NODES_MIN or max_nodes > _NODES_MAX:
        raise ValueError("max_nodes")
    return max_nodes


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


class _Readable:
    def __init__(
        self,
        authorizer: SessionAuthorizer,
        principal: Principal,
        project_id: str,
    ) -> None:
        self._authorizer = authorizer
        self._principal = principal
        self._project_id = project_id
        self._cache: dict[str, bool] = {}

    def allows(self, page_version_id: str) -> bool:
        if page_version_id not in self._cache:
            self._cache[page_version_id] = _can_read_version(
                self._authorizer,
                self._principal,
                self._project_id,
                page_version_id,
            )
        return self._cache[page_version_id]


def _can_read_version(
    authorizer: SessionAuthorizer,
    principal: Principal,
    project_id: str,
    page_version_id: str,
) -> bool:
    try:
        authorizer.authorize(
            Action.PAGE_READ,
            ResourceRef(
                project_id=project_id,
                scope_type="page_version",
                page_version_id=page_version_id,
            ),
            principal,
        )
    except AuthRequired:
        raise
    except _DENIED:
        return False
    return True
