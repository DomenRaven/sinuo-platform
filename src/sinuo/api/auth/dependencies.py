"""Request dependencies. The principal comes only from the session cookie."""

from fastapi import Request
from sqlalchemy.engine import Engine

from sinuo.config import Settings
from sinuo.domain.public_contract import session_cookie_name


def get_settings(request: Request) -> Settings:
    settings = getattr(request.app.state, "settings", None)
    if not isinstance(settings, Settings):
        raise RuntimeError("app.state.settings is required before auth routes run")
    return settings


def get_engine(request: Request) -> Engine:
    engine = getattr(request.app.state, "engine", None)
    if engine is None:
        raise RuntimeError("database engine is not ready")
    return engine


def session_token(request: Request, settings: Settings) -> str | None:
    """Read only the cookie selected for this deployment."""

    return request.cookies.get(session_cookie_name(settings.public_url))


def client_host(request: Request) -> str:
    if request.client is None or not request.client.host:
        return "unknown"
    return request.client.host
