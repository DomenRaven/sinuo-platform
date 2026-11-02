from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse
from sqlalchemy import inspect, text
from sqlalchemy.exc import SQLAlchemyError

from sinuo import __version__
from sinuo.api.agents.routes import router as agent_router
from sinuo.api.agents.start import start_router
from sinuo.api.assets.routes import router as assets_router
from sinuo.api.assets.uploads import router as upload_router
from sinuo.api.auth.errors import register_auth_exception_handlers
from sinuo.api.auth.routes import router as auth_router
from sinuo.api.candidates.errors import register_candidate_exception_handlers
from sinuo.api.candidates.routes import router as candidate_router
from sinuo.api.events.routes import router as events_router
from sinuo.api.evidence.routes import router as evidence_router
from sinuo.api.exports.routes import router as export_router
from sinuo.api.records.routes import router as records_router
from sinuo.api.reviews.errors import register_review_exception_handlers
from sinuo.api.reviews.routes import router as review_router
from sinuo.api.suggestions.errors import register_suggestion_exception_handlers
from sinuo.api.suggestions.routes import router as suggestion_router
from sinuo.api.versions.errors import register_version_exception_handlers
from sinuo.api.versions.routes import router as version_router
from sinuo.api.workspace.routes import router as workspace_router
from sinuo.application.trace.routes import router as trace_router
from sinuo.config import Settings
from sinuo.infrastructure.database import build_engine
from sinuo.infrastructure.models import SCHEMA_REVISION, Base

_DESK = Path(__file__).resolve().parents[3] / "web" / "desk.html"


def create_app(settings: Settings | None = None) -> FastAPI:
    configuration = settings or Settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        engine = build_engine(configuration.database_url, configuration.sqlite_busy_timeout_ms)
        app.state.engine = engine
        uploads = _DESK.parents[1] / "var" / "uploads"
        objects = _DESK.parents[1] / "var" / "objects"
        uploads.mkdir(parents=True, exist_ok=True)
        objects.mkdir(parents=True, exist_ok=True)
        if getattr(app.state, "uploads_root", None) is None:
            app.state.uploads_root = uploads
        if getattr(app.state, "objects_root", None) is None:
            app.state.objects_root = objects
        try:
            yield
        finally:
            engine.dispose()

    app = FastAPI(title="Sinuo API", version=__version__, lifespan=lifespan)
    app.state.settings = configuration
    app.include_router(agent_router)
    app.include_router(start_router())
    app.include_router(auth_router)
    app.include_router(version_router)
    app.include_router(review_router)
    app.include_router(events_router)
    app.include_router(export_router)
    app.include_router(evidence_router)
    app.include_router(records_router)
    app.include_router(assets_router)
    app.include_router(upload_router)
    app.include_router(suggestion_router)
    app.include_router(candidate_router)
    app.include_router(trace_router)
    app.include_router(workspace_router)
    register_auth_exception_handlers(app)
    register_version_exception_handlers(app)
    register_review_exception_handlers(app)
    register_suggestion_exception_handlers(app)
    register_candidate_exception_handlers(app)

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

    def desk_page() -> FileResponse:
        response = FileResponse(_DESK, media_type="text/html; charset=utf-8")
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.get("/", include_in_schema=False)
    def workspace_home() -> FileResponse:
        return desk_page()

    @app.get("/projects", include_in_schema=False)
    @app.get("/projects/{project_id}", include_in_schema=False)
    @app.get("/projects/{project_id}/pages/{page_id}", include_in_schema=False)
    def workspace_page(project_id: str = "", page_id: str = "") -> FileResponse:
        return desk_page()

    return app
