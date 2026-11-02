"""用真实 DeepSeek 跑一次「问下一步」，验证 stage_blank 候选能否生成。

从 .env 读取已配置的模型。一次运行最多两次调用（evidence + critic）。
"""

from __future__ import annotations

import asyncio
import json
import sys
import time

from sqlalchemy import select

from sinuo.agents.deepseek import DeepSeekAdapter
from sinuo.application.agents.execute import (
    AgentRunRequest,
    finish_agent_run,
    open_agent_run,
)
from sinuo.application.agents.intent import plan_duties
from sinuo.config import Settings
from sinuo.domain.auth.principal import Principal
from sinuo.infrastructure.database import build_engine
from sinuo.infrastructure.domain_models import AgentResult, Candidate, RunTask

PROJECT_ID = "061f59bc-cd9e-49dc-9d8c-cf42ac04e3eb"
ACCOUNT_ID = "11111111-1111-4111-8111-111111111111"
PAGE_VERSION_ID = "c4f7f343-4699-4f99-ad43-637c26a46d0d"


def _dump_output(engine, run_id: str, role: str) -> None:
    from sqlalchemy.orm import Session

    with Session(engine) as session:
        task_id = session.scalar(
            select(RunTask.id).where(RunTask.run_id == run_id, RunTask.role == role)
        )
        raw = session.scalar(
            select(AgentResult.output_json).where(
                AgentResult.run_id == run_id,
                AgentResult.task_id == task_id,
                AgentResult.disposition == "published",
            )
        )
    if not isinstance(raw, str):
        print(f"  {role}: 无输出")
        return
    parsed = json.loads(raw)
    model = parsed.get("model", {}) if isinstance(parsed, dict) else {}
    summary = model.get("summary", "")
    gaps = model.get("gaps", [])
    print(f"  {role} summary={summary!r} gaps={gaps!r}")


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    settings = Settings()
    if not settings.model_enabled:
        print("model_enabled 不是 true，无法跑真实模型")
        return
    engine = build_engine(settings.database_url, settings.sqlite_busy_timeout_ms)
    adapter = None
    try:
        now = time.time()
        principal = Principal(ACCOUNT_ID, "live-verify", 1)
        plan = plan_duties("下一步", project_id=PROJECT_ID, now=now)
        print(f"派发职责: {plan.roles}")
        request = AgentRunRequest(
            intent="下一步",
            tasks=plan.tasks,
            query="夜间取物",
            question="",
            situation="宿舍",
            constraints={},
            page_version_id=PAGE_VERSION_ID,
            now=now,
            opening_gaps=plan.gaps,
        )
        opened = open_agent_run(engine, principal, PROJECT_ID, request)
        adapter = DeepSeekAdapter.from_settings(settings)
        finished = finish_agent_run(engine, principal, opened.run_id, adapter, now=now)
        print(f"run_id={opened.run_id}")
        print(f"execution_status={finished.execution_status}")
        print(f"candidate_id={finished.candidate_id}")
        print(f"candidate_action={finished.candidate_action}")
        _dump_output(engine, opened.run_id, "evidence")
        _dump_output(engine, opened.run_id, "critic")
        if finished.candidate_id is not None:
            from sqlalchemy.orm import Session

            with Session(engine) as session:
                row = session.get(Candidate, finished.candidate_id)
                if row is not None:
                    print(f"候选 status={row.status} action={row.action} "
                          f"page_mode={row.page_mode}")
                    print(f"proposal={row.proposal_json}")
    finally:
        if adapter is not None:
            try:
                asyncio.run(adapter.client.aclose())
            except Exception:
                pass
        engine.dispose()


if __name__ == "__main__":
    main()
