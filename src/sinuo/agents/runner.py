import asyncio
import json
import math
import time

from sinuo.agents.contracts import (
    ROLES,
    InputSnapshot,
    ModelProvider,
    SpecialistResult,
    SpecialistTask,
)
from sinuo.domain.errors import ProviderDisabled


class DagRunner:
    """Small in-memory executor seam. It does not publish candidates or write business data."""

    def __init__(
        self,
        provider: ModelProvider,
        *,
        max_specialists: int = 3,
        max_parallel: int = 2,
        timeout_seconds: float = 60,
    ):
        counts = (max_specialists, max_parallel)
        if (
            any(type(value) is not int or not 1 <= value <= 3 for value in counts)
            or isinstance(timeout_seconds, bool)
            or not isinstance(timeout_seconds, (int, float))
            or not math.isfinite(timeout_seconds)
            or timeout_seconds <= 0
        ):
            raise ValueError("Invalid execution budget")
        self.provider = provider
        self.max_specialists = max_specialists
        self.max_parallel = max_parallel
        self.timeout_seconds = timeout_seconds

    def _validate(self, tasks: tuple[SpecialistTask, ...]) -> None:
        ids = {task.task_id for task in tasks}
        if not tasks or len(tasks) > self.max_specialists or len(ids) != len(tasks):
            raise ValueError("Task count exceeds budget or task IDs are invalid")
        if any(not isinstance(t.task_id, str) or not t.task_id for t in tasks):
            raise ValueError("Task IDs must be non-empty strings")
        roles = [task.role for task in tasks]
        if not set(roles) <= ROLES or len(set(roles)) != len(roles):
            raise ValueError("Roles must be known and distinct within one run")
        if any(
            not set(t.depends_on) <= ids
            or t.task_id in t.depends_on
            or len(set(t.depends_on)) != len(t.depends_on)
            for t in tasks
        ):
            raise ValueError("Invalid dependency")
        remaining = {t.task_id: set(t.depends_on) for t in tasks}
        visited: set[str] = set()
        while remaining:
            ready = {key for key, dependencies in remaining.items() if dependencies <= visited}
            if not ready:
                raise ValueError("Dependency cycle")
            visited.update(ready)
            remaining = {key: deps for key, deps in remaining.items() if key not in ready}

    @staticmethod
    def _accept(task: SpecialistTask, result: object) -> SpecialistResult:
        if not isinstance(result, SpecialistResult):
            raise TypeError("Provider must return a SpecialistResult")
        if result.task_id != task.task_id or result.role != task.role:
            raise ValueError("Provider result identity does not match the task")
        if result.status != "completed" or result.error_code is not None:
            raise ValueError("Provider result state is invalid")

        def reject_constant(value: str):
            raise ValueError(f"Invalid JSON number: {value}")

        def unique_object(pairs):
            output = {}
            for key, value in pairs:
                if key in output:
                    raise ValueError("Duplicate JSON key")
                output[key] = value
            return output

        def finite_float(value: str) -> float:
            number = float(value)
            if not math.isfinite(number):
                raise ValueError("Non-finite JSON number")
            return number

        if not isinstance(result.output_json, str) or not isinstance(
            json.loads(
                result.output_json,
                parse_constant=reject_constant,
                parse_float=finite_float,
                object_pairs_hook=unique_object,
            ),
            dict,
        ):
            raise ValueError("Provider output must be a strict JSON object")
        return SpecialistResult(task.task_id, "completed", result.output_json, role=task.role)

    async def run(
        self, tasks: tuple[SpecialistTask, ...], snapshot: InputSnapshot
    ) -> tuple[SpecialistResult, ...]:
        self._validate(tasks)
        deadline = time.monotonic() + self.timeout_seconds
        if not math.isfinite(deadline):
            raise ValueError("Execution deadline must be finite")
        semaphore = asyncio.Semaphore(self.max_parallel)
        results: dict[str, SpecialistResult] = {}
        remaining = list(tasks)

        async def run_one(task: SpecialistTask) -> SpecialistResult:
            def failed(status, error_code: str) -> SpecialistResult:
                return SpecialistResult(task.task_id, status, error_code=error_code, role=task.role)

            upstream = tuple(results[dependency] for dependency in task.depends_on)
            if any(result.status != "completed" for result in upstream):
                return failed("blocked", "DEPENDENCY_FAILED")
            try:
                async with semaphore:
                    seconds_left = deadline - time.monotonic()
                    if seconds_left <= 0:
                        raise TimeoutError
                    result = await asyncio.wait_for(
                        self.provider.execute(task, snapshot, upstream), timeout=seconds_left
                    )
                # A provider that swallows CancelledError can still return after wait_for expires.
                if time.monotonic() >= deadline:
                    raise TimeoutError
                return self._accept(task, result)
            except TimeoutError:
                return failed("failed", "DEADLINE_EXCEEDED")
            except ProviderDisabled:
                return failed("failed", "PROVIDER_DISABLED")
            except (ValueError, TypeError):
                return failed("failed", "INVALID_OUTPUT")
            except Exception:
                return failed("failed", "PROVIDER_FAILED")

        running: set[asyncio.Task[SpecialistResult]] = set()
        try:
            while remaining or running:
                ready = tuple(t for t in remaining if all(d in results for d in t.depends_on))
                running.update(asyncio.create_task(run_one(task)) for task in ready)
                ready_ids = {task.task_id for task in ready}
                remaining = [task for task in remaining if task.task_id not in ready_ids]
                completed, running = await asyncio.wait(
                    running, return_when=asyncio.FIRST_COMPLETED
                )
                # Only the coordinator publishes results. A ready child need not wait for
                # unrelated tasks from the same scheduling wave.
                for future in completed:
                    result = future.result()
                    results[result.task_id] = result
        finally:
            for future in running:
                future.cancel()
            if running:
                await asyncio.gather(*running, return_exceptions=True)
        return tuple(results[task.task_id] for task in tasks)
