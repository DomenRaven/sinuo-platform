# HANDOFF 证据锚点与测试记录写入

- 上游：白皮书 F08 的写入入口，并带上 F17 对页码、时码、区域的定位要求。任务不标 done。
- 没改 `app.py`、`uploads.py`、`execute.py`、`fake_chain.py`。没有 Git commit，没有跑 `live_model`。
- 没有新迁移。`evidence_anchors` 和 `test_records` 现有列够用。

## 路由

`create_app` 不挂这两条路由。验收测试在 `create_app()` 之后自己 `include_router`。

- `POST /api/v1/projects/{project_id}/evidence-anchors`
- `POST /api/v1/projects/{project_id}/test-records`

身份、Origin、CSRF、`Idempotency-Key` 调用版本路由的 `_guard` 和 `_idempotency_key`。项目号只取路径。作者只取会话。正文里的 `created_by_account_id`、`account_id` 会被拒绝。

权限动作是已有的 `evidence.create` 和 `test.record`。检查的资源范围是项目，对应主设计师的项目级授权。作品版本必须属于这个项目；`latest`、`head`、`current`、`newest` 返回 422 `INVALID_ANCHOR`。别的项目的版本返回 404 `NOT_FOUND_OR_HIDDEN`。

## 证据锚点

写入确切 `artifact_version_id`。locator 里保存摘录，并恰好带页码、区域、时码三者之一。`excerpt_hash` 是摘录 UTF-8 的 SHA-256。敏感原件的锚点沿用该版本的 `sensitivity`，可见性只能是 `restricted`。

更正是新行，`supersedes_id` 指向旧行。旧行的摘录、页码和版本号保持原样。更正如果改绑到另一个作品版本，返回 422 `INVALID_ANCHOR`，不插入新行。

成功回执带 locator 和摘录哈希，不经过页回执投影。审计只记锚点、作品版本、locator 类型、摘录哈希和项目修订，不记摘录正文。每次成功提交把 `projects.revision` 加 1。同键同体回放返回同一锚点，`idempotency_replayed` 为 true，不再加修订。同键异体返回 409 `IDEMPOTENCY_KEY_REUSED`。

## 测试记录

写入确切 `artifact_version_id`，以及条件、观察、结果、来源。另一作品版本的测试是另一行，原来那一行仍指向原来的版本。更正同样是新行，`supersedes_id` 指向旧行，作品版本保持不变。改绑返回 422 `INVALID_ANCHOR`。

## 验证

```text
.venv/Scripts/python.exe -m pytest -q tests/acceptance/test_evidence_records_http.py -o cache_dir=_施工文件/2026-11-01_gap_evidence/99_临时文件/pytest-cache --basetemp=_施工文件/2026-11-01_gap_evidence/99_临时文件/pytest
```

退出码 0。3 passed，1 warning。警告是既有的 Starlette/httpx 弃用。未跑全库，未跑 `live_model`。

测试名：

- `test_caller_mounts_evidence_and_record_routes`
- `test_anchor_binds_exact_version_and_correction_appends`
- `test_record_binds_exact_version_and_correction_appends`

## 还没做

检索索引不会在这次写入时重建，新锚点要等 `rebuild_evidence_index` 才能被 `search_evidence` 查到。没有发出 `test.record_confirmed`，也没有写 Outbox。从方案节点追到要求再追到原件的整条回链，以及证据为空时说明已查范围，都不在这次写入入口里。
