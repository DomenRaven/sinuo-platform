"""Object binding and page projection. Later routes call these before returning fields."""

import json

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
from sinuo.domain.auth.projection import project_visible_fields
from sinuo.domain.auth.resource import ResourceRef
from sinuo.domain.public_contract import Action
from sinuo.infrastructure.domain_models import (
    Artifact,
    ArtifactVersion,
    EvidenceAnchor,
    Page,
    PageVersion,
)

_READ_ACTION = {
    "page": Action.PAGE_READ,
    "artifact": Action.ARTIFACT_ORIGINAL_READ,
    "evidence_anchor": Action.EVIDENCE_READ,
}


def read_bound_object(
    session: Session,
    *,
    principal: Principal,
    url_project_id: str,
    object_id: str,
    object_kind: str,
) -> dict:
    """Hide an object that is not in the URL's project. The error carries no title or count."""

    project_id = _project_id(session, object_kind, object_id)
    if project_id is None or project_id != url_project_id:
        raise NotFoundOrHidden()
    action = _READ_ACTION.get(object_kind)
    if action is None:
        raise NotFoundOrHidden()
    SessionAuthorizer(session).authorize(
        action,
        _resource(object_kind, project_id, object_id),
        principal,
    )
    body = {"id": object_id}
    title = _title(session, object_kind, object_id)
    if title is not None:
        body["title"] = title
    return body


def project_page_projection(
    session: Session,
    *,
    principal: Principal,
    url_project_id: str,
    page_id: str,
) -> dict:
    """Load one page in the URL project and drop fields the principal cannot read."""

    project_id = _project_id(session, "page", page_id)
    if project_id is None or project_id != url_project_id:
        raise NotFoundOrHidden()
    authorizer = SessionAuthorizer(session)
    page_resource = ResourceRef(project_id=project_id, scope_type="page", page_id=page_id)
    authorizer.authorize(Action.PAGE_READ, page_resource, principal)
    title = session.scalar(
        select(Page.title).where(Page.id == page_id, Page.project_id == project_id)
    )
    head_id = session.scalar(
        select(Page.head_version_id).where(Page.id == page_id, Page.project_id == project_id)
    )
    version_ids = _referenced_versions(session, project_id, page_id, head_id)
    artifacts = _artifact_documents(session, project_id, version_ids)
    evidence = _evidence_documents(session, project_id, version_ids)
    document = {"page_id": page_id, "title": title, "artifacts": artifacts, "evidence": evidence}
    return project_visible_fields(
        document,
        can_read_original=_any_allowed(
            authorizer,
            Action.ARTIFACT_ORIGINAL_READ,
            principal,
            [
                ResourceRef(
                    project_id=project_id,
                    scope_type="artifact_version",
                    artifact_version_id=item["id"],
                )
                for item in artifacts
            ],
        ),
        can_read_evidence=_any_allowed(
            authorizer,
            Action.EVIDENCE_READ,
            principal,
            [
                ResourceRef(
                    project_id=project_id,
                    scope_type="evidence_anchor",
                    evidence_anchor_id=item["id"],
                )
                for item in evidence
            ],
        ),
        can_preview=_allowed(authorizer, Action.PREVIEW_READ, page_resource, principal),
    )


def _any_allowed(authorizer, action, principal, resources: list[ResourceRef]) -> bool:
    if not resources:
        return False
    return all(_allowed(authorizer, action, resource, principal) for resource in resources)


def _allowed(authorizer, action, resource, principal) -> bool:
    try:
        authorizer.authorize(action, resource, principal)
    except (ActionForbidden, NotFoundOrHidden, SourceRedacted, AuthRequired):
        return False
    return True


def _project_id(session: Session, object_kind: str, object_id: str) -> str | None:
    if object_kind == "page":
        return session.scalar(select(Page.project_id).where(Page.id == object_id))
    if object_kind == "artifact":
        return session.scalar(select(Artifact.project_id).where(Artifact.id == object_id))
    if object_kind == "evidence_anchor":
        return session.scalar(
            select(EvidenceAnchor.project_id).where(EvidenceAnchor.id == object_id)
        )
    return None


def _title(session: Session, object_kind: str, object_id: str) -> str | None:
    if object_kind == "page":
        return session.scalar(select(Page.title).where(Page.id == object_id))
    if object_kind == "artifact":
        return session.scalar(select(Artifact.title).where(Artifact.id == object_id))
    return None


def _resource(object_kind: str, project_id: str, object_id: str) -> ResourceRef:
    if object_kind == "page":
        return ResourceRef(project_id=project_id, scope_type="page", page_id=object_id)
    if object_kind == "artifact":
        return ResourceRef(project_id=project_id, scope_type="artifact", artifact_id=object_id)
    return ResourceRef(
        project_id=project_id,
        scope_type="evidence_anchor",
        evidence_anchor_id=object_id,
    )


def _referenced_versions(
    session: Session,
    project_id: str,
    page_id: str,
    head_id: str | None,
) -> list[str]:
    if head_id is None:
        return []
    content = session.scalar(
        select(PageVersion.content_json).where(
            PageVersion.id == head_id,
            PageVersion.page_id == page_id,
            PageVersion.project_id == project_id,
        )
    )
    if not content:
        return []
    document = json.loads(content)
    found: list[str] = []
    for node in document.get("nodes", []):
        version_id = node.get("artifact_version_id")
        if isinstance(version_id, str) and version_id not in found:
            found.append(version_id)
    return found


def _artifact_documents(session: Session, project_id: str, version_ids: list[str]) -> list[dict]:
    documents = []
    for version_id in version_ids:
        row = session.execute(
            select(
                ArtifactVersion.id,
                ArtifactVersion.artifact_id,
                ArtifactVersion.object_key,
                ArtifactVersion.sensitivity,
                Artifact.title,
            )
            .join(Artifact, Artifact.id == ArtifactVersion.artifact_id)
            .where(
                ArtifactVersion.id == version_id,
                ArtifactVersion.project_id == project_id,
                Artifact.project_id == project_id,
            )
        ).one_or_none()
        if row is None:
            continue
        item = {
            "id": row.id,
            "artifact_id": row.artifact_id,
            "title": row.title,
            "object_key": row.object_key,
            "thumbnail": f"{row.object_key}#thumbnail",
            "sensitivity": row.sensitivity,
        }
        if row.sensitivity in {"released", "project"}:
            item["preview_ref"] = f"synthetic-preview-{row.sensitivity}"
        documents.append(item)
    return documents


def _evidence_documents(session: Session, project_id: str, version_ids: list[str]) -> list[dict]:
    if not version_ids:
        return []
    rows = session.execute(
        select(
            EvidenceAnchor.id,
            EvidenceAnchor.locator_json,
            EvidenceAnchor.sensitivity,
            Artifact.title,
        )
        .join(ArtifactVersion, ArtifactVersion.id == EvidenceAnchor.artifact_version_id)
        .join(Artifact, Artifact.id == ArtifactVersion.artifact_id)
        .where(
            EvidenceAnchor.project_id == project_id,
            EvidenceAnchor.artifact_version_id.in_(version_ids),
        )
    ).all()
    documents = []
    for row in rows:
        locator = json.loads(row.locator_json)
        excerpt = locator.get("marker") if isinstance(locator, dict) else None
        documents.append(
            {
                "id": row.id,
                "title": row.title,
                "excerpt": excerpt,
                "locator": locator,
                "sensitivity": row.sensitivity,
            }
        )
    return documents
