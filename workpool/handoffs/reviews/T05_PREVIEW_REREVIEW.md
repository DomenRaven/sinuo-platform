# T05 栅格与 PDF 预览复审

只读了 `workpool/handoffs/T05_PREVIEW_RENDER_CONTRACT.md`、`workpool/handoffs/T05_PREVIEW_RENDER_SLICE.md`、`src/sinuo/application/assets/preview.py`、`tests/acceptance/test_preview_render.py`，并只跑了下面这一条。

## 命令与退出码

工作目录是仓库根 `sinuo-platform`。

```text
.venv/Scripts/python.exe -m pytest -q tests/acceptance/test_preview_render.py -o cache_dir=_施工文件/2026-10-31_grok-review-preview/99_临时文件/pytest-cache --basetemp=_施工文件/2026-10-31_grok-review-preview/99_临时文件/pytest
```

退出码：0。输出是 `7 passed in 0.07s`。没有 failed，没有 skipped。

## 这次测试锁住的渲染行为

`test_png_preview_is_a_png_and_leaves_the_original_bytes`：`generated is True`，`state == "ready"`，`reason == "rendered"`，`generator_name == "Pillow"`，`generator_version` 同时等于 `Image.__version__` 和 `"11.3.0"`，`relative_path == "preview.png"`。预览文件存在，字节以 `b"\x89PNG\r\n\x1a\n"` 开头，`_png_chunk_types` 里没有 `eXIf`。`Image.open` 得到 `format == "PNG"`、`size == (2, 2)`、左上角像素 `(12, 34, 56)`，`info` 里没有 `"exif"`。`source == snapshot`，磁盘上的原件 `read_bytes()` 仍等于 `snapshot`。派生目录里的文件只有这一张预览。输入快照含 `eXIf`，输出块类型里没有。

`test_pdf_preview_is_a_png_and_leaves_the_original_bytes`：`generated is True`，`state == "ready"`，`generator_name == "pypdfium2"`，`generator_version` 同时等于 `str(pypdfium2.V_PYPDFIUM2)` 和 `"4.30.0"`。预览文件存在，字节以 PNG 签名开头，块类型里没有 `eXIf`。`Image.open` 得到 `format == "PNG"`、`size == (24, 18)`。`source == snapshot`，原件 `read_bytes()` 仍等于 `snapshot`，派生目录里只有这一张预览。这个 PDF 只有一页。该测试没有断言 `reason` 或 `relative_path`。

`test_oversized_raster_writes_nothing`：6×6 的 PNG，`max_pixels=35`。`generated is False`，`state == "unsupported"`，`reason == "pixel_limit"`，`relative_path is None`，`generator_name == "Pillow"`。`source == snapshot`，原件字节不变。`_files(derived) == []`，`preview.png` 不存在。

`test_svg_writes_nothing`：媒体类型 `image/svg+xml`。`generated is False`，`relative_path is None`，`state == "unsupported"`，`reason == "svg_rejected"`。原件字节不变。`_files(derived) == []`，`preview.png` 不存在。

`test_gltf_binary_writes_nothing_while_blender_is_absent`：`shutil.which("blender") is None`。`generated is False`，`state == "unavailable"`，`reason == "converter_not_installed"`，`generator_name == "blender"`，`generator_version == ""`，`relative_path is None`。原件字节不变。`_files(derived) == []`，`preview.png` 不存在。

`test_oversized_pdf_writes_nothing`：40×40 点的一页 PDF，`max_pixels=100`。`generated is False`，`reason == "pixel_limit"`，`relative_path is None`。原件字节不变。`_files(derived) == []`。该测试没有单独断言 `state`。

`test_dotdot_segment_writes_nothing`：相对路径 `nested/../../outside.png` 抛出 `PathEscape`。`outside.png` 不存在，原件字节不变，`tmp_path` 里的文件只有原件。

## 阅读到的旧路径

`preview_disposition` 的两个分支都返回 `_unpublished(...)`。`_unpublished` 固定 `generated=False`。工具按已安装处理时是 `not_run` / `converter_not_invoked`；按未安装处理时是 `unavailable` / `converter_not_installed`。

`run_preview_converter` 里，超时、格式不支持、工具缺失、其他异常，以及转换器正常返回，都把结果交给 `_unpublished`，所以 `generated` 仍是 false。正常返回仍是 `not_run` / `converter_not_invoked`。

`tests/acceptance/test_preview_render.py` 只导入并调用 `render_original_preview`。这次运行没有锁住 `preview_disposition` 和 `run_preview_converter`。

`render_original_preview` 只在解码结果不是 `None` 时调用 `_install_png`。上面那些拒绝路径把第一项留空，于是在写文件前返回。

## 阻断数

阻断数：0。

这次阅读和这次运行里，成功的栅格预览和成功的 PDF 预览都是 PNG；原件字节保持为调用前的快照；超限栅格、声明为 SVG 的输入、超限 PDF 都没有在派生目录留下文件；本次环境下没有 Blender，GLB 也没有留下文件。

Blender 预览、下载路由、pin `fencing_token`、调用方事务内的哈希不在这次阅读和这次运行里。本文不把它们记成已关闭。

T05 仍处于 implementing。本文件不是完成声明。
