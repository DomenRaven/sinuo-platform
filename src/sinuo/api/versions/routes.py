"""HTTP adapters for version commands. Domain functions do not commit."""

import time

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from sinuo import __version__
from sinuo.api.auth.dependencies import get_engine, get_settings, session_token
from sinuo.api.auth.errors import request_id_for
from sinuo.api.auth.routes import _idempotency_key, _nostore, _success
from sinuo.api.versions.schemas import (
    parse_branch,
    parse_checkpoint,
    parse_draft,
    parse_parallel_page,
    parse_project,
)
from sinuo.application.auth.authorizer import SessionAuthorizer
from sinuo.application.auth.csrf import origin_allowed
from sinuo.application.auth.receipts import execute_authorized_command
from sinuo.application.auth.sessions import require_csrf, resolve_principal, slide_session
from sinuo.application.idempotency import CommandResponse, CommandScope
from sinuo.application.versions.branches import branch_from_version
from sinuo.application.versions.checkpoints import checkpoint_page
from sinuo.application.versions.drafts import save_draft
from sinuo.application.versions.history import read_history
from sinuo.application.versions.pages import create_parallel_page
from sinuo.application.versions.projects import create_project
from sinuo.domain.auth.errors import ActionForbidden, NotFoundOrHidden
from sinuo.domain.auth.resource import ResourceRef, account_resource
from sinuo.domain.public_contract import CSRF_HEADER, Action
from sinuo.domain.schema_types import canonical_document, document_hash
from sinuo.infrastructure.database import write_uow
from sinuo.infrastructure.domain_models import Page

router = APIRouter(prefix="/api/v1")


def _guard(request: Request, session: Session, now: float):
    settings = get_settings(request)
    principal = resolve_principal(session, session_token(request, settings), now=now)
    if not origin_allowed(request.headers.get("origin"), settings.parsed_allowed_origins()):
        raise ActionForbidden(category="origin")
    require_csrf(session, principal, request.headers.get(CSRF_HEADER))
    slide_session(session, principal, settings, now)
    return principal


@router.post("/projects", status_code=201)
async def post_project(request: Request) -> JSONResponse:
    raw = await request.body()
    body = parse_project(raw)
    now = time.time()
    with write_uow(get_engine(request)) as session:
        principal = _guard(request, session, now)
        authorizer = SessionAuthorizer(session)
        scope = CommandScope(
            project_id=f"account:{principal.account_id}",
            actor_id=principal.account_id,
            command=Action.PROJECT_CREATE.value,
            request_key=_idempotency_key(request),
        )

        def handler(active) -> CommandResponse:
            created = create_project(
                active,
                account_id=principal.account_id,
                title=body.title,
                brief=body.brief,
                template_key=body.template_key,
                page_title=body.page_title,
                now=now,
            )
            return CommandResponse(
                201,
                {
                    "project_id": created.project_id,
                    "stage_id": created.stage_id,
                    "page_id": created.page_id,
                    "page_version_id": created.page_version_id,
                    "project_revision": created.project_revision,
                    "audit_event_id": created.audit_event_id,
                },
            )

        response, replayed = execute_authorized_command(
            session,
            scope,
            body.model_dump(mode="json"),
            authorizer,
            Action.PROJECT_CREATE,
            account_resource(),
            principal,
            handler,
        )
    return _success(request, response.body, status=response.status, replayed=replayed)


@router.post("/projects/{project_id}/pages", status_code=201)
async def post_page(project_id: str, request: Request) -> JSONResponse:
    body = parse_parallel_page(await request.body())
    now = time.time()
    with write_uow(get_engine(request)) as session:
        principal = _guard(request, session, now)
        authorizer = SessionAuthorizer(session)
        resource = ResourceRef(
            project_id=project_id,
            scope_type="stage",
            stage_id=body.stage_id,
        )
        scope = CommandScope(
            project_id=project_id,
            actor_id=principal.account_id,
            command=Action.PAGE_CREATE.value,
            request_key=_idempotency_key(request),
        )

        def handler(active) -> CommandResponse:
            created = create_parallel_page(
                active,
                project_id=project_id,
                stage_id=body.stage_id,
                account_id=principal.account_id,
                title=body.title,
                expected_revision=body.expected_revision,
                now=now,
            )
            return CommandResponse(
                201,
                {
                    "page_id": created.page_id,
                    "page_version_id": created.page_version_id,
                    "project_revision": created.project_revision,
                    "audit_event_id": created.audit_event_id,
                },
            )

        response, replayed = execute_authorized_command(
            session,
            scope,
            body.model_dump(mode="json"),
            authorizer,
            Action.PAGE_CREATE,
            resource,
            principal,
            handler,
        )
    return _success(request, response.body, status=response.status, replayed=replayed)


@router.patch("/pages/{page_id}/draft")
async def patch_draft(page_id: str, request: Request) -> JSONResponse:
    body = parse_draft(await request.body())
    content = canonical_document(body.content)
    content_hash = document_hash(body.content)
    now = time.time()
    with write_uow(get_engine(request)) as session:
        principal = _guard(request, session, now)
        page = session.get(Page, page_id)
        if page is None:
            raise NotFoundOrHidden()
        authorizer = SessionAuthorizer(session)
        resource = ResourceRef(
            project_id=page.project_id,
            scope_type="page",
            page_id=page_id,
        )
        scope = CommandScope(
            project_id=page.project_id,
            actor_id=principal.account_id,
            command=Action.PAGE_DRAFT_WRITE.value,
            request_key=_idempotency_key(request),
        )

        def handler(active) -> CommandResponse:
            revision, audit_event_id = save_draft(
                active,
                page_id=page_id,
                project_id=page.project_id,
                base_version_id=body.base_version_id,
                expected_revision=body.expected_revision,
                content_json=content,
                content_hash=content_hash,
                actor_id=principal.account_id,
                now=now,
            )
            return CommandResponse(
                200,
                {
                    "content_revision": revision,
                    "content_hash": content_hash,
                    "audit_event_id": audit_event_id,
                    "page_id": page_id,
                },
            )

        response, replayed = execute_authorized_command(
            session,
            scope,
            body.model_dump(mode="json"),
            authorizer,
            Action.PAGE_DRAFT_WRITE,
            resource,
            principal,
            handler,
        )
    return _success(request, response.body, status=response.status, replayed=replayed)


@router.post("/pages/{page_id}/checkpoints", status_code=201)
async def post_checkpoint(page_id: str, request: Request) -> JSONResponse:
    body = parse_checkpoint(await request.body())
    now = time.time()
    with write_uow(get_engine(request)) as session:
        principal = _guard(request, session, now)
        page = session.get(Page, page_id)
        if page is None:
            raise NotFoundOrHidden()
        authorizer = SessionAuthorizer(session)
        resource = ResourceRef(
            project_id=page.project_id,
            scope_type="page",
            page_id=page_id,
        )
        scope = CommandScope(
            project_id=page.project_id,
            actor_id=principal.account_id,
            command=Action.PAGE_CHECKPOINT.value,
            request_key=_idempotency_key(request),
        )

        def handler(active) -> CommandResponse:
            saved = checkpoint_page(
                active,
                project_id=page.project_id,
                page_id=page_id,
                expected_head_version_id=body.expected_head_version_id,
                expected_content_revision=body.expected_content_revision,
                expected_content_hash=body.expected_content_hash,
                actor_id=principal.account_id,
                now=now,
            )
            return CommandResponse(
                201,
                {
                    "page_version_id": saved.page_version_id,
                    "version_no": saved.version_no,
                    "project_revision": saved.project_revision,
                    "audit_event_id": saved.audit_event_id,
                },
            )

        response, replayed = execute_authorized_command(
            session,
            scope,
            body.model_dump(mode="json"),
            authorizer,
            Action.PAGE_CHECKPOINT,
            resource,
            principal,
            handler,
        )
    return _success(request, response.body, status=response.status, replayed=replayed)


@router.post("/pages/{page_id}/branches", status_code=201)
async def post_branch(page_id: str, request: Request) -> JSONResponse:
    body = parse_branch(await request.body())
    now = time.time()
    with write_uow(get_engine(request)) as session:
        principal = _guard(request, session, now)
        page = session.get(Page, page_id)
        if page is None:
            raise NotFoundOrHidden()
        authorizer = SessionAuthorizer(session)
        resource = ResourceRef(
            project_id=page.project_id,
            scope_type="page",
            page_id=page_id,
        )
        scope = CommandScope(
            project_id=page.project_id,
            actor_id=principal.account_id,
            command=Action.PAGE_BRANCH.value,
            request_key=_idempotency_key(request),
        )

        def handler(active) -> CommandResponse:
            opened = branch_from_version(
                active,
                project_id=page.project_id,
                source_version_id=body.source_version_id,
                actor_id=principal.account_id,
                title=body.title,
                now=now,
            )
            return CommandResponse(
                201,
                {
                    "page_id": opened.page_id,
                    "page_version_id": opened.page_version_id,
                    "source_version_id": opened.source_version_id,
                },
            )

        response, replayed = execute_authorized_command(
            session,
            scope,
            body.model_dump(mode="json"),
            authorizer,
            Action.PAGE_BRANCH,
            resource,
            principal,
            handler,
        )
    return _success(request, response.body, status=response.status, replayed=replayed)


@router.get("/pages/{page_id}/history")
def get_history(page_id: str, request: Request) -> JSONResponse:
    settings = get_settings(request)
    with Session(get_engine(request)) as session:
        principal = resolve_principal(session, session_token(request, settings), now=time.time())
        page = session.get(Page, page_id)
        if page is None:
            raise NotFoundOrHidden()
        SessionAuthorizer(session).authorize(
            Action.PAGE_READ,
            ResourceRef(project_id=page.project_id, scope_type="page", page_id=page_id),
            principal,
        )
        versions = read_history(session, project_id=page.project_id, page_id=page_id)
    payload = {
        "versions": [
            {
                "page_version_id": item.page_version_id,
                "version_no": item.version_no,
                "content_hash": item.content_hash,
            }
            for item in versions
        ]
    }
    body = {
        "data": payload,
        "meta": {"request_id": request_id_for(request), "version": __version__},
    }
    return _nostore(JSONResponse(body))
