"""Same-volume install of an already written attempt. Does not copy bytes."""

import os
from pathlib import Path

from sinuo.infrastructure.assets.paths import PathEscape, is_storage_segment, resolve_contained


class DifferentVolume(Exception):
    """The attempt and the object directory are not on one volume."""


class ObjectAlreadyPresent(Exception):
    """The generation path already exists and must not be overwritten."""


def object_relative(project_id: str, content_hash: str, generation_id: str) -> str:
    for value in (project_id, content_hash, generation_id):
        if not is_storage_segment(value):
            shown = value if isinstance(value, str) else ""
            raise PathEscape(shown)
    return f"{project_id}/sha256/{content_hash}/{generation_id}/original"


def original_key(project_id: str, content_hash: str, generation_id: str) -> str:
    return f"objects/{object_relative(project_id, content_hash, generation_id)}"


def install_original(
    source: Path,
    objects_root: Path,
    project_id: str,
    content_hash: str,
    generation_id: str,
) -> str:
    """Link the attempt into objects/<project>/sha256/<hash>/<generation>/original.

    os.link fails if the destination exists. A different volume is refused
    instead of copied. The source name is removed after the link exists.
    """

    relative = object_relative(project_id, content_hash, generation_id)
    destination = resolve_contained(objects_root, relative)
    if destination.exists():
        raise ObjectAlreadyPresent(relative)
    destination.parent.mkdir(parents=True, exist_ok=True)
    if source.stat().st_dev != destination.parent.stat().st_dev:
        raise DifferentVolume(relative)
    os.link(source, destination)
    try:
        os.unlink(source)
    except OSError:
        if not destination.is_file():
            raise
    return f"objects/{relative}"


def read_original(
    objects_root: Path,
    project_id: str,
    content_hash: str,
    generation_id: str,
) -> bytes:
    relative = object_relative(project_id, content_hash, generation_id)
    return resolve_contained(objects_root, relative).read_bytes()
