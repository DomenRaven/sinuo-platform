from dataclasses import dataclass

from sinuo.domain.public_contract import RESOURCE_SCOPE_ACCOUNT


@dataclass(frozen=True)
class ResourceRef:
    """One object a caller asks to act on. Clients do not get to fill this in."""

    project_id: str | None
    scope_type: str
    stage_id: str | None = None
    page_id: str | None = None
    page_version_id: str | None = None
    artifact_id: str | None = None
    artifact_version_id: str | None = None
    evidence_anchor_id: str | None = None
    sensitivity: str | None = None
    availability: str | None = None
    author_account_id: str | None = None

    def target_ids(self) -> tuple[str | None, ...]:
        return (
            self.stage_id,
            self.page_id,
            self.page_version_id,
            self.artifact_id,
            self.artifact_version_id,
            self.evidence_anchor_id,
        )


def account_resource() -> ResourceRef:
    return ResourceRef(project_id=None, scope_type=RESOURCE_SCOPE_ACCOUNT)
