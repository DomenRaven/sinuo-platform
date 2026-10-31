import asyncio
from dataclasses import replace

import pytest

from sinuo.agents.contracts import InputSnapshot, SpecialistResult, SpecialistTask
from sinuo.agents.provider import DisabledModelProvider
from sinuo.agents.runner import DagRunner

SNAPSHOT = InputSnapshot("snapshot-1", "project-1", "hash", "{}")


class RecordingProvider:
    def __init__(self):
        self.started = []
        self.completed = []

    async def execute(self, task, snapshot, upstream):
        assert snapshot is SNAPSHOT
        if task.role == "critic":
            assert {r.task_id for r in upstream} == {"evidence", "method"}
            assert set(self.completed) == {"evidence", "method"}
        self.started.append(task.task_id)
        await asyncio.sleep(0)
        self.completed.append(task.task_id)
        return SpecialistResult(task.task_id, "completed", '{"summary":"test"}', role=task.role)


class CancellationSwallowingProvider:
    """Ignores the runner's cancellation and returns a normal-looking result after the deadline."""

    def __init__(self, slow_task_id):
        self.slow_task_id = slow_task_id
        self.swallowed = False

    async def execute(self, task, snapshot, upstream):
        if task.task_id == self.slow_task_id:
            try:
                await asyncio.sleep(5)
            except asyncio.CancelledError:
                self.swallowed = True
                await asyncio.sleep(0.02)
        return SpecialistResult(task.task_id, "completed", '{"late":true}', role=task.role)


class TamperingProvider:
    def __init__(self, tamper):
        self.tamper = tamper

    async def execute(self, task, snapshot, upstream):
        result = SpecialistResult(task.task_id, "completed", '{"summary":"x"}', role=task.role)
        return self.tamper(result)


def test_dependency_waits_and_results_keep_identity():
    provider = RecordingProvider()
    tasks = (
        SpecialistTask("evidence", "evidence"),
        SpecialistTask("method", "method"),
        SpecialistTask("critic", "critic", ("evidence", "method")),
    )
    results = asyncio.run(DagRunner(provider).run(tasks, SNAPSHOT))
    assert [r.task_id for r in results] == [t.task_id for t in tasks]
    assert [r.role for r in results] == [t.role for t in tasks]
    assert all(r.status == "completed" for r in results)


def test_failed_dependency_blocks_downstream():
    tasks = (
        SpecialistTask("evidence", "evidence"),
        SpecialistTask("critic", "critic", ("evidence",)),
    )
    results = asyncio.run(DagRunner(DisabledModelProvider()).run(tasks, SNAPSHOT))
    assert results[0].error_code == "PROVIDER_DISABLED"
    assert results[1].status == "blocked"


def test_blocked_status_propagates_transitively():
    tasks = (
        SpecialistTask("evidence", "evidence"),
        SpecialistTask("method", "method", ("evidence",)),
        SpecialistTask("critic", "critic", ("method",)),
    )
    runner = DagRunner(TamperingProvider(lambda r: replace(r, output_json="[]")))
    results = asyncio.run(runner.run(tasks, SNAPSHOT))
    assert [(r.status, r.error_code) for r in results] == [
        ("failed", "INVALID_OUTPUT"),
        ("blocked", "DEPENDENCY_FAILED"),
        ("blocked", "DEPENDENCY_FAILED"),
    ]


def test_cycles_fail_before_any_provider_call():
    provider = RecordingProvider()
    tasks = (
        SpecialistTask("a", "evidence", ("b",)),
        SpecialistTask("b", "critic", ("a",)),
    )
    with pytest.raises(ValueError, match="cycle"):
        asyncio.run(DagRunner(provider).run(tasks, SNAPSHOT))
    assert provider.started == []


def test_late_result_after_swallowed_cancellation_is_discarded():
    provider = CancellationSwallowingProvider("evidence")
    tasks = (
        SpecialistTask("evidence", "evidence"),
        SpecialistTask("critic", "critic", ("evidence",)),
    )
    results = asyncio.run(DagRunner(provider, timeout_seconds=0.05).run(tasks, SNAPSHOT))
    assert provider.swallowed
    assert results[0].status == "failed"
    assert results[0].error_code == "DEADLINE_EXCEEDED"
    assert results[0].output_json == "{}"
    assert results[1].status == "blocked"


@pytest.mark.parametrize(
    "tasks",
    [
        (SpecialistTask("a", "evidence"), SpecialistTask("b", "evidence")),
        (SpecialistTask("a", "evidence"), SpecialistTask("b", "unknown")),
        tuple(
            SpecialistTask(str(i), role)
            for i, role in enumerate(("clarification", "evidence", "method", "critic"))
        ),
        (SpecialistTask("", "evidence"),),
        (SpecialistTask("a", "evidence"), SpecialistTask("b", "critic", ("a", "a"))),
    ],
    ids=["duplicate-role", "unknown-role", "four-roles", "empty-id", "duplicate-dependency"],
)
def test_invalid_task_sets_are_rejected_before_any_provider_call(tasks):
    provider = RecordingProvider()
    with pytest.raises(ValueError):
        asyncio.run(DagRunner(provider).run(tasks, SNAPSHOT))
    assert provider.started == []


@pytest.mark.parametrize(
    "tamper",
    [
        lambda r: replace(r, task_id="other"),
        lambda r: replace(r, role="critic"),
        lambda r: replace(r, role=None),
        lambda r: replace(r, status="blocked"),
        lambda r: replace(r, error_code="SOMETHING"),
        lambda r: replace(r, output_json="not json"),
        lambda r: {"task_id": r.task_id, "status": "completed"},
    ],
    ids=["task-id", "role", "missing-role", "status", "error-code", "bad-json", "not-a-result"],
)
def test_provider_result_identity_and_shape_are_enforced(tamper):
    tasks = (SpecialistTask("evidence", "evidence"),)
    results = asyncio.run(DagRunner(TamperingProvider(tamper)).run(tasks, SNAPSHOT))
    assert results[0].status == "failed"
    assert results[0].error_code == "INVALID_OUTPUT"
    assert results[0].role == "evidence"
