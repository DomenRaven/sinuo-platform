"""Deterministic stand-in for a model. It does not open a network connection."""

import json

from sinuo.agents.contracts import InputSnapshot, SpecialistResult, SpecialistTask


class FakeModelProvider:
    """Echo the role output already stored in the input snapshot.

    The application service builds that output from authorized reads. This
    provider does not add sources, call a tool, or contact DeepSeek.
    """

    async def execute(
        self,
        task: SpecialistTask,
        snapshot: InputSnapshot,
        upstream: tuple[SpecialistResult, ...],
    ) -> SpecialistResult:
        del upstream
        document = json.loads(snapshot.context_json)
        outputs = document.get("outputs")
        if not isinstance(outputs, dict) or task.role not in outputs:
            raise ValueError("missing fake output")
        body = json.dumps(
            outputs[task.role],
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return SpecialistResult(task.task_id, "completed", body, role=task.role)
