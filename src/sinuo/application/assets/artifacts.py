"""Find or insert the logical artifact for one upload. Does not commit."""

import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from sinuo.domain.auth.errors import NotFoundOrHidden
from sinuo.infrastructure.domain_models import Artifact


class ArtifactRejected(Exception):
    """The name cannot be attached to the requested artifact."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


def ensure_artifact(
    session: Session,
    *,
    project_id: str,
    name: str,
    artifact_id: str | None,
    now: float,
) -> tuple[str, bool]:
    """Return ``(artifact_id, created)``.

    An explicit id must already be in this project and carry this name.
    Without an id, the same project and name reuse the earliest artifact.
    A name that is not in the project inserts a new row. Old version rows
    are not updated.
    """

    if type(now) is not float:
        raise ArtifactRejected("now")
    title = _title(name)
    if artifact_id is not None:
        row = session.get(Artifact, artifact_id)
        if row is None or row.project_id != project_id:
            raise NotFoundOrHidden()
        if row.title != title:
            raise ArtifactRejected("name")
        return row.id, False
    found = session.scalars(
        select(Artifact)
        .where(Artifact.project_id == project_id, Artifact.title == title)
        .order_by(Artifact.created_at, Artifact.id)
    ).first()
    if found is not None:
        return found.id, False
    created_id = str(uuid.uuid4())
    session.add(
        Artifact(
            id=created_id,
            project_id=project_id,
            title=title,
            created_at=now,
        )
    )
    session.flush()
    return created_id, True


def _title(name: str) -> str:
    if not isinstance(name, str):
        raise ArtifactRejected("name")
    stripped = name.strip()
    if (
        not stripped
        or stripped != name
        or len(stripped) > 200
        or any(ord(char) < 32 for char in stripped)
    ):
        raise ArtifactRejected("name")
    return stripped
