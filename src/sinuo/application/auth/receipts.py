"""Replay an authorized command and project the saved body with current rights."""

from collections.abc import Callable

from sqlalchemy.orm import Session

from sinuo.application.auth.authorizer import SessionAuthorizer
from sinuo.application.idempotency import CommandResponse, CommandScope, execute_idempotent
from sinuo.domain.auth.errors import AuthRequired
from sinuo.domain.auth.principal import Principal
from sinuo.domain.auth.projection import project_receipt
from sinuo.domain.auth.resource import ResourceRef
from sinuo.domain.public_contract import Action
from sinuo.infrastructure.models import IdempotencyRecord


def execute_authorized_command(
    session: Session,
    scope: CommandScope,
    request: dict,
    authorizer: SessionAuthorizer,
    action: Action,
    resource: ResourceRef,
    principal: Principal,
    handler: Callable[[Session], CommandResponse],
) -> tuple[CommandResponse, bool]:
    """Authorize before a stored body is returned. Do not rewrite the stored body."""

    if scope.actor_id != principal.account_id:
        raise AuthRequired()
    key = (scope.project_id, scope.actor_id, scope.command, scope.request_key)
    replayed = session.get(IdempotencyRecord, key) is not None

    def authorize(_session: Session) -> None:
        authorizer.authorize(action, resource, principal)

    response = execute_idempotent(session, scope, request, authorize, handler)
    return CommandResponse(response.status, project_receipt(response.body)), replayed
