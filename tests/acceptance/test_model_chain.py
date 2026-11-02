"""Injectable model chain. The provider is local. DeepSeek is not constructed."""

import json

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from sinuo.agents.contracts import InputSnapshot, SpecialistResult
from sinuo.agents.fake import FakeModelProvider
from sinuo.domain.auth.principal import Principal
from sinuo.infrastructure.domain_models import AgentRun, Candidate, Page, PageVersion, RunTask
from tests.acceptance.identity_fixtures import (
    PROJECT_A,
    identity_settings,
    seed_identity_world,
)

NOW = 1_762_100_000.0
INTENT = "合成四职责"
QUERY = "合成查询"
QUESTION = "合成问法"
SITUATION = "合成场景"


class OfflineFake(FakeModelProvider):
    """Fill one echo slot, then let FakeModelProvider speak. No network."""

    def __init__(self) -> None:
        self.roles: list[str] = []

    async def execute(self, task, snapshot, upstream) -> SpecialistResult:
        self.roles.append(task.role)
        document = json.loads(snapshot.context_json)
        if not isinstance(document, dict):
            document = {}
        outputs = document.get("outputs")
        if not isinstance(outputs, dict):
            outputs = {}
        if task.role not in outputs:
            filled = dict(outputs)
            filled[task.role] = {"summary": "待确认", "gaps": []}
            document = dict(document)
            document["outputs"] = filled
            snapshot = InputSnapshot(
                snapshot.snapshot_id,
                snapshot.project_id,
                snapshot.input_hash,
                json.dumps(document, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
            )
        return await super().execute(task, snapshot, upstream)


def _refuse(*_args, **_kwargs):
    raise AssertionError("live model")


def _block_live_model(monkeypatch) -> None:
    monkeypatch.setattr("sinuo.agents.deepseek.DeepSeekAdapter.__init__", _refuse)
    monkeypatch.setattr("sinuo.agents.deepseek.DeepSeekAdapter.from_settings", _refuse)
    monkeypatch.setattr("sinuo.agents.deepseek.DeepSeekAdapter.execute", _refuse)


def _count(engine, model) -> int:
    with Session(engine) as session:
        counted = session.scalar(select(func.count()).select_from(model))
    assert isinstance(counted, int)
    return counted


def test_model_chain_uses_injected_provider_for_two_runs(engine, database_url, monkeypatch):
    _block_live_model(monkeypatch)
    settings = identity_settings(database_url, model_enabled=False)
    assert settings.model_enabled is False
    world = seed_identity_world(engine, settings)
    pages = _count(engine, Page)
    versions = _count(engine, PageVersion)
    provider = OfflineFake()
    assert isinstance(provider, FakeModelProvider)
    from sinuo.application.agents.model_chain import run_model_chain

    result = run_model_chain(
        engine,
        Principal(world.lead.account_id, world.lead.session_id, world.lead.auth_epoch),
        PROJECT_A,
        provider,
        intent=INTENT,
        query=QUERY,
        question=QUESTION,
        situation=SITUATION,
        constraints={"consent": True},
        page_version_id=None,
        now=NOW,
    )
    with Session(engine) as session:
        parent = session.get(AgentRun, result.parent_run_id)
        critic = session.get(AgentRun, result.critic_run_id)
        assert parent is not None and critic is not None
        parent_roles = session.scalars(
            select(RunTask.role).where(RunTask.run_id == parent.id)
        ).all()
        critic_roles = session.scalars(
            select(RunTask.role).where(RunTask.run_id == critic.id)
        ).all()
        confirmed = session.scalar(
            select(func.count())
            .select_from(Candidate)
            .where(Candidate.status != "pending")
        )
    assert parent.execution_status == "completed"
    assert critic.execution_status == "completed"
    assert parent.parent_run_id is None
    assert critic.parent_run_id == parent.id
    assert set(parent_roles) == {"clarification", "evidence", "method"}
    assert len(parent_roles) == 3
    assert list(critic_roles) == ["critic"]
    assert provider.roles == ["clarification", "evidence", "method", "critic"]
    assert confirmed == 0
    assert _count(engine, Page) == pages
    assert _count(engine, PageVersion) == versions
