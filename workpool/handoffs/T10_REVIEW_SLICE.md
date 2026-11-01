# HANDOFF-T10 授权审阅意见

- 上游任务 / owner：T10 第一段，只要授权审阅意见。T10 是 implementing，不标 done。T05、T06、T08 仍是 implementing。T07、T09 仍是 review，不标 done。
- 没做：导出、备份恢复、普遍团队评论、交接、3D、`live_model`、Git commit。没改假模型链、`scripts/seed_night_demo.py`、`workpool/demo-5min/`。
- 没有新迁移。`review_opinions` 现有列够用。head 仍是 `0010_evidence_index`。

## 对照

`docs/construction/04_API_AND_EVENTS.md` 的两条写路径，以及 `03_DOMAIN_AND_TRANSACTIONS.md` 的 TX-08。验收第 1 条里属于意见的部分：非作者和失权者不能改原话；意见指向原页版本，后续版本不改绑。

TX-08 的原子写是意见、审计、回执。建议把审阅纳入项目修订。表上没有 `project_revision` 列，所以只把已有的 `projects.revision` 加 1，并写进 `audit_events.project_revision`。不改页版本、不改节点、不写 Outbox。

本仓库没有另设理论库。施工合同就是这条行为的依据。

## 路由

`create_app` 挂上 `review_router`。身份、Origin、CSRF、`Idempotency-Key` 调用版本路由的 `_guard` 和 `_idempotency_key`。

- `POST /api/v1/projects/{project_id}/review-opinions`：`opinion.create`。项目号只取路径。作者只取会话，正文里的 `author_account_id` 会被拒绝。`page_version_id` 必须属于该项目。`node_id` 可空；有值时必须是该页版本里的节点，否则 422 `INVALID_ANCHOR`。
- `PATCH /api/v1/review-opinions/{opinion_id}`：`opinion.edit_own`。项目号和作者只取意见行。条件更新要求当前 `revision`，只改 `body`、`revision`、`updated_at`。`page_version_id`、`node_id`、作者写不进去；多出来的字段直接 422。
- `GET /api/v1/review-opinions/{opinion_id}`：给授权范围内的阅读。门槛是锚定页版本上的 `page.read`。这不是 04 表里的第三条写命令，也不是团队评论。没有这条读路径，别人就看不到已经留下的意见。

`visibility` 只接受 `project` 和 `restricted`，原样存、原样返回。读不按这个字段再切一套人。合同没有单独的意见读动作，授权范围就是现有页读授权。

意见修订对不上时返回 409 `DRAFT_REVISION_CONFLICT`，`details.category` 为 `opinion_revision`。没有新错误码。成功回执里的正文和修订号不经过页回执投影，否则 `revision` 和 `body` 会被裁掉，作者无法做下一次 CAS。撤权后的重放仍先重新授权，过不了就不返回已存正文。

作者修改会覆盖 `body` 并把 `revision` 加 1。表只有一列正文，触发器也不许改锚定版本，所以旧话不另存一行。

## 验证

```text
.venv/Scripts/python.exe -m pytest -q tests/acceptance/test_review_opinions.py -o cache_dir=_施工文件/2026-11-01_t10review/99_临时文件/pytest-cache --basetemp=_施工文件/2026-11-01_t10review/99_临时文件/pytest
```

退出码 0。5 passed，1 warning。警告是既有的 Starlette/httpx 弃用。未跑全库，未跑 `live_model`。

测试名：

- `test_author_edits_own_opinion`
- `test_non_author_cannot_edit_opinion`
- `test_revoked_author_cannot_edit_opinion`
- `test_later_page_version_keeps_the_original_anchor`
- `test_page_reader_can_read_and_outsider_cannot`

## 还没做

导出、原件包、备份恢复，以及 T10 验收第 2 到第 6 条。普遍团队评论和交接仍是 P1。
