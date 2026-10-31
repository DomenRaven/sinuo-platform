"""Containment checks for attempt files and object keys."""

import re
from pathlib import Path

_SEGMENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


class PathEscape(Exception):
    """A relative path contains `..` or resolves outside the configured root."""

    def __init__(self, relative: str) -> None:
        self.relative = relative
        super().__init__("path escapes the storage root")


def is_storage_segment(value: object) -> bool:
    return isinstance(value, str) and _SEGMENT.fullmatch(value) is not None and ".." not in value


def resolve_contained(root: Path, relative: str) -> Path:
    """Return the absolute path or raise before any file is created."""

    if not isinstance(relative, str) or relative == "" or "\x00" in relative or ".." in relative:
        raise PathEscape(relative if isinstance(relative, str) else "")
    raw = Path(relative)
    if raw.is_absolute() or raw.drive or raw.root:
        raise PathEscape(relative)
    parts = raw.parts
    if not parts or any(part in {"", ".", ".."} for part in parts):
        raise PathEscape(relative)
    try:
        root_abs = root.resolve(strict=True)
    except FileNotFoundError as error:
        raise PathEscape(relative) from error
    if not root_abs.is_dir():
        raise PathEscape(relative)
    candidate = root_abs.joinpath(*parts).resolve()
    try:
        candidate.relative_to(root_abs)
    except ValueError as error:
        raise PathEscape(relative) from error
    if candidate == root_abs:
        raise PathEscape(relative)
    return candidate
