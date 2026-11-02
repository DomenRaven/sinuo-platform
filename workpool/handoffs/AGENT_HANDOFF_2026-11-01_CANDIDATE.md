# 智能体候选链路修复交接（2026-11-01 晚）

给下一个施工窗口。本文记录打通「待确认候选」这一核心缺项的根因、修复与验证。不要在本文之外重做。

## 结论

离汇报的核心缺项「点不到待确认候选」已闭合：真实 DeepSeek 下，从「写节点 → 问下一步 → 生成 pending 候选」已跑通，且能生成 `suggest_stage_transition` / `stage_blank` 候选（确认即建新页）。

## 根因（为什么之前候选一直是 0 条）

1. `critic` 依赖 `evidence` 的结果，但 `src/sinuo/agents/deepseek.py` 和 `fake.py` 的 `execute` 都 `del upstream`，critic 的模型输入里**根本没有证据结果**，只能输出"草稿为空，无法下一步"。
2. critic 不知道项目的 8 个阶段，`target_stage_id` 无从填，即使想建议阶段推进也写不出合法 JSON。

## 修复（`src/sinuo/application/agents/execute.py`）

- `_invoke`：把依赖任务的 output 合入 `context["upstream"]`（critic 能看到 evidence 的 hits / citations / theory）。
- critic 分支新增 `context["page_version_id"]`、`context["stages"]`（8 阶段 + position）、`context["current_stage"]`、`context["guidance"]`（`_STAGE_GUIDANCE`，明确何时输出 stage_blank）。
- 新增 `_collect_stage_context(engine, project_id, page_version_id)` 读阶段表 + 当前页所在阶段。
- **未改** `_CRITIC_CONTRACT` 与 `context["note"]` 常量（`test_stage_blank_from_run.py` 精确断言这两处，保持冻结）。

## 演示数据

- `scripts/seed_night_evidence.py`：给 `var/sinuo.db` 的「夜间取物」项目喂 1 条观察证据 + 建 FTS 索引（`search_evidence('夜间取物')` 命中 1 条）。
- 验证脚本：`scripts/verify_critic_context.py`、`verify_live_candidate.py`、`verify_stage_blank.py`。

## 验证结果（真实 DeepSeek，非假模型）

- 修复前：critic 输出"草稿内容为空，工具结果为空"，0 候选。
- 修复后：critic 读到证据，先出 `draft_decision` 候选；给草稿写 problem + requirement 节点后，出 **`suggest_stage_transition` + `stage_blank` 候选**，proposal 为：
  `{"sources":[{"page_version_id":"c4f7f343-..."}],"target_stage_id":"用户与场景研究阶段id","title":"用户与场景研究"}`。
- 全量测试 `pytest -q -m "not live_model"`：**439 passed, 2 deselected**，与基线一致。

## UI 补充

- `web/desk.html` 新增「写节点」区：可在页上记下 problem / requirement / evidence / insight 节点（走 `PATCH /pages/{id}/draft`，`record=formal`）。这是白皮书 U01「写问题」动作，让演示路径可重复操作，不依赖脚本种数据。

## 已知边界（未做，不要当成已解决）

- 页内节点当前**无正文字段**（契约只有 node_id/kind/duty/record），"问题"的具体文字靠 evidence 的 excerpt 承载；critic 是把 evidence 片段关联到节点来理解的。要不要给节点加正文是后续产品决定，本次未动契约。
- `search_theory` 对"夜间取物"返回 empty（课程理论库无该场景内容），不影响候选生成，但不是理论检索命中。
- stage_blank 候选的「确认→建新页」由 `test_stage_blank_from_run.py` 已验证，未在本库重复消费（保留 pending 候选供演示）。

## 下一步

1. 端到端浏览器走查：登录 → 打开「夜间取物」→ 上传证据 → 写节点 → 检查下一步 → 看到 stage_blank 待确认卡 → 预览 → 确认 → 跳到新页「用户与场景研究」。
2. 汇报前若需"确认后页变"的演示，用上面的路径；`draft_decision` 候选卡会显示"记录里没有可提交的页"，属正常。
