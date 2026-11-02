# 当前代码施工会话游标

更新：2026-11-01 晚。下一窗口是界面总控，先读 `workpool/handoffs/CONTROLLER_HANDOFF_2026-11-01_UI.md`。不要把 T05–T11 改成 done。不要签 G0–G4。

- 迁移 head：`0010_evidence_index`。`SCHEMA_REVISION` 与之相同。
- 纵向：`.venv/Scripts/python.exe -m pytest -q tests/acceptance/test_end_to_end.py` 退出码 0，1 passed，1 warning。全量 `.venv/Scripts/python.exe -m pytest -q` 退出码 0，406 passed，2 deselected，1 warning，45.66s。`live_model` 仍被排除。这次警告是 httpx 弃用。
- `test_end_to_end.py` 已调用上传、证据锚点、测试记录和 `POST /projects/{id}/agent-runs`。启动运行注入 `FakeModelProvider`，`Settings` 用 `_env_file=None` 且 `model_enabled=False`。导出登记后调用 `drain_export_jobs`，再下载 zip。3D 和真实模型仍写未执行。阶段空白页仍未执行：这次运行留下的候选是 `draft_decision`，没有插入阶段空白候选，也没有调用预览或确认。
- OpenAPI 30 条路径。没有改假模型链和种库脚本。没有提交。
- 用户决定：不做 GLB 和其他 3D。见 `USER_WAIVE_3D.md`。
- 周一 2026-11-02 18:00 若汇报还不能点到 pending 候选，就停后端，改两屏演示。种库脚本是 `scripts/seed_night_demo.py`。假模型链不要重做。

## 状态

- T01–T04：done。
- T05、T06、T08、T10、T11：implementing。
- T07、T09：review。不标 done。

## 下一动作

界面总控按 `CONTROLLER_HANDOFF_2026-11-01_UI.md` 做一条能点的路径。外观按用户 2026-11-01 晚的决定：苹果式，简洁大气，明亮舒服，组件圆润，可以用大卡片，层次和路径要清楚。这压过 UI 白皮书第 7.1 节的色值。行为仍按页面规范。不要先铺满 R01–R14。智能体工作要能看见。不签 G0–G4。
