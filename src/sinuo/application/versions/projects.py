"""Create a project with the fixed product-design stages and its home page.

The caller owns write_uow. The eight stage names are fixed in this module.
There is no catalog for adding or removing templates.
"""

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

PRODUCT_STAGE_NAMES = (
    "任务与情境",
    "用户与场景研究",
    "问题及机会定义",
    "设计要求",
    "概念探索",
    "深化与原型",
    "测试与迭代",
    "表达与复盘",
)


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
    """Insert a project, the eight fixed stages, and one blank home page.

    The home page is on the first stage, 任务与情境. Returned stage_id and page_id
    are that stage and that page. Each stage config keeps the project title in
    goal and the fixed stage name in name. revision and access_epoch are inserted
    as 1 and access_epoch is not increased. Does not commit.

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
    stages = [
        _insert_named_stage(
            session,
            project_id=project_id,
            position=position,
            name=name,
            goal=stored_title,
            now=now,
        )
        for position, name in enumerate(PRODUCT_STAGE_NAMES)
    ]
    stage_id, config_id = stages[0]
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


def _insert_named_stage(
    session: Session,
    *,
    project_id: str,
    position: int,
    name: str,
    goal: str,
    now: float,
) -> tuple[str, str]:
    """Insert one stage and its first config. The head is set after the config exists."""

    stage_id = str(uuid.uuid4())
    config_id = str(uuid.uuid4())
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
            position=position,
            goal_json=canonical_json({"goal": goal, "name": name}),
            effective_project_revision=1,
            schema_version=1,
            created_at=now,
        )
    )
    session.flush()
    stage.head_config_version_id = config_id
    session.flush()
    return stage_id, config_id
