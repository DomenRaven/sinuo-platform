"""Read one authorized page version. No model call and no business write."""

import json
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
from sinuo.infrastructure.domain_models import Page, PageVersion

_ID_MAX = 64
_DENIED = (ActionForbidden, NotFoundOrHidden, SourceRedacted)


@dataclass(frozen=True)
class SnapshotNode:
    node_id: str
    artifact_version_id: str | None
    kind: str | None = None
    content: str | None = None


@dataclass(frozen=True)
class SnapshotEdge:
    edge_id: str
    source_node_id: str
    target_node_id: str
    kind: str | None = None


@dataclass(frozen=True)
class PageSnapshot:
    page_version_id: str | None
    page_id: str | None
    title: str | None
    version_no: int | None
    content_hash: str | None
    body: str | None
    nodes: tuple[SnapshotNode, ...]
    edges: tuple[SnapshotEdge, ...]

    @property
    def artifact_version_ids(self) -> tuple[str, ...]:
        return tuple(
            node.artifact_version_id
            for node in self.nodes
            if node.artifact_version_id is not None
        )


def get_page_snapshot(
    session: Session,
    authorizer: SessionAuthorizer,
    principal: Principal,
    project_id: str,
    page_version_id: str,
) -> PageSnapshot:
    """Return one page version after ``page.read``.

    A missing version, another project, or a version the caller cannot read
    returns an empty snapshot. That empty value has no title and no body.
    ``AuthRequired`` still propagates. The exact id is loaded; a newer
    version on the same page is not substituted.
    """

    checked_project = _require_id(project_id, "project_id")
    checked_version = _require_id(page_version_id, "page_version_id")
    if not _can_read_version(authorizer, principal, checked_project, checked_version):
        return _empty()
    with session.no_autoflush:
        row = session.execute(
            select(
                PageVersion.id,
                PageVersion.page_id,
                PageVersion.version_no,
                PageVersion.content_hash,
                PageVersion.content_json,
                Page.title,
            )
            .join(Page, Page.id == PageVersion.page_id)
            .where(
                PageVersion.id == checked_version,
                PageVersion.project_id == checked_project,
                Page.project_id == checked_project,
            )
        ).one_or_none()
    if row is None:
        return _empty()
    nodes, edges, body = _content(row.content_json)
    return PageSnapshot(
        page_version_id=row.id,
        page_id=row.page_id,
        title=row.title,
        version_no=row.version_no,
        content_hash=row.content_hash,
        body=body,
        nodes=nodes,
        edges=edges,
    )


def _empty() -> PageSnapshot:
    return PageSnapshot(None, None, None, None, None, None, (), ())


def _require_id(value: object, field: str) -> str:
    if not isinstance(value, str) or value.strip() == "" or len(value) > _ID_MAX:
        raise ValueError(field)
    return value


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


def _content(raw: str) -> tuple[tuple[SnapshotNode, ...], tuple[SnapshotEdge, ...], str | None]:
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return (), (), None
    if not isinstance(parsed, dict):
        return (), (), None
    body = parsed.get("body")
    text = body if isinstance(body, str) else None
    return _nodes(parsed.get("nodes")), _edges(parsed.get("edges")), text


def _nodes(raw: object) -> tuple[SnapshotNode, ...]:
    if not isinstance(raw, list):
        return ()
    nodes: list[SnapshotNode] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        node_id = item.get("node_id")
        if not isinstance(node_id, str) or node_id == "":
            continue
        artifact_id = item.get("artifact_version_id")
        kind = item.get("kind")
        content = item.get("content")
        nodes.append(
            SnapshotNode(
                node_id,
                artifact_id if isinstance(artifact_id, str) and artifact_id else None,
                kind if isinstance(kind, str) and kind else None,
                content if isinstance(content, str) and content else None,
            )
        )
    return tuple(nodes)


def _edges(raw: object) -> tuple[SnapshotEdge, ...]:
    if not isinstance(raw, list):
        return ()
    edges: list[SnapshotEdge] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        edge_id = item.get("edge_id")
        source = item.get("source_node_id")
        target = item.get("target_node_id")
        if not all(isinstance(value, str) and value for value in (edge_id, source, target)):
            continue
        kind = item.get("kind")
        edges.append(
            SnapshotEdge(
                edge_id,
                source,
                target,
                kind if isinstance(kind, str) and kind else None,
            )
        )
    return tuple(edges)
