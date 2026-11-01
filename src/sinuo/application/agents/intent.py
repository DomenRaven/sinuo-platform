"""Code scheduler for one manual run.

The four question families each select one to three duties. This module does
not call a model, does not read model settings, and does not accept a client
role list. A sentence that matches more than one family is rejected.
"""

import hashlib
from dataclasses import dataclass

from sinuo.application.agents.execute import RunTaskSpec

_MAX_DUTIES = 3
_BRIEF = ("简报变化", "简报刚变化", "任务书刚变化", "任务书变化")
_NEXT = ("下一步", "检查进度")
_METHOD = ("问方法", "怎么访谈", "找方法", "选方法")
_MATERIALS = ("问资料", "问文献", "查证据", "找依据", "查研究")
_ROLES = {
    "brief": ("clarification",),
    "materials": ("evidence",),
    "method": ("method",),
    "next": ("evidence", "critic"),
}


class IntentRejected(Exception):
    """The sentence is not one family. No run and no model call."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class DutyPlan:
    kind: str
    roles: tuple[str, ...]
    tasks: tuple[RunTaskSpec, ...]
    gaps: tuple[str, ...]


def plan_duties(intent: str, *, project_id: str, now: float) -> DutyPlan:
    """Select duties, then build a plan of at most three roles.

    Next-step runs a deterministic gap note before the tasks exist. The critic
    task waits on the evidence task. Other families are a single duty.
    """

    kind = _classify(intent)
    roles = _ROLES[kind]
    if len(roles) < 1 or len(roles) > _MAX_DUTIES or len(set(roles)) != len(roles):
        raise IntentRejected("roles")
    gaps = _gap_check(kind)
    ids = {role: _task_id(project_id, intent, now, role) for role in roles}
    tasks: list[RunTaskSpec] = []
    for role in roles:
        depends: tuple[str, ...] = ()
        if role == "critic" and "evidence" in ids:
            depends = (ids["evidence"],)
        tasks.append(RunTaskSpec(ids[role], role, depends, True))
    return DutyPlan(kind, roles, tuple(tasks), gaps)


def _classify(intent: str) -> str:
    if not isinstance(intent, str):
        raise IntentRejected("intent")
    text = intent.strip()
    if text == "":
        raise IntentRejected("intent")
    hits: list[str] = []
    if any(phrase in text for phrase in _BRIEF):
        hits.append("brief")
    if any(phrase in text for phrase in _NEXT):
        hits.append("next")
    if any(phrase in text for phrase in _METHOD):
        hits.append("method")
    if any(phrase in text for phrase in _MATERIALS):
        hits.append("materials")
    if len(hits) != 1:
        raise IntentRejected("intent")
    return hits[0]


def _gap_check(kind: str) -> tuple[str, ...]:
    """Rule check only. It does not call a provider."""

    if kind != "next":
        return ()
    return ("阶段判断等待证据",)


def _task_id(project_id: str, intent: str, now: float, role: str) -> str:
    material = "\n".join((project_id, intent, repr(now), role))
    return hashlib.sha256(material.encode("utf-8")).hexdigest()
