"""Save a personal layout. The caller owns write_uow and commit."""

import hashlib
from collections.abc import Mapping
from typing import Any

from sqlalchemy import update
from sqlalchemy.orm import Session

from sinuo.domain.schema_types import canonical_json
from sinuo.infrastructure.domain_models import ViewState


class LayoutConflict(Exception):
    """The layout row was not at the expected revision."""


def save_layout(
    session: Session,
    *,
    view_state_id: str,
    account_id: str,
    project_id: str,
    page_id: str,
    expected_layout_revision: int,
    layout: Mapping[str, Any],
    now: float,
) -> int:
    """Advance layout_revision by 1. Does not commit.

    Draft content_revision, page version numbers, and project revision are not written.
    """

    layout_json = canonical_json(layout)
    layout_hash = hashlib.sha256(layout_json.encode("utf-8")).hexdigest()
    changed = session.execute(
        update(ViewState)
        .where(
            ViewState.id == view_state_id,
            ViewState.account_id == account_id,
            ViewState.project_id == project_id,
            ViewState.page_id == page_id,
            ViewState.layout_revision == expected_layout_revision,
        )
        .values(
            layout_json=layout_json,
            layout_hash=layout_hash,
            layout_revision=expected_layout_revision + 1,
            updated_at=now,
        )
    )
    if changed.rowcount != 1:
        raise LayoutConflict(view_state_id)
    return expected_layout_revision + 1
