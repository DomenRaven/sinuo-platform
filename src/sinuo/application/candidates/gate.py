"""Current-permission checks for a parsed candidate. No writes."""

from sqlalchemy.orm import Session

from sinuo.application.auth.authorizer import SessionAuthorizer
from sinuo.application.candidates.proposal import ParsedProposal
from sinuo.domain.auth.principal import Principal
from sinuo.domain.auth.resource import ResourceRef
from sinuo.domain.public_contract import Action


def authorize_proposal(
    session: Session,
    principal: Principal,
    project_id: str,
    proposal: ParsedProposal,
) -> None:
    """Re-read grants for the action this proposal will perform."""

    authorizer = SessionAuthorizer(session)
    _evidence_anchors(authorizer, principal, project_id, proposal.evidence_anchor_ids)
    if proposal.kind == "draft":
        authorizer.authorize(
            Action.PAGE_DRAFT_WRITE,
            ResourceRef(project_id=project_id, scope_type="page", page_id=proposal.page_id),
            principal,
        )
        authorizer.authorize(
            Action.PAGE_READ,
            ResourceRef(
                project_id=project_id,
                scope_type="page_version",
                page_version_id=proposal.base_version_id,
            ),
            principal,
        )
        _citations(authorizer, principal, project_id, proposal.citations)
        if proposal.artifact_version_id is not None:
            _citations(authorizer, principal, project_id, (proposal.artifact_version_id,))
        return
    if proposal.kind == "history":
        version = ResourceRef(
            project_id=project_id,
            scope_type="page_version",
            page_version_id=proposal.source_version_id,
        )
        authorizer.authorize(Action.PAGE_READ, version, principal)
        authorizer.authorize(Action.PAGE_BRANCH, version, principal)
        authorizer.authorize(
            Action.PAGE_CREATE,
            ResourceRef(project_id=project_id, scope_type="project"),
            principal,
        )
        return
    authorizer.authorize(
        Action.DECISION_CONFIRM,
        ResourceRef(project_id=project_id, scope_type="project"),
        principal,
    )
    authorizer.authorize(
        Action.PAGE_CREATE,
        ResourceRef(project_id=project_id, scope_type="project"),
        principal,
    )
    for version_id in proposal.source_version_ids:
        authorizer.authorize(
            Action.PAGE_READ,
            ResourceRef(
                project_id=project_id,
                scope_type="page_version",
                page_version_id=version_id,
            ),
            principal,
        )
    _citations(authorizer, principal, project_id, proposal.citations)


def _evidence_anchors(
    authorizer: SessionAuthorizer,
    principal: Principal,
    project_id: str,
    anchor_ids: tuple[str, ...],
) -> None:
    for anchor_id in anchor_ids:
        authorizer.authorize(
            Action.EVIDENCE_READ,
            ResourceRef(
                project_id=project_id,
                scope_type="evidence_anchor",
                evidence_anchor_id=anchor_id,
            ),
            principal,
        )


def _citations(
    authorizer: SessionAuthorizer,
    principal: Principal,
    project_id: str,
    artifact_version_ids: tuple[str, ...],
) -> None:
    for artifact_version_id in artifact_version_ids:
        authorizer.authorize(
            Action.PREVIEW_READ,
            ResourceRef(
                project_id=project_id,
                scope_type="artifact_version",
                artifact_version_id=artifact_version_id,
            ),
            principal,
        )
