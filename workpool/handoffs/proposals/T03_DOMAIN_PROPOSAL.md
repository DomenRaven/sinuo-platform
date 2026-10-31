# T03 领域模型提案

状态：提案。未写迁移、未改 ORM 注册、未跑测试、未改 `SCHEMA_REVISION`。本文不是任务完成证据。

读者：集成 owner（合入 Alembic 与 `models.py` 注册）以及随后实现领域类型的 T03 编码。共享文件仍只由集成 owner 修改。

基线：`SCHEMA_REVISION = 0002_lease_invariants`。库内现有表只有 `idempotency_records`、`outbox_events`、`outbox_deliveries`。`build_engine` 已在每个连接上执行 `PRAGMA foreign_keys=ON`、`busy_timeout`、`journal_mode=WAL`。`write_uow` 使用 `BEGIN IMMEDIATE`，块内禁止再 commit。

## 1. 范围

本修订建议 id：`0003_domain_identity`，`down_revision = 0002_lease_invariants`。只新增表、索引、CHECK 和触发器，不重建、不改写三张基元表。

合入后的物理表共 **37** 张：保留 3 张，新增 34 张。

保留：

- `idempotency_records`
- `outbox_events`
- `outbox_deliveries`

新增：

1. `accounts`
2. `sessions`
3. `projects`
4. `memberships`
5. `object_grants`
6. `stages`
7. `stage_config_versions`
8. `pages`
9. `page_drafts`
10. `view_states`
11. `page_versions`
12. `page_edges`
13. `artifacts`
14. `artifact_versions`
15. `evidence_anchors`
16. `decisions`
17. `test_records`
18. `review_opinions`
19. `agent_runs`
20. `run_tasks`
21. `run_task_dependencies`
22. `agent_results`
23. `context_snapshots`
24. `context_snapshot_refs`
25. `run_read_records`
26. `run_events`
27. `candidates`
28. `candidate_previews`
29. `candidate_preview_sources`
30. `candidate_dispositions`
31. `audit_events`
32. `jobs`
33. `consumer_receipts`
34. `privacy_tombstones`

`stage_config_versions` 来自 03 与 `stages` 同一行。`run_events` 承接 04 的 `UNIQUE(run_id, seq)`，不是第二套 Outbox。三个子表（`run_task_dependencies`、`context_snapshot_refs`、`candidate_preview_sources`）用来把“同项目”做成复合外键；只放进 JSON 的引用，SQLite 拒绝不了。

合同里的 `command_receipts` 不建表。它就是现有的 `idempotency_records`。

## 2. 禁止换成同义字段

现有列名保持不动：

| 合同用语 | 已经落地的列 | 禁止的别名 |
| --- | --- | --- |
| 幂等作用域 | `idempotency_records` 主键 `(project_id, actor_id, command, request_key)` | 不建 `command_receipts`；不把 `command` 改成 `command_type`；不把 `request_key` 改成 `idempotency_key` |
| 请求摘要 | `request_hash` | 不另加 `body_hash` |
| 已保存响应 | `response_status` + `response_json` | 不拆 `response_body` |
| 时间 | `created_at` Float，Unix UTC 秒 | 不改为 RFC3339 文本列 |
| Outbox 事件 | `event_id`、`project_id`、`event_type`、`schema_version`、`payload`、`created_at` | 不加 `seq`（序号只属于 `run_events`） |
| 投递租约 | `outbox_deliveries` 的 `consumer_name`、`state`、`lease_owner`、`fencing_token`、`lease_until`、`attempts` | 不把这些列复制到 `consumer_receipts` 或 `jobs` |

创建项目的幂等作用域继续用字符串 `project_id = "account:" + actor_id`（账号 id 为 36 字符 UUID 时，总长 44，放得进 `String(64)`）。因此 `idempotency_records.project_id`、`outbox_events.project_id`、`audit_events.project_id`、`consumer_receipts.project_id` **不加**指向 `projects.id` 的外键。已有基元行里的 `project-synthetic` 一类值必须能原样升级。

新表的公开 id 用 36 字符 UUID 文本，列类型仍是 `String(64)`，与 `actor_id` / `project_id` 对齐。哈希列是 64 字符小写十六进制，不带 `sha256:` 前缀，算法与 `application/idempotency.py` 的 `canonical_hash` 相同：UTF-8、键排序、`separators=(",", ":")`、`allow_nan=False`。

会被哈希比对的正文用 `Text` 保存这份规范化 JSON，不用 SQLAlchemy `JSON` 类型，避免往返时改键序。`idempotency_records.response_json` 与 `outbox_events.payload` 维持现状，本修订不迁移这两列。

库内时间全部是 Float（Unix UTC 秒），调用方写入，列上不设 `CURRENT_TIMESTAMP`。触发器不调用 `datetime('now')`，否则测试里的合成时钟会和 SQLite 时钟不一致。API 的 RFC3339 只在边界格式化。

外键一律 `ON DELETE RESTRICT ON UPDATE RESTRICT`。没有 `CASCADE`。SQLite 默认不启用外键；迁移和验收必须走 `build_engine`。触发器不依赖 `foreign_keys` PRAGMA。

每个被复合外键引用的父表，除主键 `id` 外再声明 `UNIQUE(project_id, id)`。只引用 `id` 的外键挡不住“用对了 id、写错了项目”。子表写 `FOREIGN KEY (project_id, parent_id) REFERENCES parent(project_id, id)`。SQLite 要求被引用的列集合本身是主键或唯一约束，所以这个复合唯一约束不能省。

被哈希的 JSON 列加 `json_valid(...)`（需要 SQLite JSON1；CPython 3.12 自带的 sqlite 有）。升级测试先执行 `SELECT json_valid('{}')`。若为 0，迁移应失败，不要悄悄拿掉 CHECK。

## 3. 删除与墓碑总则

- 确认版本、原件版本、谱系边、配置版本、证据、决定、测试、快照、读账、运行事件、预览、处置、审计、消费回执、隐私墓碑：禁止 UPDATE 和 DELETE。更正是插入新行（`supersedes_id` 或新的 `version_no`）。
- 墓碑不删除上述行，也不改它们的业务列。读路径联接 `privacy_tombstones` 后隐藏正文。文件字节的清理由以后的 T05 做，不在本修订把 GC 状态塞进 `artifact_versions`。
- 草稿、个人布局、项目头指针、阶段头指针、候选状态、运行/任务状态、作业租约、会话的滑动空闲时间：允许有限度的 UPDATE。规则写在对应触发器里。
- 成员与授权不物理删除。成员是同一行上的 `active → revoked → active`。授权只追加：旧行保持 `revoked`，重新授权插入新行。
- `view_states` 是个人偏好，允许 DELETE。没有任何候选、版本或幂等回执外键指向它。
- 不存在项目级 `current_page_id` / `head_page_id` / `current_branch_id`。历史查询不得移动 `pages.head_version_id`、`stages.head_config_version_id`、`projects.revision`、草稿或布局计数器。

## 4. 写入 owner

写入 owner 是服务名。会话的登录、Cookie、CSRF、限速和错误文案由 T02 实现；本提案只规定库能执行的约束。

| 表 | 写入 owner |
| --- | --- |
| `accounts`、`sessions`、`memberships`、`object_grants` | 授权服务（`sessions` 的产品语义由 T02 消费） |
| `projects` | 项目服务 |
| `stages`、`stage_config_versions`、`pages` 的插入 | 版本服务 |
| `pages.head_version_id` 的 CAS、`page_versions`、`page_edges` | 确认命令 |
| `page_drafts` | 草稿服务 |
| `view_states` | 视图偏好服务 |
| `artifacts`、`artifact_versions` | 原件服务 |
| `evidence_anchors` | 证据服务 |
| `decisions`、`test_records` | 领域命令 |
| `review_opinions` | 授权审阅服务 |
| `agent_runs`、`run_tasks`、`run_task_dependencies`、`agent_results`、`run_events` | 运行服务 |
| `context_snapshots`、`context_snapshot_refs`、`run_read_records` | 上下文服务 |
| `candidates`、`candidate_previews`、`candidate_preview_sources`、`candidate_dispositions` | 候选服务 |
| `audit_events`；既有 `idempotency_records`、`outbox_events` 的插入 | 命令服务 |
| 既有 `outbox_deliveries` 的租约更新；`jobs`、`consumer_receipts` | Worker |
| `privacy_tombstones` | 隐私命令 |

同一 `write_uow` 里，确认命令可以调用命令服务的插入函数去写审计、Outbox 和幂等回执。列的定义权仍在表的 owner，不在调用方。

模型与专责代码拿不到业务写会话。SQLite 没有角色，库触发器挡不住“拿着同一条连接去写”。这项由模块边界保证：`domain` 不导入 SQLAlchemy；`agents` 不导入 `domain_models`。验收里用导入检查做反例，不把它伪装成外键。

## 5. 闭集

字面量只放在 `src/sinuo/domain/schema_types.py`。`domain_models.py` 从那里取元组生成 CHECK，禁止在 `models.py` 再抄一套。

- 动作：`project.read`、`stage.configure`、`page.create`、`page.read`、`page.draft.write`、`page.checkpoint`、`page.branch`、`decision.confirm`、`artifact.upload`、`artifact.original.read`、`preview.read`、`evidence.read`、`evidence.create`、`run.create`、`run.read`、`run.cancel`、`test.record`、`opinion.create`、`opinion.edit_own`、`report.export`、`acl.manage`、`privacy.delete`。`project.create` 不是对象授权行，见未决点。
- 敏感级别（列名固定为 `sensitivity`）：`project`、`sensitive`、`released`。词表若要改名，只改这一闭集。
- 谱系 `kind`：`branches_from`、`stage_source`、`merge_source`。没有 `next` / `sequence`。同章平行页默认零条边。
- 候选动作：`add_node`、`add_typed_edge`、`attach_artifact_version`、`draft_decision`、`suggest_stage_transition`、`create_page_from_versions`。
- 建页模式：`stage_blank`、`history_branch`、`merge_blank`。
- 运行 `execution_status`：`queued`、`running`、`completed`、`partial`、`failed`、`cancelled`。
- 任务 `status`：`blocked`、`queued`、`running`、`succeeded`、`retry_wait`、`failed`、`cancelled`。
- 职责：`clarification`、`evidence`、`method`、`critic`。
- 结果 `disposition`：`published`、`discarded_late`。
- 候选 `status`：`pending`、`committed`、`rejected`、`expired`。
- 作业 `job_kind`：`export`、`derivative`、`privacy_cleanup`、`index`、`upload_finalize`、`suggestion_trigger`。
- 作业 `status`：`queued`、`running`、`succeeded`、`failed`、`cancelled`、`superseded`。
- 墓碑 `object_type`：`project`、`page`、`page_version`、`artifact`、`artifact_version`、`evidence_anchor`、`decision`、`test_record`、`review_opinion`、`context_snapshot`、`candidate`、`run_read_record`。
- 墓碑 `reason_category`：`consent_withdrawn`、`subject_request`、`policy`、`operator_correction`。

`role_label` 只是成员行上的显示文本，CHECK 不把它映射成动作。授权只读 `object_grants`。

## 6. 表清单

除特别声明外：主键 `id` 为 `String(64)`；`created_at` 为 Float；`project_id` 为 `String(64)`；哈希为 `String(64)`。`revision`、`version_no`、`content_revision`、`layout_revision`、`access_epoch`、`auth_epoch`、`attempt_no`、`fencing_token`、`seq` 都是整数。

### accounts

- 主键：`id`（36 字符 UUID）
- 列：`display_name`、`password_hash`、`status`（`active`/`disabled`）、`auth_epoch`（从 1 起）、`can_create_project`（0/1，默认 0）、`created_at`、`disabled_at`
- 约束：`password_hash LIKE '$argon2id$%'`。没有 `password` 列，没有盐列（盐在 Argon2 编码串里）。
- 外键：无
- 删除：禁止 DELETE。禁用只改 `status`、`disabled_at`，并把 `auth_epoch` 加 1。
- 触发器：`id` 与 `created_at` 冻结；`auth_epoch` 只能保持或加 1。
- owner：授权服务

`can_create_project` 不是角色。首位维护者由 T02 的 CLI 把该位置为 1。

### sessions

语义由 T02 消费。本表不规定 8 小时/24 小时的具体数字，那是 T02 的配置。库只保证列不可被改成另一种凭证。

- 主键：`id`
- 列：`account_id`、`token_sha256`、`csrf_sha256`（可空）、`auth_epoch`、`created_at`、`last_active_at`、`idle_expires_at`、`absolute_expires_at`、`revoked_at`
- 唯一：`token_sha256`
- 外键：`account_id → accounts.id`
- CHECK：两个哈希若存在则为 64 位小写十六进制；`idle_expires_at <= absolute_expires_at`；`revoked_at` 为空或 `>= created_at`；`auth_epoch >= 1`
- 没有名为 `token`、`session_token`、`secret` 的列
- 删除：触发器禁止 DELETE。退出只写 `revoked_at`，且只能从 NULL 写成一次，不能改回 NULL
- 触发器冻结：`token_sha256`、`account_id`、`created_at`、`absolute_expires_at`、`auth_epoch`。允许更新 `last_active_at`、`csrf_sha256`，以及把 `idle_expires_at` 向前滑动但仍不超过 `absolute_expires_at`
- 索引：`(account_id, revoked_at)`
- owner：授权服务

有效会话的谓词交给 T02，不做成会随账号纪元变化而自动改行的触发器：

```sql
revoked_at IS NULL
AND token 对应行的 auth_epoch = accounts.auth_epoch
AND accounts.status = 'active'
AND :now < idle_expires_at
AND :now < absolute_expires_at
```

密码重置和禁用在同一事务里把 `accounts.auth_epoch` 加 1。即使漏写 `revoked_at`，旧会话也过不了纪元谓词。

### projects

- 主键：`id`
- 列：`title`、`brief`（可空 Text）、`template_key`（可空，不建模板表）、`revision`、`access_epoch`、`created_at`
- CHECK：`revision >= 1`，`access_epoch >= 1`。插入时两者都是 1（创建项目本身是一次确认）
- 外键：无
- 删除：禁止
- 触发器：`id`、`created_at` 冻结。`revision` 与 `access_epoch` 各自只能保持或加 1，禁止加 2 或减少。`title` / `brief` / `template_key` 变化时 `revision` 必须加 1。只加 `access_epoch` 时这三个字段必须不变
- 明确不建的列：`current_page_id`、`head_page_id`、`current_branch_id`
- owner：项目服务

项目修订的账本就是带 `project_revision` 戳的业务行，加上 `projects.revision` 这个头整数。不另建 `project_revisions`，避免出现第二套确认序号。

CAS：

```sql
UPDATE projects
SET revision = revision + 1
WHERE id = :project_id AND revision = :expected;
```

权限命令可以在同一条 UPDATE 里只加 `access_epoch`，不加 `revision`。受影响行数必须为 1。

### memberships

- 主键：`id`
- 唯一：`(project_id, account_id)`；另有 `UNIQUE(project_id, id)`
- 列：`status`（`active`/`revoked`）、`role_label`（可空，不参与授权）、`created_at`、`revoked_at`
- 外键：`(project_id) → projects`；`account_id → accounts`
- 删除：禁止。重新加入是把同一行改回 `active`，不是插第二行
- 触发器：`project_id`、`account_id`、`id` 冻结
- 页范围不写在本表 JSON 里。分配页就是 `object_grants` 上的 `page.read` / `page.draft.write`
- owner：授权服务

### object_grants

- 主键：`id`
- 列：`account_id`、`action`、`scope_type`、`status`（`active`/`revoked`）、`created_at`、`revoked_at`、`created_by_account_id`
- `scope_type`：`project`、`stage`、`page`、`page_version`、`artifact`、`artifact_version`、`evidence_anchor`
- 可空目标列：`stage_id`、`page_id`、`page_version_id`、`artifact_id`、`artifact_version_id`、`evidence_anchor_id`
- CHECK：与 `scope_type` 对应的那一个目标非空，其余目标为空。`scope_type='project'` 时六个目标都为空（对象就是 `project_id`）
- 外键：每个非空目标都是 `(project_id, 目标 id) →` 对应父表的 `(project_id, id)`。`account_id → accounts`
- 唯一：SQLite 的 UNIQUE 把 NULL 当成互不相同，所以不能靠一条含空列的 UNIQUE。为每种 `scope_type` 建部分唯一索引，条件是 `status='active'`。例如项目级：`(project_id, account_id, action) WHERE scope_type='project' AND status='active'`；页级：再加 `page_id`
- 删除：禁止。撤销只把 `status` 改为 `revoked` 并写 `revoked_at`。触发器不允许从 `revoked` 改回 `active`。重新授权插入新行
- 行的含义是允许。没有 `effect='deny'` 列。没有行就是拒绝
- owner：授权服务

授权变更必须在同一事务把 `projects.access_epoch` 加 1。库触发器看不出“这次 UPDATE 是不是 ACL 命令”，所以这项放在领域函数 `acl_commit`：纪元没有加 1 就拒绝。见第 9 节。

### stages

- 主键：`id`
- 唯一：`(project_id, id)`
- 列：`head_config_version_id`（可空）、`created_at`
- 外键：`project_id → projects`。头指针 **不建外键**（与 `page_versions` 会成环，SQLite 在 Alembic 已打开的事务里不能中途关掉 `foreign_keys`，降级会删不掉表）
- 删除：禁止
- 触发器：更新 `head_config_version_id` 时，目标配置行的 `stage_id` 与 `project_id` 必须就是本行。`id`、`project_id` 冻结
- 顺序和目标不在本行，在配置版本里
- owner：版本服务

### stage_config_versions

- 主键：`id`
- 唯一：`(stage_id, version_no)`；`UNIQUE(project_id, id)`；`UNIQUE(project_id, stage_id, id)`
- 列：`version_no`（从 1 起）、`position`、`goal_json`、`effective_project_revision`、`schema_version`、`created_at`
- 外键：`(project_id, stage_id) → stages(project_id, id)`
- 删除/更新：禁止。改目标或顺序是新的 `version_no`，再 CAS 阶段头指针
- 这个 `version_no` 不是页版本号，也不是 `projects.revision`
- owner：版本服务

### pages

- 主键：`id`
- 唯一：`(project_id, id)`
- 列：`stage_id`、`head_version_id`（可空，仅插入过程中为空；命令提交前必须写上）、`status`（`active`/`abandoned`）、`title`、`created_at`
- 外键：`(project_id, stage_id) → stages(project_id, id)`。`head_version_id` 同样 **不建外键**，理由同阶段头指针
- 删除：禁止。放弃路线只把 `status` 改为 `abandoned`，版本行保留
- 触发器：`head_version_id` 若变化，新版本的 `page_id` 与 `project_id` 必须就是本页。`id`、`project_id`、`stage_id` 冻结
- 建页 owner：版本服务。`head_version_id` 的 CAS owner：确认命令

头指针 CAS 使用空值安全比较：

```sql
UPDATE pages
SET head_version_id = :new_head
WHERE id = :page_id
  AND project_id = :project_id
  AND head_version_id IS :expected_head;
```

### page_drafts

- 主键：`page_id`（一页一行）
- 列：`project_id`、`base_version_id`、`content_revision`、`content_hash`、`content_json`、`schema_version`、`updated_at`
- 外键：`(project_id, page_id) → pages`；`(project_id, page_id, base_version_id) → page_versions(project_id, page_id, id)`（基版本必须是同一页）
- CHECK：`content_revision >= 1`。建页时写入 1，不是 0，也不是 `version_no`
- 删除：禁止
- 触发器：`page_id`、`project_id` 冻结；`content_revision` 只能保持或加 1
- 没有 `layout_json`，没有 `draft_layout_revision`
- owner：草稿服务

条件更新保持 03 的语句，列名不变：

```sql
UPDATE page_drafts
SET content_json = :new_content,
    content_hash = :new_hash,
    content_revision = content_revision + 1,
    updated_at = :now
WHERE page_id = :page_id
  AND base_version_id IS :base_version
  AND content_revision = :expected_revision;
```

保存为版本时把 `base_version_id` 指到新版本，并把 `content_revision` 加 1。禁止把 `content_revision` 写成 `version_no`。历史分支的新页草稿从 1 重新计，不继承源页的草稿计数。

### view_states

- 主键：`id`
- 唯一：`(account_id, page_id, view_name)`
- 列：`project_id`、`layout_revision`（从 1 起）、`layout_hash`、`layout_json`、`updated_at`
- 外键：`account_id → accounts`；`(project_id, page_id) → pages`
- 删除：允许（只删这一行偏好）
- 触发器：身份列冻结；`layout_revision` 只能加 1
- 不引用候选，不增加 `projects.revision`，不改变 `content_revision` 或 `version_no`
- 合同里的 `subject_id` 在本表就是 `account_id`。不另加 `subject_id` 列
- owner：视图偏好服务

### page_versions

- 主键：`id`
- 唯一：`(page_id, version_no)`；`UNIQUE(project_id, id)`；`UNIQUE(project_id, page_id, id)`
- 列：`version_no`（从 1 起）、`parent_version_id`（可空）、`content_json`、`content_hash`、`schema_version`、`project_revision`、`created_by_account_id`、`created_at`
- 外键：`(project_id, page_id) → pages`；父版本 `(project_id, page_id, parent_version_id) → page_versions(project_id, page_id, id)`。父版本因此必须是同一页。历史分支 **不** 把源页版本写成新页的 `parent_version_id`；来源是 `page_edges.kind='branches_from'`。新页的 v1 父版本为空
- 没有 `updated_at`
- 删除/更新：触发器无条件 `RAISE(ABORT)`
- 更正：插入更大的 `version_no`，`parent_version_id` 指向被更正的那一版
- owner：确认命令

页内节点与页内边放在 `content_json`（`schema_version=1`）。节点有稳定 `node_id`，边有稳定 `edge_id`。Pydantic 模型 `extra=forbid`。不建权威 `nodes` 表。投影以后可以另做只读重建表，但不能成为写入入口，也不能在读历史时把最新节点写回旧版本。

### page_edges

- 主键：`id`
- 唯一：`(source_version_id, target_version_id, kind)`；`UNIQUE(project_id, id)`
- 列：`source_version_id`、`target_version_id`、`kind`、`project_revision`、`reason`（可空 Text）、`created_at`
- 外键：源、目标都是 `(project_id, version_id) → page_versions(project_id, id)`
- CHECK：`source_version_id <> target_version_id`；`kind` 为闭集
- 删除/更新：禁止
- `reason` 不得保存摘录正文。领域校验拒绝 `excerpt` / `body` / `content` 键
- owner：确认命令

### artifacts

- 主键：`id`（稳定作品 id）
- 唯一：`(project_id, id)`
- 列：`title`、`created_at`
- 外键：`project_id → projects`
- 删除：禁止
- 标题可改，且不占用 `version_no`。字节、哈希、许可、敏感级别不在这张表上
- owner：原件服务

### artifact_versions

这是“原件元数据”。与 `page_versions` 一样没有普通 UPDATE/DELETE。

- 主键：`id`
- 唯一：`(artifact_id, version_no)`；`UNIQUE(project_id, id)`
- 列：`version_no`（从 1 起）、`content_hash`、`size_bytes`、`media_type`、`object_key`、`license`、`sensitivity`、`ai_processing_allowed`（0/1）、`project_revision`、`created_by_account_id`、`created_at`
- 外键：`(project_id, artifact_id) → artifacts(project_id, id)`
- CHECK：`size_bytes >= 0`；`object_key` 不得以 `/` 开头、不得包含 `..` 或反斜杠。它是服务器生成的存储键，不是用户路径
- `object_key` 不是唯一的。同项目相同字节可以复用同一个键，业务版本仍各有 `version_no`
- 不在本表放 `state`、`fencing_token`、`preview_status`。那些会迫使原件行被更新
- 删除/更新：无条件触发器拒绝
- 许可或敏感级别写错了：插入新的 `version_no`，可以沿用同一个 `content_hash` 和 `object_key`。不就地改旧行
- 合同里的 MIME 就是列 `media_type`；哈希就是 `content_hash`。不要再加 `mime`、`sha256`、`checksum`
- owner：原件服务

`stored_objects`、上传票据、pin、generation 的 GC 状态不在本修订。T05 以后单独修订，外键指向这里的 `id`，而不是给本表加可更新列。

### evidence_anchors

- 主键：`id`
- 唯一：`UNIQUE(project_id, id)`
- 列：`artifact_version_id`、`locator_type`（`page`/`region`/`timecode`/`text_range`）、`locator_json`、`excerpt_hash`、`extractor_version`、`visibility`（`project`/`restricted`）、`sensitivity`、`supersedes_id`（可空）、`schema_version`、`created_by_account_id`、`created_at`
- 外键：作品版本与 `supersedes_id` 都是同一 `project_id` 的复合外键
- 不存摘录正文字段。定位 JSON 里禁止 `text` / `excerpt` / `body`
- 删除/更新：禁止。OCR 或定位更正是新行加 `supersedes_id`
- owner：证据服务

### decisions

- 主键：`id`
- 唯一：`UNIQUE(project_id, id)`
- 列：`account_id`、`body`、`reason`、`page_version_id`、`artifact_version_id`、`stage_id`（三者至少一个非空）、`supersedes_id`、`project_revision`、`created_at`
- 外键：上述目标与 `supersedes_id` 均为同一项目的复合外键；`account_id → accounts`
- 删除/更新：禁止。更正是新行，`supersedes_id` 指向旧决定
- owner：领域命令

### test_records

- 主键：`id`
- 唯一：`UNIQUE(project_id, id)`
- 列：`account_id`、`artifact_version_id`（非空）、`conditions_json`、`observation`、`result`、`source`（可空）、`supersedes_id`、`project_revision`、`created_at`
- 外键：作品版本复合外键，禁止指向另一项目的版本
- 删除/更新：禁止
- 没有“改绑到新作品版本”的列。历史分支不得复制测试行并写成通过
- owner：领域命令

### review_opinions

- 主键：`id`
- 唯一：`UNIQUE(project_id, id)`
- 列：`author_account_id`、`page_version_id`、`node_id`（可空，只是该版本 JSON 里的稳定 id）、`body`、`revision`（从 1 起）、`visibility`、`created_at`、`updated_at`
- 外键：`(project_id, page_version_id) → page_versions`；作者外键
- 删除：禁止
- 触发器：允许更新 `body`、`visibility`、`updated_at`，且 `revision` 必须加 1。`author_account_id`、`page_version_id`、`node_id`、`project_id` 冻结。换锚点必须新行
- `node_id` 是否真在该版本 JSON 里，库检查不了，归领域校验
- owner：授权审阅服务

P0 口径（采纳 03 的建议，避免两条时间线）：创建意见、以及每次成功的正文编辑，都把 `projects.revision` 加 1 并写审计。`review_opinions.revision` 只服务这条意见的 CAS，禁止把页 `version_no` 或项目修订抄进这个列。

### agent_runs

- 主键：`id`
- 唯一：`UNIQUE(project_id, id)`
- 列：`initiated_by_account_id`、`parent_run_id`、`supersedes_run_id`、`context_snapshot_id`、`execution_status`、`accepting_results`（0/1，插入时为 1）、`next_event_seq`（从 0 起）、`budget_json`、`created_at`、`closed_at`
- 外键：发起人；`parent_run_id` 与 `supersedes_run_id` 为同一项目的自引用；`context_snapshot_id → context_snapshots`
- CHECK：`parent_run_id <> id`
- 删除：禁止
- 触发器：身份列与 `context_snapshot_id` 冻结。`accepting_results` 只能从 1 变为 0，不能重新打开。`execution_status` 一旦进入 `completed`/`partial`/`failed`/`cancelled` 就不能再改。`next_event_seq` 只能保持或加 1
- 不增加 `projects.revision`（TX-10）
- owner：运行服务

四个职责的链是两条运行：第二条的 `parent_run_id` 指向第一条。禁止在一行里放四个职责，也禁止用 `sub_clarification` 这类角色名。

### run_tasks

专责任务的租约只在这张表。不要为同一次专责尝试再插入 `jobs`。

- 主键：`id`
- 唯一：`UNIQUE(run_id, id)`；`UNIQUE(project_id, id)`
- 列：`run_id`、`project_id`、`role`、`status`、`mandatory`（0/1）、`input_hash`、`available_at`、`attempt_no`、`lease_owner`、`lease_until`、`fencing_token`、`output_ref`、`last_error`
- 外键：`(project_id, run_id) → agent_runs`
- 同一 `role` 可以有多行（证据职责内部的并行检索槽）。职责种类的上限不靠“每种角色一行”来表达
- 删除：禁止
- 租约 CHECK 沿用 0002 的形状，并换成本表状态：`status='running'` 时 `lease_owner` 非空、`lease_until` 为有限浮点、`fencing_token > 0`；其余状态 `lease_owner` 与 `lease_until` 都为 NULL。`attempt_no >= 0`，`fencing_token >= 0`
- 认领与完成的条件 UPDATE 使用调用方传入的 `:now`，不在触发器里读墙上时钟。过期仍由应用层 `lease_until > :now` 拒绝
- 触发器：进入终态后不能改回 `queued`/`running`。`run_id`、`role`、`project_id` 冻结
- 插入之后的语句级检查：该 `run_id` 的 `COUNT(DISTINCT role) > 3` 则 `RAISE(ABORT, 'role_cap')`。角色不在四词闭集里由 CHECK 拒绝
- owner：运行服务

合同里的 fence 就是列名 `fencing_token`，与 `outbox_deliveries.fencing_token` 同名不同表，不是把投递租约搬过来。

### run_task_dependencies

- 主键：`(run_id, task_id, depends_on_task_id)`
- 外键：两个任务 id 都是 `(run_id, task_id) → run_tasks(run_id, id)`，因此依赖不能跨运行
- CHECK：`task_id <> depends_on_task_id`
- 删除/更新：禁止（计划在插入运行时一次写完）
- 有环与缺输入由领域校验在插入前拒绝。库只保证端点属于同一运行
- owner：运行服务

### agent_results

- 主键：`id`
- 唯一：`(run_id, task_id, attempt_no)`；部分唯一索引 `(run_id, task_id, input_hash) WHERE disposition = 'published'`
- 列：`project_id`、`run_id`、`task_id`、`input_hash`、`attempt_no`、`fencing_token`、`disposition`、`output_json`、`diagnostic_code`、`usage_json`、`created_at`
- 外键：运行与任务均为同一项目
- CHECK：`published` 时 `output_json` 非空且 `diagnostic_code` 为空；`discarded_late` 时 `output_json` 为空且 `diagnostic_code` 非空。两种 JSON 都禁止 `prompt`、`api_key`、`password`、`token` 键（领域校验；库用 `json_valid`）
- 删除/更新：禁止。迟到结果是新行，`disposition='discarded_late'`，不能把已发布行改掉
- 插入触发器：`published` 时该运行的 `accepting_results` 必须仍为 1，且 `fencing_token` 等于该任务当前的 `fencing_token`。`discarded_late` 不要求令牌仍是当前值（它本来就是过期令牌），但仍然插不进部分唯一索引
- owner：运行服务

发布键是 `(run_id, task_id, input_hash)` 的已发布行。封口在同一事务把 `accepting_results` 置 0、写结果、写候选、写 `run_events`。其后的发布插入被触发器拒绝。

### context_snapshots

- 主键：`id`
- 唯一：`UNIQUE(project_id, id)`
- 列：`intent`、`template_version`、`method_card_version`、`authorization_scope_json`、`access_epoch`、`exclusions_json`、`index_revision`（可空，T07 之前允许空）、`budget_json`、`draft_page_id`、`draft_content_revision`、`draft_content_hash`、`draft_content_json`、`created_at`
- 外键：仅 `project_id → projects`，以及可选 `(project_id, draft_page_id) → pages`。草稿修订是戳，不是指向 `page_drafts` 活行的外键
- CHECK：三者要么都空，要么 `draft_content_json`、哈希、修订号都有值。这是插入时的物化副本
- 删除/更新：禁止。后续工具读取追加到 `run_read_records`，不准回写本行
- `authorization_scope_json` 只解释当时的输入，授权服务不得把它当 ACL
- owner：上下文服务

### context_snapshot_refs

- 主键：`(snapshot_id, page_version_id)`
- 外键：`(project_id, snapshot_id) → context_snapshots`；`(project_id, page_version_id) → page_versions`
- 删除/更新：禁止
- owner：上下文服务

### run_read_records

- 主键：`id`
- 唯一：`(run_id, tool_call_id)`
- 列：`project_id`、`run_id`、`task_id`、`page_version_id`、`artifact_version_id`、`evidence_anchor_id`（三者恰好一个非空）、`content_hash`、`index_revision`、`access_epoch`、`read_at`、`truncated`（0/1）、`miss_scope`（可空）
- 外键：运行、任务、以及那个非空目标，全部复合到同一 `project_id`
- 不存片段正文
- 删除/更新：禁止
- owner：上下文服务

### run_events

SSE 日志。不是 `outbox_events` 的副本。

- 主键：`event_id`
- 唯一：`(run_id, seq)`
- 列：`project_id`、`run_id`、`seq`（从 1 起）、`event_type`、`schema_version`、`task_id`、`trace_id`、`context_snapshot_id`、`payload`、`created_at`
- 外键：`(project_id, run_id) → agent_runs`；可选任务与快照均为同一项目
- 信封字段 `type` 对应列 `event_type`；`occurred_at` 不入库，库内只有 `created_at`
- 与 Outbox 可以在同一事务各插一行，两边的 `event_id` 可以相同，但没有互相外键。不是每条 SSE 事件都要进 Outbox
- 序号分配：

```sql
UPDATE agent_runs
SET next_event_seq = next_event_seq + 1
WHERE id = :run_id AND next_event_seq = :expected;
```

然后插入 `seq = expected + 1`。这不是项目修订，也不是页版本号。

- 删除/更新：禁止
- owner：运行服务

### candidates

- 主键：`id`
- 唯一：`UNIQUE(project_id, id)`
- 列：`run_id`、`action`、`page_mode`（可空）、`status`、`revision`（从 1 起）、`proposal_json`、`proposal_hash`、`input_hash`、`context_snapshot_id`、`schema_version`、`created_at`
- 外键：运行与快照均为同一项目
- CHECK：`action='create_page_from_versions'` 或 `suggest_stage_transition` 时 `page_mode` 非空，否则为空
- 删除：禁止
- 更新触发器：仅当旧 `status='pending'`、新状态为 `committed`/`rejected`/`expired`、`revision` 加 1，且提案、哈希、运行、项目、动作、模式、快照全部 `IS NOT` 比较后未变。其他 UPDATE 全部 `RAISE(ABORT)`
- 应用层仍必须写：

```sql
UPDATE candidates
SET status = :next, revision = revision + 1
WHERE id = :id AND project_id = :project_id
  AND status = 'pending' AND revision = :expected;
```

受影响行数不是 1 就整笔回滚。触发器挡住漏写 WHERE 的普通 UPDATE；并发下由 SQLite 单写者和这条 WHERE 保证只有一方成功。

- 不同幂等键不能绕过这行。幂等行在 `idempotency_records`，不在本表
- owner：候选服务

### candidate_previews

- 主键：`id`
- 列：`project_id`、`candidate_id`、`candidate_revision`、`account_id`、`access_epoch`、`user_patch_json`、`user_patch_hash`、`effective_proposal_json`、`preview_hash`、`expires_at`、`created_at`
- 外键：候选与账号均为同一项目范围；`candidate_id` 复合到 `candidates`
- 插入触发器：`candidate_revision` 必须等于该候选当时的 `revision`，`access_epoch` 必须等于该项目当时的 `access_epoch`
- 删除/更新：禁止。改补丁就是新预览行。旧预览留着，提交只认被点名的那一行
- 不增加项目修订，不创建页版本
- 有效期数值是 T09 的配置；列里只存绝对 `expires_at`。测试可以用很短的期限
- owner：候选服务

### candidate_preview_sources

把“全部源版本 / 草稿戳”从 JSON 里拆出来，使跨项目引用能被外键拒绝。

- 主键：`id`
- 唯一索引：`(preview_id, source_kind, ifnull(page_version_id,''), ifnull(artifact_version_id,''), ifnull(draft_page_id,''))`
- 列：`source_kind`（`page_version`/`artifact_version`/`draft_stamp`）、对应的一个非空目标、草稿戳的 `draft_content_revision` 与 `draft_content_hash`
- 外键：页版本、作品版本、草稿所在页，都带 `project_id`
- 草稿戳不外键到活的 `content_revision`
- 删除/更新：禁止
- owner：候选服务

### candidate_dispositions

- 主键：`id`
- 唯一：`candidate_id`（一候选一行）
- 列：`project_id`、`account_id`、`outcome`（`committed`/`rejected`/`expired`）、`preview_id`（提交时非空，拒绝或过期可空）、`reason`（可空）、`created_at`
- 外键：候选、预览、账号，项目一致
- 插入触发器：该候选的 `status` 已经等于 `outcome`（同一事务里先 CAS 再插入）
- 删除/更新：禁止
- owner：候选服务

### audit_events

- 主键：`id`
- 列：`project_id`（**无**项目外键）、`actor_id`（外键到 `accounts`）、`command`（宽度与基元表相同，`String(100)`）、`subject_type`、`subject_id`、`project_revision`（可空）、`payload`、`created_at`
- 草稿保存、布局保存、预览、发起运行：`project_revision` 为空。确认类命令写入新的项目修订号
- `payload` 只放对象 id、计数和错误类别。领域校验拒绝密钥、口令、提示词、摘录正文
- 删除/更新：禁止
- 索引：`(project_id, created_at)`
- owner：命令服务

### jobs

Worker 的持久作业，不是专责任务，也不是 Outbox 投递租约。

- 主键：`id`
- 列：`project_id`、`job_kind`、`status`、`available_at`、`attempt_no`、`lease_owner`、`lease_until`、`fencing_token`、`input_hash`、`output_ref`、`last_error`、`subject_type`、`subject_id`、`source_event_id`、`payload`、`created_at`
- 外键：`project_id → projects`；`source_event_id → outbox_events.event_id`，可空
- `subject_type` + `subject_id` 是多态指针，库不做跨表外键。调用方必须已用复合外键写过真正的业务行。跨项目的多态目标归领域测试
- 租约 CHECK 与 0002 相同的有限浮点区间：`running` 必须有非空 owner、有限的 `lease_until`、`fencing_token > 0`；`queued` 与终态的 owner 和 `lease_until` 为 NULL
- 认领语句与 05 的草图一致，表名就是 `jobs`，列名就是上面这些，不引入 `worker_id` 作为写权列
- 删除：禁止，结束用终态
- 导出不另建 `export_jobs`。T10 用 `job_kind='export'`
- owner：Worker

### consumer_receipts

业务效果已经提交的凭证。投递租约仍只活在 `outbox_deliveries`。

- 主键：`(event_id, consumer_name)`
- 列：`project_id`（无项目外键）、`effect_kind`、`effect_ref`、`created_at`
- 外键：`event_id → outbox_events.event_id`
- `consumer_name` 与 `outbox_deliveries.consumer_name` 同名同宽度（`String(100)`）
- 没有 `state`、`lease_owner`、`fencing_token`、`lease_until`、`attempts`
- 删除/更新：禁止
- owner：Worker

同一 `write_uow`：写入该消费者的业务效果，插入本行，并按现有 `complete_delivery` 结束投递租约。本行已存在则不再做第二次业务效果。

### privacy_tombstones

- 主键：`id`
- 唯一：`(project_id, object_type, object_id)`
- 列：`object_type`、`object_id`、`reason_category`、`access_epoch`、`audit_event_id`、`created_at`
- 外键：`project_id → projects`；`audit_event_id → audit_events.id`
- 禁止的列：正文、摘录、标题、`content_json`、哈希原文、文件路径
- 插入触发器按 `object_type` 查询目标表：行必须存在，且目标的 `project_id` 等于墓碑的 `project_id`（`object_type='project'` 时 `object_id` 就是 `project_id`）
- 删除/更新：禁止。墓碑本身可审计，不能被后来的 UPDATE 抹掉
- 不级联删除目标行
- 幂等重放返回已有墓碑 id（靠 `idempotency_records`），不插第二行
- owner：隐私命令

## 7. 不可变策略

`page_versions` 与 `artifact_versions` 使用无条件的 `BEFORE UPDATE` 和 `BEFORE DELETE` 触发器，`SELECT RAISE(ABORT, '...')`。不使用 `BEFORE UPDATE OF 某些列`：SQLite 只在 SET 子句写到列出的列时才触发 `UPDATE OF`，漏列就会留下更新口。

同样无条件拒绝 UPDATE/DELETE 的表：`stage_config_versions`、`page_edges`、`evidence_anchors`、`decisions`、`test_records`、`context_snapshots`、`context_snapshot_refs`、`run_read_records`、`run_events`、`candidate_previews`、`candidate_preview_sources`、`candidate_dispositions`、`agent_results`、`audit_events`、`consumer_receipts`、`privacy_tombstones`。

`candidates` 是唯一允许“改状态”的确认对象，触发器条件见上。冻结列的比较必须用 `IS NOT`，不能用 `<>`（NULL 比较不会使 WHEN 成立）。

SQLite 上的判断：

- 对普通 `UPDATE` / `DELETE`、SQLAlchemy flush、`ON CONFLICT DO UPDATE`，`RAISE(ABORT)` 可靠，并表现为 `IntegrityError`。`INSERT OR REPLACE` 会先 DELETE，删除触发器会把它一起拒绝。
- 触发器不依赖 `PRAGMA foreign_keys`。外键才依赖它。
- 触发器 **不是** 权限边界。任何能执行 DDL 的连接都可以 `DROP TRIGGER` 再更新。SQLite 没有独立的应用角色。
- Alembic `batch_alter_table` 会重建表并丢掉触发器。0003 不要用 batch 模式建这些表。以后若有修订重建 `page_versions` 或 `artifact_versions`，必须在同一 `upgrade()` 里重建同名触发器。验收查询 `sqlite_master`。
- 备份 API 按页复制，不会重新触发触发器。恢复后的检查是“触发器还在”，不是“复制过程会拒绝坏数据”。
- 因此领域类型仍然拒绝就地更正，不能把“服务里没写 UPDATE”当成唯一手段。

领域层反例（纯函数，不碰数据库）：

- `correct_page_version(current, new_content)` 返回新的 `id` 和新的 `version_no`，`parent_version_id` 为旧 id。若调用方试图让新旧 `id` 相同而 `content_hash` 不同，`assert_append_only` 抛出 `ImmutableRecord`。
- `correct_artifact_version` 同样只追加 `version_no`。
- `correct_decision` / `correct_test_record` 新行带 `supersedes_id`，不改旧 `body`。
- 反例本身：一个把 `content_json` 写回同一 `id` 的假函数 `overwrite_page_version` 必须被 `assert_append_only` 拒绝。这个假函数不进入生产调用路径，只留在测试里证明“只靠约定不写 UPDATE”时领域层仍会失败关闭。

数据库反例（验收测试，第二条连接，原始 SQL）：

- `UPDATE page_versions SET content_json = '...'` 与 `DELETE FROM page_versions` 都失败。
- 对 `artifact_versions` 的 `content_hash`、`object_key`、`license`、`sensitivity` 做同样的 UPDATE，失败。
- `INSERT` 一条 `privacy_tombstones` 之后，上述版本行的 `content_hash` 仍与插入时相同。墓碑行自己的 UPDATE/DELETE 也失败。
- 测试同时查询 `sqlite_master`，确认触发器名还在。触发器被丢掉时，这条测试失败，而不是改成跳过。

## 8. 计数器

这些整数互相独立，禁止互相赋值，禁止共用一张序列表：

| 计数器 | 所在列 | 何时加 1 |
| --- | --- | --- |
| 草稿内容 | `page_drafts.content_revision` | 草稿 CAS；保存为版本后继续加，不归零 |
| 个人布局 | `view_states.layout_revision` | 只保存布局 |
| 正式页版本 | `page_versions.version_no` | 只插入新页版本。每页从 1 起 |
| 项目确认修订 | `projects.revision` | 创建项目、保存版本、历史分支、确认候选、登记原件、创建/编辑审阅意见 |
| 阶段配置 | `stage_config_versions.version_no` | 新配置行 |
| 作品版本 | `artifact_versions.version_no` | 新原件版本。每个作品从 1 起 |
| 候选 | `candidates.revision` | 插入为 1；消费时加 1 |
| 意见 | `review_opinions.revision` | 该意见的正文 CAS |
| 运行事件 | `agent_runs.next_event_seq` | 只为该运行分配 `run_events.seq` |
| 授权纪元 | `projects.access_epoch` | ACL 变更。不加项目修订 |
| 认证纪元 | `accounts.auth_epoch` | 禁用或重置密码 |
| 幂等 | 无序号 | 主键 `(project_id, actor_id, command, request_key)` |

不增加项目修订的写操作：草稿 CAS、布局 CAS、生成预览、发起运行、追加 `run_read_records`。

验收用一组同时存在的数：页 `version_no=2`，草稿 `content_revision=5`，布局 `layout_revision=7`，`projects.revision=4`，阶段配置 `version_no=3`，作品 `version_no=2`，候选 `revision=1`，意见 `revision=2`，`next_event_seq=8`。然后只做一次草稿 CAS，断言其余数字都不变。再断言不存在把这些列串成同一个生成器的表。

同页第二个 `version_no=1`、同一作品第二个 `version_no=1`、同一幂等主键第二行，都由唯一约束拒绝。

## 9. 跨项目

数据库用复合外键拒绝的情况：

- 页的 `stage_id` 属于另一项目
- 页版本的 `page_id` 或父版本属于另一页/另一项目
- 谱系边的任一端版本属于另一项目
- 草稿 `base_version_id` 不是本页版本
- 证据、决定、测试、意见锚到另一项目的版本
- 授权行的页、阶段、作品或证据属于另一项目
- 预览来源、快照引用、读账目标属于另一项目
- 运行的父运行、任务依赖、结果、事件属于另一项目
- 墓碑触发器查到的目标项目不一致

故意不建成外键、改由测试锁住的情况：

- `idempotency_records`、`outbox_events`、`audit_events`、`consumer_receipts` 的 `project_id`。原因是账号作用域键和已有基元行。新写的领域命令仍要在应用层检查：除 `account:` 前缀外，`project_id` 必须是本次事务里的那个项目。
- `content_json` 里的 `artifact_version_id`、`node_id`。领域函数 `assert_same_project_document` 拒绝文档中的作品版本不属于该页的项目。反例：两个合成项目，页 JSON 引用另一个项目的作品版本。
- 意见的 `node_id` 不在所锚页版本的 JSON 里。
- `jobs.subject_id` 的多态目标。
- 同时是两个项目成员，不能让快照引用另一个项目。成员行合法，引用行不合法。这是领域反例，不是“外键没建所以允许”。
- 授权纪元：`acl_commit` 在纪元未加 1 时拒绝。
- 历史只读：`history_statements()` 返回的语句只能是 SELECT。`assert_no_pointer_movement` 比较前后的 `projects.revision`、`pages.head_version_id`、`stages.head_config_version_id`、`page_drafts.content_revision`、`view_states.layout_revision`。反例函数若把“打开历史”实现成更新头指针，断言失败。
- 谱系 `kind='next'` 被闭集拒绝。两个同阶段页插入后边数为 0，这条作为夹具断言，不是外键能表达的。

## 10. 迁移

建议文件名（由集成 owner 创建，本提案不创建）：`migrations/versions/0003_domain_identity.py`。

- `revision = "0003_domain_identity"`
- `down_revision = "0002_lease_invariants"`
- `branch_labels = None`，`depends_on = None`
- 保持单一 head。不修改 `0001_primitives.py` 与 `0002_lease_invariants.py`

`upgrade()`：

1. 不读取、不更新、不重建 `idempotency_records`、`outbox_events`、`outbox_deliveries`。
2. 按父表到子表 `create_table`、唯一约束、CHECK、索引。
3. 表创建完成后再 `CREATE TRIGGER`。
4. 不执行 `COMMIT`，不执行 `VACUUM`，不把 `PRAGMA foreign_keys` 设为 OFF。`env.py` 已把升级包在事务里；SQLite 的 DDL 可随该事务回滚。
5. 空库路径：`base → 0001 → 0002 → 0003`。
6. 已有基元库路径：库停在 `0002_lease_invariants` 且三张表里已有回执、事件、合法租约。升级后这三张表的行逐列相等，`complete_delivery` 仍能按原令牌完成。现有 `test_lease_migration_preserves_existing_valid_data` 升到 head 的行为必须仍然成立。

升级失败：事务回滚，版本号仍是 `0002_lease_invariants`，基元行还在。不要删库重建。若进程死在提交瞬间，用 SQLite 的 WAL 恢复，然后看 `alembic_version`，再决定重跑升级或保持 0002。

`downgrade()` 可以执行，而且现有测试会 `downgrade base`，所以它必须能从空的领域库降回 0002，再降回 base。

可以降级的部分：

- 删掉 0003 的触发器和新表，使 `alembic_version` 回到 `0002_lease_invariants`。
- 三张基元表及其行不动。0002 的租约 CHECK 由它自己的降级函数处理，本修订不碰。

必须按子表到父表删除，因为降级运行在已开启外键的事务中，不能临时关闭外键。顺序：`privacy_tombstones`，`consumer_receipts`，`jobs`，候选四表，运行事件/读账/结果/依赖/任务/运行，快照引用与快照，`audit_events`，意见/测试/决定/证据，`page_edges`，`page_drafts`，`view_states`，`object_grants`，`page_versions`，`pages`，`stage_config_versions`，`stages`，`artifact_versions`，`artifacts`，`memberships`，`sessions`，`accounts`，`projects`。

不能当作降级的部分：

- 不能把已确认的页版本变回草稿，不能撤销墓碑，不能恢复被触发器拒绝的更新。降级是丢表，不是业务撤销。
- 已经写过页版本或墓碑的库，禁止用 `alembic downgrade` 当恢复。恢复走 06 的备份。开发库和验收库可以降级。
- 不提供“只删触发器、留下表”或“只保留项目、丢掉版本”的部分降级。
- 若将来的修订用 batch 重建改写了三张基元表，那一修订的降级要单独论证。0003 本身没有这种改写。

头指针不建外键，就是为了让上面的删除顺序没有环。

## 11. 认领的文件与注册

T03 编码时只写这三个路径：

- `src/sinuo/domain/schema_types.py`  
  冻结的类型、闭集、`canonical` 文档校验、`assert_append_only`、`assert_same_project_document`、`assert_role_set`、`acl_commit`、`history_statements`、`assert_no_pointer_movement`。不导入 SQLAlchemy、FastAPI、文件系统或模型 SDK。
- `src/sinuo/infrastructure/domain_models.py`  
  映射到 **已经存在** 的 `Base`。只导入 `sinuo.infrastructure.models.Base` 和领域闭集。不定义第二个 `DeclarativeBase`。
- `tests/acceptance/test_domain_schema.py`  
  使用独立临时库。数据标成合成：标题前缀 `synthetic-`。不读密钥，不指向 `var/` 或参考文档。

不认领、不修改：`models.py`、`app.py`、`pyproject.toml`、`migrations/versions/*`、`database.py`、`idempotency.py`、`domain/errors.py`。

集成 owner 在合入迁移时，在 `models.py` 末尾（`Base` 已定义之后）加入侧面导入，把领域表注册进同一个 `Base.metadata`：

```python
import sinuo.infrastructure.domain_models  # noqa: F401
```

`migrations/env.py` 已经使用这个 `Base.metadata`，因此不需要改 `env.py` 才能让 autogenerate 看见新表。0003 仍应手写，不要用 autogenerate 的 batch 重建代替。

漏掉这行导入时，`/readyz` 只检查三张基元表，领域表缺失也会报 ready。所以导入必须和 `SCHEMA_REVISION` 同一次合入。

`alembic check` 看不见触发器。验收要查 `sqlite_master`，不能只靠模型比对。

## 12. `/readyz`

建议 **保持** `scope=foundation`。不要改 `app.py` 里的这个字面量。完整产品链还没有做完，本修订只是身份和领域表，不是就绪范围扩大。

`SCHEMA_REVISION` 由集成 owner 在迁移成为 head 的同一次合入里改为 `0003_domain_identity`。在那之前常量保持 `0002_lease_invariants`。

合入之后，现有就绪逻辑会：

- 迁移版本不是该常量 → `schema_mismatch`（503）
- `Base.metadata` 里的表没有全部存在 → `schema_incomplete`（503）
- 两者都满足 → `{"status":"ready","scope":"foundation","schema_revision":"0003_domain_identity"}`

`tests/acceptance/test_domain_schema.py` 对 `/readyz` 的断言依赖这次常量更新。在集成 owner 合入之前，该断言会失败；不要为了让它先绿而把期望写回 `0002`，也不要在领域分支里私自改 `app.py`。

## 13. 验收测试要锁的行为

命令（实现之后，由编码代理在临时库上运行，不是本提案已运行的结果）：

```powershell
python -m pytest -q tests/acceptance/test_domain_schema.py
```

断言：

1. 空库 `upgrade head` 后版本为 `0003_domain_identity`，37 张表都在，`foreign_key_check` 为空，`integrity_check` 为 ok。
2. 停在 0002 的库预先写入一条回执、一条事件、一条合法租约；升级后三行不变，租约仍能完成。
3. 第 8 节的唯一约束冲突；草稿 CAS 不带动其他计数器。
4. 第 7 节的原始 SQL 更新/删除失败；墓碑插入后版本哈希不变，墓碑行不能改、不能删。
5. 第 9 节的复合外键失败；JSON、双成员、历史指针、四职责、`overwrite_page_version` 这些领域反例失败关闭。
6. `downgrade` 到 `0002_lease_invariants` 后领域表消失，基元行仍在；再 `upgrade head` 成功。测试库可以降级。文档写明含确认版本的库不用降级当恢复。
7. 历史只读夹具：只跑 SELECT 后，头指针和四个计数器不变。

最小夹具：两个合成项目、两个账号、A 的两个同阶段页、其中一页两个正式版本、一条 `branches_from`、一个作品的两个版本、一条证据、一条决定、一条测试、一条意见、一个含两个职责的运行、一条预览和来源行、一条墓碑、以及升级前就存在的基元回执。全部为合成数据。

事务示例（说明用法，不是本提案已提交的代码）：

```python
with write_uow(engine) as session:
    execute_idempotent(
        session,
        CommandScope(project_id, actor_id, "page.checkpoint", request_key),
        request,
        authorize,
        lambda db: checkpoint_inserts_only(db),
    )
```

`checkpoint_inserts_only` 在同一会话插入 `page_versions`、边、审计、Outbox，并执行项目修订 CAS 与头指针 CAS。它不调用 `commit`，不打开第二条连接，不在事务里读文件或调用模型。

## 14. 明确不在本修订里的表

名称先占住，避免下一张卡另起同义表：

| 以后的修订 | 表名 | 约束 |
| --- | --- | --- |
| T05 | `stored_objects`、`object_pins`、`upload_reservations` | 引用 `artifact_versions.id`。GC 状态不回写原件版本行 |
| T06 | `suggestion_preferences` | 列名 `preference_revision`。禁止复用草稿、布局、页版本、项目修订 |
| T06 | 建议触发记录 | 用 `jobs.job_kind='suggestion_trigger'`，不另建带第二套 `fencing_token` 的队列表 |
| T07 | FTS 索引表 | 可重建，不是页版本的权威副本 |
| T02 | 登录限速 | 若建表，不得出现密码或令牌原文列 |

节点投影表本修订不建。

## 15. 未决点

1. 敏感级别闭集暂定为 `project` / `sensitive` / `released`。若产品用词不同，只替换 CHECK 字面量，列名 `sensitivity` 不变。
2. 墓碑之后，已经写进 `page_versions.content_json` 的句子仍留在行里，读路径负责折叠。本修订不提供“红acted UPDATE”。若必须在库内抹掉这段文字，需要新的 ADR 和新修订；不能把 0003 的无条件触发器改成按列放行。
3. `project.create` 与首位维护者只用 `accounts.can_create_project`。不建 `account_capabilities`。若 T02 认为不够，先改提案再迁移。
4. 非账号主体（服务账号）不在 P0。`object_grants.account_id` 只指向 `accounts`。
5. 摘录正文不入库，只存 `excerpt_hash` 与定位。若验收要求不打开原件也显示引文，必须另加可被墓碑策略覆盖的列，不能把引文当成页版本 JSON 的唯一副本后再就地删除。
6. `jobs.subject_id` 的多态目标没有外键。T06 若要收紧，应加类型化的可空外键列，而不是新建一张作业表。
