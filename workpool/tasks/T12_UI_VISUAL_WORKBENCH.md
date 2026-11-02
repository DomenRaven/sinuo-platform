# T12 界面可视化工作台

- 状态：implementing
- Owner：Agent（续 Cursor 界面总控施工）
- 基线提交或快照：未提交（本地施工）；迁移 head `0010_evidence_index`
- 上游交接：`workpool/handoffs/CONTROLLER_HANDOFF_2026-11-01_UI.md`（界面总控交接）、`AGENT_HANDOFF_2026-11-01_CANDIDATE.md`（候选链路）
- 精确允许写入范围：`web/desk.html`（唯一写入文件）。**不改** 后端 API、迁移、`app.py`、假模型链、`demo-5min/`。
- 当前假设 / 下一步：后端已有可点路径（登录→阶段→页→上传→问下一步→候选确认），本卡把这条路径升级为「可视化工作台」。

## 目标行为

用户打开思诺界面，能直观看到三件事，而不是一串文字列表：

1. **每个阶段有哪些智能体在起作用**（职责分工可视化）。
2. **用户与智能体的交互流程**（意图分流 → 角色 → 工具调用 → 产出 → 候选）。
3. **智能体的产出及其依据来源**（结论 + 来源溯源 + 缺口，而非聊天对话框）。

同时凸显「设计大数据基础」课程特色：本体建模（节点=类）、BOM（项目=物料树）、RAG（产出带 citations 溯源）、多智能体（角色契约 + ReAct 工作流）。

## 设计主线（三条 + 贯穿原则）

课程理论来源：课件 09–15 讲（本体、BOM、向量 RAG、GraphRAG、多智能体）。

- **主线① 设计 BOM（第 10 讲）**：项目=总成，阶段=部件，页=子部件，节点/作品=零件（带版本）。
- **主线② 智能体工作流（第 13–15 讲）**：意图分流 → 角色依赖 → 工具调用 → 产出，ReAct 循环可见。
- **主线③ 页内知识图谱（第 9、12 讲）**：七类节点=本体类，按职责着色（澄清蓝/证据绿/方法紫/批评琥珀/人确认灰）。
- **贯穿原则 来源可溯源（第 11 讲 RAG）**：每个产出卡 = 结论 + 依据来源 + 缺口，三者并列，结论不冒充原话。

## 输入与输出合同

- 前端为纯静态 `web/desk.html`，只消费既有只读 API，不新增后端端点：
  - `GET /api/v1/projects`、`GET /api/v1/projects/{id}/stages`、`GET /api/v1/pages/{id}/draft`
  - `GET /api/v1/projects/{id}/artifacts`、`GET /api/v1/projects/{id}/candidates`、`GET /api/v1/agent-runs/{id}`
  - 写入复用既有端点（session/projects/pages draft/uploads/agent-runs/candidates preview/commit/reject）。
- **关键契约（测试锁定）**：`test_workspace_outline.py::test_desk_shell_and_read_routes_are_mounted` 断言 `"表达与复盘" not in home.text` —— **desk.html 不得硬编码八阶段名称**，阶段名必须来自 API。
- 四职责界面名：课题澄清 / 证据查找 / 方法建议 / 过程检查（白皮书第 3.1 节）。
- 视觉：苹果式（简洁、明亮、大卡片、圆角、路径清楚），压过 UI 白皮书第 7.1 节色值。

## 验收（依次施工顺序）

| 步骤 | 内容 | 可观察通过条件 |
| --- | --- | --- |
| S0 已交付 | 三主线基础（BOM 树 / 阶段流程条 / 阶段×职责矩阵 / 页内节点图着色 / 运行工作流 / 产出卡溯源） | 全量 439 passed；JS 语法通过；TestClient 字段对齐 |
| S1 | 四职责能力总览图 | 首屏或项目概览能看到四类职责各是什么、产出什么、用什么工具，不露虚构头像 |
| S2 | 意图分流可视化 | 用户问句 → 分流规则 → 派出的职责，可见；四个快捷动作（澄清/找依据/选方法/检查下一步）可点 |
| S3 | 工具调用链可视化 | 运行内 search_evidence / search_theory / get_page_snapshot 等工具调用画成步骤链，标注命中/空 |
| S4 | 产出多模态增强 + 图表 | 证据命中数、阶段覆盖等以图表呈现；来源可点击溯源；全量测试仍 439 passed |

## 实际证据

- S0–S4 全部完成：`pytest -q -m "not live_model"` → **439 passed, 2 deselected**（2026-11-01 晚，施工前后一致）。
- `node --check` 通过；`grep` 确认 desk.html 0 处硬编码八阶段名（符合测试契约）。
- 四个快捷动作 intent 词分流实测：`简报变化→clarification`、`查证据→evidence`、`选方法→method`、`下一步→evidence+critic`。
- 服务启动后 HTML 含全部可视化元素：四类智能体职责、设计进度、智能体工作台、意图分流、工具链、能力总览。
- 真实库数据：证据查找 → 读到 2 来源 → 过程检查给结论 → 候选「建议进入阶段」preview_ready=True，溯源链路完整。

### 本次依次施工完成项

| 步骤 | 实现 | 位置 |
| --- | --- | --- |
| S0 | 三主线基础（BOM/工作流/知识图谱） | `renderStages`/`bomTree`/`stageFlowbar`/`stageRoleMatrix`/`pageNodeGraph`/`renderRun`/`roleCard` |
| S1 | `roleCapabilities()` 四职责能力总览（产出+工具） | 书架首屏 |
| S2 | `intentRules()` 意图分流规则 + `quickActions()` 四快捷动作 | 智能体工作台 |
| S3 | `fetchToolCalls()` 解析 SSE + `toolChain()` 工具调用链 | 运行结果 |
| S4 | `stageCoverage()` 阶段覆盖度条形图 + 来源可点击溯源 | 项目概览 + roleCard |
| R05 读取范围 | `readScopePanel()` 本次读取范围（四类读源+当前页版本+本页节点统计） | 智能体工作台顶部 |

### R05「本次读取范围」面板的边界说明（诚实标注）

- 后端读集是**运行时自动构建**（`execute.py::_collect_tools`），四类固定读源：`search_evidence`（项目内证据索引）+ `get_page_snapshot`（当前页快照）+ `search_theory`（课程理论库）+ `lookup_method`（方法卡库，仅「选方法」时）。
- **没有**独立的「读集勾选」接口，`StartRunBody` 只接受 `page_version_id` 与 `draft_content_revision`；也**没有**「记忆目录」列表接口。
- 因此面板做**诚实只读展示**：列出四类读源 + 当前页版本 + 本页已确认节点统计，并明确标注「手动勾选邻页/证据锚点/方法卡属于后续能力」——不编造可勾选交互。

## 交接

- 只动 `web/desk.html`，禁止改后端与测试。
- 阶段名一律从 API 动态取；职责矩阵按 `position` 索引，不硬编码阶段名。
- 未派到的职责不画成已出场；状态不只靠颜色。
- 施工完成后重跑 `pytest -q -m "not live_model"` 确认 439 无破坏。

## 修复历史

- 首版 `STAGE_AGENTS` 硬编码了「成果表达与复盘」，触发 `test_workspace_outline.py` 断言失败（1 failed）。根因：违反"阶段名可配置、不得硬编码"契约。修复：改为 `STAGE_ROLES[position]` 按位置索引，阶段名从 `reading.stages[].name` 动态读取。
