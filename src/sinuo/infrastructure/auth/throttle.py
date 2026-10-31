"""Login buckets. Rows store hashes, never passwords, tokens, or login names."""

import hashlib
import math
from dataclasses import dataclass

from sqlalchemy import delete
from sqlalchemy.orm import Session

from sinuo.infrastructure.domain_models import LoginThrottle


@dataclass(frozen=True)
class ThrottleView:
    limited: bool
    retry_after: int


def bucket_key(prefix: str, material: str) -> str:
    digest = hashlib.sha256(material.encode("utf-8")).hexdigest()
    key = f"{prefix}:{digest}"
    if prefix not in {"acct", "src"} or len(key) > 128:
        raise ValueError("throttle bucket key is invalid")
    return key


def inspect_buckets(
    session: Session,
    keys: tuple[str, ...],
    *,
    now: float,
    window_seconds: int,
    limit: int,
) -> ThrottleView:
    limited = False
    retry_after = 1
    for key in keys:
        row = session.get(LoginThrottle, key)
        if row is None or now - row.window_started_at >= window_seconds:
            continue
        if row.attempts >= limit:
            limited = True
            remaining = math.ceil(window_seconds - (now - row.window_started_at))
            retry_after = max(retry_after, max(1, int(remaining)))
    return ThrottleView(limited=limited, retry_after=retry_after)


def record_failure(
    session: Session,
    keys: tuple[str, ...],
    *,
    now: float,
    window_seconds: int,
) -> None:
    for key in keys:
        row = session.get(LoginThrottle, key)
        if row is None:
            session.add(
                LoginThrottle(
                    bucket_key=key,
                    window_started_at=now,
                    attempts=1,
                    updated_at=now,
                )
            )
            continue
        if now - row.window_started_at >= window_seconds:
            row.window_started_at = now
            row.attempts = 1
        else:
            row.attempts += 1
        row.updated_at = now


def clear_bucket(session: Session, key: str) -> None:
    session.execute(delete(LoginThrottle).where(LoginThrottle.bucket_key == key))
