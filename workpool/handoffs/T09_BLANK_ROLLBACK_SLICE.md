# HANDOFF-T09 空白页确认回滚 → T10 / 集成

- 上游任务 / owner：T09 候选预览与确认，验收 5 的空白页路径。本切片不标 done。
- 集成基线：无新迁移。`SCHEMA_REVISION` 未改。`commit.py` 未改。
- 消费者：T10 与未来确认 UI。本切片没有 HTTP，没有 3D。
- 契约 / schema 版本：沿用现有候选预览、处置和幂等回执。空提案 `{}` 仍是 `not_implemented`。
- 实现位置：只新增 `tests/acceptance/test_candidate_commit.py` 的 `test_stage_blank_failure_after_page_insert_rolls_back`。`src/sinuo/application/candidates/commit.py` 没有改。
- 权限动作：沿用 `create_page_from_versions` + `stage_blank` 的现有确认。没有新权限，没有新命令。
- 事务与幂等边界：`_commit` 的顺序仍是 `_consume`、`_apply`、`flush`、`before_effect`、`_finish`。空白页走 `_apply_blank`，也会经过这一处 `before_effect`。它已经在候选消费和新页插入之后、`_finish` 写处置和回执之前，所以没有新注入点，也没有给它加参数。调用方仍持有 `write_uow`。
- 成功响应 / 失败响应：这条测试不走成功响应。`before_effect` 先看到候选已是 `committed`、修订 2，新页和新页版本已经插入，来源边是 `synthetic-pv-a2` / `stage_source`，处置行还是 0，然后抛 `RuntimeError`。事务回滚后：候选仍是 `pending`、修订仍是 1；项目修订和来源草稿修订与确认前相同；新页、新页版本、该页草稿、来源边、Decision、处置、幂等回执都不在。
- 最小调用示例或夹具：现有 `ready` 夹具、`_preview`、`_commit`。动作 `create_page_from_versions`，模式 `stage_blank`，提案 `_stage_proposal()`。没有 sleep。没有删旧测试。

## 验证

指定 basetemp 的上级目录原先不存在。先创建 `_施工文件/2026-11-01_t09close/99_临时文件`，再执行同一命令。第一次因此在 setup 报 23 errors，退出码 1，测试体没有运行。创建目录后的结果如下。

```text
.venv/Scripts/python.exe -m pytest -q tests/acceptance/test_candidate_commit.py -o cache_dir=_施工文件/2026-11-01_t09close/99_临时文件/pytest-cache-a --basetemp=_施工文件/2026-11-01_t09close/99_临时文件/pytest-a
```

退出码 0。`23 passed`，1 条既有 Starlette/httpx 弃用警告，2.41s。原有 22 条都还在。`commit.py` 没有改。未跑全库。未对 `var/sinuo.db` 做 upgrade。未发网络。未发 `live_model`。未做 3D、T10、T11。未 git commit。T09 不标 done。

- 未验证项 / 已知限制：没有再测 `merge_blank` 或 `history_branch` 的同类注入。草稿候选回滚仍由 `test_failure_after_consume_rolls_candidate_and_draft_back` 覆盖。空提案 `{}` 仍是 `not_implemented`。
- 下游第一次应运行的命令：上面的 `test_candidate_commit.py`。不要把 T09 标成 done。
