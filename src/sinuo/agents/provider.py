from sinuo.agents.contracts import InputSnapshot, SpecialistResult, SpecialistTask
from sinuo.domain.errors import ProviderDisabled


class DisabledModelProvider:
    """Deliberately no network transport. Configuration alone does not enable calls."""

    async def execute(
        self,
        task: SpecialistTask,
        snapshot: InputSnapshot,
        upstream: tuple[SpecialistResult, ...],
    ) -> SpecialistResult:
        raise ProviderDisabled("Implement and validate the server-side DeepSeek adapter first")
