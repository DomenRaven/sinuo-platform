"""Conditional draft update. The caller owns write_uow."""

import uuid
from collections.abc import Callable

from sqlalchemy import insert, update
from sqlalchemy.orm import Session

from sinuo.domain.public_contract import Action
from sinuo.domain.schema_types import canonical_audit_payload
from sinuo.infrastructure.domain_models import AuditEvent, PageDraft


class DraftConflict(Exception):
    """Another writer already moved this draft revision or base version."""


def update_draft(
    session: Session,
    *,
    page_id: str,
    project_id: str,
    base_version_id: str,
    expected_revision: int,
    content_json: str,
    content_hash: str,
    now: float,
) -> int:
    """Advance one draft. Returns the new content revision. Does not commit."""

    changed = session.execute(
        update(PageDraft)
        .where(
            PageDraft.page_id == page_id,
            PageDraft.project_id == project_id,
            PageDraft.base_version_id == base_version_id,
            PageDraft.content_revision == expected_revision,
        )
        .values(
            content_json=content_json,
            content_hash=content_hash,
            content_revision=expected_revision + 1,
            updated_at=now,
        )
    )
    if changed.rowcount != 1:
        raise DraftConflict(page_id)
    return expected_revision + 1


def save_draft(
    session: Session,
    *,
    page_id: str,
    project_id: str,
    base_version_id: str,
    expected_revision: int,
    content_json: str,
    content_hash: str,
    actor_id: str,
    now: float,
    before_audit: Callable[[], None] | None = None,
) -> tuple[int, str]:
    """Update the draft, then write its audit row. Does not commit or bump project revision."""

    revision = update_draft(
        session,
        page_id=page_id,
        project_id=project_id,
        base_version_id=base_version_id,
        expected_revision=expected_revision,
        content_json=content_json,
        content_hash=content_hash,
        now=now,
    )
    if before_audit is not None:
        before_audit()
    audit_event_id = str(uuid.uuid4())
    session.execute(
        insert(AuditEvent).values(
            id=audit_event_id,
            project_id=project_id,
            actor_id=actor_id,
            command=Action.PAGE_DRAFT_WRITE.value,
            subject_type="page_draft",
            subject_id=page_id,
            project_revision=None,
            payload=canonical_audit_payload(
                {
                    "content_hash": content_hash,
                    "content_revision": revision,
                    "page_id": page_id,
                }
            ),
            created_at=now,
        )
    )
    return revision, audit_event_id
