# HANDOFF-T09 候选 HTTP 切片

- 上游任务 / owner：T09 候选预览与确认。本切片只挂 HTTP。T09 不标 done。
- 集成基线：无新迁移。未改 `proposal.py`、`preview.py`、`commit.py`。未改三个动作的写入规则。
- 消费者：确认 UI 与后续验收。OpenAPI 只由 FastAPI 从路由生成，没有第二份手写规范。

## 改动文件

- `src/sinuo/api/candidates/__init__.py`
- `src/sinuo/api/candidates/routes.py`
- `src/sinuo/api/candidates/schemas.py`
- `src/sinuo/api/candidates/errors.py`
- `tests/acceptance/test_candidate_http.py`
- `src/sinuo/api/app.py`：两处调用，外加两行 import，使这两个名字可解析。其余行未动。
- 本交接。

`create_app` 里加的两行：

```python
app.include_router(candidate_router)
register_candidate_exception_handlers(app)
```

## 路由

前缀 `/api/v1`。路径没有 `project_id`。

- `POST /candidates/{candidate_id}/preview`
- `POST /candidates/{candidate_id}/commit`
- `POST /candidates/{candidate_id}/reject`

`project_id` 只读候选行。actor 只来自会话。Origin 与 CSRF 调用 `sinuo.api.versions.routes._guard`。调用方持有 `write_uow`，这三个处理函数自己不提交。

预览入参：`user_patch`、`expected_page_version_id`、`expected_draft_content_revision`。调用：

```python
preview_candidate(
    session,
    *,
    principal,
    project_id,
    candidate_id,
    now,
    user_patch=...,
    expected_page_version_id=...,
    expected_draft_content_revision=...,
)
```

写代码时 `preview_candidate` 还没有后两个关键字。仍按这个签名调用，没有改 `preview.py`。成功 `data` 含 `preview_id`、`preview_hash`、`expires_at`、`effective_proposal`、`preconditions`、`affected_objects`。`preview_hash` 是库里的 64 位十六进制，不加 `sha256:`。`expires_at` 转成 UTC RFC3339。预览每次新插一行，不读 `Idempotency-Key`，`meta` 只有 `request_id` 和服务端版本。

确认正文：`preview_id`、`preview_hash`、`expected_page_version_id`、`expected_draft_content_revision`。客户端若带 `user_patch`，原样传给 `commit_candidate`。确认和拒绝都要 `Idempotency-Key`，沿用 `_idempotency_key`。首次成功 201，同键同体 200。`meta` 带 `idempotency_replayed`；回执里有 `audit_event_id` 时写入 `meta`。`project_revision` 只在服务返回整数时写入 `meta`。`draft_decision` 不新造项目确认修订。拒绝只调现有 `reject_candidate`，原因可选。

同键异体仍由全局 `IdempotencyConflict` 处理器映射为 409 `IDEMPOTENCY_KEY_REUSED`。本切片没有再注册一遍。`AuthRequired`、`ActionForbidden`、`NotFoundOrHidden`、`SourceRedacted`、`InvalidSchema` 也沿用已有全局处理器。

## 错误码

`CandidateCommandError.code` 映射到已有 `ErrorCode`，失败包络走 `error_response`，`retryable` 为 false。没有新错误码。`not_implemented` 映射为 422 `INVALID_SCHEMA`。

| code | HTTP |
| --- | --- |
| AUTH_REQUIRED | 401 |
| ACTION_FORBIDDEN | 403 |
| NOT_FOUND_OR_HIDDEN | 404 |
| DRAFT_REVISION_CONFLICT / HEAD_CHANGED / IDEMPOTENCY_KEY_REUSED / CANDIDATE_ALREADY_RESOLVED / CANDIDATE_STALE / PREVIEW_CHANGED / PREVIEW_EXPIRED | 409 |
| SOURCE_REDACTED | 410 |
| INVALID_SCHEMA / INVALID_LINEAGE / INVALID_ANCHOR / not_implemented | 422 |

`error_response` 按 `_MESSAGES` 取英文短句。`CANDIDATE_ALREADY_RESOLVED`、`CANDIDATE_STALE`、`PREVIEW_CHANGED`、`PREVIEW_EXPIRED`、`INVALID_LINEAGE`、`INVALID_ANCHOR` 原先没有句子。注册处理器时用 `setdefault` 补上，不覆盖已有句子，也没有改 `src/sinuo/api/auth/errors.py`。

## 验证

先建了 `_施工文件/2026-11-01_t09http/99_临时文件`。pytest 的 `--basetemp` 不会创建缺失的父目录；第一次因此在收集阶段失败，6 errors，没有执行测试。父目录存在后，同一命令：

```text
.\.venv\Scripts\python.exe -m pytest -q tests/acceptance/test_candidate_http.py -o cache_dir=_施工文件/2026-11-01_t09http/99_临时文件/pytest-cache-a --basetemp=_施工文件/2026-11-01_t09http/99_临时文件/pytest-a
```

退出码 1。`3 passed`，`3 failed`，1 条既有 Starlette/httpx 弃用警告。未跑全库。未对 `var/sinuo.db` 做 upgrade。未发网络，未跑 `live_model`。未 git commit。

通过：拒绝并同键回放、匿名三条命令 401、OpenAPI 每条路由至少有一个成功例子和一个 4xx 例子。

失败：预览、确认（含同键同体回放）、错哈希。三处都是 `preview_candidate() got an unexpected keyword argument 'expected_page_version_id'`。错哈希没有打到 `PREVIEW_CHANGED` 处理器。拒绝路径不调用预览，所以通过了。

测试使用身份夹具登录、`Origin: http://127.0.0.1:8000` 和 CSRF，合成 `draft_decision` 候选，数据库在 pytest 临时目录，不指向 `var/`。

## 下游

`preview_candidate` 加上 `expected_page_version_id: str | None = None` 和 `expected_draft_content_revision: int | None = None` 之后，重跑上面的 pytest。预期失败的三条变为通过。T09 仍不标 done。
