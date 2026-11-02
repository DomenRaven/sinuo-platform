"""List the objects a person can include in a next run's read set.

This is read-only: it does not call a model and does not write. An object the
caller cannot read is omitted entirely; the result reports that something is
hidden without revealing its title, thumbnail, or count.
"""

import json
from dataclasses import dataclass

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from sinuo.application.auth.authorizer import SessionAuthorizer
from sinuo.domain.auth.errors import NotFoundOrHidden, SourceRedacted, ActionForbidden
from sinuo.domain.auth.principal import Principal
from sinuo.domain.auth.resource import ResourceRef
from sinuo.domain.public_contract import Action
from sinuo.infrastructure.domain_models import (
    Artifact,
    ArtifactVersion,
    EvidenceAnchor,
    Page,
    PageVersion,
    Project,
)
from sinuo.infrastructure.methods import MethodCard


@dataclass(frozen=True)
class ScopePageVersion:
    page_version_id: str
    page_id: str
    title: str
    version_no: int
    can_include: bool


@dataclass(frozen=True)
class ScopeEvidenceAnchor:
    evidence_anchor_id: str
    artifact_version_id: str
    locator_type: str
    can_include: bool


@dataclass(frozen=True)
class ScopeArtifactVersion:
    artifact_version_id: str
    title: str
    version_no: int
    can_include: bool
    ai_processing_allowed: bool


@dataclass(frozen=True)
class ScopeMethodCard:
    version_id: str
    card_key: str
    name: str
    version_no: int
    source_title: str


@dataclass(frozen=True)
class ContextScope:
    page_versions: tuple[ScopePageVersion, ...]
    evidence_anchors: tuple[ScopeEvidenceAnchor, ...]
    artifact_versions: tuple[ScopeArtifactVersion, ...]
    method_cards: tuple[ScopeMethodCard, ...]
    has_hidden: bool


def list_context_scope(
    session: Session,
    authorizer: SessionAuthorizer,
    principal: Principal,
    project_id: str,
) -> ContextScope:
    """Return the project's includable read-set objects after ``project.read``.

    Each page version, evidence anchor, and artifact version is authorized
    per object. A forbidden object is hidden, never substituted with a
    different version. Method cards are the course library plus this
    project's own cards.
    """

    authorizer.authorize(
        Action.PROJECT_READ,
        ResourceRef(project_id=project_id, scope_type="project"),
        principal,
    )
    if session.get(Project, project_id) is None:
        raise NotFoundOrHidden()

    page_versions, hidden_pages = _page_versions(session, authorizer, principal, project_id)
    anchors, hidden_anchors = _anchors(session, authorizer, principal, project_id)
    artifacts, hidden_artifacts = _artifacts(session, authorizer, principal, project_id)
    cards = _method_cards(session, project_id)

    return ContextScope(
        page_versions=page_versions,
        evidence_anchors=anchors,
        artifact_versions=artifacts,
        method_cards=cards,
        has_hidden=hidden_pages or hidden_anchors or hidden_artifacts,
    )


def _page_versions(
    session: Session,
    authorizer: SessionAuthorizer,
    principal: Principal,
    project_id: str,
) -> tuple[tuple[ScopePageVersion, ...], bool]:
    rows = session.execute(
        select(PageVersion.id, PageVersion.page_id, PageVersion.version_no, Page.title)
        .join(Page, Page.id == PageVersion.page_id)
        .where(
            PageVersion.project_id == project_id,
            Page.project_id == project_id,
        )
        .order_by(PageVersion.page_id, PageVersion.version_no)
    ).all()
    visible: list[ScopePageVersion] = []
    hidden = False
    for row in rows:
        if not _allows(
            authorizer,
            Action.PAGE_READ,
            ResourceRef(
                project_id=project_id,
                scope_type="page_version",
                page_version_id=row.id,
            ),
            principal,
        ):
            hidden = True
            continue
        visible.append(
            ScopePageVersion(
                page_version_id=row.id,
                page_id=row.page_id,
                title=row.title,
                version_no=row.version_no,
                can_include=True,
            )
        )
    return tuple(visible), hidden


def _anchors(
    session: Session,
    authorizer: SessionAuthorizer,
    principal: Principal,
    project_id: str,
) -> tuple[tuple[ScopeEvidenceAnchor, ...], bool]:
    rows = session.scalars(
        select(EvidenceAnchor)
        .where(EvidenceAnchor.project_id == project_id)
        .order_by(EvidenceAnchor.id)
    ).all()
    visible: list[ScopeEvidenceAnchor] = []
    hidden = False
    for row in rows:
        if not _allows(
            authorizer,
            Action.EVIDENCE_READ,
            ResourceRef(
                project_id=project_id,
                scope_type="evidence_anchor",
                evidence_anchor_id=row.id,
            ),
            principal,
        ):
            hidden = True
            continue
        visible.append(
            ScopeEvidenceAnchor(
                evidence_anchor_id=row.id,
                artifact_version_id=row.artifact_version_id,
                locator_type=row.locator_type,
                can_include=True,
            )
        )
    return tuple(visible), hidden


def _artifacts(
    session: Session,
    authorizer: SessionAuthorizer,
    principal: Principal,
    project_id: str,
) -> tuple[tuple[ScopeArtifactVersion, ...], bool]:
    rows = session.execute(
        select(
            ArtifactVersion.id,
            ArtifactVersion.version_no,
            ArtifactVersion.ai_processing_allowed,
            Artifact.title,
        )
        .join(Artifact, Artifact.id == ArtifactVersion.artifact_id)
        .where(
            ArtifactVersion.project_id == project_id,
            Artifact.project_id == project_id,
        )
        .order_by(ArtifactVersion.artifact_id, ArtifactVersion.version_no)
    ).all()
    visible: list[ScopeArtifactVersion] = []
    hidden = False
    for row in rows:
        if not _allows(
            authorizer,
            Action.ARTIFACT_ORIGINAL_READ,
            ResourceRef(
                project_id=project_id,
                scope_type="artifact_version",
                artifact_version_id=row.id,
            ),
            principal,
        ):
            hidden = True
            continue
        visible.append(
            ScopeArtifactVersion(
                artifact_version_id=row.id,
                title=row.title,
                version_no=row.version_no,
                can_include=True,
                ai_processing_allowed=row.ai_processing_allowed == 1,
            )
        )
    return tuple(visible), hidden


def _method_cards(session: Session, project_id: str) -> tuple[ScopeMethodCard, ...]:
    rows = session.scalars(
        select(MethodCard)
        .where(
            or_(
                MethodCard.library_key == "course",
                MethodCard.project_id == project_id,
            )
        )
        .order_by(MethodCard.card_key, MethodCard.version_no, MethodCard.id)
    ).all()
    cards: list[ScopeMethodCard] = []
    for row in rows:
        source_title = _source_title(row.source_json)
        cards.append(
            ScopeMethodCard(
                version_id=row.id,
                card_key=row.card_key,
                name=row.name,
                version_no=row.version_no,
                source_title=source_title,
            )
        )
    return tuple(cards)


def _source_title(raw: str) -> str:
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return ""
    if isinstance(parsed, dict) and isinstance(parsed.get("title"), str):
        return parsed["title"]
    return ""


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
