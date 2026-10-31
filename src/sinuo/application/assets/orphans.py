"""Remove one generation left on disk after a rolled-back registration.

Does not commit. A generation referenced by an artifact version is left in
place. Deletion goes through the generation claim, so a referenced or pinned
object is not unlinked.
"""

from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from sinuo.application.assets.gc import (
    claim_generation,
    delete_claimed_generation,
    reserve_generation,
)
from sinuo.infrastructure.assets.generations import StoredObjectRow
from sinuo.infrastructure.assets.objects import original_key
from sinuo.infrastructure.domain_models import ArtifactVersion


@dataclass(frozen=True)
class OrphanCleanup:
    deleted: bool
    generation_id: str
    reason: str | None


def discard_unregistered_original(
    session: Session,
    *,
    project_id: str,
    content_hash: str,
    generation_id: str,
    byte_size: int,
    objects_root: Path,
    now: float,
    retention_seconds: int,
) -> OrphanCleanup:
    """Claim and unlink one generation that no artifact version references.

    The caller chooses retention_seconds. This function does not sleep and does
    not delete a different generation.
    """

    key = original_key(project_id, content_hash, generation_id)
    referenced = session.scalar(
        select(ArtifactVersion.id).where(
            ArtifactVersion.project_id == project_id,
            ArtifactVersion.object_key == key,
        )
    )
    if referenced is not None:
        return OrphanCleanup(False, generation_id, "referenced")
    row = session.scalar(
        select(StoredObjectRow).where(
            StoredObjectRow.project_id == project_id,
            StoredObjectRow.generation_id == generation_id,
        )
    )
    if row is None:
        reserved = reserve_generation(
            session,
            project_id=project_id,
            sha256=content_hash,
            byte_size=byte_size,
            now=now,
            generation_id=generation_id,
        )
        object_id = reserved.object_id
    else:
        object_id = row.object_id
    claimed = claim_generation(
        session,
        object_id=object_id,
        now=now,
        retention_seconds=retention_seconds,
    )
    if not claimed.claimed or claimed.fencing_token is None:
        return OrphanCleanup(False, generation_id, claimed.reason)
    removed = delete_claimed_generation(
        session,
        object_id=object_id,
        fencing_token=claimed.fencing_token,
        objects_root=objects_root,
    )
    if not removed.deleted:
        return OrphanCleanup(False, generation_id, removed.reason)
    return OrphanCleanup(True, generation_id, None)
