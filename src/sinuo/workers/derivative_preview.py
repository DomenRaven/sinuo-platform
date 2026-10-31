"""Render one claimed derivative outside the database transaction.

The caller claims with ``claim_job`` inside ``write_uow`` and commits before
calling this module. Rendering reads the stored original and does not open a
session. A ready PNG is written at ``jobs/<job_id>/<fencing_token>/result``.
The caller publishes that relative path with ``finalize_job`` inside a later
``write_uow``.

Pixel-limit and SVG refusals write nothing. Their finish category is the
existing non-retryable ``invalid_schema``. A GLB without Blender writes
nothing and uses the existing ``unavailable`` category. No placeholder is
drawn. This module does not commit and does not call a model.
"""

from dataclasses import dataclass
from pathlib import Path

from sinuo.application.assets.registered_preview import render_registered_preview
from sinuo.workers.derivative_files import _attempt_relative

_NON_RETRYABLE_PREVIEW = frozenset({"pixel_limit", "svg_rejected"})


@dataclass(frozen=True)
class DerivativePreview:
    generated: bool
    state: str
    reason: str
    relative_path: str | None
    finish_category: str | None


def render_derivative_preview(
    *,
    objects_root: Path,
    project_id: str,
    content_hash: str,
    generation_id: str,
    media_type: str,
    derived_root: Path,
    job_id: str,
    fencing_token: int,
    max_pixels: int,
) -> DerivativePreview:
    """Read one stored original and render into the current attempt path.

    Success leaves a PNG at the attempt relative path and sets
    ``finish_category`` to ``None``. Any refusal leaves that path unwritten.
    The caller finishes a refusal with ``finish_job_attempt`` inside its own
    ``write_uow``.
    """

    relative = _attempt_relative(job_id, fencing_token)
    rendered = render_registered_preview(
        objects_root=objects_root,
        project_id=project_id,
        content_hash=content_hash,
        generation_id=generation_id,
        media_type=media_type,
        derived_root=derived_root,
        relative_path=relative,
        max_pixels=max_pixels,
    )
    if rendered.generated:
        return DerivativePreview(
            generated=True,
            state=rendered.state,
            reason=rendered.reason,
            relative_path=relative,
            finish_category=None,
        )
    return DerivativePreview(
        generated=False,
        state=rendered.state,
        reason=rendered.reason,
        relative_path=None,
        finish_category=_finish_category(rendered.state, rendered.reason),
    )


def _finish_category(state: str, reason: str) -> str:
    if reason in _NON_RETRYABLE_PREVIEW or state == "unsupported":
        return "invalid_schema"
    return "unavailable"


__all__ = ["DerivativePreview", "render_derivative_preview"]
