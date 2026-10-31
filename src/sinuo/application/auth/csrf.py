"""CSRF digests and Origin checks. The plaintext token is not a second cookie."""

import hashlib
import hmac
import secrets


def digest_secret(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def new_csrf_token() -> tuple[str, str]:
    token = secrets.token_urlsafe(32)
    return token, digest_secret(token)


def secret_matches(stored_hex: str | None, presented: str | None) -> bool:
    """Missing and mismatched secrets are the same failure."""

    if not stored_hex or not presented:
        return False
    actual = digest_secret(presented)
    if len(actual) != len(stored_hex):
        return False
    return hmac.compare_digest(actual, stored_hex)


def origin_allowed(presented: str | None, allowed: tuple[str, ...]) -> bool:
    if presented is None or presented == "" or presented == "*":
        return False
    presented_bytes = presented.encode("utf-8")
    for item in allowed:
        item_bytes = item.encode("utf-8")
        if len(presented_bytes) != len(item_bytes):
            continue
        if hmac.compare_digest(presented_bytes, item_bytes):
            return True
    return False
