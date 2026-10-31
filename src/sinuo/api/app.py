from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from sqlalchemy import inspect, text
from sqlalchemy.exc import SQLAlchemyError

from sinuo import __version__
from sinuo.api.agents.routes import router as agent_router
from sinuo.api.assets.routes import router as assets_router
from sinuo.api.auth.errors import register_auth_exception_handlers
from sinuo.api.auth.routes import router as auth_router
from sinuo.api.events.routes import router as events_router
from sinuo.api.suggestions.errors import register_suggestion_exception_handlers
from sinuo.api.suggestions.routes import router as suggestion_router
from sinuo.api.versions.errors import register_version_exception_handlers
from sinuo.api.versions.routes import router as version_router
from sinuo.config import Settings
from sinuo.infrastructure.database import build_engine
from sinuo.infrastructure.models import SCHEMA_REVISION, Base


def create_app(settings: Settings | None = None) -> FastAPI:
    configuration = settings or Settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        engine = build_engine(configuration.database_url, configuration.sqlite_busy_timeout_ms)
        app.state.engine = engine
        try:
            yield
        finally:
            engine.dispose()

    app = FastAPI(title="Sinuo API", version=__version__, lifespan=lifespan)
    app.state.settings = configuration
    app.include_router(agent_router)
    app.include_router(auth_router)
    app.include_router(version_router)
    app.include_router(events_router)
    app.include_router(assets_router)
    app.include_router(suggestion_router)
    register_auth_exception_handlers(app)
    register_version_exception_handlers(app)
    register_suggestion_exception_handlers(app)

    def not_ready(reason: str) -> JSONResponse:
        return JSONResponse({"status": "not_ready", "reason": reason}, 503)

    @app.get("/healthz", tags=["operations"])
    def health():
        return {"status": "alive", "version": __version__}

    @app.get("/readyz", tags=["operations"])
    def readiness():
        try:
            engine = app.state.engine
            if not inspect(engine).has_table("alembic_version"):
                return not_ready("migration_required")
            with engine.connect() as connection:
                revisions = (
                    connection.execute(text("SELECT version_num FROM alembic_version"))
                    .scalars()
                    .all()
                )
            if not revisions:
                return not_ready("migration_required")
            if revisions != [SCHEMA_REVISION]:
                return not_ready("schema_mismatch")
            existing = set(inspect(engine).get_table_names())
            if not set(Base.metadata.tables) <= existing:
                return not_ready("schema_incomplete")
        except SQLAlchemyError:
            return not_ready("database_unavailable")
        return {"status": "ready", "scope": "foundation", "schema_revision": SCHEMA_REVISION}

    return app
