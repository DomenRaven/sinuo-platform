"""Seed course-level method cards so the OXO demo's "选方法" actually matches.

These are demonstration cards written for the walk. They are course-scoped
(library_key == "course", project_id is NULL), so any project can look them
up. Insertion is idempotent: existing cards are left untouched.

The four cards are the methods the OXO theory pages already link to
(method-用户访谈 / method-任务分析 / method-可用性测试 / method-原型).
"""

from __future__ import annotations

import json
import time
import uuid

from sqlalchemy import select

from sinuo.config import Settings
from sinuo.infrastructure.database import build_engine, write_uow
from sinuo.infrastructure.methods import MethodCard

CARDS: tuple[dict[str, object], ...] = (
    {
        "card_key": "method-用户访谈",
        "name": "用户访谈",
        "question_terms": ["用户访谈"],
        "inputs": ["访谈提纲", "目标用户", "记录工具"],
        "steps": ["确定访谈目标", "招募受访者", "半结构式提问", "整理并标注发现"],
        "outputs": ["痛点与需求摘录", "可引用的访谈片段"],
        "limits": ["样本有限", "受访谈者表述影响", "不能代替观察"],
    },
    {
        "card_key": "method-任务分析",
        "name": "任务分析",
        "question_terms": ["任务分析"],
        "inputs": ["任务清单", "使用场景", "操作步骤"],
        "steps": ["拆解任务步骤", "标注施力与动作", "识别困难点", "归纳设计约束"],
        "outputs": ["任务流程图", "关键动作分类"],
        "limits": ["依赖真实场景", "动作分类可能不一致"],
    },
    {
        "card_key": "method-可用性测试",
        "name": "可用性测试",
        "question_terms": ["可用性测试"],
        "inputs": ["样机", "测试任务", "观察记录表"],
        "steps": ["定义测试任务", "招募被试", "记录操作与困难", "归纳改进项"],
        "outputs": ["可用性问题清单", "改进优先级"],
        "limits": ["需要样机", "公开资料可能没有样本量与通过率"],
    },
    {
        "card_key": "method-原型",
        "name": "原型",
        "question_terms": ["原型"],
        "inputs": ["概念方向", "材料与工艺", "目标功能"],
        "steps": ["选定概念方向", "制作实物样机", "记录形态与材料", "对比迭代"],
        "outputs": ["样机", "形态/抓握/结构记录"],
        "limits": ["样机不代表量产", "早期样机不反映最终材料"],
    },
)


def _json(payload: object) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def main() -> int:
    settings = Settings()
    engine = build_engine(settings.database_url)
    now = time.time()
    inserted = 0
    with write_uow(engine) as session:
        existing = set(
            session.scalars(
                select(MethodCard.card_key).where(MethodCard.library_key == "course")
            ).all()
        )
        for spec in CARDS:
            card_key = str(spec["card_key"])
            if card_key in existing:
                continue
            session.add(
                MethodCard(
                    id=str(uuid.uuid4()),
                    library_key="course",
                    project_id=None,
                    card_key=card_key,
                    version_no=1,
                    name=str(spec["name"]),
                    source_json=_json(
                        {
                            "title": str(spec["name"]),
                            "locator": "course/methods",
                            "license": "course-demo",
                        }
                    ),
                    applicable_json=_json(
                        {
                            "question_terms": list(spec["question_terms"]),
                            "situations": [],
                            "requires": [],
                            "stage_names": [],
                        }
                    ),
                    inputs_json=_json(spec["inputs"]),
                    steps_json=_json(spec["steps"]),
                    outputs_json=_json(spec["outputs"]),
                    limits_json=_json(spec["limits"]),
                    created_at=now,
                )
            )
            inserted += 1
    print(f"seeded {inserted} course method cards; existing {len(existing)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
