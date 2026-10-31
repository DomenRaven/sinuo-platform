"""Preview metadata for one artifact version. Refusal omits name and storage."""

import json
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy.orm import Session

from sinuo.application.assets.preview import PreviewDisposition
from sinuo.application.auth.authorizer import SessionAuthorizer
from sinuo.domain.auth.errors import ActionForbidden, NotFoundOrHidden
from sinuo.domain.auth.principal import Principal
from sinuo.domain.auth.resource import ResourceRef
from sinuo.domain.public_contract import Action
from sinuo.infrastructure.assets.paths import PathEscape, is_storage_segment, resolve_contained
from sinuo.infrastructure.domain_models import ArtifactVersion


class PreviewRecordRejected(Exception):
    """Metadata cannot be stored. The reason is a closed code, not the filename."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


@dataclass(frozen=True)
class PreviewDisclosure:
    artifact_version_id: str
    preview_state: str
    preview_reason: str
    generated: bool
    original_filename: str
    object_key: str


@dataclass(frozen=True)
class PreviewRefusal:
    """Denied preview read. Has no filename, preview, or storage key."""

    decision: str


@dataclass(frozen=True)
class _StoredFact:
    original_filename: str
    preview_state: str
    preview_reason: str


def record_preview_metadata(
    registry: Path,
    *,
    artifact_version_id: str,
    original_filename: str,
    disposition: PreviewDisposition,
) -> None:
    """Store a failed preview beside the registry. The filename is not a path."""

    if disposition.generated or disposition.output_path is not None:
        raise PreviewRecordRejected("preview_success_refused")
    if not is_storage_segment(artifact_version_id):
        raise PreviewRecordRejected("artifact_version_id")
    if not _metadata_text(original_filename):
        raise PreviewRecordRejected("original_filename")
    if not _metadata_text(disposition.state) or not _metadata_text(disposition.reason):
        raise PreviewRecordRejected("disposition")
    target = resolve_contained(registry, f"{artifact_version_id}.json")
    target.write_text(
        json.dumps(
            {
                "original_filename": original_filename,
                "preview_state": disposition.state,
                "preview_reason": disposition.reason,
                "generated": False,
            },
            ensure_ascii=True,
            separators=(",", ":"),
        ),
        encoding="utf-8",
    )


def read_preview_metadata(
    session: Session,
    principal: Principal,
    authorizer: SessionAuthorizer,
    *,
    project_id: str,
    artifact_version_id: str,
    registry: Path,
) -> PreviewDisclosure | PreviewRefusal:
    """Authorize preview.read before loading the version, name, or object key."""

    try:
        authorizer.authorize(
            Action.PREVIEW_READ,
            ResourceRef(
                project_id=project_id,
                scope_type="artifact_version",
                artifact_version_id=artifact_version_id,
            ),
            principal,
        )
    except ActionForbidden:
        return PreviewRefusal(decision="forbidden")
    except NotFoundOrHidden:
        return PreviewRefusal(decision="hidden")
    version = session.get(ArtifactVersion, artifact_version_id)
    if version is None or version.project_id != project_id:
        return PreviewRefusal(decision="hidden")
    fact = _load_fact(registry, version.id)
    if fact is None:
        return PreviewDisclosure(
            artifact_version_id=version.id,
            preview_state="not_run",
            preview_reason="converter_not_invoked",
            generated=False,
            original_filename="",
            object_key=version.object_key,
        )
    return PreviewDisclosure(
        artifact_version_id=version.id,
        preview_state=fact.preview_state,
        preview_reason=fact.preview_reason,
        generated=False,
        original_filename=fact.original_filename,
        object_key=version.object_key,
    )


def _load_fact(registry: Path, artifact_version_id: str) -> _StoredFact | None:
    if not is_storage_segment(artifact_version_id):
        return None
    try:
        target = resolve_contained(registry, f"{artifact_version_id}.json")
    except PathEscape:
        return None
    if not target.is_file():
        return None
    try:
        loaded = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    if not isinstance(loaded, dict):
        return None
    filename = loaded.get("original_filename")
    state = loaded.get("preview_state")
    reason = loaded.get("preview_reason")
    if not _metadata_text(filename) or not _metadata_text(state) or not _metadata_text(reason):
        return None
    return _StoredFact(
        original_filename=filename,
        preview_state=state,
        preview_reason=reason,
    )


def _metadata_text(value: object) -> bool:
    if not isinstance(value, str):
        return False
    if not value or len(value) > 200 or "\x00" in value:
        return False
    if ".." in value or "/" in value or "\\" in value:
        return False
    return not any(ord(char) < 32 for char in value)
