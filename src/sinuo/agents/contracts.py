from dataclasses import dataclass
from typing import Literal, Protocol, get_args

Role = Literal["clarification", "evidence", "method", "critic"]
ROLES: frozenset[str] = frozenset(get_args(Role))


@dataclass(frozen=True)
class InputSnapshot:
    snapshot_id: str
    project_id: str
    input_hash: str
    context_json: str


@dataclass(frozen=True)
class SpecialistTask:
    task_id: str
    role: Role
    depends_on: tuple[str, ...] = ()


@dataclass(frozen=True)
class SpecialistResult:
    task_id: str
    status: Literal["completed", "failed", "blocked"]
    output_json: str = "{}"
    error_code: str | None = None
    role: Role | None = None


class ModelProvider(Protocol):
    async def execute(
        self,
        task: SpecialistTask,
        snapshot: InputSnapshot,
        upstream: tuple[SpecialistResult, ...],
    ) -> SpecialistResult:
        """Echo task identity; use bounded I/O and propagate CancelledError.

        The in-memory runner requires cooperative cancellation. A transport must bound
        its own requests and cannot run blocking or unbounded work on the event loop.
        Persistent finalization must independently reject late or fenced-out results.
        """
        ...
