"""Create a project with its first stage and blank page. The caller owns write_uow."""

import uuid
from collections.abc import Callable
from dataclasses import dataclass

from sqlalchemy.orm import Session

from sinuo.application.auth.acl import ensure_membership, insert_grant, seed_creator_grants
from sinuo.application.versions.pages import (
    _insert_blank_page,
    _record_page_version,
    _required_title,
)
from sinuo.domain.auth.errors import InvalidSchema
from sinuo.domain.auth.roles import grants_for_template
from sinuo.domain.public_contract import Action, RoleTemplate
from sinuo.domain.schema_types import canonical_json
from sinuo.infrastructure.domain_models import Project, Stage, StageConfigVersion


@dataclass(frozen=True)
class CreatedProject:
    project_id: str
    stage_id: str
    stage_config_version_id: str
    page_id: str
    page_version_id: str
    content_hash: str
    content_revision: int
    project_revision: int
    access_epoch: int
    audit_event_id: str
    membership_id: str
    grant_id: str


def create_project(
    session: Session,
    *,
    account_id: str,
    title: str,
    now: float,
    brief: str | None = None,
    template_key: str | None = None,
    page_title: str | None = None,
    before_audit: Callable[[], None] | None = None,
) -> CreatedProject:
    """Insert a project, one stage, one blank page v1, draft, membership, and acl.manage.

    The project id is generated here. revision and access_epoch are inserted as 1
    and access_epoch is not increased. Does not commit.

    before_audit runs after that graph is flushed and before the audit and outbox
    rows. The caller still owns rollback.

    Idempotency for this command has no project yet. Bind CommandScope.project_id
    to the actor account id, with command project.create.
    """

    stored_title = _required_title(title)
    stored_page_title = stored_title if page_title is None else _required_title(page_title)
    if brief is not None and not isinstance(brief, str):
        raise InvalidSchema()
    if template_key is not None and not isinstance(template_key, str):
        raise InvalidSchema()
    project_id = str(uuid.uuid4())
    stage_id = str(uuid.uuid4())
    config_id = str(uuid.uuid4())
    session.add(
        Project(
            id=project_id,
            title=stored_title,
            brief=brief,
            template_key=template_key,
            revision=1,
            access_epoch=1,
            created_at=now,
        )
    )
    session.flush()
    stage = Stage(
        id=stage_id,
        project_id=project_id,
        head_config_version_id=None,
        created_at=now,
    )
    session.add(stage)
    session.flush()
    session.add(
        StageConfigVersion(
            id=config_id,
            project_id=project_id,
            stage_id=stage_id,
            version_no=1,
            position=0,
            goal_json=canonical_json({"goal": stored_title}),
            effective_project_revision=1,
            schema_version=1,
            created_at=now,
        )
    )
    session.flush()
    stage.head_config_version_id = config_id
    session.flush()
    blank = _insert_blank_page(
        session,
        project_id=project_id,
        stage_id=stage_id,
        title=stored_page_title,
        project_revision=1,
        account_id=account_id,
        now=now,
    )
    membership_id = ensure_membership(
        session,
        project_id=project_id,
        account_id=account_id,
        now=now,
    )
    grant_id = seed_creator_grants(
        session,
        project_id=project_id,
        account_id=account_id,
        now=now,
    )
    for spec in grants_for_template(RoleTemplate.LEAD_DESIGNER):
        insert_grant(
            session,
            project_id=project_id,
            account_id=account_id,
            spec=spec,
            created_by_account_id=account_id,
            now=now,
        )
    session.flush()
    if before_audit is not None:
        before_audit()
    audit_event_id = _record_page_version(
        session,
        project_id=project_id,
        actor_id=account_id,
        command=Action.PROJECT_CREATE.value,
        subject_type="project",
        subject_id=project_id,
        project_revision=1,
        stage_id=stage_id,
        page_id=blank.page_id,
        page_version_id=blank.page_version_id,
        now=now,
    )
    return CreatedProject(
        project_id=project_id,
        stage_id=stage_id,
        stage_config_version_id=config_id,
        page_id=blank.page_id,
        page_version_id=blank.page_version_id,
        content_hash=blank.content_hash,
        content_revision=blank.content_revision,
        project_revision=1,
        access_epoch=1,
        audit_event_id=audit_event_id,
        membership_id=membership_id,
        grant_id=grant_id,
    )
