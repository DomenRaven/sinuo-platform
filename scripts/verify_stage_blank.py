"""给「夜间取物」页草稿写入任务目标与约束节点，再跑真实模型验证 stage_blank。

先写草稿（writer=human 的 problem/requirement 节点），再发一次「下一步」，
看过程检查是否基于更完整的任务情境给出进入下一阶段的 stage_blank 候选。
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
import uuid

from sqlalchemy import select

from sinuo.agents.deepseek import DeepSeekAdapter
from sinuo.application.agents.execute import (
    AgentRunRequest,
    finish_agent_run,
    open_agent_run,
)
from sinuo.application.agents.intent import plan_duties
from sinuo.application.versions.drafts import save_typed_draft
from sinuo.config import Settings
from sinuo.domain.auth.principal import Principal
from sinuo.infrastructure.database import build_engine, write_uow
from sinuo.infrastructure.domain_models import Candidate, Page, PageDraft

PROJECT_ID = "061f59bc-cd9e-49dc-9d8c-cf42ac04e3eb"
ACCOUNT_ID = "11111111-1111-4111-8111-111111111111"
PAGE_ID = "8e0711fc-aaf7-4a1d-8c99-eb5d00e34ecf"
PAGE_VERSION_ID = "c4f7f343-4699-4f99-ad43-637c26a46d0d"


def _write_nodes(engine) -> None:
    with write_uow(engine) as session:
        page = session.get(Page, PAGE_ID)
        if page is None:
            raise SystemExit("找不到页")
        draft = session.get(PageDraft, PAGE_ID)
        if draft is None:
            raise SystemExit("找不到草稿")
        document = {
            "schema_version": 1,
            "nodes": [
                {"node_id": str(uuid.uuid4()), "kind": "problem"},
                {"node_id": str(uuid.uuid4()), "kind": "requirement"},
                {"node_id": str(uuid.uuid4()), "kind": "requirement"},
            ],
            "edges": [],
        }
        # 用记录内容来承载文字（record 字段在 typed 节点里是 formal 标签，正文需要另存）。
        # 这里正文不进 schema，只建立节点骨架，让 critic 能看到"有任务目标与约束"。
        revision, _ = save_typed_draft(
            session,
            page_id=PAGE_ID,
            project_id=PROJECT_ID,
            base_version_id=page.head_version_id,
            expected_revision=draft.content_revision,
            document=document,
            writer="human",
            actor_id=ACCOUNT_ID,
            now=time.time(),
        )
        print(f"草稿已写入节点，content_revision -> {revision}")


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    settings = Settings()
    if not settings.model_enabled:
        print("model_enabled 不是 true，退出")
        return
    engine = build_engine(settings.database_url, settings.sqlite_busy_timeout_ms)
    adapter = None
    try:
        _write_nodes(engine)
        now = time.time()
        principal = Principal(ACCOUNT_ID, "stage-verify", 1)
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
        adapter = DeepSeekAdapter.from_settings(settings)
        finished = finish_agent_run(engine, principal, opened.run_id, adapter, now=now)
        print(f"execution_status={finished.execution_status}")
        print(f"candidate_id={finished.candidate_id}")
        print(f"candidate_action={finished.candidate_action}")
        if finished.candidate_id is not None:
            from sqlalchemy.orm import Session

            with Session(engine) as session:
                row = session.get(Candidate, finished.candidate_id)
                if row is not None:
                    print(f"候选 action={row.action} page_mode={row.page_mode} "
                          f"status={row.status}")
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
