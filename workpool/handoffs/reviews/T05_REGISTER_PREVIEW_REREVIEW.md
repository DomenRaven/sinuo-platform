# T05 登记后预览复审

本次只读了 `workpool/handoffs/T05_REGISTER_PREVIEW_CONTRACT.md`、`workpool/handoffs/T05_REGISTER_PREVIEW_SLICE.md`、`src/sinuo/application/assets/registered_preview.py`、`tests/acceptance/test_registered_preview.py`，并在 `src/sinuo/application/assets/uploads.py` 中确认 `register_original` 是否调用 `render_registered_preview` 或 `render_original_preview`。下面只记这次阅读和这次运行。

## 命令与退出码

工作目录是仓库根 `sinuo-platform`。先建 `_施工文件/2026-10-31_grok-review-regpreview/99_临时文件`，再执行：

```text
.venv/Scripts/python.exe -m pytest -q tests/acceptance/test_registered_preview.py -o cache_dir=_施工文件/2026-10-31_grok-review-regpreview/99_临时文件/pytest-cache --basetemp=_施工文件/2026-10-31_grok-review-regpreview/99_临时文件/pytest
```

退出码：0。输出：`2 passed in 0.43s`。没有 failed，没有 skipped。

## 测试锁定的行为

1. 渲染在 `write_uow` 退出之后才调用。`_register` 把 `register_original` 放在 `with write_uow(assets)` 里。块内断言 `outcome.registered is True`、`outcome.preview_generated is False`、`outcome.preview_state == "not_run"`、`outcome.preview_reason == "converter_not_invoked"`，并且 `artifact_version_id`、`generation_id` 为 `str`、`content_hash` 等于登记摘要（138–144 行）。这个 `with` 退出之后才断言 `read_original(...) == payload` 且 `_files(derived) == []`（189–191 行），然后返回。`test_registered_png_renders_after_commit` 在 `_register` 返回后再次断言派生目录为空（215 行），然后才调用 `render_registered_preview`（217–226 行）。`test_oversized_registered_png_leaves_the_original_downloadable` 也是在 `_register` 返回后才调用 `render_registered_preview`（259–268 行）。两条测试在调用前都再次断言版本 id 与 generation id 为 `str`（213–214、256–257 行）。

2. 像素上限内容得下的小 PNG 会写出一张 PNG，原件字节仍可按原哈希下载。`test_registered_png_renders_after_commit` 使用 2×2、颜色 `(12, 34, 56)` 的 PNG，`max_pixels=4`。断言 `result.generated is True`、`result.state == "ready"`、`result.reason == "rendered"`、`result.generator_name == "Pillow"`、`result.relative_path == "preview.png"`（230–234 行）；预览字节以 `b"\x89PNG\r\n\x1a\n"` 开头（235 行）；派生目录里只有 `preview.png`（236 行）；打开后 `format == "PNG"`、`size == (2, 2)`、`(0, 0)` 为 `(12, 34, 56)`（237–240 行）；`read_original` 仍等于登记 payload（241 行）；`download_original` 的 `body == payload`，`server_hash == digest` 且等于 `sha256(body)`，`byte_size == len(payload)`，`media_type == "image/png"`（242–247 行）。这次运行该测试通过。

3. 超过调用方像素上限时不写预览，失败后的原件字节不被删除或改写，仍可下载。`test_oversized_registered_png_leaves_the_original_downloadable` 使用 6×6 的 PNG，`max_pixels=35`。断言 `result.generated is False`、`result.state == "unsupported"`、`result.reason == "pixel_limit"`、`result.relative_path is None`（270–273 行）；`_files(derived) == []`，且 `preview.png` 不存在（274–275 行）；`read_original` 仍等于登记 payload（276 行）；下载 `body == payload`，`server_hash == digest` 且等于 `sha256(body)`，`byte_size == len(payload)`，`artifact_version_id == version_id`（277–282 行）。这次运行该测试通过。

## 同一次阅读看到的调用位置

`uploads.py` 全文没有 `render_registered_preview`，也没有 `render_original_preview`。`register_original` 从第 160 行开始，下一个函数定义在第 342 行，因此 `register_original` 自身不调用这两个函数。

`render_registered_preview` 先 `read_original`，再把读到的字节交给 `render_original_preview` 并返回该结果（`registered_preview.py` 28–35 行）。这个函数的源码里没有 `write_uow`，没有打开数据库会话，也没有提交。

## blocking

blocking：0。

这次阅读和这次运行里，四条 blocking 条件都没有出现：渲染调用在 `write_uow` 退出之后；`register_original` 自身不调用 `render_registered_preview` 或 `render_original_preview`；成功的小 PNG 用例写出了 PNG，且该测试通过；超限失败后 `read_original` 与下载体仍等于登记 payload，且该测试通过。

Blender 预览、HTTP 接线、pin 的 `fencing_token` 不计入 blocking。这两个测试没有这些断言。本次阅读不把它们写成已关闭。

T05 仍是 implementing。本文件不是完成声明。
