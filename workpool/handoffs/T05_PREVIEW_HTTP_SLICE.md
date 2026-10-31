# T05 预览 HTTP 切片

状态保持 implementing。这只是授权后、在写事务外渲染的一刀，不是 T05 完成。

## 对照

合同 `workpool/handoffs/T05_PREVIEW_HTTP_CONTRACT.md`（`T05-PREVIEW-HTTP-1`）和 `docs/construction/06_SECURITY_STORAGE_RECOVERY.md`。06 要求预览按当前 `preview.read` 重新检查，响应不暴露磁盘路径；数据库与文件系统不能组成单一事务，转换不放进写锁，预览失败不影响原件取回。像素上限、对象键和派生路径由服务端决定。本仓库没有单独的预览 HTTP 理论条目。04 清单里的 `GET /artifact-versions/{v}/preview` 是另一条读取派生件的入口，本切片不实现它。

路由加在已经由 `create_app` 挂上的资产路由上，沿用下载路由的会话 cookie 和 `SessionAuthorizer`。不新开 `write_uow`，因此这条 POST 不在只读会话里轮换 CSRF。

## 行为

`POST /api/v1/artifact-versions/{artifact_version_id}/preview` 在短只读会话中解析主体，确认版本存在，按 `preview.read` 授权，并读出项目、内容哈希、generation 和媒体类型。会话关闭之后才调用 `render_registered_preview`。像素上限固定为 4194304。相对路径固定为 `previews/{artifact_version_id}.png`，版本号必须是存储段。请求 JSON 不读取，其中的路径和 `max_pixels` 不决定输出。

响应只有 `generated`、`state`、`reason`、`generator_name`、`generator_version`。没有 `objects_root` 或 `derived_root`，或者二者不是现存目录时，不调用渲染，返回 `generated=false`、`state=unavailable`、`reason=storage_not_configured`，生成器名称为空。响应不含磁盘路径。

持有该版本 `preview.read` 的主体，对一张 2×2 PNG 得到 `ready` / `rendered`，派生目录写出 PNG，原件字节不变。只有 `artifact.original.read` 的成员得到 403，响应里没有 Pillow、生成器字段或预览文件。匿名是 401。`register_original`、`preview_disposition`、`run_preview_converter` 和迁移都未改。

## 命令

工作目录是仓库根 `sinuo-platform`。先建 `_施工文件/2026-10-31_grok-dev-a4/99_临时文件`，再执行：

```text
.venv/Scripts/python.exe -m pytest -q tests/acceptance/test_preview_http.py tests/acceptance/test_registered_preview.py -o cache_dir=_施工文件/2026-10-31_grok-dev-a4/99_临时文件/pytest-cache --basetemp=_施工文件/2026-10-31_grok-dev-a4/99_临时文件/pytest
```

新测试用既有的 `write_uow` 登记一张小 PNG，再经 `create_app` 调用路由。`write_uow` 不包住渲染。没有断言新的 Alembic head。

## 退出码

0

## 通过数

3 passed。输出里没有 failed，也没有 skipped。另有一条既有的 Starlette `httpx` 弃用警告。

## 仍未完成

Blender 预览仍未做。没有占位图。T05 状态仍是 implementing。
