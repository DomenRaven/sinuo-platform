"""Identity routes. Page editing, uploads, and model calls are out of scope."""

import time

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response
from sqlalchemy import select

from sinuo import __version__
from sinuo.api.auth.dependencies import client_host, get_engine, get_settings, session_token
from sinuo.api.auth.errors import request_id_for
from sinuo.api.auth.schemas import parse_access, parse_json, parse_login
from sinuo.application.auth.acl import commit_acl
from sinuo.application.auth.authorizer import SessionAuthorizer
from sinuo.application.auth.csrf import origin_allowed
from sinuo.application.auth.receipts import execute_authorized_command
from sinuo.application.auth.sessions import (
    LogoutStatus,
    authenticate_login,
    logout_session,
    require_csrf,
    resolve_principal,
    rotate_csrf,
    slide_session,
)
from sinuo.application.idempotency import CommandResponse, CommandScope
from sinuo.domain.auth.errors import ActionForbidden, InvalidSchema
from sinuo.domain.auth.resource import ResourceRef
from sinuo.domain.public_contract import CSRF_HEADER, Action
from sinuo.infrastructure.auth.cookies import attach_session_cookie, clear_session_cookie
from sinuo.infrastructure.database import write_uow
from sinuo.infrastructure.domain_models import Account

router = APIRouter(prefix="/api/v1")


def _nostore(response: Response) -> Response:
    response.headers["Cache-Control"] = "no-store"
    return response


def _success(
    request: Request,
    data: dict,
    *,
    status: int = 200,
    replayed: bool | None = None,
    project_revision: int | None = None,
    audit_event_id: str | None = None,
) -> JSONResponse:
    meta: dict = {"request_id": request_id_for(request), "version": __version__}
    if project_revision is not None:
        meta["project_revision"] = project_revision
    if audit_event_id is not None:
        meta["audit_event_id"] = audit_event_id
    if replayed is not None:
        meta["idempotency_replayed"] = replayed
    return _nostore(JSONResponse({"data": data, "meta": meta}, status_code=status))


def _idempotency_key(request: Request) -> str:
    key = request.headers.get("idempotency-key", "")
    if not key or len(key) > 128 or any(char.isspace() for char in key):
        raise InvalidSchema()
    return key


@router.post("/session", status_code=204)
async def create_session(request: Request) -> Response:
    settings = get_settings(request)
    if not origin_allowed(request.headers.get("origin"), settings.parsed_allowed_origins()):
        raise ActionForbidden(category="origin")
    body = parse_login(parse_json(await request.body()))
    now = time.time()
    result = authenticate_login(
        get_engine(request),
        settings,
        login_name=body.login_name,
        password=body.password,
        origin=request.headers.get("origin"),
        source_host=client_host(request),
        now=now,
    )
    response = Response(status_code=204)
    attach_session_cookie(response, settings, result.token, result.absolute_expires_at, now)
    return _nostore(response)


@router.get("/session")
def read_session(request: Request) -> JSONResponse:
    settings = get_settings(request)
    now = time.time()
    with write_uow(get_engine(request)) as session:
        principal = resolve_principal(session, session_token(request, settings), now=now)
        slide_session(session, principal, settings, now)
        csrf_token = rotate_csrf(session, principal)
        display_name = session.scalar(
            select(Account.display_name).where(Account.id == principal.account_id)
        )
    return _success(
        request,
        {
            "account_id": principal.account_id,
            "display_name": display_name,
            "csrf_token": csrf_token,
        },
    )


@router.delete("/session", status_code=204)
def delete_session(request: Request) -> Response:
    settings = get_settings(request)
    token = session_token(request, settings)
    now = time.time()
    with write_uow(get_engine(request)) as session:
        status = logout_session(
            session,
            token=token,
            origin=request.headers.get("origin"),
            csrf_token=request.headers.get(CSRF_HEADER),
            allowed_origins=settings.parsed_allowed_origins(),
            now=now,
        )
    response = Response(status_code=204)
    if token or status is LogoutStatus.REVOKED:
        clear_session_cookie(response, settings)
    return _nostore(response)


@router.patch("/projects/{project_id}/access")
async def patch_access(project_id: str, request: Request) -> JSONResponse:
    settings = get_settings(request)
    raw = await request.body()
    now = time.time()
    with write_uow(get_engine(request)) as session:
        principal = resolve_principal(session, session_token(request, settings), now=now)
        if not origin_allowed(request.headers.get("origin"), settings.parsed_allowed_origins()):
            raise ActionForbidden(category="origin")
        require_csrf(session, principal, request.headers.get(CSRF_HEADER))
        slide_session(session, principal, settings, now)
        authorizer = SessionAuthorizer(session)
        resource = ResourceRef(project_id=project_id, scope_type="project")
        authorizer.authorize(Action.ACL_MANAGE, resource, principal)
        body = parse_access(parse_json(raw))
        request_key = _idempotency_key(request)
        request_body = body.model_dump(mode="json")
        scope = CommandScope(
            project_id=project_id,
            actor_id=principal.account_id,
            command=Action.ACL_MANAGE.value,
            request_key=request_key,
        )

        def handler(active_session) -> CommandResponse:
            result = commit_acl(
                active_session,
                principal=principal,
                project_id=project_id,
                expected_access_epoch=body.expected_access_epoch,
                subject_account_id=body.subject_account_id,
                role_template=body.role_template,
                page_ids=tuple(body.page_ids),
                membership_status=body.membership_status,
                revoke_grant_ids=tuple(body.revoke_grant_ids),
                now=now,
            )
            return CommandResponse(
                200,
                {
                    "access_epoch": result.access_epoch,
                    "grant_ids": list(result.grant_ids),
                    "audit_event_id": result.audit_event_id,
                    "project_revision": result.project_revision,
                },
            )

        response, replayed = execute_authorized_command(
            session,
            scope,
            request_body,
            authorizer,
            Action.ACL_MANAGE,
            resource,
            principal,
            handler,
        )
    return _success(
        request,
        {
            "access_epoch": response.body.get("access_epoch"),
            "grant_ids": response.body.get("grant_ids", []),
        },
        status=response.status,
        replayed=replayed,
        project_revision=response.body.get("project_revision"),
        audit_event_id=response.body.get("audit_event_id"),
    )
