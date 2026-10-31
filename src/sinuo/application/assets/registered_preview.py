"""Render a preview from an original that is already stored. Does not commit."""

from pathlib import Path

from sinuo.application.assets.preview import PreviewRender, render_original_preview
from sinuo.infrastructure.assets.objects import read_original


def render_registered_preview(
    *,
    objects_root: Path,
    project_id: str,
    content_hash: str,
    generation_id: str,
    media_type: str,
    derived_root: Path,
    relative_path: str,
    max_pixels: int,
) -> PreviewRender:
    """Read one stored original, then call ``render_original_preview``.

    The caller invokes this after ``write_uow`` has committed the registration.
    This function does not open a session and does not commit. A pixel-limit
    refusal, an SVG rejection, or a missing Blender for GLB is the render
    result: no preview file is written. The stored bytes are only read.
    """

    source = read_original(objects_root, project_id, content_hash, generation_id)
    return render_original_preview(
        source,
        media_type=media_type,
        derived_root=derived_root,
        relative_path=relative_path,
        max_pixels=max_pixels,
    )
