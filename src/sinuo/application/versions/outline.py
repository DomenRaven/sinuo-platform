"""Read the projects, stages, and page draft a person is allowed to see.

These functions do not commit and do not create rows. A page the caller cannot
read is omitted. The stage then says that something is hidden, without the
page title or a count.
"""

import json
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from sinuo.application.auth.authorizer import SessionAuthorizer
from sinuo.domain.auth.errors import ActionForbidden, NotFoundOrHidden, SourceRedacted
from sinuo.domain.auth.principal import Principal
from sinuo.domain.auth.resource import ResourceRef
from sinuo.domain.page_nodes import PageNodeRejected, StoredEdge, StoredNode, read_page_edges, read_page_nodes
from sinuo.domain.public_contract import Action
from sinuo.infrastructure.domain_models import (
    Membership,
    Page,
    PageDraft,
    PageEdge,
    PageVersion,
    Project,
    Stage,
    StageConfigVersion,
)


@dataclass(frozen=True)
class VisibleProject:
    project_id: str
    title: str
    revision: int


@dataclass(frozen=True)
class VisiblePage:
    page_id: str
    title: str
    status: str
    head_version_id: str | None


@dataclass(frozen=True)
class VisibleStage:
    stage_id: str
    position: int | None
    name: str | None
    has_hidden_pages: bool
    pages: tuple[VisiblePage, ...]


@dataclass(frozen=True)
class VisiblePageEdge:
    source_page_id: str
    target_page_id: str
    kind: str


@dataclass(frozen=True)
class ProjectStages:
    project_id: str
    title: str
    revision: int
    stages: tuple[VisibleStage, ...]
    edges: tuple[VisiblePageEdge, ...]


@dataclass(frozen=True)
class PageDraftView:
    project_id: str
    stage_id: str
    page_id: str
    title: str
    status: str
    head_version_id: str | None
    head_version_no: int | None
    base_version_id: str | None
    content_revision: int | None
    content_hash: str | None
    nodes_readable: bool
    nodes: tuple[StoredNode, ...]
    edges: tuple[StoredEdge, ...]


def list_visible_projects(
    session: Session,
    authorizer: SessionAuthorizer,
    principal: Principal,
) -> tuple[VisibleProject, ...]:
    """Projects with an active membership and project.read. Titles of others stay out."""

    rows = session.scalars(
        select(Project)
        .join(Membership, Membership.project_id == Project.id)
        .where(
            Membership.account_id == principal.account_id,
            Membership.status == "active",
        )
        .order_by(Project.created_at.desc(), Project.id)
    ).all()
    visible: list[VisibleProject] = []
    for project in rows:
        if not _allows(
            authorizer,
            Action.PROJECT_READ,
            ResourceRef(project_id=project.id, scope_type="project"),
            principal,
        ):
            continue
        visible.append(
            VisibleProject(
                project_id=project.id,
                title=project.title,
                revision=project.revision,
            )
        )
    return tuple(visible)


def read_project_stages(
    session: Session,
    authorizer: SessionAuthorizer,
    principal: Principal,
    project_id: str,
) -> ProjectStages:
    """Stages in stored order. Page titles require page.read on that page."""

    authorizer.authorize(
        Action.PROJECT_READ,
        ResourceRef(project_id=project_id, scope_type="project"),
        principal,
    )
    project = session.get(Project, project_id)
    if project is None:
        raise NotFoundOrHidden()
    stage_rows = session.execute(
        select(Stage, StageConfigVersion)
        .outerjoin(
            StageConfigVersion,
            Stage.head_config_version_id == StageConfigVersion.id,
        )
        .where(Stage.project_id == project_id)
    ).all()
    pages = session.scalars(
        select(Page).where(Page.project_id == project_id).order_by(Page.created_at, Page.id)
    ).all()
    grouped: dict[str, list[VisiblePage]] = {stage.id: [] for stage, _config in stage_rows}
    hidden = {stage.id: False for stage, _config in stage_rows}
    for page in pages:
        if page.stage_id not in grouped:
            continue
        if not _allows(
            authorizer,
            Action.PAGE_READ,
            ResourceRef(project_id=project_id, scope_type="page", page_id=page.id),
            principal,
        ):
            hidden[page.stage_id] = True
            continue
        grouped[page.stage_id].append(
            VisiblePage(
                page_id=page.id,
                title=page.title,
                status=page.status,
                head_version_id=page.head_version_id,
            )
        )
    stages = [
        VisibleStage(
            stage_id=stage.id,
            position=config.position if config is not None else None,
            name=_stage_name(config.goal_json) if config is not None else None,
            has_hidden_pages=hidden[stage.id],
            pages=tuple(grouped[stage.id]),
        )
        for stage, config in stage_rows
    ]
    stages.sort(
        key=lambda item: (
            item.position is None,
            item.position if item.position is not None else 0,
            item.stage_id,
        )
    )
    edges = _visible_page_edges(session, project_id, grouped)
    return ProjectStages(
        project_id=project.id,
        title=project.title,
        revision=project.revision,
        stages=tuple(stages),
        edges=edges,
    )


def _visible_page_edges(
    session: Session,
    project_id: str,
    grouped: dict[str, list[VisiblePage]],
) -> tuple[VisiblePageEdge, ...]:
    """Page-level lineage edges whose both ends the caller can read.

    PageEdge stores version-level endpoints; here we resolve them to pages and
    drop any edge touching a hidden page, so the outline never leaks a title.
    """
    rows = session.scalars(
        select(PageEdge).where(PageEdge.project_id == project_id)
    ).all()
    if not rows:
        return ()
    visible_page_ids = {page.page_id for pages in grouped.values() for page in pages}
    version_ids = set()
    for edge in rows:
        version_ids.add(edge.source_version_id)
        version_ids.add(edge.target_version_id)
    if not version_ids:
        return ()
    version_to_page = dict(
        session.execute(
            select(PageVersion.id, PageVersion.page_id).where(
                PageVersion.id.in_(version_ids)
            )
        ).all()
    )
    resolved = []
    for edge in rows:
        source_page = version_to_page.get(edge.source_version_id)
        target_page = version_to_page.get(edge.target_version_id)
        if source_page in visible_page_ids and target_page in visible_page_ids:
            resolved.append(
                VisiblePageEdge(
                    source_page_id=source_page,
                    target_page_id=target_page,
                    kind=edge.kind,
                )
            )
    return tuple(resolved)


def read_page_draft(
    session: Session,
    authorizer: SessionAuthorizer,
    principal: Principal,
    page_id: str,
) -> PageDraftView:
    """The activity draft's nodes. A missing draft is empty, not the confirmed version."""

    page = session.get(Page, page_id)
    if page is None:
        raise NotFoundOrHidden()
    authorizer.authorize(
        Action.PAGE_READ,
        ResourceRef(project_id=page.project_id, scope_type="page", page_id=page_id),
        principal,
    )
    head_version_no = _head_version_no(session, page)
    draft = session.get(PageDraft, page_id)
    if draft is None or draft.project_id != page.project_id:
        return _draft_view(page, head_version_no, draft=None, nodes=(), edges=(), readable=True)
    try:
        nodes = read_page_nodes(draft.content_json)
        edges = read_page_edges(draft.content_json)
    except PageNodeRejected:
        return _draft_view(page, head_version_no, draft=draft, nodes=(), edges=(), readable=False)
    return _draft_view(page, head_version_no, draft=draft, nodes=nodes, edges=edges, readable=True)


def _draft_view(
    page: Page,
    head_version_no: int | None,
    *,
    draft: PageDraft | None,
    nodes: tuple[StoredNode, ...],
    edges: tuple[StoredEdge, ...],
    readable: bool,
) -> PageDraftView:
    return PageDraftView(
        project_id=page.project_id,
        stage_id=page.stage_id,
        page_id=page.id,
        title=page.title,
        status=page.status,
        head_version_id=page.head_version_id,
        head_version_no=head_version_no,
        base_version_id=None if draft is None else draft.base_version_id,
        content_revision=None if draft is None else draft.content_revision,
        content_hash=None if draft is None else draft.content_hash,
        nodes_readable=readable,
        nodes=nodes,
        edges=edges,
    )


def _head_version_no(session: Session, page: Page) -> int | None:
    if not page.head_version_id:
        return None
    head = session.get(PageVersion, page.head_version_id)
    if head is None or head.page_id != page.id or head.project_id != page.project_id:
        return None
    return head.version_no


def _stage_name(goal_json: str) -> str | None:
    try:
        payload = json.loads(goal_json)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        return None
    name = payload.get("name")
    if isinstance(name, str) and name.strip():
        return name
    return None


def _allows(
    authorizer: SessionAuthorizer,
    action: Action,
    resource: ResourceRef,
    principal: Principal,
) -> bool:
    try:
        authorizer.authorize(action, resource, principal)
    except (NotFoundOrHidden, ActionForbidden, SourceRedacted):
        return False
    return True
