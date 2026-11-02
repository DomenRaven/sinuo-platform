"""Injectable model chain. Two persisted runs, at most three roles each.

The caller supplies the provider. This module does not construct a live
adapter, confirm a candidate, or create a page.
"""

import hashlib
from dataclasses import dataclass

from sqlalchemy.engine import Engine

from sinuo.agents.contracts import ModelProvider
from sinuo.application.agents.execute import (
    AgentRunRequest,
    RunTaskSpec,
    finish_agent_run,
    open_agent_run,
)
from sinuo.domain.auth.principal import Principal

_FIRST_ROLES = ("clarification", "evidence", "method")
_CRITIC_ROLE = "critic"


@dataclass(frozen=True)
class ModelChainResult:
    parent_run_id: str
    critic_run_id: str
    parent_status: str
    critic_status: str


def run_model_chain(
    engine: Engine,
    principal: Principal,
    project_id: str,
    provider: ModelProvider,
    *,
    intent: str,
    query: str,
    question: str,
    situation: str,
    constraints: dict[str, bool],
    page_version_id: str | None,
    now: float,
) -> ModelChainResult:
    """Open clarification, evidence, and method, then a critic child run.

    The second request passes the first ``run_id`` as ``parent_run_id``.
    Each run is closed by ``finish_agent_run`` with the same provider.
    """

    opened = _open(
        engine,
        principal,
        project_id,
        provider_roles=_FIRST_ROLES,
        intent=intent,
        query=query,
        question=question,
        situation=situation,
        constraints=constraints,
        page_version_id=page_version_id,
        now=now,
        parent_run_id=None,
    )
    parent = finish_agent_run(engine, principal, opened.run_id, provider, now=now)
    critic_now = now + 1.0
    child = _open(
        engine,
        principal,
        project_id,
        provider_roles=(_CRITIC_ROLE,),
        intent=intent,
        query=query,
        question=question,
        situation=situation,
        constraints=constraints,
        page_version_id=page_version_id,
        now=critic_now,
        parent_run_id=opened.run_id,
    )
    critic = finish_agent_run(engine, principal, child.run_id, provider, now=critic_now)
    return ModelChainResult(
        parent.run_id,
        critic.run_id,
        parent.execution_status,
        critic.execution_status,
    )


def _open(
    engine: Engine,
    principal: Principal,
    project_id: str,
    *,
    provider_roles: tuple[str, ...],
    intent: str,
    query: str,
    question: str,
    situation: str,
    constraints: dict[str, bool],
    page_version_id: str | None,
    now: float,
    parent_run_id: str | None,
):
    request = AgentRunRequest(
        intent=intent,
        tasks=_specs(project_id, now, provider_roles),
        query=query,
        question=question,
        situation=situation,
        constraints=constraints,
        page_version_id=page_version_id,
        now=now,
        parent_run_id=parent_run_id,
    )
    return open_agent_run(engine, principal, project_id, request)


def _specs(project_id: str, now: float, roles: tuple[str, ...]) -> tuple[RunTaskSpec, ...]:
    previous: str | None = None
    specs: list[RunTaskSpec] = []
    for role in roles:
        task_id = _task_id(project_id, now, role)
        depends = () if previous is None else (previous,)
        specs.append(RunTaskSpec(task_id, role, depends, True))
        previous = task_id
    return tuple(specs)


def _task_id(project_id: str, now: float, role: str) -> str:
    raw = "|".join(("model-chain", project_id, role, repr(now)))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()
