# 完成度核对（2026-10-31）

本文件只写这次读过的代码、测试函数和实际命令。没有跑 `live_model`。没有 Git 提交。T05、T06、T07 不标 done。

## 命令

解释器：`.venv/Scripts/python.exe`。警告栈来自 `pythoncore-3.14-64`。`pyproject.toml` 的 `addopts` 排除 `live_model`。

```text
.venv/Scripts/python.exe -m alembic heads
.venv/Scripts/python.exe -m pytest -q -o cache_dir=_施工文件/2026-10-31_grok-audit-after-graph/99_临时文件/pytest-cache --basetemp=_施工文件/2026-10-31_grok-audit-after-graph/99_临时文件/pytest
```

`alembic heads` 退出码 0，只有一行 `0008_ticket_expiry (head)`。pytest 退出码 0。`301 passed, 2 warnings in 55.22s`。两条警告不是失败：Starlette 提示 `httpx` 已弃用；`test_symlink_escape_writes_nothing` 的读线程用 gbk 解码子进程输出时抛了 `UnicodeDecodeError`。

迁移链：`0001_primitives` → `0002_lease_invariants` → `0003_domain_identity` → `0004_repair_invariants` → `0005_stored_objects` → `0006_suggestion_preferences` → `0007_run_origin` → `0008_ticket_expiry`。`src/sinuo/infrastructure/models.py` 的 `SCHEMA_REVISION` 是 `0008_ticket_expiry`。

## 入口

`create_app` 挂了 `auth_router`、`version_router`、`events_router`、`assets_router`、`suggestion_router`。资产路由有：

- `GET /artifact-versions/{artifact_version_id}/content`
- `POST /artifact-versions/{artifact_version_id}/preview`
- `GET /artifact-versions/{artifact_version_id}/preview`

对象键函数 `original_key` 返回 `objects/{project}/sha256/{hash}/{generation}/original`。

## 任务对照

T01–T04 的任务卡状态仍是 done。这次没有把它们改回 implementing。T03 任务卡完成时的迁移证据停在领域修复；仓库当前 head 已经是 `0008_ticket_expiry`，不是 `0005_stored_objects`。

T05 仍是 implementing。代码里有 `register_original`、`place_upload_pin`、`_consume_upload_pin`（条件里含 `fencing_token`）、`_record_registration_audit`、`ticket_expired`、`render_original_preview`、`render_registered_preview`。验收文件 `tests/acceptance/test_assets_derivatives.py` 在上面的 301 里。条款对应到这些测试函数：

1. `test_same_name_new_bytes_are_two_versions_and_reuse_stays_in_project`、`test_same_bytes_round_trip_and_do_not_invent_a_preview`、`test_mounted_download_route_sets_nosniff_and_attachment`
2. `test_wrong_declared_hash_registers_nothing`、`test_declared_length_mismatch_leaves_no_referable_original`、`test_subject_without_artifact_upload_leaves_no_referable_original`、`test_mismatched_ticket_owner_leaves_the_ticket_open`、`test_path_traversal_writes_nothing`
3. `test_rollback_after_install_cleans_only_the_unregistered_orphan`
4. `test_injected_converter_failures_leave_the_original_downloadable`
5. PNG：`test_png_preview_is_a_png_and_leaves_the_original_bytes`。PDF：`test_pdf_preview_is_a_png_and_leaves_the_original_bytes`。GLB：`test_gltf_binary_writes_nothing_while_blender_is_absent` 和 `test_glb_without_blender_does_not_publish` 断言不写预览。没有可检查的 GLB 预览夹具。用户决定见 `USER_WAIVE_3D.md`。条款 5 未完成。
6. `test_original_download_requires_grant_and_omits_disk_path`。`test_preview_http.py` 里匿名 401、无权 403、缺失或隐藏 404。

T06 仍是 implementing。`JOB_STATUSES` 是 queued、running、succeeded、failed、cancelled、superseded，没有 `retry_wait`。`TASK_STATUSES` 含 `retry_wait`。`finish_task_attempt` 和 `release_retry_wait` 在 `src/sinuo/workers/run_tasks.py`。另外已有 `cancel_job`、`cancel_run`、`reserve_model_call`、`save_suggestion_preference`、`recover_delivery`、`write_derivative_attempt`、`render_derivative_preview`，以及 `src/sinuo/api/events/routes.py` 的 `get_agent_run_events`。建议偏好表在迁移 `0006`，运行 `origin` 在 `0007`。真实模型适配不在这批函数里。`DisabledModelProvider.execute` 抛 `ProviderDisabled`。

T07 仍是 implementing。已有函数只有 `search_evidence` 和 `get_graph_neighborhood`。没有 `get_page_snapshot`、`lookup_method`、`compare_versions`，源码里没有 FTS5。没有方法卡表；`method_card_version` 只是 `context_snapshots` 的一列。`get_graph_neighborhood` 只走 `page_edges`，种类只有 `branches_from`、`stage_source`、`merge_source`。`tests/acceptance/test_retrieval_methods.py` 有 4 个测试：两条检索，两条图邻域。图邻域测试覆盖授权链、depth 2 截断、max_nodes 2 截断、无权页不出现、其他项目为空、越界参数拒绝。它没有覆盖反向边单独成例，也没有方法卡。

T08 任务卡仍是 planned。`src/sinuo/agents/runner.py` 的 `DagRunner` 是内存执行，文档写明不发布候选、不写业务数据。`tests/test_runner.py` 在这 301 里。验收文件 `tests/acceptance/test_agent_runs.py` 不存在。没有会发网络请求的 DeepSeek 适配器。

T09 任务卡仍是 planned。`src/sinuo/application/cas.py` 的 `consume_candidate` 只做一条 pending 候选的条件更新。`test_consume_candidate_targets_one_row_and_rolls_back` 在 `test_domain_schema.py`。没有候选预览哈希服务，也没有确认建页服务。

T10、T11 任务卡仍是 planned。`ReviewOpinion` 表在领域模型里。没有导出或恢复应用服务，没有 `test_agent_runs.py` 以外的纵向验收文件；`tests/acceptance/` 里没有 T10 或 T11 的验收文件。

## 下一刀

T07 继续做 `get_page_snapshot`：只读一个已授权页版本，不返回无权页的标题或正文。不做方法卡，除非先有方法卡的存储合同。不做 3D。
