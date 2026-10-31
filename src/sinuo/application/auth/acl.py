"""Access changes for an existing project. The epoch update is conditional."""

import uuid
from dataclasses import dataclass

from sqlalchemy import select, update
from sqlalchemy.orm import Session
from sqlalchemy.orm.util import identity_key

from sinuo.application.auth.authorizer import SessionAuthorizer
from sinuo.domain.auth.errors import (
    AccessEpochConflict,
    ActionForbidden,
    InvalidSchema,
    NotFoundOrHidden,
)
from sinuo.domain.auth.principal import Principal
from sinuo.domain.auth.resource import ResourceRef
from sinuo.domain.auth.roles import GrantTemplate, creator_acl_grant, grants_for_template
from sinuo.domain.public_contract import Action, RoleTemplate
from sinuo.domain.schema_types import ClosedSetError, acl_commit, canonical_audit_payload
from sinuo.infrastructure.domain_models import (
    Account,
    AuditEvent,
    Membership,
    ObjectGrant,
    Page,
    Project,
)
from sinuo.infrastructure.models import OutboxEvent

_TARGET_FIELDS = (
    "stage_id",
    "page_id",
    "page_version_id",
    "artifact_id",
    "artifact_version_id",
    "evidence_anchor_id",
)


@dataclass(frozen=True)
class AclCommit:
    access_epoch: int
    grant_ids: tuple[str, ...]
    audit_event_id: str
    project_revision: int


def bump_access_epoch(session: Session, project_id: str, expected_access_epoch: int) -> int:
    """Conditional +1. A rowcount other than 1 rolls the caller's transaction back."""

    next_epoch = acl_commit(
        previous_epoch=expected_access_epoch,
        next_epoch=expected_access_epoch + 1,
    )
    result = session.execute(
        update(Project)
        .where(Project.id == project_id, Project.access_epoch == expected_access_epoch)
        .values(access_epoch=Project.access_epoch + 1),
        execution_options={"synchronize_session": False},
    )
    if result.rowcount != 1:
        raise AccessEpochConflict()
    cached = session.identity_map.get(identity_key(Project, (project_id,)))
    if cached is not None:
        session.expire(cached)
    return next_epoch


def seed_creator_grants(
    session: Session,
    *,
    project_id: str,
    account_id: str,
    now: float,
) -> str:
    """Project-level acl.manage for the creator. Does not bump access_epoch."""

    spec = creator_acl_grant()
    if spec.action is not Action.ACL_MANAGE:
        raise RuntimeError("creator seed must be acl.manage")
    return insert_grant(
        session,
        project_id=project_id,
        account_id=account_id,
        spec=spec,
        created_by_account_id=account_id,
        now=now,
    )


def ensure_membership(
    session: Session,
    *,
    project_id: str,
    account_id: str,
    now: float,
) -> str:
    """Members reactivate on the same row. Grants do not."""

    row = session.scalar(
        select(Membership).where(
            Membership.project_id == project_id,
            Membership.account_id == account_id,
        )
    )
    if row is None:
        membership_id = str(uuid.uuid4())
        session.add(
            Membership(
                id=membership_id,
                project_id=project_id,
                account_id=account_id,
                status="active",
                role_label=None,
                created_at=now,
                revoked_at=None,
            )
        )
        session.flush()
        return membership_id
    if row.status != "active":
        row.status = "active"
        row.revoked_at = None
        session.flush()
    return row.id


def insert_grant(
    session: Session,
    *,
    project_id: str,
    account_id: str,
    spec: GrantTemplate,
    created_by_account_id: str,
    now: float,
) -> str:
    """Insert a new active row. A revoked row is never turned back on."""

    existing = _active_match(session, project_id, account_id, spec)
    if existing is not None:
        return existing
    grant_id = str(uuid.uuid4())
    session.add(
        ObjectGrant(
            id=grant_id,
            project_id=project_id,
            account_id=account_id,
            action=spec.action.value,
            scope_type=spec.scope_type,
            status="active",
            created_at=now,
            revoked_at=None,
            created_by_account_id=created_by_account_id,
            **_target_columns(spec),
        )
    )
    session.flush()
    return grant_id


def commit_acl(
    session: Session,
    *,
    principal: Principal,
    project_id: str,
    expected_access_epoch: int,
    subject_account_id: str,
    role_template: RoleTemplate | None,
    page_ids: tuple[str, ...],
    membership_status: str | None,
    revoke_grant_ids: tuple[str, ...],
    now: float,
) -> AclCommit:
    """Authorize, then conditionally advance access_epoch, then write grants and audit."""

    if len(principal.account_id) != 36 or len(subject_account_id) != 36:
        raise InvalidSchema()
    authorizer = SessionAuthorizer(session)
    authorizer.authorize(
        Action.ACL_MANAGE,
        ResourceRef(project_id=project_id, scope_type="project"),
        principal,
    )
    _require_account(session, subject_account_id)
    _require_pages(session, project_id, page_ids)
    _require_grants(session, project_id, subject_account_id, revoke_grant_ids)
    try:
        specs = () if role_template is None else grants_for_template(role_template, page_ids)
    except (ClosedSetError, ValueError) as error:
        raise InvalidSchema() from error
    next_epoch = bump_access_epoch(session, project_id, expected_access_epoch)
    if role_template is not None:
        ensure_membership(
            session,
            project_id=project_id,
            account_id=subject_account_id,
            now=now,
        )
    if membership_status == "active":
        ensure_membership(
            session,
            project_id=project_id,
            account_id=subject_account_id,
            now=now,
        )
    elif membership_status == "revoked":
        _revoke_membership(session, project_id, subject_account_id, now)
    touched: list[str] = []
    for spec in specs:
        touched.append(
            insert_grant(
                session,
                project_id=project_id,
                account_id=subject_account_id,
                spec=spec,
                created_by_account_id=principal.account_id,
                now=now,
            )
        )
    for grant_id in revoke_grant_ids:
        if _revoke_grant(session, grant_id, now):
            touched.append(grant_id)
    session.flush()
    managers = session.scalar(
        select(ObjectGrant.id)
        .where(
            ObjectGrant.project_id == project_id,
            ObjectGrant.action == Action.ACL_MANAGE.value,
            ObjectGrant.scope_type == "project",
            ObjectGrant.status == "active",
        )
        .limit(1)
    )
    if managers is None:
        raise ActionForbidden(Action.ACL_MANAGE.value)
    audit_event_id = str(uuid.uuid4())
    grant_ids = tuple(dict.fromkeys(touched))
    payload = {
        "project_id": project_id,
        "access_epoch": next_epoch,
        "subject_account_id": subject_account_id,
        "grant_ids": list(grant_ids),
    }
    session.add(
        AuditEvent(
            id=audit_event_id,
            project_id=project_id,
            actor_id=principal.account_id,
            command=Action.ACL_MANAGE.value,
            subject_type="project",
            subject_id=project_id,
            project_revision=None,
            payload=canonical_audit_payload(payload),
            created_at=now,
        )
    )
    session.add(
        OutboxEvent(
            event_id=str(uuid.uuid4()),
            project_id=project_id,
            event_type="project.access_changed",
            schema_version=1,
            payload=payload,
            created_at=now,
        )
    )
    session.flush()
    revision = session.scalar(select(Project.revision).where(Project.id == project_id))
    if type(revision) is not int:
        raise AccessEpochConflict()
    return AclCommit(
        access_epoch=next_epoch,
        grant_ids=grant_ids,
        audit_event_id=audit_event_id,
        project_revision=revision,
    )


def _require_account(session: Session, account_id: str) -> None:
    found = session.scalar(select(Account.id).where(Account.id == account_id))
    if found is None:
        raise InvalidSchema()


def _require_pages(session: Session, project_id: str, page_ids: tuple[str, ...]) -> None:
    for page_id in page_ids:
        found = session.scalar(
            select(Page.id).where(Page.id == page_id, Page.project_id == project_id)
        )
        if found is None:
            raise NotFoundOrHidden()


def _require_grants(
    session: Session,
    project_id: str,
    subject_account_id: str,
    grant_ids: tuple[str, ...],
) -> None:
    for grant_id in grant_ids:
        found = session.scalar(
            select(ObjectGrant.id).where(
                ObjectGrant.id == grant_id,
                ObjectGrant.project_id == project_id,
                ObjectGrant.account_id == subject_account_id,
            )
        )
        if found is None:
            raise InvalidSchema()


def _revoke_membership(session: Session, project_id: str, account_id: str, now: float) -> None:
    row = session.scalar(
        select(Membership).where(
            Membership.project_id == project_id,
            Membership.account_id == account_id,
        )
    )
    if row is None:
        raise NotFoundOrHidden()
    if row.status == "active":
        row.status = "revoked"
        row.revoked_at = now


def _revoke_grant(session: Session, grant_id: str, now: float) -> bool:
    row = session.get(ObjectGrant, grant_id)
    if row is None or row.status != "active":
        return False
    row.status = "revoked"
    row.revoked_at = now
    return True


def _active_match(
    session: Session,
    project_id: str,
    account_id: str,
    spec: GrantTemplate,
) -> str | None:
    statement = select(ObjectGrant.id).where(
        ObjectGrant.project_id == project_id,
        ObjectGrant.account_id == account_id,
        ObjectGrant.action == spec.action.value,
        ObjectGrant.scope_type == spec.scope_type,
        ObjectGrant.status == "active",
    )
    if spec.scope_type == "page":
        statement = statement.where(ObjectGrant.page_id == spec.page_id)
    return session.scalar(statement)


def _target_columns(spec: GrantTemplate) -> dict[str, str | None]:
    columns = dict.fromkeys(_TARGET_FIELDS)
    if spec.scope_type == "page":
        columns["page_id"] = spec.page_id
    return columns
