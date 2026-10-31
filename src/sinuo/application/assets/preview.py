"""Report preview capability and render one contained PNG.

Converter failures leave no preview file. A normal injected converter return
stays not_run. Real raster and PDF bytes are rendered by a separate function.
"""

import importlib.util
import io
import math
import os
import shutil
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from sinuo.infrastructure.assets.paths import PathEscape, resolve_contained

_GLB = frozenset({"model/gltf-binary", "model/gltf+json"})


@dataclass(frozen=True)
class PreviewDisposition:
    state: str
    reason: str
    generated: bool
    output_path: None = None


class PreviewTimedOut(Exception):
    """The injected converter reports that its deadline already elapsed."""


class PreviewFormatUnsupported(Exception):
    """The injected converter rejects this byte format."""


class PreviewToolMissing(Exception):
    """The injected converter reports that its tool is not installed."""


class PreviewOutputLeft(Exception):
    """A converter wrote a file that was not removed. No success is recorded."""

    def __init__(self) -> None:
        super().__init__("preview output was left behind")


def default_converter_installed(media_type: str) -> bool:
    if media_type == "application/pdf":
        return importlib.util.find_spec("pypdfium2") is not None
    if media_type.startswith("image/") and media_type != "image/svg+xml":
        return importlib.util.find_spec("PIL") is not None
    if media_type in _GLB:
        return shutil.which("blender") is not None
    return False


def preview_disposition(media_type: str, *, installed: bool | None = None) -> PreviewDisposition:
    """Missing tools stay unavailable. An installed tool is still not a preview."""

    present = default_converter_installed(media_type) if installed is None else installed
    if present:
        return _unpublished("not_run", "converter_not_invoked")
    return _unpublished("unavailable", "converter_not_installed")


def run_preview_converter(
    source: bytes,
    *,
    derived_root: Path,
    converter: Callable[[bytes, Path], object],
) -> PreviewDisposition:
    """Classify one injected converter without publishing a preview.

    The converter itself reports timeout, an unsupported format, or a missing
    tool. Any other exception is a crash. A normal return is not a preview.
    Files created for the attempt are removed. This function does not sleep.
    """

    if type(source) is not bytes:
        raise TypeError("source must be bytes")
    if not callable(converter):
        raise TypeError("converter must be callable")
    if not isinstance(derived_root, Path) or not derived_root.is_dir():
        raise TypeError("derived_root must be a directory")
    before = set(derived_root.rglob("*"))
    work = derived_root / f"work-{uuid.uuid4()}"
    work.mkdir()
    try:
        try:
            converter(source, work)
        except PreviewTimedOut:
            disposition = _unpublished("timed_out", "converter_timeout")
        except PreviewFormatUnsupported:
            disposition = _unpublished("unsupported", "format_unsupported")
        except PreviewToolMissing:
            disposition = _unpublished("unavailable", "converter_not_installed")
        except Exception:
            disposition = _unpublished("failed", "converter_crashed")
        else:
            disposition = _unpublished("not_run", "converter_not_invoked")
    finally:
        _remove_created(derived_root, before)
    if _created_files(derived_root, before):
        raise PreviewOutputLeft()
    return disposition


def _unpublished(state: str, reason: str) -> PreviewDisposition:
    return PreviewDisposition(state=state, reason=reason, generated=False)


def _created_files(root: Path, before: set[Path]) -> list[Path]:
    return [path for path in root.rglob("*") if path not in before and path.is_file()]


def _remove_created(root: Path, before: set[Path]) -> None:
    created = [path for path in root.rglob("*") if path not in before]
    for path in sorted(created, key=lambda item: len(item.parts), reverse=True):
        if path.is_symlink() or path.is_file():
            path.unlink()
        elif path.is_dir():
            path.rmdir()


@dataclass(frozen=True)
class PreviewRender:
    """One preview attempt. ``relative_path`` is the caller path, not a disk root."""

    state: str
    reason: str
    generated: bool
    generator_name: str
    generator_version: str
    relative_path: str | None


def render_original_preview(
    source: bytes,
    *,
    media_type: str,
    derived_root: Path,
    relative_path: str,
    max_pixels: int,
) -> PreviewRender:
    """Render one original to a PNG inside ``derived_root``.

    Pillow decodes a non-SVG raster. Width times height above ``max_pixels``
    writes nothing, and the PNG does not carry EXIF. PDF renders page 0 through
    pypdfium2 at scale 1, applies the same pixel limit, then closes the
    document. GLB without Blender is unavailable. No placeholder is drawn and
    no shell is started. Source bytes are only read.
    """

    base = _checked_media(source, media_type, derived_root, relative_path, max_pixels)
    destination = resolve_contained(derived_root, relative_path)
    png, state, reason, name, version = _decode_preview(base, source, max_pixels)
    if png is None:
        return _render_result(state, reason, name, version, None)
    _install_png(destination, relative_path, png)
    return _render_result("ready", "rendered", name, version, relative_path)


def _checked_media(
    source: bytes,
    media_type: str,
    derived_root: Path,
    relative_path: str,
    max_pixels: int,
) -> str:
    if type(source) is not bytes:
        raise TypeError("source must be bytes")
    if type(media_type) is not str:
        raise TypeError("media_type must be a str")
    base = media_type.split(";", 1)[0].strip().lower()
    if base == "" or "\x00" in base:
        raise TypeError("media_type must be a media type")
    if not isinstance(derived_root, Path) or not derived_root.is_dir():
        raise TypeError("derived_root must be a directory")
    if type(relative_path) is not str:
        raise TypeError("relative_path must be a str")
    if type(max_pixels) is not int or max_pixels < 1:
        raise TypeError("max_pixels must be a positive int")
    return base


def _render_result(
    state: str,
    reason: str,
    generator_name: str,
    generator_version: str,
    relative_path: str | None,
) -> PreviewRender:
    return PreviewRender(
        state=state,
        reason=reason,
        generated=relative_path is not None,
        generator_name=generator_name,
        generator_version=generator_version,
        relative_path=relative_path,
    )


def _decode_preview(
    base: str,
    source: bytes,
    max_pixels: int,
) -> tuple[bytes | None, str, str, str, str]:
    if base == "image/svg+xml":
        return _refused("svg_rejected", "Pillow", _pillow_version() or "")
    if base in _GLB:
        if default_converter_installed(base):
            return _refused("converter_not_invoked", "blender", "", state="not_run")
        return _refused("converter_not_installed", "blender", "")
    if base == "application/pdf":
        version = _pdfium_version()
        if version is None:
            return _refused("converter_not_installed", "pypdfium2", "")
        png, state, reason = _pdf_png(source, max_pixels)
        return png, state, reason, "pypdfium2", version
    if base.startswith("image/"):
        version = _pillow_version()
        if version is None:
            return _refused("converter_not_installed", "Pillow", "")
        if _svg_bytes(source):
            return _refused("svg_rejected", "Pillow", version)
        png, state, reason = _raster_png(source, max_pixels)
        return png, state, reason, "Pillow", version
    return _refused("converter_not_installed", "", "")


def _refused(
    reason: str,
    generator_name: str,
    generator_version: str,
    *,
    state: str | None = None,
) -> tuple[bytes | None, str, str, str, str]:
    if state is None:
        state = "unavailable" if reason == "converter_not_installed" else "unsupported"
    return None, state, reason, generator_name, generator_version


def _pillow_version() -> str | None:
    if not default_converter_installed("image/png"):
        return None
    import PIL

    return str(PIL.__version__)


def _pdfium_version() -> str | None:
    if not default_converter_installed("application/pdf"):
        return None
    import pypdfium2

    return str(pypdfium2.V_PYPDFIUM2)


def _svg_bytes(source: bytes) -> bool:
    head = source.lstrip(b" \t\r\n")[:512].lower()
    if head.startswith(b"\xef\xbb\xbf"):
        head = head[3:].lstrip(b" \t\r\n")
    if head.startswith(b"<svg") or head.startswith(b"<!doctype svg"):
        return True
    return head.startswith(b"<?xml") and b"<svg" in head


def _above_pixel_limit(width: int, height: int, max_pixels: int) -> bool:
    if width < 1 or height < 1:
        return True
    if width > max_pixels:
        return True
    return height > max_pixels // width


def _raster_png(source: bytes, max_pixels: int) -> tuple[bytes | None, str, str]:
    from PIL import Image, UnidentifiedImageError

    try:
        with Image.open(io.BytesIO(source)) as image:
            if (image.format or "").upper() == "SVG":
                return None, "unsupported", "svg_rejected"
            width, height = image.size
            if width < 1 or height < 1:
                return None, "unsupported", "format_unsupported"
            if _above_pixel_limit(width, height, max_pixels):
                return None, "unsupported", "pixel_limit"
            image.load()
            return _png_bytes(image), "ready", "rendered"
    except Image.DecompressionBombError:
        return None, "unsupported", "pixel_limit"
    except (UnidentifiedImageError, OSError):
        return None, "unsupported", "format_unsupported"


def _png_bytes(image: object) -> bytes:
    from PIL import Image

    if not isinstance(image, Image.Image):
        raise TypeError("image must be a Pillow image")
    if image.mode == "P":
        clean = image.copy()
    elif image.mode in {"1", "L", "LA", "RGB", "RGBA"}:
        clean = Image.frombytes(image.mode, image.size, image.tobytes())
    elif "A" in image.getbands():
        clean = image.convert("RGBA")
    else:
        clean = image.convert("RGB")
    clean.info = {}
    payload = io.BytesIO()
    clean.save(payload, format="PNG")
    return payload.getvalue()


def _pdf_png(source: bytes, max_pixels: int) -> tuple[bytes | None, str, str]:
    import pypdfium2

    try:
        document = pypdfium2.PdfDocument(source)
    except pypdfium2.PdfiumError:
        return None, "unsupported", "format_unsupported"
    try:
        return _pdf_page_png(document, max_pixels)
    finally:
        document.close()


def _pdf_page_png(document: object, max_pixels: int) -> tuple[bytes | None, str, str]:
    import pypdfium2

    page = None
    bitmap = None
    try:
        if len(document) < 1:
            return None, "unsupported", "format_unsupported"
        page = document[0]
        width, height = _pdf_pixels(page)
        if width is None or height is None:
            return None, "unsupported", "format_unsupported"
        if _above_pixel_limit(width, height, max_pixels):
            return None, "unsupported", "pixel_limit"
        bitmap = page.render(scale=1, rev_byteorder=True)
        view = bitmap.to_pil()
        view.load()
        owned = view.copy()
        if _above_pixel_limit(owned.size[0], owned.size[1], max_pixels):
            return None, "unsupported", "pixel_limit"
        return _png_bytes(owned), "ready", "rendered"
    except pypdfium2.PdfiumError:
        return None, "unsupported", "format_unsupported"
    finally:
        if bitmap is not None:
            bitmap.close()
        if page is not None:
            page.close()


def _pdf_pixels(page: object) -> tuple[int, int] | None:
    width_raw = page.get_width()
    height_raw = page.get_height()
    if isinstance(width_raw, bool) or isinstance(height_raw, bool):
        return None
    if not isinstance(width_raw, (int, float)) or not isinstance(height_raw, (int, float)):
        return None
    if not math.isfinite(width_raw) or not math.isfinite(height_raw):
        return None
    width = math.ceil(width_raw)
    height = math.ceil(height_raw)
    if width < 1 or height < 1:
        return None
    return width, height


def _install_png(destination: Path, relative_path: str, payload: bytes) -> None:
    if not payload.startswith(b"\x89PNG\r\n\x1a\n"):
        raise OSError("preview payload is not a png")
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        raise FileExistsError(destination)
    temporary = destination.with_name(destination.name + ".writing")
    if ".." in temporary.name:
        raise PathEscape(relative_path)
    try:
        temporary.write_bytes(payload)
        os.replace(temporary, destination)
    except Exception:
        if temporary.is_file() and not temporary.is_symlink():
            temporary.unlink()
        raise
