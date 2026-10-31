"""One fake chain: clarify, read evidence, ask about method, then critique.

A single in-memory run accepts at most three roles, so the critic is a second
``AgentRun`` whose ``parent_run_id`` points at the first. The fake provider
runs outside ``write_uow``. No method-card table and no live model.
"""

import asyncio
import hashlib
import json
import math
from dataclasses import dataclass

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from sinuo.agents.contracts import InputSnapshot, ModelProvider, SpecialistTask
from sinuo.agents.fake import FakeModelProvider
from sinuo.agents.runner import DagRunner
from sinuo.application.auth.authorizer import SessionAuthorizer
from sinuo.application.retrieval.search import search_evidence
from sinuo.application.retrieval.snapshot import get_page_snapshot
from sinuo.domain.auth.errors import (
    ActionForbidden,
    AuthRequired,
    NotFoundOrHidden,
    SourceRedacted,
)
from sinuo.domain.auth.principal import Principal
from sinuo.domain.auth.resource import ResourceRef
from sinuo.domain.public_contract import Action
from sinuo.infrastructure.database import write_uow
from sinuo.infrastructure.domain_models import (
    AgentResult,
    AgentRun,
    Candidate,
    ContextSnapshot,
    Project,
    RunReadRecord,
    RunTask,
    RunTaskDependency,
)

_DENIED = (ActionForbidden, NotFoundOrHidden, SourceRedacted)
METHOD_QUESTION = "没有方法卡可查。上面的证据是否够用？"


class FakeChainRejected(Exception):
    """The fake provider did not echo the authorized read, or the project is closed."""


@dataclass(frozen=True)
class FakeChainResult:
    parent_run_id: str
    critic_run_id: str
    candidate_id: str
    roles: tuple[str, ...]


@dataclass(frozen=True)
class _Facts:
    project_id: str
    account_id: str
    intent: str
    query: str
    access_epoch: int
    now: float
    evidence_status: str
    hits: tuple[tuple[str, str], ...]
    page_version_id: str | None
    page_title: str | None
    page_body: str | None
    page_hash: str | None
    input_hash: str


def run_fake_chain(
    engine,
    principal: Principal,
    project_id: str,
    *,
    intent: str,
    query: str,
    page_version_id: str | None,
    now: float,
    provider: ModelProvider | None = None,
) -> FakeChainResult:
    """Read, ask the fake provider, then store two runs and one pending candidate."""

    checked_now = _require_now(now)
    checked_intent = _require_intent(intent)
    with write_uow(engine) as session:
        facts = _read(
            session,
            SessionAuthorizer(session),
            principal,
            project_id,
            intent=checked_intent,
            query=query,
            page_version_id=page_version_id,
            now=checked_now,
        )
    speech = _speak(facts, provider or FakeModelProvider())
    with write_uow(engine) as session:
        _reread_matches(session, SessionAuthorizer(session), principal, facts)
        return _store(session, facts, speech)


def _read(
    session: Session,
    authorizer: SessionAuthorizer,
    principal: Principal,
    project_id: str,
    *,
    intent: str,
    query: str,
    page_version_id: str | None,
    now: float,
) -> _Facts:
    before = session.scalar(text("SELECT total_changes()"))
    _require_project(authorizer, principal, project_id)
    found = search_evidence(session, authorizer, principal, project_id, query, 20)
    snapshot = None
    if page_version_id is not None:
        snapshot = get_page_snapshot(
            session, authorizer, principal, project_id, page_version_id
        )
    epoch = session.scalar(select(Project.access_epoch).where(Project.id == project_id))
    after = session.scalar(text("SELECT total_changes()"))
    if after != before:
        raise RuntimeError("fake chain read wrote")
    if type(epoch) is not int:
        raise FakeChainRejected("project")
    hits = tuple(
        (hit.artifact_version_id, str(hit.locator.get("excerpt", "")))
        for hit in found.hits
        if isinstance(hit.locator.get("excerpt"), str)
    )
    if found.hits:
        status = "hit"
    elif found.counts.get("no_hit", 0):
        status = "no_hit"
    else:
        status = "not_indexed"
    visible_page = snapshot is not None and snapshot.page_version_id is not None
    page_id = snapshot.page_version_id if visible_page and snapshot is not None else None
    frozen = {
        "intent": intent,
        "query": query,
        "artifact_version_ids": [item[0] for item in hits],
        "page_version_id": page_id,
    }
    return _Facts(
        project_id=project_id,
        account_id=principal.account_id,
        intent=intent,
        query=query,
        access_epoch=epoch,
        now=now,
        evidence_status=status,
        hits=hits,
        page_version_id=page_id,
        page_title=snapshot.title if visible_page and snapshot is not None else None,
        page_body=snapshot.body if visible_page and snapshot is not None else None,
        page_hash=snapshot.content_hash if visible_page and snapshot is not None else None,
        input_hash=_digest(_canonical(frozen)),
    )


def _speak(facts: _Facts, provider: ModelProvider) -> dict[str, dict]:
    outputs = _outputs(facts)
    snapshot = InputSnapshot(
        _token("context", facts),
        facts.project_id,
        facts.input_hash,
        _canonical({"outputs": outputs}),
    )
    parent = _run_roles(
        provider,
        snapshot,
        (
            SpecialistTask(_token("clarification", facts), "clarification"),
            SpecialistTask(
                _token("evidence", facts),
                "evidence",
                (_token("clarification", facts),),
            ),
            SpecialistTask(
                _token("method", facts),
                "method",
                (_token("evidence", facts),),
            ),
        ),
    )
    critic = _run_roles(
        provider,
        snapshot,
        (SpecialistTask(_token("critic", facts), "critic"),),
    )
    spoken = {**parent, **critic}
    if spoken != outputs:
        raise FakeChainRejected("output")
    return spoken


def _run_roles(provider: ModelProvider, snapshot: InputSnapshot, tasks) -> dict[str, dict]:
    results = asyncio.run(DagRunner(provider).run(tasks, snapshot))
    spoken: dict[str, dict] = {}
    for result in results:
        if result.status != "completed" or result.role is None:
            raise FakeChainRejected(result.error_code or "output")
        try:
            parsed = json.loads(result.output_json)
        except json.JSONDecodeError as exc:
            raise FakeChainRejected("output") from exc
        if not isinstance(parsed, dict):
            raise FakeChainRejected("output")
        spoken[result.role] = parsed
    return spoken


def _outputs(facts: _Facts) -> dict[str, dict]:
    evidence = {
        "status": facts.evidence_status,
        "artifact_version_ids": [item[0] for item in facts.hits],
        "excerpts": [item[1] for item in facts.hits],
        "page_version_id": facts.page_version_id,
        "page_title": facts.page_title,
        "page_body": facts.page_body,
    }
    method = {"question": METHOD_QUESTION, "method_card": None}
    proposal = {
        "summary": "待确认",
        "clarification": facts.intent,
        "artifact_version_ids": evidence["artifact_version_ids"],
        "excerpts": evidence["excerpts"],
        "page_version_id": facts.page_version_id,
        "page_title": facts.page_title,
        "method_card": None,
        "method_question": METHOD_QUESTION,
    }
    return {
        "clarification": {"question": facts.intent, "needs_user": True},
        "evidence": evidence,
        "method": method,
        "critic": {"action": "draft_decision", "proposal": proposal},
    }


def _reread_matches(
    session: Session,
    authorizer: SessionAuthorizer,
    principal: Principal,
    facts: _Facts,
) -> None:
    fresh = _read(
        session,
        authorizer,
        principal,
        facts.project_id,
        intent=facts.intent,
        query=facts.query,
        page_version_id=facts.page_version_id,
        now=facts.now,
    )
    if fresh.input_hash != facts.input_hash or fresh.hits != facts.hits:
        raise FakeChainRejected("stale")
    if fresh.page_version_id != facts.page_version_id or fresh.page_title != facts.page_title:
        raise FakeChainRejected("stale")


def _store(session: Session, facts: _Facts, speech: dict[str, dict]) -> FakeChainResult:
    if speech != _outputs(facts):
        raise FakeChainRejected("output")
    snapshot_id = _token("context", facts)
    parent_id = _token("run", facts)
    critic_id = _token("critic-run", facts)
    candidate_id = _token("candidate", facts)
    proposal = _canonical(speech["critic"]["proposal"])
    session.add(
        ContextSnapshot(
            id=snapshot_id,
            project_id=facts.project_id,
            intent=facts.intent,
            template_version="fake-1",
            method_card_version="none",
            authorization_scope_json=_canonical(
                {
                    "project_id": facts.project_id,
                    "page_version_id": facts.page_version_id,
                    "query": facts.query,
                }
            ),
            access_epoch=facts.access_epoch,
            exclusions_json="[]",
            index_revision=0,
            budget_json=_canonical({"provider": "fake"}),
            draft_page_id=None,
            draft_content_revision=None,
            draft_content_hash=None,
            draft_content_json=None,
            created_at=facts.now,
        )
    )
    session.flush()
    _add_run(session, facts, parent_id, snapshot_id, None)
    roles = ("clarification", "evidence", "method")
    previous: str | None = None
    for role in roles:
        task_id = _token(role, facts)
        _add_task(session, facts, parent_id, task_id, role)
        if previous is not None:
            session.add(
                RunTaskDependency(
                    run_id=parent_id,
                    task_id=task_id,
                    depends_on_task_id=previous,
                )
            )
        _add_result(session, facts, parent_id, task_id, role, speech[role])
        previous = task_id
    session.flush()
    _add_reads(session, facts, parent_id, _token("evidence", facts))
    _seal(session, parent_id, facts.now)
    _add_run(session, facts, critic_id, snapshot_id, parent_id)
    critic_task = _token("critic", facts)
    _add_task(session, facts, critic_id, critic_task, "critic")
    _add_result(session, facts, critic_id, critic_task, "critic", speech["critic"])
    session.add(
        Candidate(
            id=candidate_id,
            project_id=facts.project_id,
            run_id=critic_id,
            action="draft_decision",
            page_mode=None,
            status="pending",
            revision=1,
            proposal_json=proposal,
            proposal_hash=_digest(proposal),
            input_hash=facts.input_hash,
            context_snapshot_id=snapshot_id,
            schema_version=1,
            created_at=facts.now,
        )
    )
    _seal(session, critic_id, facts.now)
    return FakeChainResult(
        parent_id,
        critic_id,
        candidate_id,
        ("clarification", "evidence", "method", "critic"),
    )


def _add_run(
    session: Session,
    facts: _Facts,
    run_id: str,
    snapshot_id: str,
    parent_run_id: str | None,
) -> None:
    session.add(
        AgentRun(
            id=run_id,
            project_id=facts.project_id,
            initiated_by_account_id=facts.account_id,
            parent_run_id=parent_run_id,
            supersedes_run_id=None,
            context_snapshot_id=snapshot_id,
            execution_status="running",
            accepting_results=1,
            next_event_seq=0,
            budget_json=_canonical({"provider": "fake"}),
            created_at=facts.now,
            closed_at=None,
            origin="manual",
            preference_revision=None,
        )
    )
    session.flush()


def _seal(session: Session, run_id: str, now: float) -> None:
    run = session.get(AgentRun, run_id)
    if run is None:
        raise FakeChainRejected("run")
    run.execution_status = "completed"
    run.accepting_results = 0
    run.closed_at = now


def _add_task(
    session: Session,
    facts: _Facts,
    run_id: str,
    task_id: str,
    role: str,
) -> None:
    session.add(
        RunTask(
            id=task_id,
            run_id=run_id,
            project_id=facts.project_id,
            role=role,
            status="succeeded",
            mandatory=1,
            input_hash=facts.input_hash,
            available_at=facts.now,
            attempt_no=1,
            lease_owner=None,
            lease_until=None,
            fencing_token=0,
            output_ref=None,
            last_error=None,
        )
    )
    session.flush()


def _add_result(
    session: Session,
    facts: _Facts,
    run_id: str,
    task_id: str,
    role: str,
    output: dict,
) -> None:
    session.add(
        AgentResult(
            id=_token(f"result-{role}", facts),
            project_id=facts.project_id,
            run_id=run_id,
            task_id=task_id,
            input_hash=facts.input_hash,
            attempt_no=1,
            fencing_token=0,
            disposition="published",
            output_json=_canonical(output),
            diagnostic_code=None,
            usage_json=_canonical({"provider": "fake", "model_calls": 1}),
            created_at=facts.now,
        )
    )


def _add_reads(session: Session, facts: _Facts, run_id: str, task_id: str) -> None:
    for index, (version_id, excerpt) in enumerate(facts.hits):
        session.add(
            _read_row(
                facts,
                run_id,
                task_id,
                _token(f"read-e{index}", facts),
                f"tool-e-{index}",
                artifact_version_id=version_id,
                content_hash=_digest(excerpt),
            )
        )
    if facts.page_version_id is not None and facts.page_hash is not None:
        session.add(
            _read_row(
                facts,
                run_id,
                task_id,
                _token("read-page", facts),
                "tool-page",
                page_version_id=facts.page_version_id,
                content_hash=facts.page_hash,
            )
        )


def _read_row(
    facts: _Facts,
    run_id: str,
    task_id: str,
    row_id: str,
    tool_call_id: str,
    *,
    content_hash: str,
    page_version_id: str | None = None,
    artifact_version_id: str | None = None,
) -> RunReadRecord:
    return RunReadRecord(
        id=row_id,
        project_id=facts.project_id,
        run_id=run_id,
        task_id=task_id,
        tool_call_id=tool_call_id,
        page_version_id=page_version_id,
        artifact_version_id=artifact_version_id,
        evidence_anchor_id=None,
        content_hash=content_hash,
        index_revision=0,
        access_epoch=facts.access_epoch,
        read_at=facts.now,
        truncated=0,
        miss_scope=None,
    )


def _require_project(authorizer: SessionAuthorizer, principal: Principal, project_id: str) -> None:
    try:
        authorizer.authorize(
            Action.PROJECT_READ,
            ResourceRef(project_id=project_id, scope_type="project"),
            principal,
        )
    except AuthRequired:
        raise
    except _DENIED as exc:
        raise FakeChainRejected("project") from exc


def _require_now(now: object) -> float:
    if isinstance(now, bool) or not isinstance(now, (int, float)) or not math.isfinite(now):
        raise ValueError("now")
    return float(now)


def _require_intent(intent: object) -> str:
    if not isinstance(intent, str) or intent.strip() == "" or len(intent) > 200:
        raise ValueError("intent")
    return intent


def _token(label: str, facts: _Facts) -> str:
    raw = "|".join(
        (
            label,
            facts.project_id,
            facts.intent,
            facts.query,
            facts.page_version_id or "",
            str(facts.now),
        )
    )
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    return f"fake-{label}-{digest[:16]}"[:64]


def _canonical(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()
