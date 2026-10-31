"""Write an attempt file. This module does not open a database transaction."""

import hashlib
import os
from pathlib import Path

from sinuo.infrastructure.assets.paths import PathEscape, resolve_contained


class AttemptTooLarge(Exception):
    """The payload is above the caller limit. No attempt byte is stored."""

    def __init__(self, byte_limit: int) -> None:
        self.byte_limit = byte_limit
        super().__init__("attempt exceeds byte_limit")


def write_attempt_bytes(
    temp_root: Path,
    relative_path: str,
    payload: bytes,
    *,
    byte_limit: int | None = None,
) -> Path:
    """Create one attempt file under temp_root. A rejected path writes nothing.

    When byte_limit is set, a longer payload is refused before any attempt file
    or parent directory is created.
    """

    if not isinstance(payload, bytes):
        raise TypeError("attempt payload must be bytes")
    if byte_limit is not None:
        if type(byte_limit) is not int or byte_limit < 0:
            raise TypeError("byte_limit must be a non-negative int")
        if len(payload) > byte_limit:
            raise AttemptTooLarge(byte_limit)
    destination = resolve_contained(temp_root, relative_path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        raise FileExistsError(destination)
    temporary = destination.with_name(destination.name + ".writing")
    if ".." in temporary.name:
        raise PathEscape(relative_path)
    temporary.write_bytes(payload)
    os.replace(temporary, destination)
    return destination


def digest_file(path: Path) -> tuple[str, int]:
    """Stream sha256 and size. The caller decides whether a transaction is open."""

    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as handle:
        while True:
            block = handle.read(1024 * 1024)
            if not block:
                break
            size += len(block)
            digest.update(block)
    return digest.hexdigest(), size
