"""HTTP adapters for an authorized original download and a registered preview.

``create_app`` includes this router. Set ``app.state.objects_root`` before a
download can return bytes. The preview POST reads that directory and
``app.state.derived_root`` only after ``preview.read`` succeeds and the read
session has closed. Rendering does not open ``write_uow``. A missing root is
an unavailable result and neither response includes a disk path.

The preview GET authorizes ``preview.read`` and then reads an existing derived
PNG. It does not render or create that file. The response filename is
``preview.png``.
"""

from dataclasses import dataclass
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response
from sqlalchemy.orm import Session

from sinuo.api.auth.dependencies import get_engine, get_settings, session_token
from sinuo.api.auth.errors import forbidden_details, request_id_for
from sinuo.application.assets.download import (
    OriginalUnreadable,
    _generation_id,
    download_original,
    download_response_headers,
)
from sinuo.application.assets.preview import PreviewRender
from sinuo.application.assets.registered_preview import render_registered_preview
from sinuo.application.auth.authorizer import SessionAuthorizer
from sinuo.application.auth.sessions import resolve_principal
from sinuo.domain.auth.errors import ActionForbidden, AuthRequired, NotFoundOrHidden
from sinuo.domain.auth.principal import Principal
from sinuo.domain.auth.resource import ResourceRef
from sinuo.domain.public_contract import Action, ErrorCode
from sinuo.infrastructure.assets.paths import PathEscape, is_storage_segment, resolve_contained
from sinuo.infrastructure.domain_models import Artifact, ArtifactVersion

router = APIRouter(prefix="/api/v1")

_UNREADABLE = frozenset({"missing_bytes", "hash", "object_key"})
_PREVIEW_MAX_PIXELS = 4_194_304
_PREVIEW_KEYS = ("generated", "state", "reason", "generator_name", "generator_version")
_PREVIEW_PNG = b"\x89PNG\r\n\x1a\n"
_PREVIEW_DISPOSITION = 'inline; filename="preview.png"'
_MESSAGES = {
    ErrorCode.AUTH_REQUIRED: "Authentication required.",
    ErrorCode.ACTION_FORBIDDEN: "Action not allowed.",
    ErrorCode.NOT_FOUND_OR_HIDDEN: "Not found.",
}


def _authorize(
    session: Session,
    principal: Principal,
    project_id: str,
    artifact_version_id: str,
    action: Action = Action.ARTIFACT_ORIGINAL_READ,
) -> None:
    SessionAuthorizer(session).authorize(
        action,
        ResourceRef(
            project_id=project_id,
            scope_type="artifact_version",
            artifact_version_id=artifact_version_id,
        ),
        principal,
    )


def _json_error(request: Request, exc: Exception) -> JSONResponse:
    details: dict[str, str]
    if isinstance(exc, AuthRequired):
        status = 401
        code = ErrorCode.AUTH_REQUIRED
        details = {}
    elif isinstance(exc, ActionForbidden):
        status = 403
        code = ErrorCode.ACTION_FORBIDDEN
        details = forbidden_details(exc)
    elif isinstance(exc, OriginalUnreadable):
        status = 404
        code = ErrorCode.NOT_FOUND_OR_HIDDEN
        details = {"category": exc.reason} if exc.reason in _UNREADABLE else {}
    elif isinstance(exc, NotFoundOrHidden):
        status = 404
        code = ErrorCode.NOT_FOUND_OR_HIDDEN
        details = {}
    else:
        raise exc
    body = {
        "error": {
            "code": code.value,
            "message": _MESSAGES[code],
            "details": details,
            "retryable": False,
            "request_id": request_id_for(request),
        }
    }
    response = JSONResponse(body, status_code=status)
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Content-Type-Options"] = "nosniff"
    return response


def _filename_source(session: Session, version: ArtifactVersion) -> str:
    artifact = session.get(Artifact, version.artifact_id)
    if artifact is None or artifact.project_id != version.project_id:
        return ""
    return artifact.title


@router.get("/artifact-versions/{artifact_version_id}/content")
def get_artifact_version_content(artifact_version_id: str, request: Request) -> Response:
    try:
        settings = get_settings(request)
        with Session(get_engine(request)) as session:
            principal = resolve_principal(session, session_token(request, settings))
            version = session.get(ArtifactVersion, artifact_version_id)
            if version is None:
                raise NotFoundOrHidden()
            root = getattr(request.app.state, "objects_root", None)
            if not isinstance(root, Path):
                _authorize(session, principal, version.project_id, version.id)
                raise OriginalUnreadable("missing_bytes")
            downloaded = download_original(
                session,
                principal,
                SessionAuthorizer(session),
                project_id=version.project_id,
                artifact_version_id=version.id,
                objects_root=root,
            )
            payload = downloaded.body
            headers = download_response_headers(
                _filename_source(session, version),
                downloaded.media_type,
            )
    except (AuthRequired, ActionForbidden, NotFoundOrHidden, OriginalUnreadable) as exc:
        return _json_error(request, exc)
    return Response(content=payload, headers=headers)


@dataclass(frozen=True)
class _PreviewFacts:
    project_id: str
    content_hash: str
    generation_id: str
    media_type: str
    artifact_version_id: str


def _configured_roots(request: Request) -> tuple[Path, Path] | None:
    objects = getattr(request.app.state, "objects_root", None)
    derived = getattr(request.app.state, "derived_root", None)
    if not isinstance(objects, Path) or not objects.is_dir():
        return None
    if not isinstance(derived, Path) or not derived.is_dir():
        return None
    return objects, derived


def _server_relative_preview(artifact_version_id: str) -> str | None:
    if not is_storage_segment(artifact_version_id):
        return None
    relative = f"previews/{artifact_version_id}.png"
    if ".." in relative or "\\" in relative or relative.startswith(("/", "\\")):
        return None
    return relative


def _unavailable_preview() -> dict[str, object]:
    body = {
        "generated": False,
        "state": "unavailable",
        "reason": "storage_not_configured",
        "generator_name": "",
        "generator_version": "",
    }
    return {key: body[key] for key in _PREVIEW_KEYS}


def _public_preview(rendered: PreviewRender) -> dict[str, object]:
    body = {
        "generated": rendered.generated,
        "state": rendered.state,
        "reason": rendered.reason,
        "generator_name": rendered.generator_name,
        "generator_version": rendered.generator_version,
    }
    return {key: body[key] for key in _PREVIEW_KEYS}


def _preview_response(payload: dict[str, object]) -> JSONResponse:
    response = JSONResponse({key: payload[key] for key in _PREVIEW_KEYS})
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Content-Type-Options"] = "nosniff"
    return response


def _preview_facts(version: ArtifactVersion) -> _PreviewFacts:
    return _PreviewFacts(
        project_id=version.project_id,
        content_hash=version.content_hash,
        generation_id=_generation_id(version.object_key, version.project_id, version.content_hash),
        media_type=version.media_type,
        artifact_version_id=version.id,
    )


@router.post("/artifact-versions/{artifact_version_id}/preview")
def post_artifact_version_preview(artifact_version_id: str, request: Request) -> JSONResponse:
    try:
        settings = get_settings(request)
        with Session(get_engine(request)) as session:
            principal = resolve_principal(session, session_token(request, settings))
            version = session.get(ArtifactVersion, artifact_version_id)
            if version is None:
                raise NotFoundOrHidden()
            _authorize(
                session,
                principal,
                version.project_id,
                version.id,
                Action.PREVIEW_READ,
            )
            facts = _preview_facts(version)
    except (AuthRequired, ActionForbidden, NotFoundOrHidden, OriginalUnreadable) as exc:
        return _json_error(request, exc)
    roots = _configured_roots(request)
    relative = _server_relative_preview(facts.artifact_version_id)
    if roots is None or relative is None:
        return _preview_response(_unavailable_preview())
    objects_root, derived_root = roots
    try:
        rendered = render_registered_preview(
            objects_root=objects_root,
            project_id=facts.project_id,
            content_hash=facts.content_hash,
            generation_id=facts.generation_id,
            media_type=facts.media_type,
            derived_root=derived_root,
            relative_path=relative,
            max_pixels=_PREVIEW_MAX_PIXELS,
        )
    except (OSError, PathEscape):
        return _preview_response(_unavailable_preview())
    return _preview_response(_public_preview(rendered))


def _existing_preview_bytes(derived_root: object, artifact_version_id: str) -> bytes | None:
    if not isinstance(derived_root, Path) or not derived_root.is_dir():
        return None
    relative = _server_relative_preview(artifact_version_id)
    if relative is None:
        return None
    try:
        path = resolve_contained(derived_root, relative)
    except PathEscape:
        return None
    if not path.is_file():
        return None
    try:
        payload = path.read_bytes()
    except OSError:
        return None
    if not payload.startswith(_PREVIEW_PNG):
        return None
    return payload


def _preview_png_response(payload: bytes) -> Response:
    response = Response(content=payload, media_type="image/png")
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Content-Disposition"] = _PREVIEW_DISPOSITION
    return response


@router.get("/artifact-versions/{artifact_version_id}/preview")
def get_artifact_version_preview(artifact_version_id: str, request: Request) -> Response:
    try:
        settings = get_settings(request)
        with Session(get_engine(request)) as session:
            principal = resolve_principal(session, session_token(request, settings))
            version = session.get(ArtifactVersion, artifact_version_id)
            if version is None:
                raise NotFoundOrHidden()
            _authorize(
                session,
                principal,
                version.project_id,
                version.id,
                Action.PREVIEW_READ,
            )
            version_id = version.id
    except (AuthRequired, ActionForbidden, NotFoundOrHidden) as exc:
        return _json_error(request, exc)
    payload = _existing_preview_bytes(
        getattr(request.app.state, "derived_root", None),
        version_id,
    )
    if payload is None:
        return _json_error(request, NotFoundOrHidden())
    return _preview_png_response(payload)
