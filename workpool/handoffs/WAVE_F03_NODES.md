# WAVE F03 页内节点种类

- 上游：`WAVE_2026-11-01_CONTRACT.md` 的 F03 行，以及 `USER_SCOPE_2026-11-01.md` 的 F03 和职责对照表。不标 done，不签 G0–G4。没有 Git commit。
- 白皮书只读了第 4.1 节里页内节点那一句：页内节点记录问题、素材、证据、洞察、要求、概念、作品、测试和决定。合同把种类收成七个键，素材和概念没有入库。
- 做了：草稿保存仍走 `update_draft` / `save_draft`。新写入走 `save_typed_draft`。读旧草稿走 `read_page_nodes`。没有新表，没有 `0011`。库头仍是 `0010_evidence_index`。

## 节点怎么存

存在 `page_drafts.content_json`，`schema_version` 仍是 1。不建权威节点表。

新节点字段：`node_id`、`kind`、`duty`、`record`，可选 `artifact_version_id`。边仍是 `edge_id`、`source_node_id`、`target_node_id`。

`kind` 只允许 `problem`、`evidence`、`insight`、`requirement`、`artifact`、`test`、`decision`。显示名是问题、证据、洞察、要求、作品、测试、决定。

职责按合同固定，写入时不能改成别的：

| kind | duty | 含义 |
| --- | --- | --- |
| problem | clarification | 课题边界 / 问题 |
| evidence | evidence | 证据 |
| insight | evidence | 洞察 |
| requirement | critic | 要求是否可测 |
| artifact | human | 作品，由人确认 |
| test | human | 测试记录，由人确认 |
| decision | human | 决定，由人确认 |

`human` 不是第五个模型职责，表示由人确认。`record` 是 `proposal` 或 `formal`。模型写入前四类时记 `proposal`。人写入记 `formal`。作品、测试记录、决定只有 `writer="human"` 才能落成 `formal`；模型调用 `save_typed_draft` 会被拒绝。未带 `human_confirmed` 的 `save_draft` / `update_draft` 也不会收下这三类的正式记录。

旧节点只有 `node_id`，或再加 `artifact_version_id`。读出来 `kind`、`duty`、`record` 都空着，不补种类。

## 挂载

没有。不需要主控往 `app.py` 加路由。

现有 `PATCH /api/v1/pages/{page_id}/draft` 仍先走 `canonical_document`。那个函数只允许 `node_id` 和 `artifact_version_id`，带种类的正文在进 `save_draft` 之前就会被拒绝。接通种类要改 `src/sinuo/api/versions/routes.py` 的 `patch_draft`，并放宽 `canonical_document`。这两处不在 F03 的写入范围。

## 验证

```text
.venv/Scripts/python.exe -m pytest -q tests/acceptance/test_page_nodes.py
```

退出码 0。5 passed，0.68s。覆盖七类由人写入、非法种类拒绝、职责与合同一致、只有 `node_id` 的旧草稿仍可读，以及模型不能把作品、测试记录、决定写成正式记录。

```text
.venv/Scripts/python.exe -m ruff check src/sinuo/domain/page_nodes.py src/sinuo/application/versions/drafts.py tests/acceptance/test_page_nodes.py
```

退出码 0。

另跑了仍调用 `update_draft` 的旧测试：`test_versions_commands.py`、`test_versions_history.py`、`test_candidate_commit.py`、`test_versions_http.py`、`test_agent_runs.py::test_draft_freeze_and_content_expiry`。退出码 0。未跑全库。未跑 `test_end_to_end.py`。未跑 `live_model`。

## 没完成

- 方法职责仍是 `method`，但七个键里没有方法卡、概念路径或测试计划。测试键按测试记录由人确认，没有把测试计划写进这个键。所以这七类的 `duty` 里不会出现 `method`。这是对照合同后的边界。若主控要把测试计划放进 `test`，需要先改验收。
- 白皮书第 4.1 节还写了素材和概念。合同没有这两个键，未实现。
- HTTP 草稿路由还不能保存种类。见上面的挂载说明。候选投影若对带种类的草稿调用 `canonical_document`，仍会拒绝。F03 没有改那条路径。
