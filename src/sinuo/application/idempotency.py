import hashlib
import json
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from sqlalchemy.orm import Session

from sinuo.domain.errors import IdempotencyConflict
from sinuo.infrastructure.models import IdempotencyRecord


@dataclass(frozen=True)
class CommandScope:
    project_id: str
    actor_id: str
    command: str
    request_key: str


@dataclass(frozen=True)
class CommandResponse:
    status: int
    body: dict[str, Any]


def canonical_hash(payload: dict[str, Any]) -> str:
    encoded = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def execute_idempotent(
    session: Session,
    scope: CommandScope,
    request: dict[str, Any],
    authorize: Callable[[Session], None],
    handler: Callable[[Session], CommandResponse],
) -> CommandResponse:
    """Caller owns write_uow. Authorization is rechecked even when replaying a result."""
    authorize(session)
    key = (scope.project_id, scope.actor_id, scope.command, scope.request_key)
    digest = canonical_hash(request)
    existing = session.get(IdempotencyRecord, key)
    if existing is not None:
        if existing.request_hash != digest:
            raise IdempotencyConflict(scope.command)
        return CommandResponse(existing.response_status, existing.response_json)
    response = handler(session)
    session.add(
        IdempotencyRecord(
            project_id=scope.project_id,
            actor_id=scope.actor_id,
            command=scope.command,
            request_key=scope.request_key,
            request_hash=digest,
            response_status=response.status,
            response_json=response.body,
            created_at=time.time(),
        )
    )
    session.flush()
    return response
