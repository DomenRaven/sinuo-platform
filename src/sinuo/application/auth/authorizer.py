"""Database-backed authorizer. It reads the current rows and calls pure decide()."""

from sqlalchemy import select
from sqlalchemy.orm import Session

from sinuo.domain.auth.decide import Decision, GrantFact, ObjectBinding, decide
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
    Account,
    Artifact,
    ArtifactVersion,
    EvidenceAnchor,
    Membership,
    ObjectGrant,
    Page,
    PageVersion,
    PrivacyTombstone,
    Project,
    Stage,
)


class SessionAuthorizer:
    """One command's Session. This class does not keep an authorization cache."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def authorize(self, action: Action, resource: ResourceRef, principal: Principal) -> None:
        if not isinstance(action, Action):
            raise TypeError("action must be an Action member")
        account = self._session.get(Account, principal.account_id)
        if (
            account is None
            or account.status != "active"
            or account.auth_epoch != principal.auth_epoch
        ):
            raise AuthRequired()
        if action is Action.PROJECT_CREATE:
            outcome = decide(
                action,
                resource,
                principal,
                account_status=account.status,
                can_create_project=bool(account.can_create_project),
                membership_status=None,
                grants=(),
            )
            _raise_for(outcome, action)
            return
        if not resource.project_id:
            raise NotFoundOrHidden()
        binding, sensitivity, availability = self._load_binding(resource)
        membership_status = self._session.scalar(
            select(Membership.status).where(
                Membership.project_id == resource.project_id,
                Membership.account_id == principal.account_id,
            )
        )
        grants = ()
        if membership_status == "active":
            grants = self._active_grants(resource.project_id, principal.account_id)
        adjusted = ResourceRef(
            project_id=resource.project_id,
            scope_type=resource.scope_type,
            stage_id=resource.stage_id,
            page_id=resource.page_id,
            page_version_id=resource.page_version_id,
            artifact_id=resource.artifact_id,
            artifact_version_id=resource.artifact_version_id,
            evidence_anchor_id=resource.evidence_anchor_id,
            sensitivity=sensitivity,
            availability=availability,
            author_account_id=resource.author_account_id,
        )
        outcome = decide(
            action,
            adjusted,
            principal,
            account_status=account.status,
            can_create_project=bool(account.can_create_project),
            membership_status=membership_status,
            grants=grants,
            binding=binding,
        )
        _raise_for(outcome, action)

    def authorize_many(
        self,
        action: Action,
        resources: tuple[ResourceRef, ...] | list[ResourceRef],
        principal: Principal,
    ) -> None:
        hidden = False
        redacted = False
        forbidden = False
        for resource in resources:
            try:
                self.authorize(action, resource, principal)
            except NotFoundOrHidden:
                hidden = True
            except SourceRedacted:
                redacted = True
            except ActionForbidden:
                forbidden = True
        if hidden:
            raise NotFoundOrHidden()
        if redacted:
            raise SourceRedacted()
        if forbidden:
            raise ActionForbidden(action.value)

    def _active_grants(self, project_id: str, account_id: str) -> tuple[GrantFact, ...]:
        rows = self._session.scalars(
            select(ObjectGrant).where(
                ObjectGrant.project_id == project_id,
                ObjectGrant.account_id == account_id,
                ObjectGrant.status == "active",
            )
        ).all()
        return tuple(
            GrantFact(
                action=row.action,
                scope_type=row.scope_type,
                status=row.status,
                stage_id=row.stage_id,
                page_id=row.page_id,
                page_version_id=row.page_version_id,
                artifact_id=row.artifact_id,
                artifact_version_id=row.artifact_version_id,
                evidence_anchor_id=row.evidence_anchor_id,
            )
            for row in rows
        )

    def _tombstoned(self, project_id: str, object_type: str, object_id: str) -> bool:
        found = self._session.scalar(
            select(PrivacyTombstone.id).where(
                PrivacyTombstone.project_id == project_id,
                PrivacyTombstone.object_type == object_type,
                PrivacyTombstone.object_id == object_id,
            )
        )
        return found is not None

    def _load_binding(self, resource: ResourceRef) -> tuple[ObjectBinding, str | None, str]:
        project_id = resource.project_id or ""
        if resource.scope_type == "project":
            exists = self._session.scalar(select(Project.id).where(Project.id == project_id))
            if exists is None:
                raise NotFoundOrHidden()
            if self._tombstoned(project_id, "project", project_id):
                availability = "tombstoned"
            else:
                availability = "available"
            return ObjectBinding(), None, availability
        if resource.scope_type == "stage" and resource.stage_id:
            return self._stage(project_id, resource.stage_id)
        if resource.scope_type == "page" and resource.page_id:
            return self._page(project_id, resource.page_id)
        if resource.scope_type == "page_version" and resource.page_version_id:
            return self._page_version(project_id, resource.page_version_id)
        if resource.scope_type == "artifact" and resource.artifact_id:
            return self._artifact(project_id, resource.artifact_id)
        if resource.scope_type == "artifact_version" and resource.artifact_version_id:
            return self._artifact_version(project_id, resource.artifact_version_id)
        if resource.scope_type == "evidence_anchor" and resource.evidence_anchor_id:
            return self._evidence(project_id, resource.evidence_anchor_id)
        raise NotFoundOrHidden()

    def _availability(
        self,
        project_id: str,
        object_type: str,
        object_id: str,
        fallback: str,
    ) -> str:
        if self._tombstoned(project_id, object_type, object_id):
            return "tombstoned"
        return fallback

    def _stage(self, project_id: str, stage_id: str) -> tuple[ObjectBinding, str | None, str]:
        found = self._session.execute(
            select(Stage.project_id).where(Stage.id == stage_id)
        ).one_or_none()
        if found is None or found.project_id != project_id:
            raise NotFoundOrHidden()
        availability = self._availability(project_id, "stage", stage_id, "available")
        return ObjectBinding(stage_id=stage_id), None, availability

    def _page(self, project_id: str, page_id: str) -> tuple[ObjectBinding, str | None, str]:
        found = self._session.execute(
            select(Page.project_id, Page.status, Page.stage_id).where(Page.id == page_id)
        ).one_or_none()
        if found is None or found.project_id != project_id:
            raise NotFoundOrHidden()
        fallback = "abandoned" if found.status == "abandoned" else "available"
        return (
            ObjectBinding(stage_id=found.stage_id, page_id=page_id),
            None,
            self._availability(project_id, "page", page_id, fallback),
        )

    def _page_version(
        self, project_id: str, page_version_id: str
    ) -> tuple[ObjectBinding, str | None, str]:
        found = self._session.execute(
            select(PageVersion.project_id, PageVersion.page_id).where(
                PageVersion.id == page_version_id
            )
        ).one_or_none()
        if found is None or found.project_id != project_id:
            raise NotFoundOrHidden()
        return (
            ObjectBinding(page_id=found.page_id),
            None,
            self._availability(project_id, "page_version", page_version_id, "available"),
        )

    def _artifact(self, project_id: str, artifact_id: str) -> tuple[ObjectBinding, str | None, str]:
        found = self._session.execute(
            select(Artifact.project_id).where(Artifact.id == artifact_id)
        ).one_or_none()
        if found is None or found.project_id != project_id:
            raise NotFoundOrHidden()
        return (
            ObjectBinding(artifact_id=artifact_id),
            None,
            self._availability(project_id, "artifact", artifact_id, "available"),
        )

    def _artifact_version(
        self, project_id: str, artifact_version_id: str
    ) -> tuple[ObjectBinding, str | None, str]:
        found = self._session.execute(
            select(
                ArtifactVersion.project_id,
                ArtifactVersion.artifact_id,
                ArtifactVersion.sensitivity,
            ).where(ArtifactVersion.id == artifact_version_id)
        ).one_or_none()
        if found is None or found.project_id != project_id:
            raise NotFoundOrHidden()
        return (
            ObjectBinding(artifact_id=found.artifact_id),
            found.sensitivity,
            self._availability(project_id, "artifact_version", artifact_version_id, "available"),
        )

    def _evidence(self, project_id: str, evidence_id: str) -> tuple[ObjectBinding, str | None, str]:
        found = self._session.execute(
            select(EvidenceAnchor.project_id, EvidenceAnchor.sensitivity).where(
                EvidenceAnchor.id == evidence_id
            )
        ).one_or_none()
        if found is None or found.project_id != project_id:
            raise NotFoundOrHidden()
        return (
            ObjectBinding(),
            found.sensitivity,
            self._availability(project_id, "evidence_anchor", evidence_id, "available"),
        )


def authorize_many(
    authorizer: SessionAuthorizer,
    action: Action,
    resources: tuple[ResourceRef, ...] | list[ResourceRef],
    principal: Principal,
) -> None:
    authorizer.authorize_many(action, resources, principal)


def _raise_for(outcome: Decision, action: Action) -> None:
    if outcome is Decision.ALLOW:
        return
    if outcome is Decision.HIDDEN:
        raise NotFoundOrHidden()
    if outcome is Decision.REDACTED:
        raise SourceRedacted()
    raise ActionForbidden(action.value)
