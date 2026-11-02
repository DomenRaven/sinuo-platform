"""验证 critic 的输入上下文是否携带 evidence 结果与阶段表（不调用真实模型）。

注入一个记录 context 的假模型跑一次「问下一步」，打印 critic 看到的
upstream / stages / current_stage / guidance，确认 execute.py 的改动生效。
"""

from __future__ import annotations

import json
import sys
import time

from sinuo.agents.contracts import SpecialistResult
from sinuo.application.agents.execute import (
    AgentRunRequest,
    finish_agent_run,
    open_agent_run,
)
from sinuo.application.agents.intent import plan_duties
from sinuo.config import Settings
from sinuo.domain.auth.principal import Principal
from sinuo.infrastructure.database import build_engine

PROJECT_ID = "061f59bc-cd9e-49dc-9d8c-cf42ac04e3eb"
ACCOUNT_ID = "11111111-1111-4111-8111-111111111111"
PAGE_VERSION_ID = "c4f7f343-4699-4f99-ad43-637c26a46d0d"


class RecordingFake:
    def __init__(self) -> None:
        self.contexts: dict[str, dict] = {}

    async def execute(self, task, snapshot, upstream):
        del upstream
        context = json.loads(snapshot.context_json)
        self.contexts[task.role] = context
        body = json.dumps({"gaps": [], "summary": "待确认"}, ensure_ascii=False)
        return SpecialistResult(task.task_id, "completed", body, role=task.role)


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    settings = Settings(_env_file=None)
    engine = build_engine(settings.database_url, settings.sqlite_busy_timeout_ms)
    try:
        now = time.time()
        principal = Principal(ACCOUNT_ID, "verify-session", 1)
        plan = plan_duties("下一步", project_id=PROJECT_ID, now=now)
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
        provider = RecordingFake()
        finished = finish_agent_run(engine, principal, opened.run_id, provider, now=now)
        print(f"status={finished.execution_status} candidate={finished.candidate_id} "
              f"action={finished.candidate_action}")
        critic = provider.contexts.get("critic")
        evidence = provider.contexts.get("evidence")
        if critic is None:
            print("!! 没有 critic 上下文")
            return
        print("critic 上下文字段:", sorted(critic.keys()))
        print("  upstream 键:", list(critic.get("upstream", {}).keys()))
        if evidence:
            print("  evidence.hits 条数:", len(evidence.get("hits", [])))
        stages = critic.get("stages")
        print("  stages 条数:", len(stages) if isinstance(stages, list) else "n/a")
        if isinstance(stages, list) and stages:
            print("  阶段:", [s["name"] for s in stages])
        print("  current_stage:", critic.get("current_stage"))
        print("  guidance:", critic.get("guidance"))
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
