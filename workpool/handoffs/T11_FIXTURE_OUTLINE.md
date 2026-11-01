# T11 夹具大纲

日期：2026-11-01。只读设计。本文件不跑验收，不签署门禁。

T11 与 T10 在 `workpool/SESSION.md` 里仍是 planned。审阅、导出、备份恢复都还没有实现。G0、G1、G2、G3、G4 没有本次运行证据，本大纲不填写通过、失败或未执行链接。

纵向场景句见 `docs/construction/07_BACKEND_ACCEPTANCE.md`：从空数据库经迁移启动；创建项目与平行页；保存版本和原件；形成可定位证据；按任务调用最多三种职责；生成合法候选；人工提交一次性生效；支持历史分支、授权审阅、导出和备份恢复。

下面按 `workpool/tasks/T11_END_TO_END_ACCEPTANCE.md` 的完整场景列步骤。合同里的演示项目名是「夜间取物辅助产品」，虚构数据标记 `synthetic=true`。将来的总入口是 `tests/acceptance/test_end_to_end.py`，该文件现在不存在。T10 计划中的 `tests/acceptance/test_review_export_recovery.py` 也不存在。

路由前缀都是 `/api/v1`。

## 完整场景

### 1. 建项目与两张平行页

已有端点。

- `POST /projects` → `create_project`（`sinuo.application.versions.projects`）。一次写入项目、一个阶段、第一张空白页、草稿和创建者授权。
- `POST /projects/{project_id}/pages` → `create_parallel_page`（`sinuo.application.versions.pages`）。在同一阶段再插一张空白页，不写先后边。

会话已有 `POST /session`，登录服务是 `authenticate_login`。空库迁移不是这条业务端点。

### 2. 上传原件

没有上传端点。已有服务函数。

- `request_upload_ticket`（`sinuo.application.assets.uploads`）
- `register_original`（同模块）

`request_upload_ticket` 要求项目里已经有 `Artifact` 行。仓库里没有创建 `Artifact` 的服务函数。登记后的原件读取端点是 `GET /artifact-versions/{artifact_version_id}/content` → `download_original`。

### 3. 定位证据

没有检索端点。已有服务函数。

- `search_evidence`（`sinuo.application.retrieval.search`）
- 索引缺行时补行：`rebuild_evidence_index`（同模块）

`EvidenceAnchor` 表已在域模型里。仓库里没有写入证据锚点的服务函数。动作名 `evidence.create`、`evidence.read` 已在 `public_contract` 中。

### 4. 确认草稿

已有端点。

- 先改草稿：`PATCH /pages/{page_id}/draft` → `save_draft`（`sinuo.application.versions.drafts`）
- 确认成版本：`POST /pages/{page_id}/checkpoints` → `checkpoint_page`（`sinuo.application.versions.checkpoints`）

### 5. 假模型运行

启动没有端点。已有服务函数 `run_fake_chain`（`sinuo.application.agents.fake_chain`）。默认提供者是 `FakeModelProvider`。读阶段调用 `search_evidence`；给出页版本时再调用 `get_page_snapshot`。方法问句是固定字符串「没有方法卡可查。上面的证据是否够用？」，这条链不调用 `lookup_method`。

读取已有端点，都进 `read_fake_chain`：

- `GET /projects/{project_id}/agent-runs/{parent_run_id}/fake-chain`
- `GET /projects/{project_id}/agent-runs/{parent_run_id}/fake-chain/view`

运行事件：`GET /agent-runs/{run_id}/events`。

单次 `open_agent_run`（`sinuo.application.agents.execute`）最多三种职责，也没有启动用的 HTTP 端点。假模型链是上面的 `run_fake_chain`。

### 6. 预览确认空白页

已有端点。

- `POST /candidates/{candidate_id}/preview` → `preview_candidate`
- `POST /candidates/{candidate_id}/commit` → `commit_candidate`

阶段空白页的动作为 `create_page_from_versions`，`page_mode` 为 `stage_blank`。确认后由 `commit_candidate` 内部的 `_apply_blank` 写入新页：引用进节点，边为空，并写来源边和决定。`suggest_stage_transition` 走同一条空白页写入。`merge_blank` 是合流空白页，不是本步的主路径。

候选由上一步 `run_fake_chain` 留下。没有单独的「打开运行」端点。

### 7. 保留旧测试

没有登记作品测试的端点，也没有写入 `TestRecord` 的服务函数。

新作品版本仍走第 2 步的 `request_upload_ticket` 与 `register_original`。测试行的外键是 `artifact_version_id`。这两个函数不改写已有 `test_records`。

读侧已有 `compare_versions`（`sinuo.application.retrieval.methods`）。它按作品版本读 `TestRecord`，只出现在一侧时标 `needs_review`。

### 8. 历史分支

已有端点。

- `GET /pages/{page_id}/history` → `read_history`（`sinuo.application.versions.history`）
- `POST /pages/{page_id}/branches` → `branch_from_version`（`sinuo.application.versions.branches`）

`branch_from_version` 新开一页的 v1，来源边种类是 `branches_from`，节点映射写在边的 reason 里，源页 HEAD 不改，也不复制 `test_records`。

候选上的另一条路径：`create_page_from_versions` 加 `page_mode=history_branch`，经第 6 步的预览和确认进入 `commit_candidate` 内部的 `_apply_history`。

### 9. 授权审阅

待 T10。

动作名 `opinion.create`、`opinion.edit_own` 和表 `ReviewOpinion` 已在域模型里。没有审阅路由，没有写入或修改意见的服务函数。`assert_opinion_anchor` 只校验文档形状。

### 10. 导出

待 T10。

动作名 `report.export` 已在 `public_contract` 中。没有导出路由，没有导出服务或 Worker。

### 11. 隔离恢复

待 T10。

没有备份函数，没有恢复到隔离目录的函数，也没有对应 CLI。`recover_delivery`（`sinuo.workers.leases`）只恢复 Outbox 投递，不算本步。

## 六条故障反例

下列名字是仓库里已经有的测试函数。没有覆盖的子项写「缺」。列出名字不等于该门禁已通过。

### 1. 同草稿双写 CAS；相同请求键重放与异体；不同键双确认同一候选

- 同草稿双写：`test_two_connections_cannot_both_advance_the_same_draft`，`test_second_draft_write_on_the_same_revision_returns_409`
- 同键重放：`test_project_command_replays_and_history_does_not_write`，`test_repeated_checkpoint_key_creates_one_version`，`test_commit_replays_the_same_body_without_a_new_project_revision`，`test_replay_projects_receipt_and_rejects_a_reused_key`
- 同键异体：`test_same_key_different_body_is_rejected_and_replay_reauthorizes`，`test_project_key_with_a_different_body_conflicts`，`test_parallel_key_with_a_different_body_conflicts`，`test_checkpoint_key_with_a_different_body_conflicts`
- 不同键双确认同一候选：`test_two_actors_confirm_once`

### 2. 事务提交边界断开；Outbox 重投；过期 Worker 回写；晚到模型结果隔离

- 提交边界断开：`test_project_failure_before_audit_drops_page_and_receipt`，`test_draft_failure_before_audit_rolls_back`，`test_checkpoint_failure_before_audit_rolls_back`，`test_failure_after_consume_rolls_candidate_and_draft_back`，`test_stage_blank_failure_after_page_insert_rolls_back`，`test_consume_candidate_targets_one_row_and_rolls_back`
- Outbox 重投：`test_committed_event_is_recovered_claimed_and_not_reset`，`test_two_connections_recover_one_delivery_row`，`test_same_consumer_effect_is_reused`
- 过期 Worker 回写：`test_expired_lease_cannot_persist_after_takeover`，`test_late_output_does_not_cover_a_candidate`（其中旧 fencing token 的 `publish_task_output` 得到 `discarded_late`）
- 晚到模型结果：`test_late_output_does_not_cover_a_candidate`，`test_cancel_run_closes_status_and_rejects_late_publish`，`test_late_result_after_swallowed_cancellation_is_discarded`

### 3. 运行中撤权、SSE 重连、历史、搜索、缩略图、回执、导出旁路

- 运行中撤权：`test_manual_run_stops_the_model_when_its_grant_is_revoked`，`test_queued_job_after_revoke_or_disable_does_not_call_a_model`
- SSE 重连与游标：`test_sse_route_returns_persisted_events_in_order`，`test_replay_returns_later_events_in_seq_order`，`test_sse_route_checks_permission_before_the_next_frame`，`test_replay_checks_current_permission_before_each_read`，`test_foreign_last_event_id_does_not_return_other_body`
- 历史旁路：`test_revoked_membership_hides_draft_checkpoint_and_branch_replays`
- 搜索旁路：`test_search_evidence_returns_the_authorized_version_only`，`test_policy_context_omits_disallowed_excerpts`
- 缩略图旁路：`test_page_projection_strips_original_thumbnail_excerpt_citation_and_hidden_counts`，`test_preview_metadata_stops_at_the_new_version_grant`
- 回执旁路：`test_replay_projects_stored_receipt_after_original_read_is_revoked`，`test_revoked_membership_replay_does_not_return_stored_page`，`test_revocation_blocks_session_receipt_replay_and_auth_cache`
- 导出旁路：缺。待 T10。

### 4. 上传登记失败、转换器崩溃、索引落后、原件损坏与有审计墓碑

- 上传登记失败：`test_wrong_declared_hash_registers_nothing`，`test_declared_length_mismatch_leaves_no_referable_original`，`test_rejected_registration_leaves_the_audit_table_empty`，`test_rollback_after_install_cleans_only_the_unregistered_orphan`
- 转换器崩溃：`test_injected_converter_failures_leave_the_original_downloadable`，`test_crash_before_finalize_keeps_file_and_same_token_publishes_once`
- 索引落后：`test_anchor_without_an_index_row_is_not_indexed`
- 原件损坏：缺。`download_original` 会在字节哈希或长度与登记不符时拒绝，仓库里没有改写已登记原件再读取的测试函数。
- 墓碑拒绝：`test_tombstoned_evidence_anchor_rejects_commit_without_new_rows`，`test_attach_artifact_version_rejects_a_tombstone_without_new_rows`。这两处使用预先插入的墓碑行。产生墓碑并写入审计事件的删除命令：缺。

### 5. 阶段新页主体空白、旧版分支保留内容映射、原 HEAD、历史与旧测试不被覆盖

- 阶段新页主体空白：`test_stage_blank_and_merge_blank_keep_explicit_sources`，`test_suggest_stage_transition_confirms_like_stage_blank`
- 旧版分支与内容映射：`test_branch_keeps_later_source_versions_and_skips_tests`，`test_history_branch_survives_a_later_head`
- 原 HEAD 与历史不被覆盖：`test_read_history_selects_without_moving_pointers`，`test_history_selects_do_not_move_heads`，`test_checkpoint_appends_and_keeps_old_bytes`，`test_history_keeps_confirmed_bytes_after_a_later_draft`
- 分支不改写已有测试行：`test_branch_keeps_later_source_versions_and_skips_tests`
- 上传作品新版本后旧测试仍绑在旧版本：缺。

### 6. 从备份恢复准确历史、权限及原件，派生物可重建

缺。整条待 T10。现有的 `recover_delivery` 测试只覆盖 Outbox，不覆盖备份恢复。

## 步骤归类

已有业务端点：建项目与两张平行页、确认草稿、预览确认空白页、历史分支。假模型运行另有只读端点。

已有服务函数、没有对应写端点：上传原件、定位证据、假模型运行的启动、保留旧测试所依赖的再次登记。保留旧测试没有登记测试的函数。

待 T10：授权审阅、导出、隔离恢复。
