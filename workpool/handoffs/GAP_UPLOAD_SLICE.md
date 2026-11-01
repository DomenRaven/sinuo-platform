# F06 上传 HTTP 切片

- 上游：白皮书 F06。上传原件后记下哈希和元数据，更新不覆盖旧版。任务卡状态没有改成 done。
- 没做：3D、预览转换、`live_model`、Git commit。没改 `app.py`、`execute.py`、`fake_chain.py`、现有 `src/sinuo/api/assets/routes.py`。
- 没有新迁移。head 仍是 `0010_evidence_index`。

## 对照

`docs/construction/04_API_AND_EVENTS.md` 的三条上传命令，以及 `06_SECURITY_STORAGE_RECOVERY.md` 的隔离暂存。登记仍调用已有的 `request_upload_ticket` 和 `register_original`。权限是已有的 `artifact.upload`。身份、Origin、CSRF、`Idempotency-Key` 调用版本路由的 `_guard` 和 `_idempotency_key`。

没有作品时，`ensure_artifact` 先插入一行 `artifacts`，再开票据。同一个项目里同名作品复用最早的那一行。显式 `artifact_id` 必须已经属于该项目，而且标题与 `name` 一致。新字节始终是新的 `artifact_version_id` 和新的 `version_no`。旧行的 `content_hash` 不改。另一个项目里的同名作品是另一行。

## 路由

`create_app` 没有挂这三条。测试在 `create_app()` 之后自己 `include_router`。以后要进运行中的应用，只加这一行：`app.include_router(router)`，`router` 来自 `sinuo.api.assets.uploads`。

暂存目录和原件目录也不在 `create_app` 里。调用前要把现存目录放到 `app.state.uploads_root` 和 `app.state.objects_root`。缺目录时 PUT 和 complete 返回 503 `STORAGE_BUSY`，不写文件。

- `POST /api/v1/projects/{project_id}/artifacts/uploads`：`artifact.upload`。正文是名称、声明 MIME、`size`、`declared_hash`、许可、敏感度和 `ai_processing_allowed`。`artifact_id` 可空。服务端字节上限 8 MiB，票据 3600 秒后到期。成功 201，返回 `ticket_id` 与 `artifact_id`。新名称时 `artifact_created` 为 true。
- `PUT /api/v1/uploads/{ticket_id}/content`：原始字节。长度必须等于票据上的 `size`，并且不超过上限。字节写到 `uploads/<ticket_id>/attempt.part`，写在短事务外面。同键同字节回放已有暂存结果。已有暂存字节不同则 409，不覆盖那个文件。
- `POST /api/v1/uploads/{ticket_id}/complete`：再交声明哈希，调用 `register_original`。成功 201，带新版本号、内容哈希、项目修订和审计号。`preview_generated` 为 false，`derivative_status` 为 `pending`。这里不跑转换器，也不写预览文件。响应不含 `object_key` 和磁盘路径。

同键异体是已有的 409 `IDEMPOTENCY_KEY_REUSED`。匿名 401。缺 Origin 或缺 CSRF 是 403，`details.category` 为 `origin` 或 `csrf`。项目成员没有 `artifact.upload` 时 403，`details.action` 为 `artifact.upload`。跨项目是 404 `NOT_FOUND_OR_HIDDEN`。声明长度超过上限是 413 `UPLOAD_TOO_LARGE`，不建作品、不写暂存。暂存字节和声明哈希不一致时，票据记为 rejected，422，`details.category` 为 `declared_hash_mismatch`，不登记版本，不安装原件。已关闭的票据再完成是 409 `HEAD_CHANGED`，`details.category` 为 `ticket_closed`。

## 验证

先建 `_施工文件/2026-11-01_gap_upload/99_临时文件`，否则 pytest 的 `--basetemp` 在 Windows 上找不到父目录。然后：

```text
.venv/Scripts/python.exe -m pytest -q tests/acceptance/test_upload_http.py -o cache_dir=_施工文件/2026-11-01_gap_upload/99_临时文件/pytest-cache --basetemp=_施工文件/2026-11-01_gap_upload/99_临时文件/pytest
```

退出码 0。3 passed，1 warning。警告是既有的 Starlette/httpx 弃用。未跑全库，未跑 `live_model`。

测试名：

- `test_create_app_leaves_upload_routes_unmounted`
- `test_same_name_new_bytes_keep_the_previous_hash`
- `test_upload_refuses_unpermitted_callers_and_wrong_bytes`

同名两次上传后，版本 1 的哈希仍是第一份字节，版本 2 是第二份。两份都能经已有的 `GET /api/v1/artifact-versions/{artifact_version_id}/content` 取回。敏感作品的旧哈希不变。

## 还没做

运行中的 `create_app` 仍不提供这三条。下载、预览 GET/POST 仍走原来的资产路由。没有自动预览，没有 3D。F06 的任务卡没有标 done。
