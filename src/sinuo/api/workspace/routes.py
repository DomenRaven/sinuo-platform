"""Read-only workspace routes. They do not slide the session or rotate CSRF."""

import time
from collections.abc import Iterator
from contextlib import contextmanager

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from sinuo.api.auth.dependencies import get_engine, get_settings, session_token
from sinuo.api.auth.routes import _success
from sinuo.application.auth.authorizer import SessionAuthorizer
from sinuo.application.auth.sessions import resolve_principal
from sinuo.application.versions.outline import (
    PageDraftView,
    ProjectStages,
    VisibleProject,
    list_visible_projects,
    read_page_draft,
    read_project_stages,
)
from sinuo.application.workspace.activity import (
    AgentRunView,
    ArtifactList,
    CandidateCard,
    list_candidates,
    list_visible_artifacts,
    read_agent_run,
)
from sinuo.application.workspace.context_scope import (
    ContextScope,
    list_context_scope,
)
from sinuo.domain.auth.principal import Principal
from sinuo.domain.page_nodes import EDGE_LABELS, NODE_LABELS, StoredNode

router = APIRouter(prefix="/api/v1")

_DUTY_LABELS = {
    "clarification": "课题澄清",
    "evidence": "证据查找",
    "method": "方法建议",
    "critic": "过程检查",
    "human": "由人确认",
}
_RECORD_LABELS = {
    "proposal": "待确认",
    "formal": "已确认",
}
_RUN_STATUS = {
    "queued": "等待开始",
    "running": "正在运行",
    "completed": "这次运行已结束",
    "partial": "这次运行有未完成的步骤",
    "failed": "这次运行没有完成",
    "cancelled": "已取消",
}
_TASK_STATUS = {
    "blocked": "还在等前一步",
    "queued": "等待",
    "running": "正在做",
    "succeeded": "已完成",
    "retry_wait": "等待重试",
    "failed": "没有完成",
    "cancelled": "已取消",
}
_CANDIDATE_STATUS = {
    "pending": "待确认",
    "committed": "已确认",
    "rejected": "已拒绝",
    "expired": "已过期",
}
_ACTIONS = {
    "add_node": "新增节点",
    "add_typed_edge": "新增关系",
    "attach_artifact_version": "挂上作品版本",
    "draft_decision": "草稿决定",
    "suggest_stage_transition": "建议进入阶段",
    "create_page_from_versions": "从版本建立新页",
}


@contextmanager
def _reader(request: Request) -> Iterator[tuple[Session, Principal]]:
    settings = get_settings(request)
    session = Session(get_engine(request))
    try:
        principal = resolve_principal(session, session_token(request, settings), now=time.time())
        yield session, principal
    finally:
        session.close()


@router.get("/projects")
def get_projects(request: Request) -> JSONResponse:
    with _reader(request) as (session, principal):
        projects = list_visible_projects(session, SessionAuthorizer(session), principal)
    return _success(request, {"projects": [_project(item) for item in projects]})


@router.get("/projects/{project_id}/stages")
def get_stages(project_id: str, request: Request) -> JSONResponse:
    with _reader(request) as (session, principal):
        reading = read_project_stages(
            session,
            SessionAuthorizer(session),
            principal,
            project_id,
        )
    return _success(request, _stages(reading))


@router.get("/pages/{page_id}/draft")
def get_page_draft(page_id: str, request: Request) -> JSONResponse:
    with _reader(request) as (session, principal):
        reading = read_page_draft(session, SessionAuthorizer(session), principal, page_id)
    return _success(request, _draft(reading))


@router.get("/projects/{project_id}/artifacts")
def get_artifacts(project_id: str, request: Request) -> JSONResponse:
    with _reader(request) as (session, principal):
        reading = list_visible_artifacts(
            session,
            SessionAuthorizer(session),
            principal,
            project_id,
        )
    return _success(request, _artifacts(reading))


@router.get("/projects/{project_id}/candidates")
def get_candidates(project_id: str, request: Request) -> JSONResponse:
    with _reader(request) as (session, principal):
        cards = list_candidates(session, SessionAuthorizer(session), principal, project_id)
    return _success(request, {"candidates": [_candidate(item) for item in cards]})


@router.get("/agent-runs/{run_id}")
def get_agent_run(run_id: str, request: Request) -> JSONResponse:
    with _reader(request) as (session, principal):
        reading = read_agent_run(session, SessionAuthorizer(session), principal, run_id)
    return _success(request, _run(reading))


@router.get("/projects/{project_id}/context-scope")
def get_context_scope(project_id: str, request: Request) -> JSONResponse:
    with _reader(request) as (session, principal):
        reading = list_context_scope(
            session,
            SessionAuthorizer(session),
            principal,
            project_id,
        )
    return _success(request, _scope(reading))


def _scope(reading: ContextScope) -> dict:
    return {
        "page_versions": [
            {
                "page_version_id": item.page_version_id,
                "page_id": item.page_id,
                "title": item.title,
                "version_no": item.version_no,
                "can_include": item.can_include,
            }
            for item in reading.page_versions
        ],
        "evidence_anchors": [
            {
                "evidence_anchor_id": item.evidence_anchor_id,
                "artifact_version_id": item.artifact_version_id,
                "locator_type": item.locator_type,
                "can_include": item.can_include,
            }
            for item in reading.evidence_anchors
        ],
        "artifact_versions": [
            {
                "artifact_version_id": item.artifact_version_id,
                "title": item.title,
                "version_no": item.version_no,
                "can_include": item.can_include,
                "ai_processing_allowed": item.ai_processing_allowed,
            }
            for item in reading.artifact_versions
        ],
        "method_cards": [
            {
                "version_id": item.version_id,
                "card_key": item.card_key,
                "name": item.name,
                "version_no": item.version_no,
                "source_title": item.source_title,
            }
            for item in reading.method_cards
        ],
        "has_hidden": reading.has_hidden,
    }


def _project(item: VisibleProject) -> dict:
    return {
        "project_id": item.project_id,
        "title": item.title,
        "revision": item.revision,
    }


def _stages(reading: ProjectStages) -> dict:
    return {
        "project_id": reading.project_id,
        "title": reading.title,
        "revision": reading.revision,
        "stages": [
            {
                "stage_id": stage.stage_id,
                "position": stage.position,
                "name": stage.name,
                "has_hidden_pages": stage.has_hidden_pages,
                "pages": [
                    {
                        "page_id": page.page_id,
                        "title": page.title,
                        "status": page.status,
                        "head_version_id": page.head_version_id,
                    }
                    for page in stage.pages
                ],
            }
            for stage in reading.stages
        ],
        "edges": [
            {
                "source_page_id": edge.source_page_id,
                "target_page_id": edge.target_page_id,
                "kind": edge.kind,
            }
            for edge in reading.edges
        ],
    }


def _draft(reading: PageDraftView) -> dict:
    draft = None
    if reading.base_version_id is not None:
        draft = {
            "base_version_id": reading.base_version_id,
            "content_revision": reading.content_revision,
            "content_hash": reading.content_hash,
        }
    return {
        "project_id": reading.project_id,
        "stage_id": reading.stage_id,
        "page_id": reading.page_id,
        "title": reading.title,
        "status": reading.status,
        "head_version_id": reading.head_version_id,
        "head_version_no": reading.head_version_no,
        "draft": draft,
        "nodes_readable": reading.nodes_readable,
        "nodes": [_node(node) for node in reading.nodes],
        "edges": [
            {
                "edge_id": edge.edge_id,
                "source_node_id": edge.source_node_id,
                "target_node_id": edge.target_node_id,
                "kind": edge.kind_or_default,
                "kind_label": EDGE_LABELS.get(edge.kind_or_default),
            }
            for edge in reading.edges
        ],
    }


def _node(node: StoredNode) -> dict:
    payload = {
        "node_id": node.node_id,
        "kind": node.kind,
        "label": NODE_LABELS.get(node.kind) if node.kind else None,
        "duty": node.duty,
        "duty_label": _DUTY_LABELS.get(node.duty) if node.duty else None,
        "record": node.record,
        "record_label": _RECORD_LABELS.get(node.record) if node.record else None,
    }
    if node.artifact_version_id:
        payload["artifact_version_id"] = node.artifact_version_id
    if node.content:
        payload["content"] = node.content
    return payload


def _artifacts(reading: ArtifactList) -> dict:
    return {
        "artifacts": [
            {
                "artifact_id": item.artifact_id,
                "title": item.title,
                "artifact_version_id": item.artifact_version_id,
                "version_no": item.version_no,
                "media_type": item.media_type,
            }
            for item in reading.artifacts
        ],
        "has_hidden_artifacts": reading.has_hidden_artifacts,
    }


def _candidate(item: CandidateCard) -> dict:
    return {
        "candidate_id": item.candidate_id,
        "run_id": item.run_id,
        "action": item.action,
        "action_label": _ACTIONS.get(item.action),
        "page_mode": item.page_mode,
        "status": item.status,
        "status_label": _CANDIDATE_STATUS.get(item.status),
        "summary": item.summary,
        "title": item.title,
        "target_stage_id": item.target_stage_id,
        "page_id": item.page_id,
        "input_page_version_id": item.input_page_version_id,
        "edge_kind": item.edge_kind,
        "edge_kind_label": EDGE_LABELS.get(item.edge_kind) if item.edge_kind else None,
        "gaps": list(item.gaps),
        "preview_ready": item.preview_ready,
    }


def _run(reading: AgentRunView) -> dict:
    return {
        "run_id": reading.run_id,
        "project_id": reading.project_id,
        "status": reading.status,
        "status_label": _RUN_STATUS.get(reading.status),
        "intent": reading.intent,
        "page_version_id": reading.page_version_id,
        "opening_gaps": list(reading.opening_gaps),
        "duties": [
            {
                "task_id": step.task_id,
                "role": step.role,
                "role_label": _DUTY_LABELS.get(step.role),
                "status": step.status,
                "status_label": _TASK_STATUS.get(step.status),
                "summary": step.summary,
                "gaps": list(step.gaps),
                "error": step.error,
                "reads": [
                    {
                        "target": item.target,
                        "label": item.label,
                        "object_id": item.object_id,
                    }
                    for item in step.reads
                ],
            }
            for step in reading.duties
        ],
        "candidates": [_candidate(item) for item in reading.candidates],
    }
