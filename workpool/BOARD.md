# 思诺后端施工工作池

基线：本施工包交付时的项目骨架。UI 延后。状态以实际测试和集成证据更新；T01 仅在 ADR-003 明确的 G0 基础范围内完成。T02 与 T03 已 done，不等于 G1。T04 已于 2026-10-31 复审后 done，不等于 G2。用户于 2026-10-31 决定绕开 GLB 和其他 3D 工程，见 `workpool/handoffs/USER_WAIVE_3D.md`。2026-11-01 快照：迁移 head 仍是 `0008_ticket_expiry`。T05/T06 现有验收 155 passed。全量 pytest 308 passed，2 warnings。T05、T06、T07、T08 都是 implementing，都不标 done。

| 任务 | 当前状态 | 依赖 | Owner | 当前证据 / 下一步 |
| --- | --- | --- | --- | --- |
| T01 基础与配置 | done | 无 | Codex（历史基线交付） | G0基础范围通过；原生产认证条目转T02未测；66项为历史基础实测，见ADR-003与T01交接 |
| T02 身份与授权 | done | T01 | Grok 主控 | 复审 blocking 0。见 T02_T03 交接 |
| T03 领域模型与迁移 | done | T01 | Grok 主控 | 领域修复在 0004。复审 blocking 0。N2–N4 按 ADR-004 关闭。仓库当前 head 已是后来的 `0008_ticket_expiry` |
| T04 草稿、版本与幂等命令 | done | T02,T03 | Grok 主控 | 复审 blocking 0，验收 6 条都有直接测试。见 T04 交接 |
| T05 原件与派生任务 | implementing | T04 | Grok 主控 | 2026-11-01 现有验收重跑通过。PNG/PDF 有测试。GLB 仍无夹具，不写成 done。见 T05_T06_CLOSEOUT |
| T06 持久任务、Outbox 与 SSE | implementing | T04 | Grok 主控 | 2026-11-01 现有验收重跑通过。`jobs` 无 `retry_wait`。真实模型没有。T06 不标 done |
| T07 证据检索与方法库 | implementing | T05 的非 3D 原件 | Grok 主控 | `get_page_snapshot` 已有，先 `page.read`，无权页不返回标题或正文。无 FTS5、方法卡表、版本对比。T07 不标 done |
| T08 智能体运行与 DeepSeek | implementing | T06,T07 | Grok 主控 | `read_fake_chain` 先做 `run.read`。页面可点开步骤、证据、方法问句和 pending 候选。确认拒绝只改本地状态。T08 不标 done |
| T09 候选预览与确认 | planned | T04,T08 | 未分配 | 已有 `consume_candidate` 条件更新及 `test_consume_candidate_targets_one_row_and_rolls_back`。没有预览哈希服务，也没有确认建页服务 |
| T10 审阅、导出与恢复 | planned | T05,T09 | 未分配 | 授权意见、快照导出和恢复演练 |
| T11 纵向与故障验收 | planned | T06,T10 | 未分配 | G0–G4 实测与 UI API 交接 |

更新规则见 `docs/construction/51_施工工作池与自检循环.md`。认领时将任务卡的 owner、精确写入范围和基线填写完整；同一时段同一文件只交给一位写入 owner。blocked 任务注明外部条件，其他 ready 任务继续。


