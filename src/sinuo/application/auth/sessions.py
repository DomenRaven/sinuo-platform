"""Session issue, resolution, and login. Argon2 runs outside write_uow."""

import hmac
import secrets
import time
import uuid
from dataclasses import dataclass
from enum import Enum

from sqlalchemy import select, update
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from sinuo.application.auth.csrf import (
    digest_secret,
    new_csrf_token,
    origin_allowed,
    secret_matches,
)
from sinuo.application.auth.passwords import (
    canonical_login_name,
    hash_password,
    needs_rehash,
    verify_password,
)
from sinuo.config import Settings
from sinuo.domain.auth.errors import ActionForbidden, AuthRequired
from sinuo.domain.auth.principal import Principal
from sinuo.domain.schema_types import ClosedSetError
from sinuo.infrastructure.auth.throttle import (
    bucket_key,
    clear_bucket,
    inspect_buckets,
    record_failure,
)
from sinuo.infrastructure.database import write_uow
from sinuo.infrastructure.domain_models import Account
from sinuo.infrastructure.domain_models import Session as AccountSession


class RateLimited(Exception):
    """Both the account bucket and the source bucket must be under the limit."""

    def __init__(self, retry_after: int) -> None:
        self.retry_after = max(1, int(retry_after))
        super().__init__("rate limited")


class LogoutStatus(Enum):
    ABSENT = "absent"
    REVOKED = "revoked"


@dataclass(frozen=True)
class LoginResult:
    token: str
    principal: Principal
    absolute_expires_at: float


def _clock(now: float | None) -> float:
    return time.time() if now is None else now


def _account_material(login_name: str) -> str:
    try:
        return canonical_login_name(login_name)
    except (ClosedSetError, TypeError):
        raw = login_name.encode("utf-8")[:1024] if isinstance(login_name, str) else b""
        return "invalid:" + digest_secret(raw.decode("utf-8", "ignore"))


def _throttle_keys(login_name: str, source_host: str) -> tuple[str, str]:
    host = source_host if source_host else "unknown"
    return (bucket_key("acct", _account_material(login_name)), bucket_key("src", host))


def _load_login_hash(
    engine: Engine, login_name: str
) -> tuple[str | None, str | None, int | None]:
    try:
        normalized = canonical_login_name(login_name)
    except (ClosedSetError, TypeError):
        return None, None, None
    with Session(engine) as session:
        account = session.scalar(select(Account).where(Account.login_name == normalized))
        if account is None:
            return None, None, None
        return account.password_hash, account.id, account.auth_epoch


def issue_session(
    session: Session,
    account: Account,
    settings: Settings,
    now: float,
) -> LoginResult:
    """Insert one new session. Other sessions for the account stay active."""

    if len(account.id) != 36:
        raise RuntimeError("account id must be 36 characters")
    token = secrets.token_urlsafe(32)
    absolute = now + settings.session_absolute_seconds
    idle = min(now + settings.session_idle_seconds, absolute)
    row = AccountSession(
        id=str(uuid.uuid4()),
        account_id=account.id,
        token_sha256=digest_secret(token),
        csrf_sha256=None,
        auth_epoch=account.auth_epoch,
        created_at=now,
        last_active_at=now,
        idle_expires_at=idle,
        absolute_expires_at=absolute,
        revoked_at=None,
    )
    session.add(row)
    session.flush()
    principal = Principal(
        account_id=account.id,
        session_id=row.id,
        auth_epoch=account.auth_epoch,
    )
    return LoginResult(token=token, principal=principal, absolute_expires_at=absolute)


def authenticate_login(
    engine: Engine,
    settings: Settings,
    *,
    login_name: str,
    password: str,
    origin: str | None,
    source_host: str,
    now: float | None = None,
) -> LoginResult:
    """Same 401 for an unknown name, a bad password, and a disabled account."""

    current = _clock(now)
    if not origin_allowed(origin, settings.parsed_allowed_origins()):
        raise ActionForbidden(category="origin")
    keys = _throttle_keys(login_name, source_host)
    with write_uow(engine) as session:
        view = inspect_buckets(
            session,
            keys,
            now=current,
            window_seconds=settings.login_rate_limit_window_seconds,
            limit=settings.login_rate_limit_attempts,
        )
        if view.limited:
            raise RateLimited(view.retry_after)
        record_failure(
            session,
            keys,
            now=current,
            window_seconds=settings.login_rate_limit_window_seconds,
        )
    stored_hash, account_id, seen_epoch = _load_login_hash(engine, login_name)
    # Argon2 stays outside write_uow. The write below rechecks this exact hash.
    password_ok = verify_password(settings, password, stored_hash)
    replacement = None
    if password_ok and stored_hash and needs_rehash(settings, stored_hash):
        replacement = hash_password(settings, password)
    if not password_ok or account_id is None or seen_epoch is None:
        raise AuthRequired()
    with write_uow(engine) as session:
        changed = session.execute(
            update(Account)
            .where(
                Account.id == account_id,
                Account.status == "active",
                Account.auth_epoch == seen_epoch,
                Account.password_hash == stored_hash,
            )
            .values(password_hash=replacement if replacement is not None else stored_hash)
        )
        if changed.rowcount != 1:
            raise AuthRequired()
        account = session.get(Account, account_id)
        if account is None or account.auth_epoch != seen_epoch:
            raise AuthRequired()
        result = issue_session(session, account, settings, current)
        clear_bucket(session, keys[0])
        return result


def resolve_principal(
    session: Session,
    token: str | None,
    *,
    now: float | None = None,
) -> Principal:
    """Valid session predicate. ``now`` is Unix UTC seconds, never SQLite datetime."""

    current = _clock(now)
    if not token:
        raise AuthRequired()
    digest = digest_secret(token)
    row = session.scalar(select(AccountSession).where(AccountSession.token_sha256 == digest))
    if row is None or not hmac.compare_digest(row.token_sha256, digest):
        raise AuthRequired()
    account = session.get(Account, row.account_id)
    if account is None or row.revoked_at is not None:
        raise AuthRequired()
    if row.auth_epoch != account.auth_epoch or account.status != "active":
        raise AuthRequired()
    if not (current < row.idle_expires_at and current < row.absolute_expires_at):
        raise AuthRequired()
    return Principal(
        account_id=account.id,
        session_id=row.id,
        auth_epoch=account.auth_epoch,
    )


def slide_session(
    session: Session,
    principal: Principal,
    settings: Settings,
    now: float,
) -> None:
    """Slide the idle deadline forward. Failure here does not extend an expired session."""

    row = session.get(AccountSession, principal.session_id)
    if row is None or row.revoked_at is not None:
        return
    if now >= row.idle_expires_at or now >= row.absolute_expires_at:
        return
    if row.last_active_at >= now - 60:
        return
    updated = min(now + settings.session_idle_seconds, row.absolute_expires_at)
    if updated < row.idle_expires_at:
        return
    row.last_active_at = now
    row.idle_expires_at = updated


def rotate_csrf(session: Session, principal: Principal) -> str:
    row = session.get(AccountSession, principal.session_id)
    if row is None or row.revoked_at is not None:
        raise AuthRequired()
    token, digest = new_csrf_token()
    row.csrf_sha256 = digest
    return token


def require_csrf(session: Session, principal: Principal, presented: str | None) -> None:
    row = session.get(AccountSession, principal.session_id)
    stored = None if row is None else row.csrf_sha256
    if not secret_matches(stored, presented):
        raise ActionForbidden(category="csrf")


def logout_session(
    session: Session,
    *,
    token: str | None,
    origin: str | None,
    csrf_token: str | None,
    allowed_origins: tuple[str, ...],
    now: float,
) -> LogoutStatus:
    """Revoke only the presented live session. A repeat call does not touch other rows."""

    if not token:
        return LogoutStatus.ABSENT
    try:
        principal = resolve_principal(session, token, now=now)
    except AuthRequired:
        return LogoutStatus.ABSENT
    if not origin_allowed(origin, allowed_origins):
        raise ActionForbidden(category="origin")
    require_csrf(session, principal, csrf_token)
    row = session.get(AccountSession, principal.session_id)
    if row is None or row.revoked_at is not None:
        return LogoutStatus.ABSENT
    row.revoked_at = now
    return LogoutStatus.REVOKED
