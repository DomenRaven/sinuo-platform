# 思诺后端施工工作池

基线：本施工包交付时的项目骨架。UI 延后。状态以实际测试和集成证据更新；T01 仅在 ADR-003 明确的 G0 基础范围内完成。T02 与 T03 已 done，不等于 G1。T04 已于 2026-10-31 复审后 done，不等于 G2。用户于 2026-10-31 决定绕开 GLB 和其他 3D 工程，见 `workpool/handoffs/USER_WAIVE_3D.md`。2026-11-01 的范围决定见 `workpool/handoffs/USER_SCOPE_2026-11-01.md`。下一窗口做界面，先读 `workpool/handoffs/CONTROLLER_HANDOFF_2026-11-01_UI.md`。外观按用户当晚决定：苹果式，简洁大气，明亮，圆角，可用大卡片，层次和路径要清楚。T05、T06、T08、T10、T11 是 implementing。T07、T09 是 review。都不标 done。G0–G4 不签。

| 任务 | 当前状态 | 依赖 | Owner | 当前证据 / 下一步 |
| --- | --- | --- | --- | --- |
| T01 基础与配置 | done | 无 | Codex（历史基线交付） | G0基础范围通过；原生产认证条目转T02未测；66项为历史基础实测，见ADR-003与T01交接 |
| T02 身份与授权 | done | T01 | Grok 主控 | 复审 blocking 0。见 T02_T03 交接 |
| T03 领域模型与迁移 | done | T01 | Grok 主控 | 领域修复在 0004。复审 blocking 0。N2–N4 按 ADR-004 关闭。仓库当前 head 是 `0010_evidence_index` |
| T04 草稿、版本与幂等命令 | done | T02,T03 | Grok 主控 | 复审 blocking 0，验收 6 条都有直接测试。见 T04 交接 |
| T05 原件与派生任务 | implementing | T04 | Grok 主控 | PNG/PDF 有测试。上传三条 HTTP 已挂进 `create_app`，`test_upload_http.py` 3 passed。GLB 仍不做，不写成 done |
| T06 持久任务、Outbox 与 SSE | implementing | T04 | Grok 主控 | `jobs` 仍无 `retry_wait`。`run_tasks` 有。用户要求此处少花精力。不标 done |
| T07 证据检索与方法库 | review | T05 的非 3D 原件 | Grok 主控 | FTS5 与 `rebuild_evidence_index` 已测。方法卡与版本对比已接。概念页和方法页已导出到 git 忽略的 `var/theory`，启动运行经 `SINUO_THEORY_ROOT` 读取。原始课件不在仓库。不标 done |
| T08 智能体运行与 DeepSeek | implementing | T06,T07 | Grok 主控 | `POST /projects/{id}/agent-runs` 已挂上。测试注入 `app.state.agent_provider`。澄清和证据用 `deepseek-flash`，方法和批评用 `deepseek-v4-pro`。假模型链未改。T06 未签收，不标 done |
| T09 候选预览与确认 | review | T04,T08 | Grok 主控 | 预览、确认、驳回和三个草稿动作有 HTTP。确认前会重查证据锚点。不标 done |
| T10 审阅、导出与恢复 | implementing | T05,T09 | 未分配 | 审阅意见、导出登记、`drain_export_jobs`、备份恢复已测。T05 和 T09 还没 done，不标 done |
| T11 纵向与故障验收 | implementing | T06,T10 | Grok 主控 | `test_end_to_end.py` 已调用上传、证据锚点、测试记录、`FakeModelProvider` 启动运行和 `drain_export_jobs`。阶段空白页、3D、真实模型仍写未执行。G0–G4 不签。不标 done |
| T12 界面可视化工作台 | implementing | T09 | Agent（续界面总控） | 只动 `web/desk.html`。三主线（设计BOM/智能体工作流/页内知识图谱）已落 S0，全量 439 passed。依次施工 S1 四职责总览、S2 意图分流、S3 工具调用链、S4 产出多模态。不标 done |
| T13 本次读取范围（可勾选读集） | implementing | T08,T09 | Agent（T13a+T13b） | T13a 排除项已完成（exclusions→落库→跳过 theory/methods）。T13b 多版本读集已完成（context-scope 候选列表接口 + page_version_ids/artifact_version_ids 参数 + search_evidence 多版本过滤 + ContextSnapshotRef 多行 + 邻页并入检索），全量 451 passed。索引修订快照+摘要来源哈希 → T13c。前端接入待 T13 整段结束后统一做。不标 done |

更新规则见 `docs/construction/51_施工工作池与自检循环.md`。认领时将任务卡的 owner、精确写入范围和基线填写完整；同一时段同一文件只交给一位写入 owner。blocked 任务注明外部条件，其他 ready 任务继续。


