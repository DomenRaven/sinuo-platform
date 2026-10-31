"""Read one finished fake chain. No model call and no business write."""

import json
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from sinuo.application.agents.fake_chain import METHOD_QUESTION
from sinuo.application.auth.authorizer import SessionAuthorizer
from sinuo.domain.auth.errors import (
    ActionForbidden,
    AuthRequired,
    NotFoundOrHidden,
    SourceRedacted,
)
from sinuo.domain.auth.principal import Principal
from sinuo.domain.auth.resource import ResourceRef
from sinuo.domain.public_contract import Action
from sinuo.infrastructure.domain_models import AgentResult, AgentRun, Candidate, RunTask

_ID_MAX = 64
_DENIED = (ActionForbidden, NotFoundOrHidden, SourceRedacted)
_ROLE_ORDER = ("clarification", "evidence", "method", "critic")


@dataclass(frozen=True)
class ChainStep:
    run_id: str
    role: str
    status: str


@dataclass(frozen=True)
class ChainHit:
    artifact_version_id: str
    excerpt: str


@dataclass(frozen=True)
class FakeChainReading:
    parent_run_id: str | None
    parent_status: str | None
    critic_run_id: str | None
    critic_status: str | None
    steps: tuple[ChainStep, ...]
    hits: tuple[ChainHit, ...]
    page_title: str | None
    page_body: str | None
    method_question: str | None
    candidate_status: str | None
    candidate_body: str | None

    @property
    def found(self) -> bool:
        return self.parent_run_id is not None


def read_fake_chain(
    session: Session,
    authorizer: SessionAuthorizer,
    principal: Principal,
    project_id: str,
    parent_run_id: str,
) -> FakeChainReading:
    """Return one finished chain after ``run.read``.

    A missing run, another project, or a caller without ``run.read`` returns
    an empty reading. That value has no title, no page body, and no candidate
    body. ``AuthRequired`` still propagates.
    """

    checked_project = _require_id(project_id, "project_id")
    checked_parent = _require_id(parent_run_id, "parent_run_id")
    if not _can_read_run(authorizer, principal, checked_project):
        return _empty()
    with session.no_autoflush:
        parent = session.execute(
            select(AgentRun.id, AgentRun.execution_status, AgentRun.parent_run_id).where(
                AgentRun.id == checked_parent,
                AgentRun.project_id == checked_project,
            )
        ).one_or_none()
        if parent is None or parent.parent_run_id is not None:
            return _empty()
        critic = session.execute(
            select(AgentRun.id, AgentRun.execution_status)
            .where(
                AgentRun.project_id == checked_project,
                AgentRun.parent_run_id == parent.id,
            )
            .order_by(AgentRun.id)
        ).first()
        run_ids = [parent.id]
        if critic is not None:
            run_ids.append(critic.id)
        task_rows = session.execute(
            select(RunTask.run_id, RunTask.role, RunTask.status, RunTask.id).where(
                RunTask.project_id == checked_project,
                RunTask.run_id.in_(run_ids),
            )
        ).all()
        evidence = _output(session, checked_project, parent.id, "evidence")
        method = _output(session, checked_project, parent.id, "method")
        proposal = None
        if critic is not None:
            proposal = session.scalar(
                select(Candidate.proposal_json)
                .where(
                    Candidate.project_id == checked_project,
                    Candidate.run_id == critic.id,
                    Candidate.action == "draft_decision",
                    Candidate.status == "pending",
                )
                .order_by(Candidate.id)
            )
    hits, title, body = _hits(evidence)
    question = _question(method)
    steps = tuple(
        ChainStep(row.run_id, row.role, row.status)
        for row in sorted(task_rows, key=lambda item: _role_key(item.role))
    )
    return FakeChainReading(
        parent_run_id=parent.id,
        parent_status=parent.execution_status,
        critic_run_id=None if critic is None else critic.id,
        critic_status=None if critic is None else critic.execution_status,
        steps=steps,
        hits=hits,
        page_title=title,
        page_body=body,
        method_question=question,
        candidate_status="pending" if isinstance(proposal, str) else None,
        candidate_body=proposal if isinstance(proposal, str) else None,
    )


def _empty() -> FakeChainReading:
    return FakeChainReading(None, None, None, None, (), (), None, None, None, None, None)


def _require_id(value: object, field: str) -> str:
    if not isinstance(value, str) or value.strip() == "" or len(value) > _ID_MAX:
        raise ValueError(field)
    return value


def _can_read_run(
    authorizer: SessionAuthorizer,
    principal: Principal,
    project_id: str,
) -> bool:
    try:
        authorizer.authorize(
            Action.RUN_READ,
            ResourceRef(project_id=project_id, scope_type="project"),
            principal,
        )
    except AuthRequired:
        raise
    except _DENIED:
        return False
    return True


def _output(session: Session, project_id: str, run_id: str, role: str) -> dict | None:
    raw = session.scalar(
        select(AgentResult.output_json)
        .join(RunTask, RunTask.id == AgentResult.task_id)
        .where(
            AgentResult.project_id == project_id,
            AgentResult.run_id == run_id,
            RunTask.role == role,
            AgentResult.disposition == "published",
        )
        .order_by(AgentResult.id)
    )
    if not isinstance(raw, str):
        return None
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(parsed, dict):
        return None
    return parsed


def _hits(evidence: dict | None) -> tuple[tuple[ChainHit, ...], str | None, str | None]:
    if evidence is None:
        return (), None, None
    versions = evidence.get("artifact_version_ids")
    excerpts = evidence.get("excerpts")
    hits: list[ChainHit] = []
    if isinstance(versions, list) and isinstance(excerpts, list):
        for version_id, excerpt in zip(versions, excerpts, strict=False):
            if isinstance(version_id, str) and version_id and isinstance(excerpt, str) and excerpt:
                hits.append(ChainHit(version_id, excerpt))
    title = evidence.get("page_title")
    body = evidence.get("page_body")
    return (
        tuple(hits),
        title if isinstance(title, str) and title else None,
        body if isinstance(body, str) and body else None,
    )


def _question(method: dict | None) -> str | None:
    if method is None:
        return None
    question = method.get("question")
    if question == METHOD_QUESTION:
        return METHOD_QUESTION
    return None


def _role_key(role: str) -> tuple[int, str]:
    try:
        return (_ROLE_ORDER.index(role), role)
    except ValueError:
        return (len(_ROLE_ORDER), role)
