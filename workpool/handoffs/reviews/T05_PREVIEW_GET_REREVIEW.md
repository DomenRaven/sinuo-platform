# T05 预览 GET 复审

本次只读了 `workpool/handoffs/T05_PREVIEW_GET_CONTRACT.md`、`workpool/handoffs/T05_PREVIEW_GET_SLICE.md`、`src/sinuo/api/assets/routes.py` 里的 `get_artifact_version_preview` 及其直接使用的读取与错误响应，以及 `tests/acceptance/test_preview_http.py` 里的 GET 断言。下面只记这次阅读和这次运行。

## 命令与退出码

工作目录是仓库根 `sinuo-platform`。先建 `_施工文件/2026-10-31_grok-review-preview-get/99_临时文件`，再执行：

```text
.venv/Scripts/python.exe -m pytest -q tests/acceptance/test_preview_http.py -o cache_dir=_施工文件/2026-10-31_grok-review-preview-get/99_临时文件/pytest-cache --basetemp=_施工文件/2026-10-31_grok-review-preview-get/99_临时文件/pytest
```

退出码：0。输出：`1 passed, 1 warning in 0.57s`。没有 failed，没有 skipped。警告是 `fastapi/testclient.py` 的 `StarletteDeprecationWarning`：`httpx` 与 `starlette.testclient` 的搭配被标为弃用。

这次运行的测试只有 `test_preview_route_renders_outside_the_write_transaction`。GET 断言写在这个函数里。退出码 0 表示这些断言没有失败。

## 测试锁定的 GET 行为

1. `get_artifact_version_preview` 的源码文本不含 `write_uow`、`render_registered_preview`、`render_original_preview`。`_assert_get_reads_only` 用 `inspect.getsource(get_artifact_version_preview)` 做这三条 `not in source` 断言（186–190 行）。测试入口调用它（196 行）。

2. OpenAPI 的预览路径下有 `get`。断言 `"get" in app.openapi()["paths"][PREVIEW_PATH]`（284 行）。`PREVIEW_PATH` 是 `/api/v1/artifact-versions/{artifact_version_id}/preview`（44 行）。

3. 预览文件还不存在时，持有 `preview.read` 的 GET 得到 404，正文没有 PNG 签名，不创建预览文件。此时 cookie 已是 `reader_token`（339 行），`derived_root` 已设为现存目录（366 行），POST 尚未发出。`absent = client.get(url)`（368 行）：`status_code == 404`（369 行），`error.code == "NOT_FOUND_OR_HIDDEN"`（370 行），`x-content-type-options` 为 `nosniff`（371 行），`b"\x89PNG"` 不在 `absent.content`（372 行），`title` 不在 `_visible(absent)`（373 行），`_assert_hidden(absent, secrets)`（374 行），`assert_no_preview_file()`（375 行）。`title` 是 `"synthetic-asset-title"`（367 行）。`_visible` 把正文按 latin-1 解码后拼上全部响应头（155–159 行）。`_assert_hidden` 要求 `secrets` 里的每个字符串及其反斜杠转义形式都不出现（162–166 行）。`secrets` 含 objects、derived、`tmp_path`、attempts 的解析路径，以及 `C:\secret\store`、`C:\secret\derived`、`outside.png`、`max_pixels`、`relative_path`（296–305 行）。`assert_no_preview_file` 断言派生目录没有文件、`tmp_path` 下没有 `*.png`、`tmp_path / "outside.png"` 不存在（308–311 行）。

4. 不存在的版本 GET 得到 404，响应不含标题和 `secrets` 里的路径，也不写出 `missing-version-id.png`。`hidden = client.get("/api/v1/artifact-versions/missing-version-id/preview")`（376 行）：状态码 404（377 行），`error.code == "NOT_FOUND_OR_HIDDEN"`（378 行），`b"\x89PNG"` 不在正文（379 行），`title` 不在 `_visible(hidden)`（380 行），`_assert_hidden`（381 行），`assert_no_preview_file()`（382 行），`derived / "previews" / "missing-version-id.png"` 不存在（383 行）。这次请求仍带着读者 cookie。

5. 同一测试里的 POST 已经写出 `previews/{version_id}.png` 之后，持有 `preview.read` 的 GET 返回该文件字节，类型是 PNG，下载名固定为 `preview.png`。`written = preview_path.read_bytes()`（406 行），`fetched = client.get(url)`（407 行）：状态码 200（408 行），`cache-control` 为 `no-store`（409 行），`x-content-type-options` 为 `nosniff`（410 行），`content-type` 按分号切开后的第一段是 `image/png`（411 行），`content-disposition` 等于 `inline; filename="preview.png"`（412 行），`"synthetic-asset-title"` 不在这份 `content-disposition` 里（413 行），正文以 `b"\x89PNG\r\n\x1a\n"` 开头（414 行），`fetched.content == written`（415 行），`title` 不在 `_visible(fetched)`（416 行），`_assert_hidden(fetched, secrets)`（417 行）。随后 `preview_path.read_bytes() == written`（418 行），`_files(derived) == [preview_path]`（419 行）。

6. 只有 `artifact.original.read`、没有 `preview.read` 的 GET 得到 403，正文没有 PNG 签名，不含标题和 `secrets` 里的路径，已有预览文件不变。cookie 换成 `member_token`（421 行）。该成员的授权是 `Action.ARTIFACT_ORIGINAL_READ`（270–276 行）。`denied_get = client.get(url)`（422 行）：状态码 403（423 行），`error.code == "ACTION_FORBIDDEN"`（424 行），`error.details.action == "preview.read"`（425 行），`b"\x89PNG"` 不在正文（426 行），`title` 不在 `_visible(denied_get)`（427 行），`_assert_hidden`（428 行），`preview_path.read_bytes() == written`（429 行），`_files(derived) == [preview_path]`（430 行）。

7. 匿名 GET 得到 401，正文没有 PNG 签名，不含标题和 `secrets` 里的路径，已有预览文件不变。`client.cookies.clear()` 之后 `anonymous_get = client.get(url)`（432–433 行）：状态码 401（434 行），`error.code == "AUTH_REQUIRED"`（435 行），`b"\x89PNG"` 不在正文（436 行），`title` 不在 `_visible(anonymous_get)`（437 行），`_assert_hidden`（438 行），文件字节仍等于 `written`（439 行），派生目录仍只有那一个预览文件（440 行）。

这次 GET 断言没有把一份非 PNG 文件放到派生目录里再请求。401、403，以及文件不存在时的 404，没有断言 `cache-control`。不存在版本的 404 也没有断言 `nosniff`。

## 同一次阅读看到的 GET

`get_artifact_version_preview` 在 281–306 行。它在短 `Session` 里解析主体、读取版本，版本不存在时抛 `NotFoundOrHidden`，并按 `Action.PREVIEW_READ` 调用 `_authorize`（285–296 行）。`AuthRequired`、`ActionForbidden`、`NotFoundOrHidden` 返回 `_json_error`（298–299 行）。会话结束后用 `version.id` 调用 `_existing_preview_bytes`（300–303 行）。结果为 `None` 时返回 `NotFoundOrHidden` 的 JSON（304–305 行）。有字节时调用 `_preview_png_response`（306 行）。这个函数体没有 `render_registered_preview`、`render_original_preview`、`write_uow`，也没有调用 `_filename_source`。

`_existing_preview_bytes` 在 252–270 行。派生根不是现存目录、相对路径为 `None`、`resolve_contained` 抛 `PathEscape`、目标不是文件、`read_bytes` 抛 `OSError`，或字节不以 `_PREVIEW_PNG` 开头时，返回 `None`。`_PREVIEW_PNG` 是 `b"\x89PNG\r\n\x1a\n"`（45 行）。这里只有 `read_bytes`。`resolve_contained` 在 `src/sinuo/infrastructure/assets/paths.py` 21–45 行，函数体在返回路径前没有创建文件。

`_preview_png_response` 在 273–278 行。`media_type` 为 `image/png`，`Cache-Control` 为 `no-store`，`X-Content-Type-Options` 为 `nosniff`，`Content-Disposition` 为模块常量 `_PREVIEW_DISPOSITION`，即 `inline; filename="preview.png"`（46、277 行）。

`_json_error` 在 72–104 行。401、403、404 的正文是 `error.code`、`message`、`details`、`retryable`、`request_id`。`forbidden_details` 在有 action 时只放 `action`（`src/sinuo/api/auth/errors.py` 75–80 行）。`request_id_for` 返回已有请求号、匹配正则的 `x-request-id`，或新的 UUID（37–47 行）。这次阅读没有在这条错误正文里看到磁盘路径或作品标题。

## blocking

blocking：0。

这次阅读和这次运行里，四条 blocking 条件都没有出现。文件还不存在时的 GET，以及不存在版本的 GET，都通过了 `assert_no_preview_file`；预览文件已经存在时，有权、无权、匿名三次 GET 之后字节和文件列表都保持为 POST 写下的那一个文件。GET 函数源码断言不含两个渲染函数名，函数体与 `_existing_preview_bytes` 里也没有调用它们，没有打开 `write_uow`。无权 GET 与匿名 GET 的断言排除了作品标题和 `secrets` 里的路径字符串；错误响应的构造里没有这些字段。返回 `image/png` 的那条路径只接收 `_existing_preview_bytes` 留下的字节，而该函数要求字节以 PNG 签名开头。成功 GET 断言正文以这个签名开头，并且 `content-type` 的主类型是 `image/png`。

Blender 预览和 pin 的 `fencing_token` 不计入 blocking。这次测试的 GET 断言没有这两项。切片写着 Blender 预览仍未做，并且没有安装 Blender。本次不把这两项写成已关闭。

T05 仍是 implementing。本文件不是完成声明。
