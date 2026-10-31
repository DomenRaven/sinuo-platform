"""Authenticated session and access routes."""

from sinuo.api.auth.errors import register_auth_exception_handlers
from sinuo.api.auth.routes import router

__all__ = ["register_auth_exception_handlers", "router"]
