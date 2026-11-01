# HANDOFF-T10 快照导出

- 上游任务 / owner：T10 第二段，只要快照导出。T10 是 implementing，不标 done。备份恢复没有做。T05、T06、T08 仍是 implementing。T07、T09 仍是 review，不标 done。
- 没做：备份恢复、DOCX、普遍团队评论、交接、3D、`live_model`、Git commit。没改假模型链、`scripts/seed_night_demo.py`、`workpool/demo-5min/`。没改审阅意见的写入规则。
- 没有新迁移。导出任务用现有 `jobs` 表，`job_kind='export'`。来源清单和当时的权限范围冻在该行的 `payload` 里，之后不改这列。head 仍是 `0010_evidence_index`。

## 对照

`docs/construction/04_API_AND_EVENTS.md` 的 `POST /projects/{p}/exports` 与 `GET /exports/{e}`、`/content`。`06_SECURITY_STORAGE_RECOVERY.md` 里导出要 `report.export`，生成和下载再查当前权限。验收第 2、3 条。

`pyproject.toml` 没有写 docx 的依赖，所以没有 DOCX。首版产物是可读 Markdown 和一份 JSON 清单。报告草稿是带来源的固定模板，不调模型。

## 路由

`create_app` 挂上 `export_router`。身份、Origin、CSRF、`Idempotency-Key` 调用版本路由的 `_guard` 和 `_idempotency_key`。

- `POST /api/v1/projects/{project_id}/exports`：`report.export`。202，返回 `export_job_id` 和 `input_snapshot_id`。`latest` 固定为 false。同一启动键、同一正文只登记一个 `export` 任务；同键异体是已有的 `IDEMPOTENCY_KEY_REUSED`。`format` 只接受 `markdown`，`docx` 走已有的 `INVALID_SCHEMA`。
- `GET /api/v1/exports/{export_id}`：再查当前 `report.export`。返回任务状态和冻结的来源清单，并标输入快照。来源后来变了，这里仍是当时的页版本，不冒充最新。
- `GET /api/v1/exports/{export_id}/content`：下载前再查当前权限。响应是按当前授权重新拼出的 zip，不把磁盘上的旧包直接送出去。清单里的文件哈希对得上包内文件。

POST 只登记任务。生成由 `run_export_job` 认领现有 `jobs` 行：先 `claim_job`，重查 `report.export`，再按当前权限写 `exports/<export_job_id>/<manifest_hash>/`。撤掉 `report.export` 之后认领，任务记 `failed`，`last_error` 用已有的 `unauthorized`，不发布包。

可选原件要逐件有 `artifact.original.read`。缩略图要 `preview.read`，摘录要 `evidence.read`，页要 `page.read`。没权的原件、缩略图和摘录不进包。敏感作品上的 `preview.read` 本来就会被拒绝，缩略图因此不进包。缺失或墓碑的原件在清单里标 `missing` 或 `tombstoned`，不写文件哈希，也不把正文放进包。

冻结的 `permission_scope` 只说明当时的输入。执行、生成和下载都不拿它当授权。

## 验证

```text
.venv/Scripts/python.exe -m pytest -q tests/acceptance/test_review_export_recovery.py -o cache_dir=_施工文件/2026-11-01_t10export/99_临时文件/pytest-cache --basetemp=_施工文件/2026-11-01_t10export/99_临时文件/pytest
```

退出码 0。3 passed，1 warning。警告是既有的 Starlette/httpx 弃用。未跑全库，未跑 `live_model`。

测试名：

- `test_export_scope_originals_thumbnails_and_excerpts_follow_acl`
- `test_revoke_during_export_refuses_restricted_download`
- `test_same_export_key_registers_one_job_and_file_hashes_match`

## 还没做

备份恢复，以及 T10 验收第 4 到第 6 条。普遍团队评论和交接仍是 P1。
