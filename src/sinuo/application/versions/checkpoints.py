"""Append one confirmed page version. The caller owns write_uow and commit."""

import uuid
from collections.abc import Callable
from dataclasses import dataclass

from sqlalchemy import func, insert, select, update
from sqlalchemy.orm import Session

from sinuo.domain.public_contract import Action
from sinuo.domain.schema_types import canonical_audit_payload
from sinuo.infrastructure.domain_models import (
    AuditEvent,
    Page,
    PageDraft,
    PageVersion,
    Project,
)
from sinuo.infrastructure.models import OutboxEvent


class CheckpointConflict(Exception):
    """A conditional checkpoint write did not change exactly one row."""


@dataclass(frozen=True)
class CheckpointResult:
    page_version_id: str
    version_no: int
    parent_version_id: str
    project_revision: int
    content_revision: int
    content_hash: str
    audit_event_id: str
    outbox_event_id: str


def _one(result, token: str) -> None:
    if result.rowcount != 1:
        raise CheckpointConflict(token)


def checkpoint_page(
    session: Session,
    *,
    project_id: str,
    page_id: str,
    expected_head_version_id: str,
    expected_content_revision: int,
    expected_content_hash: str,
    actor_id: str,
    now: float,
    before_audit: Callable[[], None] | None = None,
) -> CheckpointResult:
    """Move HEAD off the expected version. Does not commit.

    The new row uses a higher version number. Its parent is that previous HEAD.
    The draft base follows the new row and content_revision increases by 1.
    Project revision increases by 1 and access_epoch is not written.
    """

    draft = session.execute(
        select(
            PageDraft.content_json,
            PageDraft.content_hash,
            PageDraft.schema_version,
        ).where(PageDraft.page_id == page_id, PageDraft.project_id == project_id)
    ).one_or_none()
    highest = session.scalar(
        select(func.max(PageVersion.version_no)).where(
            PageVersion.page_id == page_id,
            PageVersion.project_id == project_id,
        )
    )
    parent_no = session.scalar(
        select(PageVersion.version_no).where(
            PageVersion.id == expected_head_version_id,
            PageVersion.page_id == page_id,
            PageVersion.project_id == project_id,
        )
    )
    current_revision = session.scalar(select(Project.revision).where(Project.id == project_id))
    if (
        draft is None
        or type(highest) is not int
        or type(parent_no) is not int
        or type(current_revision) is not int
        or highest < parent_no
    ):
        raise CheckpointConflict(page_id)
    version_no = highest + 1
    if version_no <= parent_no:
        raise CheckpointConflict(page_id)
    next_revision = current_revision + 1
    next_content_revision = expected_content_revision + 1
    version_id = str(uuid.uuid4())
    audit_event_id = str(uuid.uuid4())
    outbox_event_id = str(uuid.uuid4())
    # The head trigger requires the new version row, and forbids a NULL head.
    _one(
        session.execute(
            insert(PageVersion).values(
                id=version_id,
                project_id=project_id,
                page_id=page_id,
                version_no=version_no,
                parent_version_id=expected_head_version_id,
                content_json=draft.content_json,
                content_hash=draft.content_hash,
                schema_version=draft.schema_version,
                project_revision=next_revision,
                created_by_account_id=actor_id,
                created_at=now,
            )
        ),
        page_id,
    )
    _one(
        session.execute(
            update(Page)
            .where(
                Page.id == page_id,
                Page.project_id == project_id,
                Page.head_version_id == expected_head_version_id,
            )
            .values(head_version_id=version_id)
        ),
        page_id,
    )
    _one(
        session.execute(
            update(PageDraft)
            .where(
                PageDraft.page_id == page_id,
                PageDraft.project_id == project_id,
                PageDraft.base_version_id == expected_head_version_id,
                PageDraft.content_revision == expected_content_revision,
                PageDraft.content_hash == expected_content_hash,
            )
            .values(
                base_version_id=version_id,
                content_revision=next_content_revision,
                updated_at=now,
            )
        ),
        page_id,
    )
    _one(
        session.execute(
            update(Project)
            .where(Project.id == project_id, Project.revision == current_revision)
            .values(revision=next_revision)
        ),
        project_id,
    )
    if before_audit is not None:
        before_audit()
    payload = {
        "content_hash": draft.content_hash,
        "content_revision": next_content_revision,
        "page_id": page_id,
        "page_version_id": version_id,
        "parent_version_id": expected_head_version_id,
        "project_revision": next_revision,
        "version_no": version_no,
    }
    _one(
        session.execute(
            insert(AuditEvent).values(
                id=audit_event_id,
                project_id=project_id,
                actor_id=actor_id,
                command=Action.PAGE_CHECKPOINT.value,
                subject_type="page_version",
                subject_id=version_id,
                project_revision=next_revision,
                payload=canonical_audit_payload(payload),
                created_at=now,
            )
        ),
        page_id,
    )
    _one(
        session.execute(
            insert(OutboxEvent).values(
                event_id=outbox_event_id,
                project_id=project_id,
                event_type="page.version_created",
                schema_version=1,
                payload=payload,
                created_at=now,
            )
        ),
        page_id,
    )
    return CheckpointResult(
        page_version_id=version_id,
        version_no=version_no,
        parent_version_id=expected_head_version_id,
        project_revision=next_revision,
        content_revision=next_content_revision,
        content_hash=draft.content_hash,
        audit_event_id=audit_event_id,
        outbox_event_id=outbox_event_id,
    )
