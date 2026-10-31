# 03 领域模型与事务施工合同

状态：施工基线，2026-10-29。适用：后端 P0，供实现者与独立验收者共同使用。产品定义以锁定产品白皮书 2.1 为准；本文件细化其工程语义。技术规范曾出现单运行职责数放宽错误，2026-10-30 已与本施工包统一为最多 3 个职责；四职责链拆关联运行，见工作流合同。

## 目标与完成条件

实现一条可恢复、可追溯的业务链：项目与阶段→平行页草稿→不可变确认版本→原件及证据→AI 候选→人工确认→下一阶段空白页或历史分支。即使两个请求同时编辑、重复确认、响应丢失或 Worker 崩溃，确认版与原件仍能准确重建。以下不变量必须由数据库约束、条件更新和集成测试共同落实。

## 固定技术边界

- Python 3.12+、FastAPI、Pydantic 2、SQLAlchemy 2、Alembic；本地 P0 数据库 SQLite，启用 WAL、外键和有限 `busy_timeout`。
- 关系库保存业务身份、版本、权限、运行账本及任务；项目隔离的本地内容寻址目录保存不可变原件。预览和索引是可重建数据。
- SQLite 同一时刻只有一个写事务提交。每个请求/任务使用独立 Session；任务之间禁止共享 Session 或可变 ORM 实例。事务只包围短小数据库操作，模型、网络、文件转换和长文件读取在事务外。
- 首版采用页级草稿 CAS，暂不实现 CRDT、节点实时共编、跨数据库事务或分布式锁。替换数据库时保留本文的约束与验收。

## 不变量

| 编号 | 必须始终成立 |
| --- | --- |
| INV-01 | `PageVersion` 的内容和原件版本字节创建后不可覆盖；合法删除通过受控墓碑与清理流程处理 |
| INV-02 | 历史查询只读；不会移动任何共享“当前分支”指针 |
| INV-03 | 同章平行页没有自动先后关系；所有谱系边绑定确切版本 |
| INV-04 | 草稿内容修订、个人布局修订、正式页版本、项目提交序号各自独立 |
| INV-05 | 模型与专责能力不持有业务写仓储；只有具权者发起的领域命令可提交候选 |
| INV-06 | 确认候选、建页、来源边、决定、审计、Outbox、幂等结果在同一事务提交 |
| INV-07 | 已提交候选只能消费一次，即使两个操作者使用不同幂等键 |
| INV-08 | 引用的页、作品、节点、证据和候选属于同一项目；跨项目引用在 P0 拒绝 |
| INV-09 | 授权按当前成员与对象权限计算；历史权限快照仅用于解释运行输入 |
| INV-10 | 持久事件和重试允许重复投递，业务效果与有效派生结果可去重 |

## 聚合与数据表

使用 UUID 字符串作为公开身份；数据库时间保存 UTC；业务排序依赖整数序号，不能仅靠墙钟。JSON 使用明确版本的 Pydantic Schema，拒绝未知危险字段。JSON 内容哈希采用确定性规范化 UTF-8 表示：键排序、固定分隔符、禁止 NaN/Infinity；数组保持语义顺序。价格、字节数、修订号用整数或明确的 Decimal 表示，避免浮点哈希歧义。

| 聚合/表 | 核心字段与约束 | 所有者 |
| --- | --- | --- |
| `projects` | id、title、revision、access_epoch；revision 原子递增 | 项目服务 |
| `memberships` / `object_grants` | project、subject、action、object scope；唯一授权键 | 授权服务 |
| `stages` / `stage_config_versions` | 稳定 stage_id，配置版、顺序、目标、effective_project_revision | 版本服务 |
| `pages` | id、stage_id、head_version_id、status、title | 版本服务 |
| `page_drafts` | page_id 唯一、base_version_id、content_revision、content_hash、content_json | 草稿服务 |
| `view_states` | subject_id、page/version、view_name、layout_revision、layout_json；个人偏好 | 视图偏好服务 |
| `page_versions` | id、page_id、version_no、parent_version_id、content_json、hash、project_revision；UNIQUE(page_id,version_no) | 确认命令 |
| `page_edges` | source_version_id、target_version_id、kind、project_revision、reason；有类型谱系 | 确认命令 |
| `artifacts` / `artifact_versions` | 稳定作品 ID；版本号、hash、size、MIME、object_key、license、sensitivity；UNIQUE(artifact_id,version_no) | 原件服务 |
| `evidence_anchors` | 原件版、locator_type、locator_json、excerpt_hash、extractor_version、可见性 | 证据服务 |
| `decisions` / `test_records` | 责任人、理由/条件、确切作品版；更正通过 supersedes_id 追加 | 领域命令 |
| `review_opinions` | 作者、锚定版本/节点、body、revision、visibility | 授权审阅服务 |
| `agent_runs` / `run_tasks` / `agent_results` | 运行与任务生命周期；独立任务槽、attempt、lease、fence | 运行服务 |
| `context_snapshots` / `run_read_records` | 初始输入冻结快照；实际读取的追加清单 | 上下文服务 |
| `candidates` / `candidate_previews` / `candidate_dispositions` | 原提案、输入哈希、状态修订；用户修订和预览绑定 | 候选服务 |
| `command_receipts` | project、actor、command、idempotency_key、request_hash、response_json；联合唯一 | 命令服务 |
| `audit_events` / `outbox_events` | 追加审计、消息；业务事务一起提交 | 命令服务 |
| `jobs` / `consumer_receipts` | 任务租约与 fencing；UNIQUE(event_id,consumer) | Worker |
| `privacy_tombstones` | 对象范围、删除理由类别、纪元、审计号；不存被删正文 | 隐私命令 |

节点与页内边在 P0 可以存于受 Schema 约束的草稿/页版本 JSON；给节点分配稳定 `node_id`，边分配 `edge_id`，并建立可检索的只读投影。投影不是权威写入入口。实现者可改为规范化节点版本表，但必须证明不会让最新节点内容回填旧快照，且不改变 API 合同。

`PageDraft` 的历史只承担尚未确认的工作态，不承诺每次键击永久保存。`PageVersion` 保存冻结节点/边和确切原件版本引用；创建正式版本后，把草稿 base 指向新版本，继续下一修订。布局写入 `view_states`，旧规范中的 `draft_layout_revision` 对外字段映射到相应个人视图修订，不能共用页级业务内容行。

## 事务矩阵

| 命令 | 事务内校验 | 原子写集合 | 不产生的结果 |
| --- | --- | --- | --- |
| TX-01 编辑草稿 | 当前权限、base、expected_content_revision | 条件更新草稿、草稿审计、命令回执 | 不创建 PageVersion 或 ProjectRevision |
| TX-02 保存为版本 | 当前权限、expected HEAD、草稿修订/哈希、引用有效 | 新 PageVersion、HEAD CAS、草稿 base、ProjectRevision、Audit、Outbox、Receipt | 不自动创建下一章页 |
| TX-03 从历史分支 | 源版存在且可读、目标章可写、来源合法 | 新 Page+v1+草稿、branches_from、来源映射、ProjectRevision、Audit、Outbox、Receipt | 不更新源页 HEAD、不复制旧测试为“通过” |
| TX-04 确认阶段/合流候选 | 当前决策权、预览绑定、候选状态、全部来源预条件 | 原子消费候选、新空白 Page+v1、来源边、显式引用、Decision、ProjectRevision、Audit、Outbox、Receipt | 不复制旧页全部节点、不自动推进其他页 |
| TX-05 普通候选写入草稿 | 草稿写权、候选/预览、修订 CAS | 候选消费、类型化草稿变更、处置、审计、Receipt | 不新建正式版本、不递增项目确认时间轴 |
| TX-06 拒绝候选 | 可处置且当前可读、状态仍待处置 | 状态 CAS、拒绝处置、运行事件、Receipt | 不改业务图 |
| TX-07 上传登记 | 字节已校验并不可变落盘、票据归属、许可、当前上传权 | ArtifactVersion、ProjectRevision、Audit、Outbox、Receipt | 不把预览标成成功 |
| TX-08 留审阅意见 | 授权范围、锚定版本 | 意见、审计、Receipt；如计入项目历史则增加项目修订 | 不改设计节点 |
| TX-09 修改权限 | acl.manage、目标范围 | ACL、access_epoch++、审计、失效 Outbox、Receipt | 不靠异步失效才生效；读授权立即基于新 ACL |
| TX-10 发起运行/导出 | 当前授权、输入完整、预算 | 冻结快照、Run/ExportJob、任务、事件、Receipt | 不执行外部模型、不生成设计版本 |

项目历史的确认修订包括原件登记和已确认业务对象变更；草稿、运行轨迹、派生状态单独记时间。审阅意见若纳入统一项目时间线，必须在迁移与文档中采用单一口径；建议 P0 纳入 ProjectRevision，便于按时间重建审阅。

## 幂等与条件更新的执行顺序

关键写命令必须带 `Idempotency-Key`。唯一作用域是 `(project_id, actor_id, command_type, idempotency_key)`；创建项目使用账号作用域 `project_id='account:'+actor_id`。规范化请求哈希包括路径目标、命令类型、预期版本、操作正文、预览 ID/哈希，排除传输时间与重试计数。同键同体返回已保存业务结果；同键异体返回 `409 IDEMPOTENCY_KEY_REUSED`。

短命令采用一个事务：读取并验证当前身份/权限→查回执→若存在则对请求哈希并按当前权限投影原响应→若不存在则校验业务前提→执行 CAS/插入→写审计、Outbox 和回执→提交。SQLite 可在写命令入口使用 `BEGIN IMMEDIATE`，降低读后升级锁的竞态；测试必须在独立连接/进程下成立。唯一约束兜住并发同键。数据库忙时只能有界重试整个未提交事务，不能重试已提交后的业务主体。

提交成功但 HTTP 响应丢失时，原键重试返回同一对象。已无读取权限的用户不能借回执获得原响应里的敏感内容；返回受限结果或安全提交状态。失败回滚不留下“成功”回执。长任务的启动回执只表示 Job 已可靠登记，状态由 Job 查询。

草稿必须用条件 UPDATE，不能“读 revision 后普通 UPDATE”：

```sql
UPDATE page_drafts
SET content_json=:new_content,
    content_hash=:new_hash,
    content_revision=content_revision+1
WHERE page_id=:page_id
  AND base_version_id=:base_version
  AND content_revision=:expected_revision;
```

受影响行数为 0 时返回 409，保留调用者的编辑内容和可见的远端版本信息。新内容在事务外完成纯计算可行；事务内仍需复核校验依赖和 CAS 条件。

## 预览绑定与候选原子消费

预览必须由服务器生成。`candidate_previews` 保存 `preview_id`、`candidate_id`、candidate_revision、actor_id、当前 access_epoch、规范化 `user_patch`、完整 `effective_proposal`、全部 expected 版本、`preview_hash`、expires_at。预览只写操作域，不创建任何业务版本。默认有效期可配置，建议起始值 10 分钟，不作为性能承诺。

提交请求只携 `preview_id + preview_hash + expected_* + idempotency_key`；服务器从预览取有效提案。若客户端再传 `user_patch`，其规范化哈希必须与预览一致；任何修改都重新预览。提交时重新授权、校验源对象与预览有效期，然后用 `WHERE candidate.status='pending' AND candidate.revision=:r` 的条件更新消费候选。候选消费与目标对象写入同事务，任一步失败全部回滚。

候选被另一键或另一操作者先消费后返回 `409 CANDIDATE_ALREADY_RESOLVED`，附可见的处置引用。不能用幂等键解决不同请求键的双重确认。历史运行、过期来源、布局变化和草稿内容变化须分别处理：历史解释不能直写旧版；布局变化不使内容候选过期；内容变化使其重新检查。

## 三种新页模式

`stage_blank`：一或多个当前有效来源版本，目标章明确，主体空白，用户选中的资料引用可进入引用区。`merge_blank`：多来源空白页，保留每条来源、各自版本和冲突提示。`history_branch`：从一个明确旧版克隆可编辑节点/边到新页，分配新 ID，保存 origin 映射；原件只引用旧版本，旧决定/测试作为历史依据。

P0 的关键动作使用“先保存为版本，再重新检查”规则。处于未确认草稿上的阶段建议可以展示，但提交阶段/合流要求基于已确认快照重新运行。普通便签候选可在 CAS 成功后写草稿。谱系边方向按创建顺序，不按阶段顺序；回访研究阶段仍建新的页版本，避免环。

## 开发者可自主决定的部分

实现者可优化表索引、仓储结构、纯计算缓存、JSON 与规范化表的分配，以及批量读投影；先写等价测试再调整。外部可见版本语义、权限边界、原子提交集合、候选只消费一次和“不覆盖原件”属于验收合同。发现合同无法同时满足时，用 ADR 提供最小复现、方案比较和影响，不在实现里默默修改规则。
