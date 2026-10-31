"""Host-only session cookies. Names come from the public contract."""

from urllib.parse import urlparse

from starlette.responses import Response

from sinuo.config import Settings
from sinuo.domain.public_contract import session_cookie_name


def cookie_is_secure(public_url: str) -> bool:
    return urlparse(public_url).scheme == "https"


def session_max_age(absolute_expires_at: float, now: float, absolute_seconds: int) -> int:
    remaining = int(absolute_expires_at - now)
    return min(max(remaining, 0), absolute_seconds, 86400)


def attach_session_cookie(
    response: Response,
    settings: Settings,
    token: str,
    absolute_expires_at: float,
    now: float,
) -> None:
    response.set_cookie(
        key=session_cookie_name(settings.public_url),
        value=token,
        max_age=session_max_age(absolute_expires_at, now, settings.session_absolute_seconds),
        path="/",
        secure=cookie_is_secure(settings.public_url),
        httponly=True,
        samesite="lax",
    )


def clear_session_cookie(response: Response, settings: Settings) -> None:
    response.delete_cookie(
        key=session_cookie_name(settings.public_url),
        path="/",
        secure=cookie_is_secure(settings.public_url),
        httponly=True,
        samesite="lax",
    )
