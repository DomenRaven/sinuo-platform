# T13 本次读取范围（可勾选读集）

- 状态：implementing（T13a 排除项 + T13b 多版本读集 + 前端接入 均已完成；索引修订快照+摘要来源哈希 → T13c）
- Owner：Agent（T13a+T13b+前端）
- 基线提交或快照：未提交（本地施工）；迁移 head `0010_evidence_index`
- 上游交接：`workpool/tasks/T12_UI_VISUAL_WORKBENCH.md`（前端读集面板已做只读版）、`CONTROLLER_HANDOFF_2026-11-01_UI.md`、`workpool/handoffs/reviews/T13_CONTEXT_SCOPE_REVIEW.md`
- 精确允许写入范围：后端新增读集候选列表接口 + `StartRunBody` 读集参数 + 运行时读集构建。涉及 `src/sinuo/api/`、`src/sinuo/application/agents/execute.py`、`src/sinuo/application/retrieval/`、`src/sinuo/application/evidence/`。**不改** 假模型链、`demo-5min/`。
- 当前假设 / 下一步：**评审后拆分**——先做 T13a 排除项（最小闭环），再做 T13b 多版本读集（大切片）。

## 目标行为

设计师在单页工作台右侧的「本次读取范围」面板，能**勾选/取消**下一次运行要纳入的读集对象；按钮写「用于下一次检查」而非「切换项目记忆」。改变读集只影响下一次 `AgentRun` 的 `ContextSnapshot`，不改动已确认项目事实，不删历史。

## 评审修正后的范围（2026-11-01）

评审（`reviews/T13_CONTEXT_SCOPE_REVIEW.md`）指出三点：

1. **`exclusions_json` 从未被消费**——全仓库只有写入 `"[]"`，无读取点。S3"尊重排除项"是**从零实现**，不是接线。
2. **policy run 的多版本读集是覆盖校验，不是检索读集**——`_policy_binding_ok`/`_outbound_allowed` 只判断"是否还覆盖当前页/作品是否允许 AI"，不改变 `search_evidence` 检索范围。不能直接复刻。
3. **`search_evidence` 只支持单版本过滤**（`artifact_version_id: str | None`），多版本读集需改签名或循环调用。

据此拆两片：

### T13a 排除项（最小闭环，先做）

- 只做 `exclusions`：`StartRunBody.exclusions: list[str] | None` → `_persist_run` 写 `exclusions_json`（非空数组）→ `_collect_tools` 读排除项，`search_theory`/`lookup_method` 加两个 if 分支跳过。
- **不碰 `search_evidence`**。这是前端"四类读源"面板能立即受益、风险最低的一步。

### T13b 多版本读集 + 索引快照（大切片，后续）

- S1 候选列表接口：**需新建三个读函数**（证据锚点列表 / 方法卡列表 / 邻页版本列表，各按当前主体逐对象授权，参照 `activity.py::_allows`）。
- S2 `StartRunBody` 增加 `page_version_ids` / `artifact_version_ids`。
- S3 `search_evidence` 支持多版本过滤（改签名或循环合并，认领时二选一）。
- 补充技术规范 4.4 的「索引修订快照 + 摘要来源哈希」，否则读集切换后重演会读到后来变化的索引。

## 输入与输出合同

### T13a（本卡先做）

- `StartRunBody.exclusions`：`list[str] | None`，元素白名单如 `"theory"` / `"methods"`（认领时定死）。
- `_persist_run`：`exclusions_json` 由 `"[]"` 改为 `json.dumps(exclusions)`（缺省仍 `"[]"`，向后兼容）。
- `_collect_tools`：`search_theory` 在 `"theory" in exclusions` 时跳过；`lookup_method` 在 `"methods" in exclusions` 时跳过（method 角色原本不调 theory，注意角色差异）。
- 排除项只影响本次运行，不改索引、不改事实。

### T13b（后续）

- S1 候选列表接口：路径建议 `GET /api/v1/projects/{project_id}/context-scope`，返回四类可纳入对象（页版本/证据锚点/作品版本/方法卡），带授权与 `ai_processing_allowed` 标记；无权限只标受限。授权动作认领时二选一（倾向 `PROJECT_READ`）。
- S2 `StartRunBody`：`page_version_ids` / `artifact_version_ids`，越权 id 拒绝（参照 `_outbound_allowed`）。
- S3 `search_evidence` 多版本过滤；`ContextSnapshotRef` 写多行；索引修订快照 + 摘要来源哈希落库。

## 验收

| 步骤 | 内容 | 可观察通过条件 |
| --- | --- | --- |
| T13a-S1 | 排除项参数 | `StartRunBody.exclusions` 可选，缺省向后兼容；有单测 |
| T13a-S2 | 排除项落库 | 启动后 `ContextSnapshot.exclusions_json` 非空；有单测 |
| T13a-S3 | 排除项生效 | `exclusions=["theory"]` 时 `search_theory` 不调用；`["methods"]` 时 `lookup_method` 不调用；有单测 |
| T13b-S1 | 候选列表接口 | `GET /context-scope` 返回四类对象，带授权标记；无权限只标受限；有单测 |
| T13b-S2 | 读集参数 | `page_version_ids`/`artifact_version_ids` 启动，越权拒绝；有单测 |
| T13b-S3 | 多版本检索 | `search_evidence` 尊重多版本范围；索引快照落库；有单测 |

**回归门槛**：全量 `pytest -q -m "not live_model"` 保持通过（当前 439 passed, 2 deselected），不新增 `live_model` 依赖。

## 实际证据

- 前端 T12 已做只读读集面板，标注"手动勾选属于后续能力"。
- 评审确认：`exclusions_json` 只写未读；policy run 多版本读集是覆盖校验；`search_evidence` 单版本过滤；候选列表三读函数不存在。详见 `reviews/T13_CONTEXT_SCOPE_REVIEW.md`。

### T13a 已完成（2026-11-01）

- `AgentRunRequest.exclusions: tuple[str, ...] = ()`、`_Frozen.exclusions: frozenset[str]` 两个 dataclass 字段。
- `_load_frozen` 从 `snapshot.exclusions_json` 读排除项（`_exclusions_from_json`）；`_persist_run` 用 `_normalized_exclusions` 过滤白名单后写 `exclusions_json`（`sorted` 保证确定性，缺省仍 `"[]"`）。
- `_collect_tools`：`"theory" in exclusions` 跳过 `search_theory`；`"methods" in exclusions` 跳过 `lookup_method`（method 角色）。
- `StartRunBody.exclusions: list[str]`（白名单 `{"theory","methods"}`，越界值 422，去重）；`_run_request` 传 `tuple(body.exclusions)`。
- 新增 `tests/acceptance/test_context_exclusions.py`：5 条测试（theory 排除/方法排除/每运行独立/未知键忽略/HTTP 校验落库）。
- 全量 **444 passed, 2 deselected**（439 基线 + 5 新增）。

### T13b 已完成（2026-11-01）

**S1 候选列表接口**：新建 `src/sinuo/application/workspace/context_scope.py`（`list_context_scope`，四类对象逐对象授权 + `has_hidden`）；`GET /api/v1/projects/{project_id}/context-scope`（`workspace/routes.py`）。返回 page_versions / evidence_anchors / artifact_versions / method_cards，无权限对象只标 `has_hidden`，不泄露标题。

**S2 StartRunBody 读集参数**：`StartRunBody.page_version_ids` / `artifact_version_ids`（各 ≤8，去重，格式校验）；`AgentRunRequest` 加同名字段。领域层 `_validated_page_versions`（属项目 + `page.read`）/ `_validated_artifact_versions`（属项目 + `artifact.original.read` + `ai_processing_allowed=1`），越权 `RunPlanRejected` → HTTP 422。

**S3 多版本检索 + 落库**：`search_evidence` 加 `artifact_version_ids: tuple[str, ...] | None`（`None` 不限制、`()` 空集）；`_Frozen` 加 `page_version_ids`/`artifact_version_ids`（frozenset），`_load_frozen` 从 scope 读；`_persist_run` 写 scope 两键 + `ContextSnapshotRef` 多行（当前页 + 邻页）；`_collect_tools` 的 `_read_set_versions` 读邻页快照提取 `artifact_version_ids` 并入检索范围。

- 新增 `tests/acceptance/test_context_scope.py`：6 条测试（S1 接口 / S2 越权拒绝 3 类 / S3 落库 + 缺省兼容）。
- `tests/acceptance/test_retrieval_methods.py` 加 `test_search_evidence_multi_version_filter`。
- 全量 **451 passed, 2 deselected**（444 基线 + 7 新增）。
- 真实库验证：`context-scope` 对「夜间取物」返回 200，1 页版本 + 1 证据锚点 + 1 作品版本。

**T13c（后续，未做）**：索引修订快照（`index_revision` 实际数字版本）+ 摘要来源哈希（`context_source_set_hash`）+ 重演按清单读取机制。当前 `index_revision` 仍写 0（FTS 无数字版本号概念），本轮不做。

## 交接

- 认领 T13a 时读 `execute.py` 的 `_Frozen`（需新增排除项字段）、`_persist_run`、`_collect_tools`、`_course_theory`、`lookup_method` 五处。
- 认领 T13b 时先读 `activity.py::_allows` 的逐对象授权模式，新建三个读函数。
- 不需要新迁移：读集字段都是 JSON/Text，`ContextSnapshotRef` 已支持多行。
- 不要改 `intent.py` 意图词表；不动假模型链与 `demo-5min/`。
- 前端接 T12 时，勾选状态只影响下一次 `POST /agent-runs`，不回写权威事实。

## 修复历史

- 2026-11-01 评审：原卡把 S3 写成"接线"，实为从零实现；原卡把 policy run 覆盖校验误当检索读集参照。已拆 T13a/T13b，修正范围与验收。
- 2026-11-01 T13a 施工：`frozenset` 迭代顺序不确定导致 `_stored_exclusions` 断言偶发失败，`_persist_run` 改为 `sorted(exclusions)` 保证确定性。
- 2026-11-01 T13b 施工：邻页版本语义定为「读快照 → 提取页内引用作品版本 → 并入证据检索范围」；`artifact_version_ids=()` 表示空集（不匹配任何），`None` 表示不限制，据此区分「未勾选读集」与「勾选但无引用」。前端接入留待 T13 整段结束后统一做。
