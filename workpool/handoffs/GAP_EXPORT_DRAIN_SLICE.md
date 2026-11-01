# GAP 导出从 queued 到可下载

- 上游任务：T10 快照导出。T10 仍是 implementing，本切片不标 done。T05、T06、T08 仍是 implementing。T07、T09 仍是 review。
- 没做：备份恢复、DOCX、普遍团队评论、交接、3D、`live_model`、Git commit。没改 `package.py` 已有函数、`app.py`、审阅路由、`tests/acceptance/test_review_export_recovery.py`。没有新迁移。head 仍是 `0010_evidence_index`。

## 对照

`docs/construction/04_API_AND_EVENTS.md` 的 `POST /projects/{p}/exports` 只登记。`GET /exports/{e}/content` 在任务还不是 `succeeded` 时是 404。`05_WORKFLOW_AND_CONCURRENCY.md` 的认领条件是到期的 `queued`，时钟用数据库的 `julianday('now')`。`06_SECURITY_STORAGE_RECOVERY.md` 与验收 R04：生成和下载再查当前权限，旧包不能变成受限内容的旁路。

`open_export` 把 `available_at` 写成 `0.0`，所以登记后的导出对这次 drain 已经到期。认领、写包、收尾仍在 `run_export_job` 里。

理论缺口：`F:/工业设计理论库` 是设计心理学和用户体验课件，没有任务租约或导出工人的文献。本切片不把课件当成依据，也没有新下载文献。行为以施工合同和已有 `claim_job` / `run_export_job` 为准。

## 选型

新增 `drain_export_jobs(engine, exports_root, objects_root, derived_root)`。一次短读列出 `job_kind='export'`、`status='queued'`、`available_at` 不晚于数据库时钟的 id，按 `created_at`、`id` 排序，然后逐个调用已有 `run_export_job`。返回这些 `ExportRun`。

drain 自己不再先 `claim_job`。`run_export_job` 会认领；先认领会使第二次 claim 因租约未过期被拒绝。未到期的 queued export 和其他 `job_kind` 留在原状态。没有把生成挂进 POST 或 `create_app`，因为登记合同仍是只入队，本切片也不改路由。

两个 drain 同时抢到同一行时，后一次 `claim_job` 按已有的 `StaleLease` 失败。这里没有再包重试。

## 验证

```text
.venv/Scripts/python.exe -m pytest -q tests/acceptance/test_export_drain.py -o cache_dir=_施工文件/2026-11-01_gap_export/99_临时文件/pytest-cache --basetemp=_施工文件/2026-11-01_gap_export/99_临时文件/pytest
```

退出码 0。1 passed，1 warning。警告是既有的 Starlette/httpx 弃用。未跑全库，未跑 `live_model`。没有 Git commit。

测试名：`test_drain_queued_export_download_matches_manifest_and_revoke_omits`。

它登记一个导出，drain 之前 `GET /content` 是 404。drain 之后用这条下载路径拿到 zip。清单里的文件哈希对得上包内文件，`manifest.json` 的哈希等于任务的 `output_ref`。撤掉原件、预览、摘录和页的读取之后，同一次下载不再带这些受限正文，清单哈希仍对得上当时包内文件。磁盘上 drain 写下的旧包还在，下载没有把那份旧包直接送出。未到期的 export 和一条 `derivative` 任务仍是 `queued`，第二次 drain 是空的。

## 还没做

备份恢复，以及 T10 验收第 4 到第 6 条。普遍团队评论和交接仍是 P1。drain 没有进程或 HTTP 入口。
