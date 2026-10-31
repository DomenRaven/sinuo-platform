"""Argon2id passwords. Verification stays outside write_uow."""

import secrets
import unicodedata

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError
from argon2.low_level import Type

from sinuo.config import Settings
from sinuo.domain.schema_types import ClosedSetError, normalize_login_name

_DUMMY_HASHES: dict[tuple[int, int, int, int, int], str] = {}


def password_hasher(settings: Settings) -> PasswordHasher:
    return PasswordHasher(
        time_cost=settings.argon2_time_cost,
        memory_cost=settings.argon2_memory_kib,
        parallelism=settings.argon2_parallelism,
        hash_len=settings.argon2_hash_len,
        salt_len=settings.argon2_salt_len,
        type=Type.ID,
    )


def dummy_password_hash(settings: Settings) -> str:
    """Process-local hash that matches no account. It is not written or logged."""

    key = (
        settings.argon2_time_cost,
        settings.argon2_memory_kib,
        settings.argon2_parallelism,
        settings.argon2_hash_len,
        settings.argon2_salt_len,
    )
    found = _DUMMY_HASHES.get(key)
    if found is None:
        found = password_hasher(settings).hash(secrets.token_urlsafe(32))
        _DUMMY_HASHES[key] = found
    return found


def canonical_login_name(value: str) -> str:
    """NFKC, casefold, and strip. Reject whitespace and control characters."""

    normalized = normalize_login_name(value)
    for char in normalized:
        if char.isspace() or unicodedata.category(char)[:1] in {"C", "Z"}:
            raise ClosedSetError("login_name rejects whitespace and control characters")
    return normalized


def hash_password(settings: Settings, password: str) -> str:
    hashed = password_hasher(settings).hash(password)
    if not hashed.startswith("$argon2id$"):
        raise RuntimeError("password hasher did not produce an argon2id hash")
    return hashed


def verify_password(settings: Settings, password: str, stored_hash: str | None) -> bool:
    """One Argon2id check. Unknown accounts and foreign hashes use the dummy hash."""

    hasher = password_hasher(settings)
    dummy = dummy_password_hash(settings)
    candidate = stored_hash if stored_hash and stored_hash.startswith("$argon2id$") else dummy
    material = password if isinstance(password, str) and 1 <= len(password) <= 128 else "x"
    try:
        matched = hasher.verify(candidate, material)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False
    return bool(matched) and candidate is not dummy


def needs_rehash(settings: Settings, stored_hash: str) -> bool:
    return password_hasher(settings).check_needs_rehash(stored_hash)


def validate_new_password(password: str, login_name: str) -> None:
    normalized = canonical_login_name(login_name)
    if not isinstance(password, str) or not 12 <= len(password) <= 128:
        raise ClosedSetError("password length must be 12 to 128")
    if password.casefold() == normalized:
        raise ClosedSetError("password must not match the login name")
