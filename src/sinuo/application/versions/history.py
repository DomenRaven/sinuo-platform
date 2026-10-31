"""Read immutable page versions. SELECT only; the caller owns the session."""

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from sinuo.infrastructure.domain_models import PageVersion


@dataclass(frozen=True)
class HistoryVersion:
    page_version_id: str
    version_no: int
    parent_version_id: str | None
    content_hash: str
    content_json: str
    project_revision: int


def read_history(
    session: Session,
    *,
    project_id: str,
    page_id: str,
) -> tuple[HistoryVersion, ...]:
    """Return one page's versions in version_no order. Does not write."""

    statement = (
        select(
            PageVersion.id.label("page_version_id"),
            PageVersion.version_no,
            PageVersion.parent_version_id,
            PageVersion.content_hash,
            PageVersion.content_json,
            PageVersion.project_revision,
        )
        .where(PageVersion.project_id == project_id, PageVersion.page_id == page_id)
        .order_by(PageVersion.version_no, PageVersion.id)
    )
    with session.no_autoflush:
        rows = session.execute(statement).all()
    return tuple(
        HistoryVersion(
            page_version_id=row.page_version_id,
            version_no=row.version_no,
            parent_version_id=row.parent_version_id,
            content_hash=row.content_hash,
            content_json=row.content_json,
            project_revision=row.project_revision,
        )
        for row in rows
    )
