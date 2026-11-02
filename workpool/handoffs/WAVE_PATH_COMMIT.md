# 路径交接（F04 / F05 / F19）

对照 `USER_SCOPE_2026-11-01.md` 的 F04、F05、F19，以及白皮书 4.2。没有改生产代码。分支、历史只读、阶段空白页、合流和候选确认都已能由人确认后写入。

## 做了什么

补了一条测试 `test_path_candidates_leave_pages_and_stages_until_confirm`，放在 `tests/acceptance/test_candidate_commit.py`。它先预览阶段空白页、历史分叉和合流，确认前正式页、页版本、阶段和阶段配置都不变，历史读取也不写。确认后再检查：新阶段页的来源边是确切的 `synthetic-pv-a2`；分叉边是 `branches_from`，来源页头仍是旧版本；合流留下两条 `merge_source`。

## 已经有测试的条款

- 新阶段页记下确切来源版本：`test_stage_blank_and_merge_blank_keep_explicit_sources`、`test_suggest_stage_transition_confirms_like_stage_blank`、`test_stage_blank_candidate_confirms_into_an_empty_page`。
- 分叉和合流由人确认后写入：`test_history_branch_survives_a_later_head`、`test_stage_blank_and_merge_blank_keep_explicit_sources`。直接分叉命令另有 `test_branch_keeps_later_source_versions_and_skips_tests`。
- 旧版本只读：`test_read_history_selects_without_moving_pointers`、`test_confirmed_page_version_content_cannot_be_updated`、`test_history_keeps_confirmed_bytes_after_a_later_draft`、`test_project_command_replays_and_history_does_not_write`。
- 从旧版本继续会新开页，原路线还在：`test_history_branch_survives_a_later_head`、`test_branch_keeps_later_source_versions_and_skips_tests`。
- 确认前正式页和阶段不变：预览不建版本见 `test_preview_is_server_bound_and_creates_no_version`；运行留下待确认候选、确认前不建页见 `test_stage_blank_candidate_confirms_into_an_empty_page`。阶段行本身原先没有断言，由上面的补缺测试补上。候选预览和确认的 HTTP 见 `test_candidate_http.py`。

## 测试命令和退出码

```
.venv\Scripts\python.exe -m pytest tests/acceptance/test_candidate_commit.py tests/acceptance/test_versions_history.py tests/acceptance/test_stage_blank_from_run.py tests/acceptance/test_versions_checkpoint_commands.py tests/acceptance/test_versions_http.py::test_project_command_replays_and_history_does_not_write tests/acceptance/test_versions_http.py::test_history_keeps_confirmed_bytes_after_a_later_draft tests/acceptance/test_versions_projects.py::test_confirmed_page_version_content_cannot_be_updated -q --tb=line
```

退出码 0。42 passed，1 warning。

```
.venv\Scripts\python.exe -m pytest tests/acceptance/test_candidate_http.py tests/acceptance/test_candidate_evidence.py tests/acceptance/test_candidate_projection.py -q --tb=line
```

退出码 0。9 passed，1 warning。

## 没完成的条款

没有。这五条都已有行为，也有测试。

## 需要主控挂到 app.py 的那一行

没有。分支、历史和候选预览、确认、驳回都已经挂上。
