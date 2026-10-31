"""Persistence adapters for sessions and login throttles."""

from sinuo.infrastructure.auth.cookies import (
    attach_session_cookie,
    clear_session_cookie,
    cookie_is_secure,
)
from sinuo.infrastructure.auth.throttle import (
    bucket_key,
    clear_bucket,
    inspect_buckets,
    record_failure,
)

__all__ = [
    "attach_session_cookie",
    "bucket_key",
    "clear_bucket",
    "clear_session_cookie",
    "cookie_is_secure",
    "inspect_buckets",
    "record_failure",
]
