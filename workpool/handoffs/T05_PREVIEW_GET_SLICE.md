# T05 预览读取切片

状态保持 implementing。这只读取已经生成的派生 PNG，不是 T05 完成。

## 对照

合同 `workpool/handoffs/T05_PREVIEW_GET_CONTRACT.md`（`T05-PREVIEW-GET-1`）。GET 先按当前 `preview.read` 授权，再读已有 PNG。不调用渲染，不打开 `write_uow`，文件缺失时不补写。响应文件名固定为 `preview.png`，不使用作品标题。无权、匿名、版本不存在或文件不存在时，响应不含标题、预览字节和磁盘路径。

## 行为

`GET /api/v1/artifact-versions/{artifact_version_id}/preview` 在短只读会话中解析主体，确认版本存在，并按 `preview.read` 授权。会话关闭之后才读取 `app.state.derived_root` 下由服务端生成的 `previews/{artifact_version_id}.png`。路径经 `resolve_contained`。不读取作品标题，不调用 `render_registered_preview`，不打开 `write_uow`。

有权且该文件已存在、并以 PNG 签名开头时，返回文件字节。`Content-Type` 为 `image/png`，`Cache-Control: no-store`，`X-Content-Type-Options: nosniff`，`Content-Disposition` 为 `inline; filename="preview.png"`。

派生根目录不可用、相对路径不能落在根内、文件不存在或内容不是 PNG 时，返回 404，不创建文件。版本不存在或授权结果为隐藏时同样是 404。没有 `preview.read` 的调用方得到 403，正文不是 PNG。匿名是 401。这些响应不含作品标题和磁盘路径。

POST 的渲染行为、`register_original` 和迁移都未改。

## 命令

工作目录是仓库根 `sinuo-platform`。先建 `_施工文件/2026-10-31_grok-dev-a5/99_临时文件`，再执行：

```text
.venv/Scripts/python.exe -m pytest -q tests/acceptance/test_preview_http.py -o cache_dir=_施工文件/2026-10-31_grok-dev-a5/99_临时文件/pytest-cache --basetemp=_施工文件/2026-10-31_grok-dev-a5/99_临时文件/pytest
```

GET 断言加在既有 POST 测试里。POST 写出预览之后，持有 `preview.read` 的 GET 返回 PNG 签名和 `nosniff`，文件名是 `preview.png`。没有 `preview.read` 的调用方得到 403，正文不是 PNG。预览文件还不存在时，有权 GET 不创建文件。匿名是 401。不存在的版本是 404，响应不含标题。

## 退出码

0

## 通过数

1 passed。输出里没有 failed，也没有 skipped。另有一条既有的 Starlette `httpx` 弃用警告。

## 仍未完成

Blender 预览仍未做。没有安装 Blender。T05 状态仍是 implementing。
