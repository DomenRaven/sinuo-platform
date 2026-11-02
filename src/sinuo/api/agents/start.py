"""POST one agent run.

``app.state.agent_provider`` overrides the model for tests. Otherwise an
enabled ``Settings`` opens DeepSeek from the environment, and a disabled
model uses ``DisabledModelProvider``. ``open_agent_run`` and
``finish_agent_run`` remain the manual run entry points.
"""

import asyncio
import json
import time
from typing import Literal

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator
from sqlalchemy.orm import Session

from sinuo.agents.contracts import InputSnapshot, ModelProvider
from sinuo.agents.deepseek import DeepSeekAdapter
from sinuo.agents.fake import FakeModelProvider
from sinuo.agents.provider import DisabledModelProvider
from sinuo.api.auth.dependencies import get_engine, get_settings
from sinuo.api.auth.routes import _idempotency_key, _success
from sinuo.api.auth.schemas import parse_json
from sinuo.api.versions.routes import _guard
from sinuo.application.agents.execute import (
    AgentRunRequest,
    RunPlanRejected,
    bind_theory_root,
    finish_agent_run,
    open_agent_run,
)
from sinuo.application.agents.intent import IntentRejected, plan_duties
from sinuo.application.auth.authorizer import SessionAuthorizer
from sinuo.application.idempotency import canonical_hash
from sinuo.application.versions.drafts import DraftConflict
from sinuo.domain.auth.errors import InvalidSchema
from sinuo.domain.auth.resource import ResourceRef
from sinuo.domain.errors import IdempotencyConflict
from sinuo.domain.public_contract import Action
from sinuo.infrastructure.database import write_uow
from sinuo.infrastructure.domain_models import PageDraft, PageVersion
from sinuo.infrastructure.models import IdempotencyRecord

_COMMAND = Action.RUN_CREATE.value
_EVENTS = "/api/v1/agent-runs/{run_id}/events"


class StartRunBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    intent: str = Field(min_length=1, max_length=200)
    input_mode: Literal["confirmed", "draft_preview", "historical_readonly"]
    page_version_id: str | None = None
    draft_content_revision: int | None = Field(default=None, ge=1)
    query: str = Field(default="", max_length=40)
    question: str = Field(default="", max_length=80)
    situation: str = Field(default="", max_length=80)
    constraints: dict[str, bool] = Field(default_factory=dict)
    exclusions: list[str] = Field(default_factory=list)
    page_version_ids: list[str] = Field(default_factory=list)
    artifact_version_ids: list[str] = Field(default_factory=list)

    @field_validator("intent", "query", "question", "situation")
    @classmethod
    def _strip_text(cls, value: str) -> str:
        return value.strip()

    @field_validator("exclusions")
    @classmethod
    def _exclusions(cls, value: list[str]) -> list[str]:
        if len(value) > 4:
            raise ValueError("exclusions")
        allowed = {"theory", "methods"}
        cleaned: list[str] = []
        for item in value:
            if item not in allowed:
                raise ValueError("exclusions")
            if item not in cleaned:
                cleaned.append(item)
        return cleaned

    @field_validator("page_version_ids", "artifact_version_ids")
    @classmethod
    def _version_ids(cls, value: list[str]) -> list[str]:
        if len(value) > 8:
            raise ValueError("version_ids")
        cleaned: list[str] = []
        for item in value:
            if (
                not isinstance(item, str)
                or item.strip() == ""
                or item != item.strip()
                or len(item) > 64
            ):
                raise ValueError("version_ids")
            if item not in cleaned:
                cleaned.append(item)
        return cleaned

    @field_validator("page_version_id")
    @classmethod
    def _page_version(cls, value: str | None) -> str | None:
        if value is None:
            return None
        stripped = value.strip()
        if stripped == "" or len(stripped) > 64:
            raise ValueError("page_version_id")
        return stripped

    @field_validator("intent")
    @classmethod
    def _intent_present(cls, value: str) -> str:
        if value == "" or len(value) > 200:
            raise ValueError("intent")
        return value


def start_router(provider: ModelProvider | None = None) -> APIRouter:
    """Routes that finish one run.

    A passed provider is fixed for that router. The mounted app router reads
    ``app.state.agent_provider`` first, then the process settings.
    """

    router = APIRouter(prefix="/api/v1")

    @router.post("/projects/{project_id}/agent-runs")
    async def post_agent_run(project_id: str, request: Request) -> JSONResponse:
        body = _parse_start(await request.body())
        digest = canonical_hash(body.model_dump(mode="json"))
        now = time.time()
        engine = get_engine(request)
        with write_uow(engine) as session:
            principal = _guard(request, session, now)
            request_key = _idempotency_key(request)
            SessionAuthorizer(session).authorize(
                Action.RUN_CREATE,
                ResourceRef(project_id=project_id, scope_type="project"),
                principal,
            )
            record_key = (project_id, principal.account_id, _COMMAND, request_key)
            existing = session.get(IdempotencyRecord, record_key)
            if existing is not None:
                if existing.request_hash != digest:
                    raise IdempotencyConflict(_COMMAND)
                stored = existing.response_json
                status = existing.response_status
                replayed = True
            else:
                _check_draft(session, project_id, body)
                stored = None
                status = 202
                replayed = False
        if replayed:
            if not isinstance(stored, dict):
                raise InvalidSchema()
            return _success(request, stored, status=status, replayed=True)
        try:
            plan = plan_duties(body.intent, project_id=project_id, now=now)
            opened = open_agent_run(
                engine,
                principal,
                project_id,
                _run_request(body, plan.tasks, plan.roles, plan.gaps, now),
            )
        except (IntentRejected, RunPlanRejected, ValueError) as exc:
            raise InvalidSchema() from exc
        chosen, owns_client = _select_provider(request, provider)
        try:
            with bind_theory_root(get_settings(request).theory_root):
                await asyncio.to_thread(
                    finish_agent_run,
                    engine,
                    principal,
                    opened.run_id,
                    _bind_provider(chosen),
                    now=now,
                )
        finally:
            if owns_client:
                try:
                    await chosen.client.aclose()
                except RuntimeError as exc:
                    if "Event loop is closed" not in str(exc):
                        raise
        data = {
            "run_id": opened.run_id,
            "snapshot_id": opened.snapshot_id,
            "events_url": _EVENTS.format(run_id=opened.run_id),
        }
        with write_uow(engine) as session:
            existing = session.get(IdempotencyRecord, record_key)
            if existing is None:
                session.add(
                    IdempotencyRecord(
                        project_id=project_id,
                        actor_id=principal.account_id,
                        command=_COMMAND,
                        request_key=request_key,
                        request_hash=digest,
                        response_status=202,
                        response_json=data,
                        created_at=now,
                    )
                )
            elif existing.request_hash != digest:
                raise IdempotencyConflict(_COMMAND)
        return _success(request, data, status=202, replayed=False)

    return router


class _FakeEcho:
    """Let FakeModelProvider echo one selected role.

    ``finish_agent_run`` stores tool results, not an ``outputs`` map. The fake
    provider only speaks a role that is already in that map. Unselected roles
    never reach this call.
    """

    def __init__(self, inner: FakeModelProvider) -> None:
        self.inner = inner

    async def execute(self, task, snapshot, upstream):
        document = json.loads(snapshot.context_json)
        if not isinstance(document, dict):
            document = {}
        outputs = document.get("outputs")
        if not isinstance(outputs, dict):
            outputs = {}
        if task.role not in outputs:
            filled = dict(outputs)
            filled[task.role] = {"gaps": [], "summary": "待确认"}
            document = dict(document)
            document["outputs"] = filled
            snapshot = InputSnapshot(
                snapshot.snapshot_id,
                snapshot.project_id,
                snapshot.input_hash,
                json.dumps(document, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
            )
        return await self.inner.execute(task, snapshot, upstream)


def _select_provider(
    request: Request, fixed: ModelProvider | None
) -> tuple[ModelProvider, bool]:
    if fixed is not None:
        return fixed, False
    override = getattr(request.app.state, "agent_provider", None)
    if override is not None:
        return override, False
    settings = get_settings(request)
    if settings.model_enabled is True:
        return DeepSeekAdapter.from_settings(settings), True
    return DisabledModelProvider(), False


def _bind_provider(provider: ModelProvider) -> ModelProvider:
    if isinstance(provider, FakeModelProvider):
        return _FakeEcho(provider)
    return provider


def _parse_start(raw: bytes) -> StartRunBody:
    payload = parse_json(raw)
    if not isinstance(payload, dict):
        raise InvalidSchema()
    try:
        return StartRunBody.model_validate(payload)
    except ValidationError as exc:
        raise InvalidSchema() from exc


def _check_draft(session: Session, project_id: str, body: StartRunBody) -> None:
    if body.draft_content_revision is None:
        return
    if body.page_version_id is None:
        raise InvalidSchema()
    version = session.get(PageVersion, body.page_version_id)
    if version is None or version.project_id != project_id:
        raise InvalidSchema()
    draft = session.get(PageDraft, version.page_id)
    if (
        draft is None
        or draft.project_id != project_id
        or draft.content_revision != body.draft_content_revision
    ):
        raise DraftConflict()


def _run_request(
    body: StartRunBody,
    tasks: tuple,
    roles: tuple[str, ...],
    gaps: tuple[str, ...],
    now: float,
) -> AgentRunRequest:
    query = body.query
    question = body.question
    if "evidence" in roles and query == "":
        query = body.intent
    if "method" in roles and question == "":
        question = body.intent
    return AgentRunRequest(
        intent=body.intent,
        tasks=tasks,
        query=query,
        question=question,
        situation=body.situation,
        constraints=body.constraints,
        page_version_id=body.page_version_id,
        now=now,
        opening_gaps=gaps,
        exclusions=tuple(body.exclusions),
        page_version_ids=tuple(body.page_version_ids),
        artifact_version_ids=tuple(body.artifact_version_ids),
    )
