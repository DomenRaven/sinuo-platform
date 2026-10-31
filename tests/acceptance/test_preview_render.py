"""Raster and PDF preview files. No database and no Alembic."""

import shutil
from pathlib import Path

import pypdfium2
import pytest
from PIL import Image

from sinuo.application.assets.preview import render_original_preview
from sinuo.infrastructure.assets.paths import PathEscape


def test_png_preview_is_a_png_and_leaves_the_original_bytes(tmp_path):
    original = tmp_path / "original.png"
    _write_png(original, (2, 2), (12, 34, 56), exif_note="secret-note")
    source = original.read_bytes()
    snapshot = bytes(source)
    derived = tmp_path / "derived"
    derived.mkdir()
    assert b"eXIf" in snapshot

    result = render_original_preview(
        source,
        media_type="image/png",
        derived_root=derived,
        relative_path="preview.png",
        max_pixels=4,
    )

    preview = derived / "preview.png"
    rendered = preview.read_bytes()
    assert result.generated is True
    assert result.state == "ready"
    assert result.reason == "rendered"
    assert result.generator_name == "Pillow"
    assert result.generator_version == Image.__version__
    assert result.generator_version == "11.3.0"
    assert result.relative_path == "preview.png"
    assert preview.is_file()
    assert rendered.startswith(b"\x89PNG\r\n\x1a\n")
    assert b"eXIf" not in _png_chunk_types(rendered)
    assert source == snapshot
    assert original.read_bytes() == snapshot
    assert _files(derived) == [preview]
    with Image.open(preview) as opened:
        assert opened.format == "PNG"
        assert opened.size == (2, 2)
        assert opened.getpixel((0, 0)) == (12, 34, 56)
        assert "exif" not in opened.info


def test_pdf_preview_is_a_png_and_leaves_the_original_bytes(tmp_path):
    original = tmp_path / "original.pdf"
    source = _write_pdf(original, 24, 18)
    snapshot = bytes(source)
    derived = tmp_path / "derived"
    derived.mkdir()

    result = render_original_preview(
        source,
        media_type="application/pdf",
        derived_root=derived,
        relative_path="preview.png",
        max_pixels=24 * 18,
    )

    preview = derived / "preview.png"
    rendered = preview.read_bytes()
    assert result.generated is True
    assert result.state == "ready"
    assert result.generator_name == "pypdfium2"
    assert result.generator_version == str(pypdfium2.V_PYPDFIUM2)
    assert result.generator_version == "4.30.0"
    assert preview.is_file()
    assert rendered.startswith(b"\x89PNG\r\n\x1a\n")
    assert b"eXIf" not in _png_chunk_types(rendered)
    assert source == snapshot
    assert original.read_bytes() == snapshot
    assert _files(derived) == [preview]
    with Image.open(preview) as opened:
        assert opened.format == "PNG"
        assert opened.size == (24, 18)


def test_oversized_raster_writes_nothing(tmp_path):
    original = tmp_path / "wide.png"
    _write_png(original, (6, 6), (1, 2, 3), exif_note=None)
    source = original.read_bytes()
    snapshot = bytes(source)
    derived = tmp_path / "derived"
    derived.mkdir()

    result = render_original_preview(
        source,
        media_type="image/png",
        derived_root=derived,
        relative_path="preview.png",
        max_pixels=35,
    )

    assert result.generated is False
    assert result.state == "unsupported"
    assert result.reason == "pixel_limit"
    assert result.relative_path is None
    assert result.generator_name == "Pillow"
    assert source == snapshot
    assert original.read_bytes() == snapshot
    assert _files(derived) == []
    assert not (derived / "preview.png").exists()


def test_svg_writes_nothing(tmp_path):
    original = tmp_path / "mark.svg"
    source = b'<svg xmlns="http://www.w3.org/2000/svg" width="2" height="2"></svg>'
    original.write_bytes(source)
    snapshot = bytes(source)
    derived = tmp_path / "derived"
    derived.mkdir()

    result = render_original_preview(
        source,
        media_type="image/svg+xml",
        derived_root=derived,
        relative_path="preview.png",
        max_pixels=1000,
    )

    assert result.generated is False
    assert result.relative_path is None
    assert result.state == "unsupported"
    assert result.reason == "svg_rejected"
    assert source == snapshot
    assert original.read_bytes() == snapshot
    assert _files(derived) == []
    assert not (derived / "preview.png").exists()


def test_gltf_binary_writes_nothing_while_blender_is_absent(tmp_path):
    assert shutil.which("blender") is None
    original = tmp_path / "model.glb"
    source = b"glb-bytes"
    original.write_bytes(source)
    snapshot = bytes(source)
    derived = tmp_path / "derived"
    derived.mkdir()

    result = render_original_preview(
        source,
        media_type="model/gltf-binary",
        derived_root=derived,
        relative_path="preview.png",
        max_pixels=1000,
    )

    assert result.generated is False
    assert result.state == "unavailable"
    assert result.reason == "converter_not_installed"
    assert result.generator_name == "blender"
    assert result.generator_version == ""
    assert result.relative_path is None
    assert source == snapshot
    assert original.read_bytes() == snapshot
    assert _files(derived) == []
    assert not (derived / "preview.png").exists()


def test_oversized_pdf_writes_nothing(tmp_path):
    original = tmp_path / "large.pdf"
    source = _write_pdf(original, 40, 40)
    snapshot = bytes(source)
    derived = tmp_path / "derived"
    derived.mkdir()

    result = render_original_preview(
        source,
        media_type="application/pdf",
        derived_root=derived,
        relative_path="preview.png",
        max_pixels=100,
    )

    assert result.generated is False
    assert result.reason == "pixel_limit"
    assert result.relative_path is None
    assert source == snapshot
    assert original.read_bytes() == snapshot
    assert _files(derived) == []


def test_dotdot_segment_writes_nothing(tmp_path):
    original = tmp_path / "original.png"
    _write_png(original, (2, 2), (9, 8, 7), exif_note=None)
    source = original.read_bytes()
    derived = tmp_path / "derived"
    derived.mkdir()
    escaped = tmp_path / "outside.png"

    with pytest.raises(PathEscape):
        render_original_preview(
            source,
            media_type="image/png",
            derived_root=derived,
            relative_path="nested/../../outside.png",
            max_pixels=4,
        )

    assert not escaped.exists()
    assert original.read_bytes() == source
    assert _files(tmp_path) == [original]


def _write_png(
    path: Path,
    size: tuple[int, int],
    color: tuple[int, int, int],
    exif_note: str | None,
) -> None:
    image = Image.new("RGB", size, color)
    if exif_note is None:
        image.save(path, format="PNG")
        return
    exif = image.getexif()
    exif[270] = exif_note
    image.save(path, format="PNG", exif=exif)


def _write_pdf(path: Path, width: float, height: float) -> bytes:
    document = pypdfium2.PdfDocument.new()
    try:
        page = document.new_page(width, height)
        page.close()
        document.save(path)
    finally:
        document.close()
    return path.read_bytes()


def _files(root: Path) -> list[Path]:
    return sorted(path for path in root.rglob("*") if path.is_file())


def _png_chunk_types(payload: bytes) -> list[bytes]:
    assert payload.startswith(b"\x89PNG\r\n\x1a\n")
    offset = 8
    found: list[bytes] = []
    while offset + 8 <= len(payload):
        length = int.from_bytes(payload[offset : offset + 4], "big")
        kind = payload[offset + 4 : offset + 8]
        found.append(kind)
        offset += 12 + length
        if kind == b"IEND":
            break
    return found
