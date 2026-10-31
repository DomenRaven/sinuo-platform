"""Local account bootstrap. Passwords come from the terminal, never from argv."""

import getpass
import sys
import time
import uuid

from sqlalchemy import select, update

from sinuo.application.auth.passwords import (
    canonical_login_name,
    hash_password,
    validate_new_password,
)
from sinuo.config import Settings
from sinuo.domain.schema_types import ClosedSetError
from sinuo.infrastructure.database import build_engine, write_uow
from sinuo.infrastructure.domain_models import Account


class BootstrapError(Exception):
    """The local account command cannot be completed."""


def argv_supplies_password(argv: list[str]) -> bool:
    for arg in argv:
        lowered = arg.lower()
        if lowered in {"--password", "-p", "--pass", "--pwd"}:
            return True
        if lowered.startswith("--password=") or "password=" in lowered:
            return True
    return False


def create_initial_account(
    settings: Settings,
    login_name: str,
    display_name: str,
    password: str,
    *,
    now: float | None = None,
) -> str:
    """Create the first maintainer. Fails when any account already exists."""

    validate_new_password(password, login_name)
    stored_name = canonical_login_name(login_name)
    shown = display_name.strip()
    if not shown:
        raise ClosedSetError("display_name is empty")
    hashed = hash_password(settings, password)
    current = time.time() if now is None else now
    engine = build_engine(settings.database_url, settings.sqlite_busy_timeout_ms)
    try:
        with write_uow(engine) as session:
            existing = session.scalar(select(Account.id).limit(1))
            if existing is not None:
                raise BootstrapError("accounts already exist")
            account_id = str(uuid.uuid4())
            if len(account_id) != 36:
                raise RuntimeError("account id must be 36 characters")
            session.add(
                Account(
                    id=account_id,
                    login_name=stored_name,
                    display_name=shown,
                    password_hash=hashed,
                    status="active",
                    auth_epoch=1,
                    can_create_project=1,
                    created_at=current,
                    disabled_at=None,
                )
            )
            session.flush()
            return account_id
    finally:
        engine.dispose()


def disable_account(settings: Settings, login_name: str, *, now: float | None = None) -> None:
    current = time.time() if now is None else now
    normalized = canonical_login_name(login_name)
    engine = build_engine(settings.database_url, settings.sqlite_busy_timeout_ms)
    try:
        with write_uow(engine) as session:
            row = session.execute(
                select(Account.id, Account.auth_epoch, Account.status).where(
                    Account.login_name == normalized
                )
            ).one_or_none()
            if row is None or row.status != "active":
                raise BootstrapError("account unavailable")
            result = session.execute(
                update(Account)
                .where(
                    Account.id == row.id,
                    Account.auth_epoch == row.auth_epoch,
                    Account.status == "active",
                )
                .values(
                    auth_epoch=Account.auth_epoch + 1,
                    status="disabled",
                    disabled_at=current,
                ),
                execution_options={"synchronize_session": False},
            )
            if result.rowcount != 1:
                raise BootstrapError("account changed")
    finally:
        engine.dispose()


def reset_password(
    settings: Settings,
    login_name: str,
    password: str,
    *,
    now: float | None = None,
) -> None:
    validate_new_password(password, login_name)
    hashed = hash_password(settings, password)
    normalized = canonical_login_name(login_name)
    engine = build_engine(settings.database_url, settings.sqlite_busy_timeout_ms)
    try:
        with write_uow(engine) as session:
            row = session.execute(
                select(Account.id, Account.auth_epoch).where(Account.login_name == normalized)
            ).one_or_none()
            if row is None:
                raise BootstrapError("account unavailable")
            result = session.execute(
                update(Account)
                .where(Account.id == row.id, Account.auth_epoch == row.auth_epoch)
                .values(auth_epoch=Account.auth_epoch + 1, password_hash=hashed),
                execution_options={"synchronize_session": False},
            )
            if result.rowcount != 1:
                raise BootstrapError("account changed")
    finally:
        engine.dispose()


def _prompt_password() -> str:
    first = getpass.getpass("Password: ")
    second = getpass.getpass("Repeat password: ")
    if first != second:
        raise BootstrapError("password confirmation did not match")
    return first


def main(argv: list[str] | None = None) -> int:
    raw = list(sys.argv[1:] if argv is None else argv)
    if argv_supplies_password(raw):
        sys.stderr.write("refusing password on the command line\n")
        return 2
    if len(raw) < 2 or raw[0] not in {"init", "disable", "reset"}:
        sys.stderr.write(
            "usage: python -m sinuo.application.auth.bootstrap init|disable|reset LOGIN [DISPLAY]\n"
        )
        return 2
    command, login_name = raw[0], raw[1]
    try:
        settings = Settings(_env_file=None)
        if command == "init":
            if len(raw) != 3:
                raise BootstrapError("init requires a display name")
            create_initial_account(settings, login_name, raw[2], _prompt_password())
        elif command == "disable":
            if len(raw) != 2:
                raise BootstrapError("disable accepts only the login name")
            disable_account(settings, login_name)
        else:
            if len(raw) != 2:
                raise BootstrapError("reset accepts only the login name")
            reset_password(settings, login_name, _prompt_password())
    except (BootstrapError, ClosedSetError, OSError, ValueError):
        sys.stderr.write("bootstrap failed\n")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
