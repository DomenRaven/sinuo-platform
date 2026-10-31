# 04 API、命令与事件通信合同

状态：施工基线，2026-10-29。目标：后端能被 CLI、集成测试和未来 UI 调用；本阶段不制作 UI。所有类型以 Pydantic 2 为代码真源，FastAPI 生成 OpenAPI；本文规定不可变语义，不要求维护第二份手写 OpenAPI。

## 通用请求规则

根路径 `/api/v1`；JSON UTF-8；UTC RFC3339 时间；公开 ID 使用 UUID；列表游标分页，默认 50、最大 100。服务端从已验证凭据确定 actor，忽略客户端自报 `role`、`owner_id` 和 `allowed_tools`。项目范围必须在服务端绑定到对象，嵌套 URL 不能仅检查最外层 ID。

每个业务修改命令带 `Idempotency-Key`（登录、登出等身份生命周期按 06 的例外处理），服务端生成或验证 `request_id` 作为追踪号，两者含义分开。日志贯通 `request_id`、`trace_id`、`project_id`、`run_id`、`task_id`，日志无密钥、完整提示词或受限原文。配置仅从服务端读取。身份替换只在 pytest 依赖注入中启用，不提供开发身份 HTTP 旁路；若未来增加开发认证配置，生产配置启动时必须拒绝它。

成功响应通用包络：`data` 和 `meta`。`meta` 包括 `request_id`、服务端版本；已确认业务提交再带 `project_revision`、`audit_event_id`、`idempotency_replayed`。草稿保存返回内容修订/哈希，不能伪造新项目确认修订。错误包络固定 `error.code`、安全 `message`、可见 `details`、`retryable`、`request_id`，不得直接输出 SQL、文件路径或供应商密钥。

## P0 端点清单

| 方法与端点 | 关键输入 | 结果和权限 |
| --- | --- | --- |
| POST `/projects` | 标题、简报、模板、幂等键 | 项目、首阶段和空白页初版；账号创建权 |
| GET `/projects` | cursor、limit | 当前成员可见摘要 |
| GET `/projects/{p}` | 可选 at_project_revision | 按当前权限裁剪的项目路径 |
| POST `/projects/{p}/stages` | 阶段目标/顺序、expected_project_revision | 新配置版；stage.configure |
| GET `/projects/{p}/stages/{s}` | at_project_revision | StageViewSnapshot；无业务写入 |
| POST `/projects/{p}/pages` | stage_id、标题、空白模式 | 新平行页和 v1；page.create；不自动连边 |
| GET `/pages/{g}` | version_id 可选 | 当前草稿+HEAD 或确切历史；page.read |
| PATCH `/pages/{g}/draft` | base_version_id、expected_draft_content_revision、类型化 operations | 新修订/哈希或 409；page.draft.write |
| PUT `/pages/{g}/view-state` | 个人布局、expected_layout_revision | 个人视图；不改变候选有效性 |
| POST `/pages/{g}/checkpoints` | expected HEAD、草稿修订/哈希、摘要 | 新确认版；page.checkpoint |
| POST `/pages/{g}/branches` | source_version_id、target_stage_id、标题、继承范围 | 新页/初版/来源映射；page.branch |
| GET `/pages/{g}/history` | cursor | 不可变版本与有权谱系 |
| GET `/projects/{p}/suggestion-preferences/me` | 无 | 只读本人的频率与范围；project.read |
| PATCH `/projects/{p}/suggestion-preferences/me` | mode、scope、cooldown_seconds、digest_interval_seconds、expected_revision | 本人设置 CAS；run.create；独立 preference revision，不改设计 ProjectRevision |
| POST `/projects/{p}/agent-runs` | 输入页/版、input_mode、草稿修订可选、intent、所选范围 | 202 run_id、snapshot_id、events_url；run.create |
| GET `/agent-runs/{r}` | 无 | 授权后的运行快照与任务状态 |
| POST `/agent-runs/{r}/cancel` | 原因可选 | 取消执行；不撤销已提交业务 |
| GET `/agent-runs/{r}/events` | Last-Event-ID | SSE 当前授权重放 |
| POST `/candidates/{c}/preview` | user_patch、expected 版本 | 预览 ID/哈希、有效提案和影响范围；无业务提交 |
| POST `/candidates/{c}/commit` | preview_id、preview_hash、expected 版本 | 201 首次结果/200 幂等回放；按提案要求的动作权 |
| POST `/candidates/{c}/reject` | 原因可选 | 候选处置；业务图不变 |
| POST `/projects/{p}/artifacts/uploads` | 逻辑 artifact_id 可选、名称、声明 MIME/size/hash、许可/敏感度 | 隔离上传票据；artifact.upload |
| PUT `/uploads/{u}/content` | 字节流、长度 | 暂存状态；票据归属/限额 |
| POST `/uploads/{u}/complete` | 票据与声明 hash | 校验后 ArtifactVersion；派生为 pending |
| GET `/artifact-versions/{v}` | 无 | 当前授权的元数据及各派生状态 |
| GET `/artifact-versions/{v}/content` | 无 | 每次授权下载；不返回文件系统路径 |
| GET `/artifact-versions/{v}/preview` | generator_version 可选 | 授权派生件；无权不能推断标题 |
| POST `/projects/{p}/evidence-anchors` | 原件版、locator、摘录 | 有效定位锚点；evidence.create |
| GET `/projects/{p}/search` | query、scope、cursor | ACL 过滤的片段与已查/未索引范围 |
| POST `/projects/{p}/test-records` | artifact_version_id、条件、观察/结果、来源 | 确切原件版测试；test.record |
| POST `/projects/{p}/review-opinions` | 页/节点版本、意见、可见性 | 自己的意见；opinion.create |
| PATCH `/review-opinions/{o}` | expected_revision、正文 | 作者且仍获授权；opinion.edit_own |
| POST `/projects/{p}/exports` | source manifest、格式、确认范围 | 202 export_job_id；report.export |
| GET `/exports/{e}` 与 `/content` | 无 | 任务/下载均当前授权，标输入快照 |
| GET `/commands/{request_id}` | 无 | 调用者有权的提交状态与结果引用 |
| PATCH `/projects/{p}/access` | 授权变更、expected_access_epoch | 当前 ACL 新纪元；acl.manage |

每个端点都要在 OpenAPI 中给一个成功和一个关键失败例子。实现过程中若拆路由以降低耦合，需保留本文动作语义和生成的契约测试。最终 UI 未开发，不妨碍端点完成与验证。

## 版本与 Schema

业务对象的身份和版本要分开。`page_id` 可稳定不变，`page_version_id` 每次确认新建；`artifact_id` 稳定，`artifact_version_id` 不变。任何“latest”参数只能用于浏览，提交前必须转成确切 ID，并带预期 HEAD/CAS 条件。

候选动作是 discriminated union：`add_node`、`add_typed_edge`、`attach_artifact_version`、`draft_decision`、`suggest_stage_transition`、`create_page_from_versions`。建页模式必须明确为 `stage_blank | history_branch | merge_blank`。用户可修改提案内容，但动作种类、授权要求和来源校验仍由服务器判定。

P0 的版本对象和事件用 `schema_version=1`。新增可选字段可兼容；删除/改变含义需新 Schema 版本或 API 大版本与迁移。未知动作、额外写权限字段和未知 Schema 版本拒绝，不能让 Pydantic 默认忽略决定权限的字段。

## 预览与提交示例

```json
{
  "candidate_id": "c-id",
  "user_patch": {"target_title": "夜间取物需求定义"},
  "expected_page_version_id": "pv-3",
  "expected_draft_content_revision": 8
}
```

预览响应含 `preview_id`、`preview_hash`、`expires_at`、`effective_proposal`、`preconditions`、`affected_objects`。`preview_hash` 绑定 actor、候选修订、完整有效提案、全部来源版本、草稿修订、当前权限纪元、到期时间。提交只引用服务器保存的预览，不接受客户端替换计算结果。

```json
{
  "preview_id": "preview-id",
  "preview_hash": "sha256:...",
  "expected_page_version_id": "pv-3",
  "expected_draft_content_revision": 8
}
```

`POST /candidates/{c}/commit` 使用新的命令幂等键；断网后重发同一请求体和键。所有 sources 都要重新校验，不能只查当前页。读取旧历史后分支是有意使用旧来源，不要求旧来源等于 HEAD；阶段推进候选则要求生成时声明的当前来源仍有效。

## 事件信封与传递

```json
{
  "schema_version": 1,
  "event_id": "event-id",
  "seq": 12,
  "type": "candidate.ready",
  "project_id": "project-id",
  "run_id": "run-id",
  "task_id": null,
  "trace_id": "trace-id",
  "input_snapshot_id": "snapshot-id",
  "occurred_at": "2026-10-29T10:00:00Z",
  "payload": {"candidate_id": "candidate-id", "status": "pending"}
}
```

`seq` 仅在一个 run 的事件流内单调，数据库 `UNIQUE(run_id,seq)`；序号分配在短事务中原子完成。事件是已发生的事实，命令是请求执行的意图，二者的类型与处理函数分开。`event_id` 全局唯一，用于消费者去重。运行事件持久化后再发送 SSE；进程内队列仅用作唤醒提示，丢通知也能从持久表补读。

允许的运行事件：`run.received/context_scoped/planned/started/completed/partial/failed/cancelled`、`task.started/completed/failed/retry_scheduled`、`tool.started/completed/failed`、`candidate.ready/expired/committed/rejected`、`access.changed`。业务 Outbox 另含 `page.version_created`、`artifact.version_registered`、`artifact.redacted`、`project.access_changed`。事件载荷使用对象引用、安全计数和错误类别，原文与模型敏感输出走授权详情接口。

服务内部同步读采用类型化函数；异步任务由 DB Job/Outbox 驱动。P0 不需要 Kafka、Redis、WebSocket 或自由自然语言角色消息总线。未来替换消息传输层时保留消息 ID、幂等键、Schema、租约和权限字段。

## SSE 重连与权限

`Last-Event-ID` 必须归属当前 run；非法或跨项目 ID 返回明确错误，不能拿作任意游标。事件保留期内按序回放；游标已被清理则发安全 `stream.reset_required` 并让客户端 GET 运行快照。心跳只保持连接，不表示任务有进展。

订阅绑定经认证主体、项目与运行。建立、重连、批量回放和发出新数据前检查当前访问纪元；ACL 变化唤醒并关闭/降级受影响连接。使用 Cookie 时采取 SameSite、CSRF/Origin 防护；若需要临时流令牌，令牌只能打开一个主体的指定 run，短期有效，日志不记录明文。过去已经下载或在网络传输中的字节无法召回；验收保证撤权后的新受控读取拒绝，已打开界面刷新清理。

## 错误与重试约定

| HTTP / code | 含义 | 自动重试 |
| --- | --- | --- |
| 401 AUTH_REQUIRED | 身份失效 | 重新认证后重试原键 |
| 403 ACTION_FORBIDDEN / 404 NOT_FOUND_OR_HIDDEN | 当前无权限或对象不可见 | 否 |
| 409 DRAFT_REVISION_CONFLICT / HEAD_CHANGED | 旧内容预条件失效 | 否，先比较/重检 |
| 409 IDEMPOTENCY_KEY_REUSED | 同键不同正文 | 否 |
| 409 CANDIDATE_ALREADY_RESOLVED / CANDIDATE_STALE | 已处置或候选过期 | 否 |
| 409 PREVIEW_CHANGED / PREVIEW_EXPIRED | 预览绑定不符或到期 | 重新预览 |
| 410 SOURCE_REDACTED | 依据已合法删除 | 否 |
| 413 UPLOAD_TOO_LARGE | 超过项目/请求上限 | 否 |
| 422 INVALID_LINEAGE / INVALID_ANCHOR / INVALID_SCHEMA | 类型或关系不合法 | 修正输入 |
| 429 REQUEST_BUDGET_EXCEEDED | 本地并发/调用预算满 | Retry-After 内有限重试 |
| 503 STORAGE_BUSY / PROVIDER_UNAVAILABLE | 临时数据库/提供方故障 | 保持原键，有界退避 |

HTTP 成功响应是业务提交的确认来源。SSE 的 `candidate.ready` 仅说明候选就绪。查询超时与写响应丢失要分别处理；后者先查回执，避免盲目换键提交。
