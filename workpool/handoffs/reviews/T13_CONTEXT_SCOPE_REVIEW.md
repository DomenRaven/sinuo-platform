# T13 本次读取范围（可勾选读集）审查

日期：2026-11-01。只读。对照 `workpool/tasks/T13_CONTEXT_SCOPE.md` 的三条验收（S1 候选列表接口、S2 StartRunBody 读集参数、S3 运行时读集构建），读了 `src/sinuo/application/agents/execute.py`（`open_agent_run`、`open_policy_run`、`_persist_run`、`_collect_tools`、`_execution_allowed`、`_policy_binding_ok`、`_policy_excerpts`、`_outbound_allowed`、`_covers_page`）、`src/sinuo/application/retrieval/search.py`、`src/sinuo/application/evidence/anchors.py`、`src/sinuo/application/retrieval/methods.py`、`src/sinuo/infrastructure/domain_models.py`（`ContextSnapshot`、`ContextSnapshotRef`）、`src/sinuo/api/suggestions/routes.py`，以及 `docs/reference/思诺智能体辅助设计平台技术实施规范_v1.1.md` 第 4.4、14.3 节。

结论：**blocking 2 条，note 4 条。** T13 卡把 S3 写成"接线"，实际上"按用户勾选的读集过滤检索"当前**不存在**，是从零实现。S1 的候选列表也需要新建三个读函数。范围比卡里写的大，接近一个新功能切片，不是"暴露已有扩展点"。

## blocking

### B1. `exclusions_json` 从未被消费，S3 的"尊重排除项"是从零实现

- 标记：blocking
- 文件：`execute.py` 第 394 行、`fake_chain.py` 第 295 行只写 `exclusions_json="[]"`。全仓库 `grep exclusions` 无任何读取点（只有模型定义、CheckConstraint、两处写入）。
- 影响：T13 卡 S3 说"`_collect_tools` 尊重 `exclusions` 含 `"theory"` 时跳过 `search_theory`"——但 `_course_theory`（`execute.py` 第 1210 行）和 `lookup_method` 都不接受排除项参数，也没有任何地方读 `exclusions_json`。这不是"接线"，是要在 `_Frozen` 里新增排除项字段、从 snapshot 读出、传到检索函数、并在检索函数里加过滤分支的**完整新实现**。

### B2. policy run 的"多版本读集"是覆盖校验，不是检索读集，不能直接复刻

- 标记：blocking
- 文件：`open_policy_run` 的 docstring（第 299–303 行）说它读 `page_version_ids`、`queries`、`artifact_version_ids`。但实际消费点是 `_policy_binding_ok`（第 2228 行）和 `_outbound_allowed`（第 2299 行）——前者用 `_covers_page` 判断"这次 policy run 是否还覆盖当前页/当前 query"，后者校验"作品版本是否允许 AI 处理"。**它们都不改变 `search_evidence` 的检索范围。**
- 影响：T13 卡 S3 说"复刻 policy run 的读集校验"，前提不成立。手动 run 要"只检索用户勾选的对象"，得改 `search_evidence` 支持多版本过滤（当前只支持单个 `artifact_version_id`，见 `search.py` 第 73 行、第 86–100 行），这是一处签名和行为改动，不是复制。

## note

### 1. `search_evidence` 只支持单版本过滤，多版本读集需改签名

- 文件：`search.py` 第 65–75 行，`artifact_version_id: str | None`，第 97–100 行 `version_ids = [version_filter]`。
- 影响：要支持"勾选多个作品版本"或"勾选多个邻页版本"，要么加 `artifact_version_ids: list` 参数，要么在 `_collect_tools` 里对每个勾选版本各跑一次 `search_evidence` 再合并（后者改动小、但会多轮调用）。认领时需二选一并写进卡。

### 2. 候选列表所需的三个读函数都不存在

- 文件：`evidence/anchors.py` 只有 `create_anchor`（写）；`methods.py` 只有 `lookup_method`（按 query 检索，不是列表）；`outline.py` 的 `read_project_stages` 返回页列表，不返回页版本列表。
- 影响：S1 的"证据锚点列表 / 方法卡列表 / 邻页版本列表"都要从零新建读函数，且每个都要按当前主体逐对象授权（参照 `activity.py` 里 `_allows` 的模式）。这是 S1 的实际工作量，卡里没体现。

### 3. 技术规范 4.4 定义的读集比卡的 S1–S3 更宽

- 文件：技术规范第 4.4 节：读集 = 当前页版本与可选草稿修订 + 获授权邻页版本 + 证据锚点 + 作品版本 + 方法卡版本 + 索引修订 + 权限快照 + 排除项 + 摘要来源哈希。
- 影响：完整 R11 是一个大切片。卡的 S1–S3 只覆盖了"候选列表 + 参数 + 检索过滤"的最小闭环，**没提"索引修订快照"和"摘要来源哈希"**。这两个对"重演时按当时的索引读、不能凭后来变化的索引重搜"（技术规范第 341 行）是必需的，否则读集切换后重演会读到新索引。认领时应明确：第一版是否只做"排除项"这一最小能力，把多版本读集 + 索引快照拆到后续。

### 4. 建议的最小范围重估

- 影响：对课程汇报而言，最有价值、风险最低、且前端面板（T12 已做的四类读源展示）能立即受益的是**排除项**（用户勾"本次不读理论库/不读方法库"）。它只动 `_Frozen` + `_persist_run` 读 `exclusions_json` + `_collect_tools` 两个 if 分支，不碰 `search_evidence` 签名。完整多版本读集（S1 列表 + S3 多版本检索）工作量大，建议拆成 T13a（排除项）与 T13b（多版本读集 + 索引快照）。

## 验收覆盖

| 条 | 卡的验收 | 审查后的实际情况 |
| --- | --- | --- |
| S1 候选列表接口 | 返回四类可纳入对象 | 需新建证据锚点/方法卡/邻页版本三个读函数，卡没写这个工作量 |
| S2 StartRunBody 读集参数 | 扩展 body，越权拒绝 | 可行，缺省向后兼容；但"多版本 id"的校验要参照 `_outbound_allowed`，不能照搬 policy 的覆盖校验 |
| S3 运行时读集构建 | 写读集进 snapshot + `_collect_tools` 尊重 | **B1/B2**：排除项从未消费、`search_evidence` 不支持多版本，是新建不是接线 |

## 建议

1. 先把 T13 拆成 **T13a 排除项**（最小、风险低、前端立即受益）与 **T13b 多版本读集 + 索引快照**（大切片，后续）。
2. T13a 只做：`StartRunBody.exclusions` 参数 → `_persist_run` 写 `exclusions_json`（非空）→ `_collect_tools` 读排除项，`search_theory`/`lookup_method` 两个 if 分支跳过。不碰 `search_evidence`。
3. 修正 T13 卡，把 S3 从"接线"改为"从零实现"，并标注 S1 需新建三个读函数。
