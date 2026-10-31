"""Authorized original download. This module does not write a preview.

The download name is metadata chosen by the server. It is not a storage path.
"""

import hashlib
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote

from sqlalchemy.orm import Session

from sinuo.application.auth.authorizer import SessionAuthorizer
from sinuo.domain.auth.errors import NotFoundOrHidden
from sinuo.domain.auth.principal import Principal
from sinuo.domain.auth.resource import ResourceRef
from sinuo.domain.public_contract import Action
from sinuo.infrastructure.assets.objects import read_original
from sinuo.infrastructure.assets.paths import PathEscape, is_storage_segment
from sinuo.infrastructure.domain_models import ArtifactVersion


class OriginalUnreadable(Exception):
    """The version is authorized, but the stored bytes cannot be returned."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


@dataclass(frozen=True)
class OriginalDownload:
    artifact_version_id: str
    server_hash: str
    byte_size: int
    media_type: str
    body: bytes


def download_original(
    session: Session,
    principal: Principal,
    authorizer: SessionAuthorizer,
    *,
    project_id: str,
    artifact_version_id: str,
    objects_root: Path,
) -> OriginalDownload:
    """Return the original bytes and the hash computed from those bytes.

    Authorization runs before any object path is resolved. The result has no
    filesystem path.
    """

    authorizer.authorize(
        Action.ARTIFACT_ORIGINAL_READ,
        ResourceRef(
            project_id=project_id,
            scope_type="artifact_version",
            artifact_version_id=artifact_version_id,
        ),
        principal,
    )
    version = session.get(ArtifactVersion, artifact_version_id)
    if version is None or version.project_id != project_id:
        raise NotFoundOrHidden()
    generation_id = _generation_id(version.object_key, project_id, version.content_hash)
    try:
        body = read_original(objects_root, project_id, version.content_hash, generation_id)
    except (FileNotFoundError, OSError, PathEscape):
        raise OriginalUnreadable("missing_bytes") from None
    server_hash = hashlib.sha256(body).hexdigest()
    if server_hash != version.content_hash or len(body) != version.size_bytes:
        raise OriginalUnreadable("hash")
    return OriginalDownload(
        artifact_version_id=version.id,
        server_hash=server_hash,
        byte_size=len(body),
        media_type=version.media_type,
        body=body,
    )


_FILENAME_FALLBACK = "original"
_QUOTES = frozenset("\"'`“”‘’")
_MEDIA_CHARS = frozenset("abcdefghijklmnopqrstuvwxyz0123456789!#$&^_.+-")
_ACTIVE_MEDIA_TYPES = frozenset(
    {
        "text/html",
        "application/xhtml+xml",
        "image/svg+xml",
        "text/xml",
        "application/xml",
        "application/javascript",
        "text/javascript",
        "application/x-javascript",
    }
)


def safe_download_filename(source: str) -> str:
    """Keep a single display name. Drop directories, quotes, and line breaks."""

    if not isinstance(source, str):
        return _FILENAME_FALLBACK
    leaf = source.replace("\\", "/").split("/")[-1]
    kept: list[str] = []
    for char in leaf:
        if char in _QUOTES or char == ":" or ord(char) < 32 or ord(char) == 127:
            continue
        if char in "/\\":
            continue
        kept.append(char)
    name = "".join(kept).strip(" .")
    if len(name) > 180:
        name = name[:180].strip(" .")
    if not name or name in {".", ".."} or set(name) <= {".", " "}:
        return _FILENAME_FALLBACK
    if any(char in name for char in '/\\"\'\r\n'):
        return _FILENAME_FALLBACK
    return name


def safe_media_type(media_type: str) -> str:
    """A header token. Parameters, breaks, and paths become octet-stream."""

    if not isinstance(media_type, str):
        return "application/octet-stream"
    base = media_type.split(";", 1)[0].strip().lower()
    if ".." in base or base.count("/") != 1:
        return "application/octet-stream"
    kind, subtype = base.split("/", 1)
    if not kind or not subtype:
        return "application/octet-stream"
    if kind[0] not in _MEDIA_CHARS or subtype[0] not in _MEDIA_CHARS:
        return "application/octet-stream"
    if any(char not in _MEDIA_CHARS for char in kind + subtype):
        return "application/octet-stream"
    return base


def is_active_content(media_type: str) -> bool:
    """HTML, SVG, script, and XML can run if a browser treats them as inline."""

    if not isinstance(media_type, str):
        return False
    base = media_type.split(";", 1)[0].strip().lower()
    return base in _ACTIVE_MEDIA_TYPES


def download_response_headers(filename_source: str, media_type: str) -> dict[str, str]:
    """Server filename plus nosniff and an attachment disposition.

    Active content is an attachment. Every other original is an attachment too.
    """

    filename = safe_download_filename(filename_source)
    # Active markup and every other original are both attachments.
    if is_active_content(media_type):
        disposition = "attachment"
    else:
        disposition = "attachment"
    ascii_name = filename.encode("ascii", "ignore").decode("ascii").strip(" .")
    if not ascii_name:
        ascii_name = _FILENAME_FALLBACK
    encoded = quote(filename, safe="")
    header = f"{disposition}; filename=\"{ascii_name}\"; filename*=UTF-8''{encoded}"
    return {
        "X-Content-Type-Options": "nosniff",
        "Content-Disposition": header,
        "Cache-Control": "no-store",
        "Content-Type": safe_media_type(media_type),
    }


def _generation_id(object_key: str, project_id: str, content_hash: str) -> str:
    if (
        not isinstance(object_key, str)
        or ".." in object_key
        or "\\" in object_key
        or object_key.startswith("/")
    ):
        raise OriginalUnreadable("object_key")
    parts = object_key.split("/")
    if len(parts) != 6:
        raise OriginalUnreadable("object_key")
    root_name, key_project, kind, key_hash, generation_id, leaf = parts
    if (root_name, kind, leaf) != ("objects", "sha256", "original"):
        raise OriginalUnreadable("object_key")
    if key_project != project_id or key_hash != content_hash:
        raise OriginalUnreadable("object_key")
    if not is_storage_segment(generation_id):
        raise OriginalUnreadable("object_key")
    return generation_id
