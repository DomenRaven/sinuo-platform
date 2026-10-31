"""Read and save the signed-in actor's suggestion preference.

GET does not insert a row. PATCH authorizes ``run.create`` again on replay.
The saved body is the preference itself, so it is not passed through the
page-receipt projection. A preference revision conflict is not a design head
change, and the success meta does not include ``project_revision``.
"""

import json
import time
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy.orm import Session

from sinuo import __version__
from sinuo.api.auth.dependencies import get_engine, get_settings, session_token
from sinuo.api.auth.errors import request_id_for
from sinuo.api.auth.routes import _idempotency_key, _nostore, _success
from sinuo.api.auth.schemas import parse_json
from sinuo.application.auth.authorizer import SessionAuthorizer
from sinuo.application.auth.csrf import origin_allowed
from sinuo.application.auth.sessions import require_csrf, resolve_principal, slide_session
from sinuo.application.idempotency import CommandResponse, CommandScope, execute_idempotent
from sinuo.domain.auth.errors import ActionForbidden, InvalidSchema
from sinuo.domain.auth.resource import ResourceRef
from sinuo.domain.public_contract import CSRF_HEADER, Action
from sinuo.infrastructure.database import write_uow
from sinuo.infrastructure.models import IdempotencyRecord
from sinuo.workers.suggestions import (
    PreferenceView,
    read_suggestion_preference,
    save_suggestion_preference,
)

router = APIRouter(prefix="/api/v1")
_PATH = "/projects/{project_id}/suggestion-preferences/me"


class PreferencePatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mode: str
    scope: dict[str, Any]
    cooldown_seconds: int = Field(ge=0, strict=True)
    digest_interval_seconds: int = Field(ge=1, strict=True)
    expected_revision: int = Field(ge=1, strict=True)


def _parse_patch(payload: object) -> PreferencePatch:
    if not isinstance(payload, dict):
        raise InvalidSchema()
    try:
        return PreferencePatch.model_validate(payload)
    except ValidationError as error:
        raise InvalidSchema() from error


def _data(view: PreferenceView) -> dict[str, Any]:
    scope = json.loads(view.scope_json)
    if not isinstance(scope, dict):
        raise InvalidSchema()
    return {
        "project_id": view.project_id,
        "mode": view.mode,
        "scope": scope,
        "cooldown_seconds": view.cooldown_seconds,
        "digest_interval_seconds": view.digest_interval_seconds,
        "revision": view.revision,
        "next_due_at": view.next_due_at,
        "stored": view.stored,
    }


def _authorize(session: Session, principal, project_id: str, action: Action) -> None:
    SessionAuthorizer(session).authorize(
        action,
        ResourceRef(project_id=project_id, scope_type="project"),
        principal,
    )


@router.get(_PATH)
def get_my_suggestion_preference(project_id: str, request: Request) -> JSONResponse:
    settings = get_settings(request)
    with Session(get_engine(request)) as session:
        principal = resolve_principal(session, session_token(request, settings), now=time.time())
        _authorize(session, principal, project_id, Action.PROJECT_READ)
        view = read_suggestion_preference(
            session,
            project_id=project_id,
            actor_id=principal.account_id,
        )
        payload = _data(view)
    body = {
        "data": payload,
        "meta": {"request_id": request_id_for(request), "version": __version__},
    }
    return _nostore(JSONResponse(body))


@router.patch(_PATH)
async def patch_my_suggestion_preference(project_id: str, request: Request) -> JSONResponse:
    body = _parse_patch(parse_json(await request.body()))
    settings = get_settings(request)
    now = time.time()
    with write_uow(get_engine(request)) as session:
        principal = resolve_principal(session, session_token(request, settings), now=now)
        if not origin_allowed(request.headers.get("origin"), settings.parsed_allowed_origins()):
            raise ActionForbidden(category="origin")
        require_csrf(session, principal, request.headers.get(CSRF_HEADER))
        slide_session(session, principal, settings, now)
        scope = CommandScope(
            project_id=project_id,
            actor_id=principal.account_id,
            command="suggestion.preference.save",
            request_key=_idempotency_key(request),
        )
        key = (scope.project_id, scope.actor_id, scope.command, scope.request_key)
        replayed = session.get(IdempotencyRecord, key) is not None

        def authorize(active: Session) -> None:
            _authorize(active, principal, project_id, Action.RUN_CREATE)

        def handler(active: Session) -> CommandResponse:
            try:
                saved = save_suggestion_preference(
                    active,
                    project_id=project_id,
                    actor_id=principal.account_id,
                    expected_revision=body.expected_revision,
                    mode=body.mode,
                    scope=body.scope,
                    cooldown_seconds=body.cooldown_seconds,
                    digest_interval_seconds=body.digest_interval_seconds,
                    now=now,
                )
            except ValueError as error:
                raise InvalidSchema() from error
            return CommandResponse(
                200,
                _data(
                    PreferenceView(
                        project_id=saved.project_id,
                        actor_id=saved.actor_id,
                        mode=saved.mode,
                        scope_json=saved.scope_json,
                        cooldown_seconds=saved.cooldown_seconds,
                        digest_interval_seconds=saved.digest_interval_seconds,
                        revision=saved.revision,
                        next_due_at=saved.next_due_at,
                        updated_at=saved.updated_at,
                        stored=True,
                    )
                ),
            )

        response = execute_idempotent(
            session,
            scope,
            body.model_dump(mode="json"),
            authorize,
            handler,
        )
    return _success(request, response.body, status=response.status, replayed=replayed)
