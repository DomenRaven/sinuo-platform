"""Read artifacts, one agent run, and candidates. These functions do not commit."""

import json
import re
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from sinuo.application.auth.authorizer import SessionAuthorizer
from sinuo.application.candidates.errors import CandidateCommandError
from sinuo.application.candidates.proposal import parse_proposal
from sinuo.domain.auth.errors import (
    ActionForbidden,
    InvalidSchema,
    NotFoundOrHidden,
    SourceRedacted,
)
from sinuo.domain.auth.principal import Principal
from sinuo.domain.auth.resource import ResourceRef
from sinuo.domain.public_contract import Action
from sinuo.infrastructure.domain_models import (
    AgentResult,
    AgentRun,
    Artifact,
    ArtifactVersion,
    Candidate,
    ContextSnapshot,
    EvidenceAnchor,
    Page,
    PageVersion,
    RunReadRecord,
    RunTask,
)

_ROLE_ORDER = ("clarification", "evidence", "method", "critic")
_MISS = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


@dataclass(frozen=True)
class ArtifactCard:
    artifact_id: str
    title: str
    artifact_version_id: str
    version_no: int
    media_type: str


@dataclass(frozen=True)
class ArtifactList:
    artifacts: tuple[ArtifactCard, ...]
    has_hidden_artifacts: bool


@dataclass(frozen=True)
class RunReadItem:
    task_id: str
    target: str
    label: str | None
    object_id: str | None


@dataclass(frozen=True)
class DutyStep:
    task_id: str
    role: str
    status: str
    summary: str | None
    gaps: tuple[str, ...]
    error: str | None
    reads: tuple[RunReadItem, ...]


@dataclass(frozen=True)
class CandidateCard:
    candidate_id: str
    run_id: str
    action: str
    page_mode: str | None
    status: str
    summary: str | None
    title: str | None
    target_stage_id: str | None
    page_id: str | None
    input_page_version_id: str | None
    edge_kind: str | None
    gaps: tuple[str, ...]
    preview_ready: bool


@dataclass(frozen=True)
class AgentRunView:
    run_id: str
    project_id: str
    status: str
    intent: str
    page_version_id: str | None
    opening_gaps: tuple[str, ...]
    duties: tuple[DutyStep, ...]
    candidates: tuple[CandidateCard, ...]


def list_visible_artifacts(
    session: Session,
    authorizer: SessionAuthorizer,
    principal: Principal,
    project_id: str,
) -> ArtifactList:
    """Latest readable original of each artifact. Hidden titles stay out."""

    _require_project(authorizer, principal, project_id, Action.PROJECT_READ)
    rows = session.scalars(
        select(Artifact)
        .where(Artifact.project_id == project_id)
        .order_by(Artifact.created_at, Artifact.id)
    ).all()
    versions = session.scalars(
        select(ArtifactVersion).where(ArtifactVersion.project_id == project_id)
    ).all()
    latest: dict[str, ArtifactVersion] = {}
    for version in versions:
        current = latest.get(version.artifact_id)
        if current is None or version.version_no > current.version_no:
            latest[version.artifact_id] = version
    visible: list[ArtifactCard] = []
    hidden = False
    for artifact in rows:
        version = latest.get(artifact.id)
        if version is None or not _allows(
            authorizer,
            Action.ARTIFACT_ORIGINAL_READ,
            ResourceRef(
                project_id=project_id,
                scope_type="artifact_version",
                artifact_version_id=version.id,
            ),
            principal,
        ):
            hidden = True
            continue
        visible.append(
            ArtifactCard(
                artifact_id=artifact.id,
                title=artifact.title,
                artifact_version_id=version.id,
                version_no=version.version_no,
                media_type=version.media_type,
            )
        )
    return ArtifactList(tuple(visible), hidden)


def list_candidates(
    session: Session,
    authorizer: SessionAuthorizer,
    principal: Principal,
    project_id: str,
) -> tuple[CandidateCard, ...]:
    """Candidates in the project. Requires run.read. Does not preview or commit."""

    _require_project(authorizer, principal, project_id, Action.RUN_READ)
    rows = session.scalars(
        select(Candidate)
        .where(Candidate.project_id == project_id)
        .order_by(Candidate.created_at.desc(), Candidate.id)
    ).all()
    return tuple(_candidate(row) for row in rows)


def read_agent_run(
    session: Session,
    authorizer: SessionAuthorizer,
    principal: Principal,
    run_id: str,
) -> AgentRunView:
    """Duties that were actually stored, what they read, and their candidates."""

    run = session.get(AgentRun, run_id)
    if run is None:
        raise NotFoundOrHidden()
    authorizer.authorize(
        Action.RUN_READ,
        ResourceRef(project_id=run.project_id, scope_type="project"),
        principal,
    )
    snapshot = session.get(ContextSnapshot, run.context_snapshot_id)
    intent = snapshot.intent if snapshot is not None else ""
    scope = _object(snapshot.authorization_scope_json) if snapshot is not None else {}
    page_version_id = _text(scope.get("page_version_id"), 64)
    opening = _strings(scope.get("opening_gaps"), 8)
    tasks = session.scalars(select(RunTask).where(RunTask.run_id == run_id)).all()
    ordered = sorted(tasks, key=lambda item: (_role_index(item.role), item.id))
    results = {
        row.task_id: row
        for row in session.scalars(
            select(AgentResult).where(
                AgentResult.run_id == run_id,
                AgentResult.disposition == "published",
            )
        ).all()
    }
    grouped: dict[str, list[RunReadRecord]] = {}
    for item in session.scalars(
        select(RunReadRecord)
        .where(RunReadRecord.run_id == run_id)
        .order_by(RunReadRecord.read_at, RunReadRecord.id)
    ).all():
        grouped.setdefault(item.task_id, []).append(item)
    candidates = session.scalars(
        select(Candidate)
        .where(Candidate.run_id == run_id)
        .order_by(Candidate.created_at, Candidate.id)
    ).all()
    duties = tuple(
        _duty(session, authorizer, principal, run.project_id, task, results.get(task.id), grouped)
        for task in ordered
    )
    return AgentRunView(
        run_id=run.id,
        project_id=run.project_id,
        status=run.execution_status,
        intent=intent,
        page_version_id=page_version_id,
        opening_gaps=opening,
        duties=duties,
        candidates=tuple(_candidate(row) for row in candidates),
    )


def _duty(
    session: Session,
    authorizer: SessionAuthorizer,
    principal: Principal,
    project_id: str,
    task: RunTask,
    result: AgentResult | None,
    grouped: dict[str, list[RunReadRecord]],
) -> DutyStep:
    summary, gaps, error = _published(None if result is None else result.output_json)
    if error is None:
        error = _safe_error(task.last_error)
    return DutyStep(
        task_id=task.id,
        role=task.role,
        status=task.status,
        summary=summary,
        gaps=gaps,
        error=error,
        reads=tuple(
            _read_item(session, authorizer, principal, project_id, item)
            for item in grouped.get(task.id, ())
        ),
    )


def _read_item(
    session: Session,
    authorizer: SessionAuthorizer,
    principal: Principal,
    project_id: str,
    row: RunReadRecord,
) -> RunReadItem:
    if row.page_version_id:
        return _page_read(session, authorizer, principal, project_id, row)
    if row.artifact_version_id:
        return _artifact_read(session, authorizer, principal, project_id, row)
    if row.evidence_anchor_id:
        return _evidence_read(session, authorizer, principal, project_id, row)
    if row.miss_scope and _MISS.fullmatch(row.miss_scope):
        return RunReadItem(row.task_id, "miss", row.miss_scope, None)
    return RunReadItem(row.task_id, "restricted", None, None)


def _page_read(
    session: Session,
    authorizer: SessionAuthorizer,
    principal: Principal,
    project_id: str,
    row: RunReadRecord,
) -> RunReadItem:
    version = session.get(PageVersion, row.page_version_id)
    if version is None or version.project_id != project_id:
        return RunReadItem(row.task_id, "restricted", None, None)
    allowed = _allows(
        authorizer,
        Action.PAGE_READ,
        ResourceRef(project_id=project_id, scope_type="page_version", page_version_id=version.id),
        principal,
    )
    if not allowed:
        return RunReadItem(row.task_id, "restricted", None, None)
    page = session.get(Page, version.page_id)
    label = None if page is None else page.title
    return RunReadItem(row.task_id, "page_version", label, version.id)


def _artifact_read(
    session: Session,
    authorizer: SessionAuthorizer,
    principal: Principal,
    project_id: str,
    row: RunReadRecord,
) -> RunReadItem:
    version = session.get(ArtifactVersion, row.artifact_version_id)
    if version is None or version.project_id != project_id:
        return RunReadItem(row.task_id, "restricted", None, None)
    allowed = _allows(
        authorizer,
        Action.ARTIFACT_ORIGINAL_READ,
        ResourceRef(
            project_id=project_id,
            scope_type="artifact_version",
            artifact_version_id=version.id,
        ),
        principal,
    )
    if not allowed:
        return RunReadItem(row.task_id, "restricted", None, None)
    artifact = session.get(Artifact, version.artifact_id)
    label = None
    if artifact is not None:
        label = artifact.title + " · 第 " + str(version.version_no) + " 版"
    return RunReadItem(row.task_id, "artifact_version", label, version.id)


def _evidence_read(
    session: Session,
    authorizer: SessionAuthorizer,
    principal: Principal,
    project_id: str,
    row: RunReadRecord,
) -> RunReadItem:
    anchor = session.get(EvidenceAnchor, row.evidence_anchor_id)
    if anchor is None or anchor.project_id != project_id:
        return RunReadItem(row.task_id, "restricted", None, None)
    allowed = _allows(
        authorizer,
        Action.EVIDENCE_READ,
        ResourceRef(
            project_id=project_id,
            scope_type="evidence_anchor",
            evidence_anchor_id=anchor.id,
        ),
        principal,
    )
    if not allowed:
        return RunReadItem(row.task_id, "restricted", None, None)
    return RunReadItem(row.task_id, "evidence_anchor", anchor.locator_type, anchor.id)


def _candidate(row: Candidate) -> CandidateCard:
    document = _object(row.proposal_json)
    return CandidateCard(
        candidate_id=row.id,
        run_id=row.run_id,
        action=row.action,
        page_mode=row.page_mode,
        status=row.status,
        summary=_text(document.get("summary"), 500),
        title=_text(document.get("title"), 200),
        target_stage_id=_text(document.get("target_stage_id"), 64),
        page_id=_text(document.get("page_id"), 64),
        input_page_version_id=_text(document.get("input_page_version_id"), 64),
        edge_kind=_text(document.get("kind"), 32),
        gaps=_strings(document.get("gaps"), 12),
        preview_ready=_preview_ready(row.action, row.page_mode, document),
    )


def _preview_ready(action: str, page_mode: str | None, document: dict) -> bool:
    if not document:
        return False
    try:
        parse_proposal(action, page_mode, document)
    except (InvalidSchema, CandidateCommandError, TypeError, ValueError):
        return False
    return True


def _published(output_json: str | None) -> tuple[str | None, tuple[str, ...], str | None]:
    parsed = _object(output_json)
    gaps = list(_strings(parsed.get("gaps"), 12))
    model = parsed.get("model")
    summary = None
    if isinstance(model, dict):
        summary = _text(model.get("summary"), 500)
        for item in _strings(model.get("gaps"), 12):
            if item not in gaps and len(gaps) < 12:
                gaps.append(item)
    for key in ("evidence_gap", "method_gap"):
        text = _text(parsed.get(key), 200)
        if text and text not in gaps and len(gaps) < 12:
            gaps.append(text)
    return summary, tuple(gaps), _safe_error(_text(parsed.get("error"), 160))


def _require_project(
    authorizer: SessionAuthorizer,
    principal: Principal,
    project_id: str,
    action: Action,
) -> None:
    authorizer.authorize(
        action,
        ResourceRef(project_id=project_id, scope_type="project"),
        principal,
    )


def _allows(
    authorizer: SessionAuthorizer,
    action: Action,
    resource: ResourceRef,
    principal: Principal,
) -> bool:
    try:
        authorizer.authorize(action, resource, principal)
    except (NotFoundOrHidden, ActionForbidden, SourceRedacted):
        return False
    return True


def _object(raw: str | None) -> dict:
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    if isinstance(parsed, dict):
        return parsed
    return {}


def _text(value: object, limit: int) -> str | None:
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    if not stripped:
        return None
    return stripped[:limit]


def _strings(value: object, limit: int) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    found: list[str] = []
    for item in value:
        text = _text(item, 200)
        if text and text not in found:
            found.append(text)
        if len(found) >= limit:
            break
    return tuple(found)


def _safe_error(value: str | None) -> str | None:
    if not value or "sk-" in value.lower():
        return None
    return value[:160]


def _role_index(role: str) -> int:
    try:
        return _ROLE_ORDER.index(role)
    except ValueError:
        return len(_ROLE_ORDER)
