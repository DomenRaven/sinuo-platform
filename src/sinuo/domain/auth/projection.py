"""Field projection shared by later page, search, download, export, and receipt paths."""

from collections.abc import Mapping
from typing import Any

_ORIGINAL_FIELD_KEYS = frozenset(
    {
        "object_key",
        "original",
        "original_bytes",
        "thumbnail",
        "thumbnail_url",
    }
)
_CITATION_FIELD_KEYS = frozenset(
    {
        "excerpt",
        "excerpt_text",
        "citation",
        "locator",
        "locator_json",
        "quote",
    }
)
_COUNT_KEYS = frozenset(
    {"count", "total", "hidden_count", "redacted_count", "omitted", "sensitive_count"}
)
_PREVIEW_SENSITIVITY = frozenset({"released", "project"})
_RECEIPT_KEYS = frozenset(
    {
        "id",
        "status",
        "project_revision",
        "audit_event_id",
        "access_epoch",
        "grant_ids",
        "content_revision",
        "content_hash",
        "version_no",
        "layout_revision",
    }
)
_ID_KEYS = (
    "id",
    "page_id",
    "artifact_id",
    "artifact_version_id",
    "evidence_anchor_id",
)


def project_visible_fields(
    document: Mapping[str, Any],
    *,
    can_read_original: bool,
    can_read_evidence: bool,
    can_preview: bool,
) -> dict[str, Any]:
    """Drop originals, thumbnails, excerpts, citation detail, and sensitive counts.

    ``page.read`` and ``preview.read`` do not reveal a sensitive original. A
    released or project-sensitivity preview can remain when preview read is allowed.
    """

    projected = _walk(
        document,
        can_read_original=can_read_original,
        can_read_evidence=can_read_evidence,
        can_preview=can_preview,
        sensitivity=None,
    )
    if not isinstance(projected, dict):
        return {}
    return projected


def project_receipt(body: Mapping[str, Any]) -> dict[str, Any]:
    """Keep ids and bookkeeping that are still visible. Do not write this back."""

    projected = _receipt(body)
    if not isinstance(projected, dict):
        return {}
    return projected


def _is_count_key(key: str) -> bool:
    return key in _COUNT_KEYS or key.endswith("_count")


def _kind(document: Mapping[str, Any]) -> str:
    artifact = any(key in document for key in ("object_key", "original_bytes", "thumbnail"))
    evidence = any(key in document for key in ("locator", "locator_json", "excerpt", "quote"))
    if artifact:
        return "artifact"
    if evidence:
        return "evidence"
    if "page_id" in document:
        return "page"
    return "other"


def _ids(document: Mapping[str, Any]) -> dict[str, Any]:
    return {key: document[key] for key in _ID_KEYS if key in document}


def _preview_allowed(sensitivity: str | None, can_preview: bool) -> bool:
    if not can_preview:
        return False
    if sensitivity is None:
        return True
    return sensitivity in _PREVIEW_SENSITIVITY


def _title_hidden(
    kind: str,
    sensitivity: str | None,
    *,
    can_read_original: bool,
    can_read_evidence: bool,
) -> bool:
    if kind == "page":
        return False
    if kind == "artifact":
        return sensitivity == "sensitive" and not can_read_original
    if kind == "evidence":
        return not can_read_evidence
    return False


def _walk(
    value: Any,
    *,
    can_read_original: bool,
    can_read_evidence: bool,
    can_preview: bool,
    sensitivity: str | None,
) -> Any:
    if isinstance(value, Mapping):
        return _walk_object(
            value,
            can_read_original=can_read_original,
            can_read_evidence=can_read_evidence,
            can_preview=can_preview,
            sensitivity=sensitivity,
        )
    if isinstance(value, list):
        projected = []
        for item in value:
            child_sensitivity = sensitivity
            if isinstance(item, Mapping) and isinstance(item.get("sensitivity"), str):
                child_sensitivity = item["sensitivity"]
            child = _walk(
                item,
                can_read_original=can_read_original,
                can_read_evidence=can_read_evidence,
                can_preview=can_preview,
                sensitivity=child_sensitivity,
            )
            if child is not None:
                projected.append(child)
        return projected
    return value


def _walk_object(
    document: Mapping[str, Any],
    *,
    can_read_original: bool,
    can_read_evidence: bool,
    can_preview: bool,
    sensitivity: str | None,
) -> dict[str, Any]:
    raw_sensitivity = document.get("sensitivity")
    local = raw_sensitivity if isinstance(raw_sensitivity, str) else sensitivity
    kind = _kind(document)
    if kind == "artifact" and local == "sensitive" and not can_read_original:
        return _ids(document)
    if kind == "evidence" and local == "sensitive" and not can_read_evidence:
        return _ids(document)
    projected: dict[str, Any] = {}
    for key, item in document.items():
        if _is_count_key(key):
            continue
        if key in _ORIGINAL_FIELD_KEYS and not can_read_original:
            continue
        if key in _CITATION_FIELD_KEYS and not can_read_evidence:
            continue
        if key == "title" and _title_hidden(
            kind,
            local,
            can_read_original=can_read_original,
            can_read_evidence=can_read_evidence,
        ):
            continue
        if key == "preview_ref" and not _preview_allowed(local, can_preview):
            continue
        projected[key] = _walk(
            item,
            can_read_original=can_read_original,
            can_read_evidence=can_read_evidence,
            can_preview=can_preview,
            sensitivity=local,
        )
    return projected


def _receipt(value: Any) -> Any:
    if isinstance(value, Mapping):
        projected: dict[str, Any] = {}
        for key, item in value.items():
            if _is_count_key(key):
                continue
            if key not in _RECEIPT_KEYS and not _id_key(key):
                continue
            projected[key] = _receipt(item)
        return projected
    if isinstance(value, list):
        return [_receipt(item) for item in value]
    return value


def _id_key(key: str) -> bool:
    return key == "id" or key.endswith("_id") or key.endswith("_ids")
