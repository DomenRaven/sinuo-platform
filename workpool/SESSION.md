# 当前代码施工会话游标

更新：2026-11-01 晚。下一窗口先读 `workpool/handoffs/CONTROLLER_HANDOFF_2026-11-01_CLOSE.md`。不要把 T05–T11 改成 done。不要签 G0–G4。

- 迁移 head：`0010_evidence_index`。`alembic heads` 退出码 0，只有这一行。`SCHEMA_REVISION` 与之相同。
- 本轮全量：`.venv/Scripts/python.exe -m pytest -q`，缓存放在已删除的 `_施工文件/2026-11-01_slim/99_临时文件/`。`pyproject.toml` 的 addopts 排除 `live_model`。退出码 0。406 passed，2 deselected，2 warnings，30.75s。警告是 httpx 弃用，以及符号链接测试读线程的 gbk 解码。
- OpenAPI 30 条路径。`create_app` 已挂上传三条、证据锚点、测试记录、启动运行、候选三条、审阅、导出。
- 本地 `_施工文件` 约 1.6 GB 的一次性缓存、冻结环境和文档修补脚本已删除。该目录在 `.gitignore` 里。`.env` 被忽略，没有进入提交。
- 用户决定：不做 GLB 和其他 3D。见 `USER_WAIVE_3D.md`。
- 周一 2026-11-02 18:00 若汇报还不能点到 pending 候选，就停后端，改两屏演示。种库脚本是 `scripts/seed_night_demo.py`。假模型链不要重做。

## 状态

- T01–T04：done。
- T05、T06、T08、T10、T11：implementing。
- T07、T09：review。不标 done。

## 下一动作

改 `tests/acceptance/test_end_to_end.py`，让它调用已经挂上的上传、证据锚点、测试记录和启动运行。启动运行用 `app.state.agent_provider` 注入假模型，不要读本机 `.env` 去打 DeepSeek。导出生成调用已有的 `drain_export_jobs`。3D 和真实模型仍写未执行。跑通后仍不签 G0–G4。

第二件是把 `F:/工业设计理论库` 导出成运行目录里的 `chunks.json` 和 `graph.json`，用 `SINUO_THEORY_ROOT` 指向它。不要把原始课件复制进 git。
