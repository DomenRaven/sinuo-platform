# T05 预览 HTTP 复审

本次只读了 `workpool/handoffs/T05_PREVIEW_HTTP_CONTRACT.md`、`workpool/handoffs/T05_PREVIEW_HTTP_SLICE.md`、`src/sinuo/api/assets/routes.py` 里的预览路由，以及 `tests/acceptance/test_preview_http.py`。下面只记这次阅读和这次运行。

## 命令与退出码

工作目录是仓库根 `sinuo-platform`。先建 `_施工文件/2026-10-31_grok-review-preview-http/99_临时文件`，再执行：

```text
.venv/Scripts/python.exe -m pytest -q tests/acceptance/test_preview_http.py -o cache_dir=_施工文件/2026-10-31_grok-review-preview-http/99_临时文件/pytest-cache --basetemp=_施工文件/2026-10-31_grok-review-preview-http/99_临时文件/pytest
```

退出码：0。输出：`1 passed, 1 warning in 0.60s`。没有 failed，没有 skipped。警告是 `fastapi/testclient.py` 的 `StarletteDeprecationWarning`：`httpx` 与 `starlette.testclient` 的搭配被标为弃用。

这次运行的测试只有 `test_preview_route_renders_outside_the_write_transaction`。

## 测试锁定的行为

1. 路由函数源码不含 `write_uow`，`render_registered_preview(` 出现在 `with Session(` 块之外。`_assert_render_outside_write` 用 `inspect.getsource(post_artifact_version_preview)` 断言 `"write_uow" not in source`（171 行），并在扫到 `with Session(` 之后、遇到 `except ` 之前把会话内标记为真；`render_registered_preview(` 落在该标记为假的行上时把 `rendered_outside` 设为真，然后断言它为真（174–183 行）。测试入口先调用这个函数（188 行）。登记用的 `with write_uow` 在 268 行结束；`token_for` 里的 `write_uow` 在 279–282 行，于 284–285 行返回。`client.post` 从 307 行开始，不在这些 `write_uow` 块里。

2. OpenAPI 含有这条 POST。断言 `PREVIEW_PATH` 在 `app.openapi()["paths"]` 中，且该路径下有 `"post"`（274–275 行）。`PREVIEW_PATH` 是 `/api/v1/artifact-versions/{artifact_version_id}/preview`（44 行）。

3. 进入 HTTP 之前，登记结果是已登记且未生成预览，派生目录为空，原件字节等于登记 payload。`register_original` 的 `outcome.registered is True`、`outcome.preview_generated is False`，`artifact_version_id` 与 `generation_id` 为 `str`（224–229 行）。该 `write_uow` 退出后断言 `read_original(objects, PROJECT_ID, digest, generation_id) == payload`，且 `_files(derived) == []`（269–270 行）。

4. 匿名 POST 得到 401，响应里没有生成器字段，也没有预览文件。`anonymous.status_code == 401`（308 行），`anonymous.json()["error"]["code"] == "AUTH_REQUIRED"`（309 行），`"Pillow"`、`"generator_name"`、`"generator_version"` 不在 `anonymous.text`（310–312 行），登记 payload 不在 `anonymous.content`（313 行）。`_assert_hidden` 检查正文（按 latin-1 解码）和全部响应头都不含 `secrets` 里的字符串及其反斜杠转义形式（155–166、314 行）。`secrets` 含 objects、derived、`tmp_path`、attempts 的解析路径，以及 `C:\secret\store`、`C:\secret\derived`、`outside.png`、`max_pixels`、`relative_path`（287–296 行）。`assert_no_preview_file` 断言派生目录没有文件、`tmp_path` 下没有 `*.png`、`tmp_path / "outside.png"` 不存在（299–302、315 行）。这次请求的 JSON 是 `HOSTILE`（45–51、307 行）。

5. 只有 `artifact.original.read`、没有 `preview.read` 的成员得到 403，响应里没有生成器输出，也没有预览文件。`denied.status_code == 403`（319 行），`error.code == "ACTION_FORBIDDEN"`（320 行），`error.details.action == "preview.read"`（321 行），`"Pillow"`、`"generator_name"`、`"generator_version"`、`"rendered"` 不在 `denied.text`（322–325 行），payload 不在 `denied.content`（326 行），路径字符串被 `_assert_hidden` 排除（327 行），`assert_no_preview_file` 通过（328 行）。授权数据是 `_grant(..., action=Action.ARTIFACT_ORIGINAL_READ)`（262–268 行）。

6. 持有 `preview.read` 但 `objects_root` 与 `derived_root` 都是 `None` 时，响应是固定的不可用 JSON，并且不写预览。状态码 200（334 行），`cache-control` 为 `no-store`（335 行），`x-content-type-options` 为 `nosniff`（336 行），JSON 等于 `generated: False`、`state: "unavailable"`、`reason: "storage_not_configured"`、`generator_name: ""`、`generator_version: ""`（337–343 行），`"Pillow"` 不在正文（344 行），`_assert_hidden` 与 `assert_no_preview_file` 通过（345–346 行）。

7. 只设置现存的 `objects_root`、`derived_root` 仍为 `None` 时，不写预览。状态码 200（350 行），`generated is False`（351 行），`state == "unavailable"`（352 行），`"Pillow"` 不在正文（353 行），`_assert_hidden` 与 `assert_no_preview_file` 通过（354–355 行）。这次断言没有锁定 `reason`、生成器字段或响应头。

8. 两个根目录都是现存目录、调用方持有该版本 `preview.read` 时，成功响应只有五个字段，正文不含原件字节和 PNG 魔数。状态码 200（359 行），`cache-control` 为 `no-store`（360 行），`x-content-type-options` 为 `nosniff`（361 行），`set(allowed.json())` 等于 `generated`、`state`、`reason`、`generator_name`、`generator_version`（362–368 行）。值分别为 `generated is True`、`state == "ready"`、`reason == "rendered"`、`generator_name == "Pillow"`、`generator_version == PIL.__version__`（369–373 行）。payload 不在 `allowed.content`（374 行），`b"\x89PNG"` 不在 `allowed.content`（375 行），`_assert_hidden` 通过（376 行）。请求 JSON 仍是 `HOSTILE`，其中 `max_pixels` 为 1（48、358 行）。

9. 成功调用之后，派生目录里只有 `previews/{version_id}.png`，原件字节不变。`_files(derived) == [preview]`（378–379 行）。该文件字节以 `b"\x89PNG\r\n\x1a\n"` 开头（380–381 行）；打开后 `format == "PNG"`、`size == (2, 2)`、`(0, 0)` 为 `(12, 34, 56)`（382–385 行），与 `_png_bytes` 的 2×2 颜色一致（59–66 行）。`read_original(...) == payload`（386 行）。`tmp_path / "outside.png"` 与 `tmp_path / "escaped"` 不存在（387–388 行）。`HOSTILE` 没有名为 `escaped` 的字段；388 行只断言这条路径不存在。

这次测试没有 Alembic head 断言。它没有请求 GET，没有 Blender 断言，也没有 `fencing_token` 断言。它没有断言服务器像素常数的数值。

## 同一次阅读看到的路由

`routes.py` 全文到 243 行。`post_artifact_version_preview` 在 206–243 行。签名只有 `artifact_version_id` 和 `request`（207 行），函数体内没有读取请求 JSON。它先 `with Session(get_engine(request))`（210 行），在会话里解析主体、读取版本，并按 `Action.PREVIEW_READ` 调用 `_authorize`（215–221 行），然后取出 `_preview_facts`（222 行）。`AuthRequired`、`ActionForbidden`、`NotFoundOrHidden`、`OriginalUnreadable` 在渲染前返回 `_json_error`（223–224 行）。`render_registered_preview(` 在这个 try/except 之后（231–240 行），调用参数里没有会话；`max_pixels` 传入的是 `_PREVIEW_MAX_PIXELS`，值为 4194304（39、239 行）。相对路径由 `_server_relative_preview` 生成，形如 `previews/{artifact_version_id}.png`，并且要求 `is_storage_segment`（158–164 行）。`_configured_roots` 要求两个值都是现存目录（148–155 行）；根目录不可用或相对路径为 `None` 时返回 `_unavailable_preview`，不调用渲染（225–228 行）。`OSError` 与 `PathEscape` 同样返回这份不可用 JSON（241–242 行）。

`_public_preview` 与 `_preview_response` 只输出 `_PREVIEW_KEYS` 里的五个字段（40、178–193 行）。`_unavailable_preview` 的五个值与测试第 6 条的 JSON 相同（167–175 行）。成功响应的构建路径不插入磁盘路径字段。`_json_error` 的正文是 `error.code`、`message`、`details`、`retryable`、`request_id`（86–98 行）。这次阅读没有打开 `render_registered_preview` 的函数体。

这个文件里预览路径的处理函数是 POST。测试只断言 OpenAPI 里有 `"post"`，没有发 GET。

## blocking

blocking：0。

这次阅读和这次运行里，四条 blocking 条件都没有出现。路由函数在 `write_uow` 之外调用 `render_registered_preview`，测试 171 与 183 行通过。成功响应被断言为上述五个值，列出的路径字符串不在该响应的正文和头里，路由构建成功 JSON 时不加入路径字段。匿名调用方与只有 `artifact.original.read` 的成员得到 401 或 403，响应没有生成器字段，派生目录没有预览文件。成功之后 `read_original` 仍等于登记 payload（386 行），该测试通过。

Blender 预览、已有预览的 GET、pin 的 `fencing_token` 不计入 blocking。本测试没有这些断言。本次阅读不把它们写成已关闭。

T05 仍是 implementing。本文件不是完成声明。
