# T05 栅格与 PDF 预览切片

状态保持 implementing。这只是真实栅格和 PDF 预览的一刀，不是 T05 完成。

## 对照

合同 `workpool/handoffs/T05_PREVIEW_RENDER_CONTRACT.md`（`T05-PREVIEW-RENDER-1`）和 `docs/construction/06_SECURITY_STORAGE_RECOVERY.md` 的预览节。本仓库没有单独的预览理论条目。像素上限、EXIF 和 PDF 页尺寸按已安装的 Pillow 11.3.0、pypdfium2 4.30.0 接口实现：PNG 的 `eXIf` 块只在保存时传入才会写入；`page.render(scale=1)` 的像素数是页面宽高向上取整后的乘积。

新函数是 `render_original_preview`。不改 `preview_disposition` 和 `run_preview_converter`，因为注入转换器的正常返回必须仍是 `not_run`，`generated` 为 false。超过像素上限就拒绝，不缩小，也不画占位图。

## 行为

调用方给出派生根、相对路径和像素上限。相对路径交给现有的 `resolve_contained`，段里带 `..` 会在写文件前拒绝。

栅格用 Pillow 解码。`image/svg+xml` 和内容像 SVG 的图片拒绝，不写文件。宽乘高大于上限时不解码像素、不写文件。成功时把像素重写成 PNG，不带 EXIF。结果里的生成器是 `Pillow`，版本是导入得到的 `11.3.0`。

PDF 只打开第 0 页，按 scale 1 用 pypdfium2 渲染，同一像素上限，然后关闭 document。结果里的生成器是 `pypdfium2`，版本是导入得到的 `4.30.0`。不启动 shell。

`model/gltf-binary` 在没有 Blender 时返回 `unavailable` / `converter_not_installed`，生成器名 `blender`，版本空字符串，不写文件。原件字节只读。

## 命令

工作目录是仓库根 `sinuo-platform`。先建 `_施工文件/2026-10-31_grok-dev-a2/99_临时文件`，再执行：

```text
.venv/Scripts/python.exe -m pytest -q tests/acceptance/test_preview_render.py tests/acceptance/test_assets_derivatives.py::test_missing_converter_does_not_fabricate_a_preview tests/acceptance/test_assets_derivatives.py::test_injected_converter_failures_leave_the_original_downloadable -o cache_dir=_施工文件/2026-10-31_grok-dev-a2/99_临时文件/pytest-cache --basetemp=_施工文件/2026-10-31_grok-dev-a2/99_临时文件/pytest
```

新测试在 `tmp_path` 里自己写小 PNG 和一页 PDF，不使用数据库，也不跑 Alembic。后面两条旧测试用来确认注入转换器的旧行为没变；其中需要数据库的那条本次也通过了。

## 退出码

0

## 通过数

9 passed。输出里没有 failed，也没有 skipped。

## 仍未完成

Blender 预览仍未做。没有占位图。T05 状态仍是 implementing。
