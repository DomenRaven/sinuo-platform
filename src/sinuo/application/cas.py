"""Single-target conditional updates. Triggers cannot see a missing WHERE."""

from sqlalchemy import update
from sqlalchemy.orm import Session

from sinuo.infrastructure.domain_models import Candidate


class CasMismatch(Exception):
    """The conditional update did not change exactly one row."""


def consume_candidate(
    session: Session,
    *,
    project_id: str,
    candidate_id: str,
    expected_revision: int,
    new_status: str,
) -> None:
    """Consume one pending candidate. The caller owns write_uow and must not catch this."""

    if new_status not in {"committed", "rejected", "expired"}:
        raise CasMismatch(new_status)
    changed = session.execute(
        update(Candidate)
        .where(
            Candidate.id == candidate_id,
            Candidate.project_id == project_id,
            Candidate.status == "pending",
            Candidate.revision == expected_revision,
        )
        .values(status=new_status, revision=expected_revision + 1)
    )
    if changed.rowcount != 1:
        raise CasMismatch(candidate_id)
