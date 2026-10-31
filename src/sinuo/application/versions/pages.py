"""Blank pages and same-stage parallel pages. The caller owns write_uow."""

import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from sqlalchemy import update
from sqlalchemy.orm import Session
from sqlalchemy.orm.util import identity_key

from sinuo.domain.auth.errors import InvalidSchema, NotFoundOrHidden
from sinuo.domain.public_contract import Action
from sinuo.domain.schema_types import canonical_audit_payload, canonical_document, document_hash
from sinuo.infrastructure.domain_models import (
    AuditEvent,
    Page,
    PageDraft,
    PageVersion,
    Project,
    Stage,
)
from sinuo.infrastructure.models import OutboxEvent

PAGE_VERSION_CREATED = "page.version_created"


class ProjectRevisionConflict(Exception):
    """Conditional projects.revision update did not change exactly one row."""

    def __init__(self, project_id: str) -> None:
        self.project_id = project_id
        super().__init__(project_id)


@dataclass(frozen=True)
class BlankPage:
    page_id: str
    page_version_id: str
    content_hash: str
    content_revision: int


@dataclass(frozen=True)
class CreatedPage:
    project_id: str
    stage_id: str
    page_id: str
    page_version_id: str
    content_hash: str
    content_revision: int
    project_revision: int
    audit_event_id: str


def create_parallel_page(
    session: Session,
    *,
    project_id: str,
    stage_id: str,
    account_id: str,
    title: str,
    expected_revision: int,
    now: float,
    before_audit: Callable[[], None] | None = None,
) -> CreatedPage:
    """Insert one blank page and v1 in an existing stage.

    Same-stage pages stay unlinked: this does not insert a lineage edge of any
    kind, including next. projects.revision advances by exactly one when
    expected_revision still matches. access_epoch is left unchanged. Does not commit.
    """

    stage = session.get(Stage, stage_id)
    if stage is None or stage.project_id != project_id:
        raise NotFoundOrHidden()
    project_revision = _advance_project_revision(session, project_id, expected_revision)
    blank = _insert_blank_page(
        session,
        project_id=project_id,
        stage_id=stage_id,
        title=title,
        project_revision=project_revision,
        account_id=account_id,
        now=now,
    )
    session.flush()
    if before_audit is not None:
        before_audit()
    audit_event_id = _record_page_version(
        session,
        project_id=project_id,
        actor_id=account_id,
        command=Action.PAGE_CREATE.value,
        subject_type="page",
        subject_id=blank.page_id,
        project_revision=project_revision,
        stage_id=stage_id,
        page_id=blank.page_id,
        page_version_id=blank.page_version_id,
        now=now,
    )
    return CreatedPage(
        project_id=project_id,
        stage_id=stage_id,
        page_id=blank.page_id,
        page_version_id=blank.page_version_id,
        content_hash=blank.content_hash,
        content_revision=blank.content_revision,
        project_revision=project_revision,
        audit_event_id=audit_event_id,
    )


def _advance_project_revision(session: Session, project_id: str, expected_revision: int) -> int:
    if type(expected_revision) is not int or expected_revision < 1:
        raise InvalidSchema()
    result = session.execute(
        update(Project)
        .where(Project.id == project_id, Project.revision == expected_revision)
        .values(revision=Project.revision + 1),
        execution_options={"synchronize_session": False},
    )
    if result.rowcount != 1:
        raise ProjectRevisionConflict(project_id)
    cached = session.identity_map.get(identity_key(Project, (project_id,)))
    if cached is not None:
        session.expire(cached)
    return expected_revision + 1


def _insert_blank_page(
    session: Session,
    *,
    project_id: str,
    stage_id: str,
    title: str,
    project_revision: int,
    account_id: str,
    now: float,
) -> BlankPage:
    """Insert the page with an empty head, then the version, then point the head at it."""

    stored_title = _required_title(title)
    page_id = str(uuid.uuid4())
    version_id = str(uuid.uuid4())
    content_json, content_hash = _empty_page_content()
    page = Page(
        id=page_id,
        project_id=project_id,
        stage_id=stage_id,
        head_version_id=None,
        status="active",
        title=stored_title,
        created_at=now,
    )
    session.add(page)
    session.flush()
    session.add(
        PageVersion(
            id=version_id,
            project_id=project_id,
            page_id=page_id,
            version_no=1,
            parent_version_id=None,
            content_json=content_json,
            content_hash=content_hash,
            schema_version=1,
            project_revision=project_revision,
            created_by_account_id=account_id,
            created_at=now,
        )
    )
    session.flush()
    page.head_version_id = version_id
    session.flush()
    session.add(
        PageDraft(
            page_id=page_id,
            project_id=project_id,
            base_version_id=version_id,
            content_revision=1,
            content_hash=content_hash,
            content_json=content_json,
            schema_version=1,
            updated_at=now,
        )
    )
    session.flush()
    return BlankPage(
        page_id=page_id,
        page_version_id=version_id,
        content_hash=content_hash,
        content_revision=1,
    )


def _record_page_version(
    session: Session,
    *,
    project_id: str,
    actor_id: str,
    command: str,
    subject_type: str,
    subject_id: str,
    project_revision: int,
    stage_id: str,
    page_id: str,
    page_version_id: str,
    now: float,
) -> str:
    payload = _version_payload(
        project_id=project_id,
        stage_id=stage_id,
        page_id=page_id,
        page_version_id=page_version_id,
        project_revision=project_revision,
    )
    audit_event_id = str(uuid.uuid4())
    session.add(
        AuditEvent(
            id=audit_event_id,
            project_id=project_id,
            actor_id=actor_id,
            command=command,
            subject_type=subject_type,
            subject_id=subject_id,
            project_revision=project_revision,
            payload=canonical_audit_payload(payload),
            created_at=now,
        )
    )
    session.add(
        OutboxEvent(
            event_id=str(uuid.uuid4()),
            project_id=project_id,
            event_type=PAGE_VERSION_CREATED,
            schema_version=1,
            payload=payload,
            created_at=now,
        )
    )
    session.flush()
    return audit_event_id


def _version_payload(
    *,
    project_id: str,
    stage_id: str,
    page_id: str,
    page_version_id: str,
    project_revision: int,
) -> dict[str, Any]:
    return {
        "page_id": page_id,
        "page_version_id": page_version_id,
        "project_id": project_id,
        "project_revision": project_revision,
        "stage_id": stage_id,
    }


def _empty_page_content() -> tuple[str, str]:
    document = {"schema_version": 1, "nodes": [], "edges": []}
    return canonical_document(document), document_hash(document)


def _required_title(title: str) -> str:
    if not isinstance(title, str) or len(title.strip()) == 0:
        raise InvalidSchema()
    return title
