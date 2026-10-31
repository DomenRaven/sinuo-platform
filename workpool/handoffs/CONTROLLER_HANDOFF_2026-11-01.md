# 总控交接（2026-11-01）

给下一个总控窗口。本文件只记录这次读过的文档、这次跑过的命令，以及源码里有或没有的符号。不要把聊天记忆当成更新的代码状态。不要把 T05–T08 改成 done。不要把同一段进度追加进 `SESSION.md` 或 `BOARD.md`。

用户调整：不做 GLB 和其他 3D 预览、转换、夹具。见 `workpool/handoffs/USER_WAIVE_3D.md`。这只取消 3D，不把 F07 或 T05 验收 5 算成完成。

期限：周一 2026-11-02 18:00 前，若汇报还不能从页面点到一张 pending 候选，就停后端，改两屏演示。周二 2026-11-03 上午汇报。只读页面已经能点开一条已存在的假模型链；临时演示库已删，仓库里没有 `demo/` 目录，也没有可重复的种库脚本。

## 这次实际跑过的命令

工作目录 `sinuo-platform`。解释器 `.venv/Scripts/python.exe`。缓存目录 `_施工文件/2026-11-01_grok-audit/99_临时文件/`。

```text
.venv/Scripts/python.exe -m alembic heads
.venv/Scripts/python.exe -c "from sinuo.api.app import create_app; ..."
.venv/Scripts/python.exe -m pytest -q -o cache_dir=_施工文件/2026-11-01_grok-audit/99_临时文件/pytest-cache --basetemp=_施工文件/2026-11-01_grok-audit/99_临时文件/pytest
```

`alembic heads` 退出码 0，只有一行 `0008_ticket_expiry (head)`。pytest 退出码 0。`308 passed, 2 warnings in 19.63s`。警告不是失败：Starlette 提示 `httpx` 已弃用；`test_symlink_escape_writes_nothing` 的读线程用 gbk 解码子进程输出。`live_model` 未跑。没有 Git commit。没有读密钥。

探针打印的 OpenAPI 路径共 16 条：

```text
GET /api/v1/agent-runs/{run_id}/events
GET /api/v1/artifact-versions/{artifact_version_id}/content
GET,POST /api/v1/artifact-versions/{artifact_version_id}/preview
POST /api/v1/pages/{page_id}/branches
POST /api/v1/pages/{page_id}/checkpoints
PATCH /api/v1/pages/{page_id}/draft
GET /api/v1/pages/{page_id}/history
POST /api/v1/projects
PATCH /api/v1/projects/{project_id}/access
GET /api/v1/projects/{project_id}/agent-runs/{parent_run_id}/fake-chain
GET /api/v1/projects/{project_id}/agent-runs/{parent_run_id}/fake-chain/view
POST /api/v1/projects/{project_id}/pages
GET,PATCH /api/v1/projects/{project_id}/suggestion-preferences/me
DELETE,GET,POST /api/v1/session
GET /healthz
GET /readyz
```

`04_API_AND_EVENTS.md` 还列出、但这次 OpenAPI 里没有的端点：`GET /projects`、`GET /projects/{p}`、阶段两条、`GET /pages/{g}`、`PUT /pages/{g}/view-state`、`POST /projects/{p}/agent-runs`、`GET /agent-runs/{r}`、`POST /agent-runs/{r}/cancel`、候选 preview/commit/reject、上传票据三条、`GET /artifact-versions/{v}`、证据锚点、`GET /projects/{p}/search`、测试记录、审阅意见两条、导出两条、`GET /commands/{request_id}`。文档还要求每个端点有一个成功例子和一个关键失败例子。这次没有逐条检查 OpenAPI example，不能把这 16 条写成契约测试已齐。

词汇探针：`LINEAGE_KINDS` 只有 `branches_from`、`stage_source`、`merge_source`。`JOB_STATUSES` 没有 `retry_wait`。`TASK_STATUSES` 有 `retry_wait`。`CANDIDATE_ACTIONS` 六种都在：`add_node`、`add_typed_edge`、`attach_artifact_version`、`draft_decision`、`suggest_stage_transition`、`create_page_from_versions`。假模型链实际写入的动作只有 `draft_decision`。

## 门禁

施工总览 `00` 的门禁没有一条可以签全通过。

| 门禁 | 对照 | 结论 |
| --- | --- | --- |
| G0 可启动 | `/healthz`、`/readyz` 在 OpenAPI 里。全量 pytest 能跑完。迁移 head 是 `0008_ticket_expiry` | T01 任务卡仍是 done，范围只是 G0 基础。本文件没有重数历史交接里的 66 项 |
| G1 写入可信 | T02–T04 任务卡是 done。全量里有授权、草稿双写、检查点、分支、历史只读测试 | 不等于 G1 文档里的 A11、A12 已通过 |
| G2 资料与任务 | T05–T07 仍是 implementing | 不签 |
| G3 候选可信 | T08 implementing，T09 planned。没有预览哈希，没有一次消费后建页 | 不签 |
| G4 可交接 | T10、T11 planned。没有备份恢复，没有纵向场景 | 不签 |

## 产品功能 F01–F24

出处：产品白皮书定稿的功能表，以及 `07` 的范围句。P1/P2 按原文不在本轮验收里，这里仍逐条写明有没有代码。

| ID | 原文要求 | 这次看到的代码和测试 | 结论 |
| --- | --- | --- | --- |
| F01 P0 | 建项目，选阶段模板，改名，记录课题约束 | `POST /api/v1/projects` 在。阶段配置 `POST /projects/{p}/stages` 不在 OpenAPI | 只做到建项目 |
| F02 P0 | 同阶段两张平行页，总览不自动连边 | `test_parallel_pages_have_no_default_edge`；`POST /projects/{id}/pages` 在 | 有直接测试 |
| F03 P0 | 页内问题、证据、洞察、要求、作品、测试、决定节点，可追源 | `schema_types.py` 没有这套节点类型。页间边只有三种谱系，没有支持/反对边 | 没有实现 |
| F04 P0 | 新阶段页记录一个或多个确切来源；分叉与合流 | `branch_from_version` 和 `POST /pages/{id}/branches` 在。没有合流建空白页的命令 | 只有分支 |
| F05 P0 | 历史只读；从旧版继续生成新页；原路线可重访 | `GET /pages/{id}/history`；`test_read_history_selects_without_moving_pointers`；`test_branch_keeps_later_source_versions_and_skips_tests` | 有直接测试。不是一条可点击的产品路径 |
| F06 P0 | 上传原件记哈希，更新不覆盖旧版 | `register_original` 与 `test_same_name_new_bytes_are_two_versions_and_reuse_stays_in_project`。上传三条 HTTP 不在 OpenAPI。下载 `GET .../content` 在 | 应用层有测试，上传 HTTP 没有 |
| F07 P0 | 图片、PDF、至少一种 3D；不支持的格式可另附封面并下载原件 | PNG、PDF 有渲染测试。GLB 只有 `test_gltf_binary_writes_nothing_while_blender_is_absent` 和 `test_glb_without_blender_does_not_publish`，断言不写预览。封面读取用例没有 | 3D 按用户决定不做。F07 因此未完成。`07` 写明只存 GLB 而没有可用预览不算完成 F07 |
| F08 P0 | 从方案找到要求和原始证据；测试指向特定作品版本 | `EvidenceAnchor`、`TestRecord` 表在。`search_evidence` 能按摘录回到 `artifact_version_id`。没有一条测试从方案节点追到要求再追到原件 | 检索有测试。整条回链没有 |
| F09 P0 | 建议含理由、缺口、来源和版本；系统不自行翻页；关闭和手动频率有效 | 建议偏好 off/manual/event/digest 有测试，见 `T05_T06_CLOSEOUT_2026-11-01.md`。假模型链留下 pending 候选，不建页。建议正文不是带缺口的阶段建议 | 频率有测试。阶段建议未完成 |
| F10 P0 | 保存输入版本、路由、工具调用、错误和用户纠正 | `run_fake_chain` 写两条 `AgentRun`、任务、一条 `RunReadRecord` 路径和一条 pending 候选。没有用户纠正记录，没有真实模型错误账 | 只有假模型链的部分账本 |
| F11 P0 | 授权审阅，非作者不能改意见 | `ReviewOpinion` 表在。`POST /review-opinions` 不在 OpenAPI | 没有实现 |
| F12 P0 | 导出路径、来源索引、作品清单、报告草稿 | 导出路由不在 OpenAPI | 没有实现 |
| F13 P1 | 团队评论与交接 | 无对应路由 | 本轮不验收，也没有实现 |
| F14 P1 | 两页或两分支并列比较 | 无 `compare_versions` | 本轮不验收，也没有实现 |
| F15 P1 | 更多格式逐个接转换器 | 无 | 本轮不验收，也没有实现 |
| F16 P0 | 按任务选择澄清、证据、方法、进度；未调用的角色不耗预算 | `run_fake_chain` 固定跑澄清→证据→方法，批评在第二条运行。不是按任务挑选，也不是证据与方法并行 | 只符合“一次运行最多三个职责” |
| F17 P0 | 关键主张带来源版本和页码/时码/区域；无证据要说明缺口 | `search_evidence` 命中带回作品版本和 locator。有锚点但摘录不同是 `no_hit`，没有锚点是 `not_indexed`。查询上限是 20。技术规范工具表写 `limit<=12` | 关键词检索有测试。上限与原文不一致。不是每条主张的引用 |
| F18 P0 | 方法建议含问题、输入、步骤、局限和来源 | 方法问句固定为「没有方法卡可查。上面的证据是否够用？」。`method_card` 写成 None。没有方法卡表 | 没有实现 |
| F19 P0 | 智能体只生成候选；确认前业务图和阶段不变 | 假模型链写 pending 的 `draft_decision`。页面确认不调用 `consume_candidate`，不建页。测试断言候选行数不变、状态仍是 pending | 生成候选有测试。人工提交没有 |
| F20 P0 | 区分会话、任务状态、已确认事实和可重建摘要；过时结论退出默认检索 | 运行会写 `ContextSnapshot`。没有换页后旧运行读集不变的测试，也没有过时摘要退出检索的测试 | 只有快照字段，没有这条行为 |
| F21 P0 | 超时、无证据、引用失效、版本冲突、智能体分歧都有可解释状态 | 草稿 409、票据过期、预算耗尽、任务失败有测试。没有模型 429/5xx 映射，没有分歧状态 | 部分有测试 |
| F22 P1 | 证据聚类 | 无 | 本轮不验收，也没有实现 |
| F23 P1 | 带来源的方案批评和报告草稿 | 假模型链的批评角色只产出 `draft_decision` 提案，不是 F23 的报告 | 本轮不验收。现有批评不是这条 |
| F24 P2 | 实时共编、自动工程结论、全面知识图谱 | 无 | 本轮不验收，也没有实现 |

## 验收条款 A / W / S / R

出处：`07_BACKEND_ACCEPTANCE.md`。下表的“有直接测试”指全量 308 里能点名的测试函数，不是推断。

| ID | 结论 | 依据 |
| --- | --- | --- |
| A01 | 有直接测试 | `test_parallel_pages_have_no_default_edge`。建项目 HTTP 在 |
| A02 | 没有实现原文 | 没有支持/反对边类型，也没有节点类型闭集 |
| A03 | 有直接测试 | `test_two_connections_cannot_both_advance_the_same_draft`。文档要求至少稳定重复 20 次。这次全量只跑了 1 次，不能写成 20 次稳定 |
| A04 | 有相邻测试 | `test_checkpoint_conflict_rolls_back_unless_one_row_moves`、`test_two_checkpoints_cannot_both_move_the_same_head` |
| A05 | 有直接测试 | `test_branch_keeps_later_source_versions_and_skips_tests` |
| A06 | 没有实现 | 没有合流进入下一章并让新页主体空白的命令 |
| A07 | 有直接测试 | `test_save_layout_advances_only_layout_revision`。内容未变的候选不因此过期，没有单独测试 |
| A08 | 有直接测试 | `test_read_history_selects_without_moving_pointers`、`test_history_selects_do_not_move_heads` |
| A09 | 有相邻测试 | 分支测试会跳过旧测试。没有一条专门断言作品 V1 的测试在 V2 出现后仍只绑 V1 |
| A10 | 有相邻测试 | `test_project_command_replays_and_history_does_not_write`、`test_repeated_checkpoint_key_creates_one_version`。不是 A11 那种双人确认 |
| A11 | 只有条件更新 | `test_consume_candidate_targets_one_row_and_rolls_back`。没有两名具权者经 HTTP 确认同一候选并只产生一张页 |
| A12 | 没有实现 | 无 preview/commit 路由，无 `preview_hash` 服务 |
| A13 | 有相邻测试 | 草稿和检查点在审计前失败会回滚。候选、页、边、Outbox 同事务回滚没有一条测试 |
| A14 | 没有对应测试 | `supersedes_id` 列在决定、证据、测试记录上。没有“追加更正且原记录仍可读”的验收测试 |
| W01 | 没有这条调度 | 假模型链是澄清→证据→方法的串行，批评在另一次运行。不是证据与方法并行、批评等待两者 |
| W02 | 内存调度有部分 | `src/sinuo/agents/runner.py` 的 `DagRunner` 拒绝超预算、重复角色和环。`tests/test_runner.py` 在这 308 里。它不写业务库，也不产生阶段候选 |
| W03 | 有预算测试 | `test_reserve_and_settle_move_model_call_counts`、`test_unknown_actual_stays_reserved`。假模型链没有把这次调用记入该预算 |
| W04 | 有相邻测试 | `test_cancel_run_closes_status_and_rejects_late_publish`、`test_old_fence_cannot_finalize_or_renew_after_cancel`。不是“候选已发布后旧任务返回” |
| W05 | 有直接测试 | `test_expired_running_job_can_be_claimed`、`test_two_connections_only_one_claim_succeeds`、`test_finalize_requires_live_lease_and_writes_one_receipt` |
| W06 | 有直接测试 | `test_committed_event_is_recovered_claimed_and_not_reset`。这不是跨系统恰好一次 |
| W07 | 有直接测试 | `test_crash_before_finalize_keeps_file_and_same_token_publishes_once`、`test_old_token_cannot_replace_the_new_attempt_ref` |
| W08 | 没有模型传输 | 无 429/5xx 适配器。取消运行有测试。`DisabledModelProvider` 抛 `ProviderDisabled`，不落运行 |
| W09 | 有直接测试 | `test_same_consumer_effect_is_reused`、`test_different_consumer_effect_rolls_back_finalize` |
| W10 | 有直接测试 | `test_replay_returns_later_events_in_seq_order`、`test_foreign_last_event_id_does_not_return_other_body`、`test_sse_route_returns_persisted_events_in_order` |
| W11 | 没有测试 | 未见切换页后旧运行保持原快照、新运行另建 ID 的测试 |
| W12 | 没有实现 | 无索引重建，无 FTS5 |
| S01 | 有直接测试 | `test_missing_expired_and_revoked_session_rejected` |
| S02 | 有直接测试 | `test_foreign_project_id_in_authorized_url_hides_title_and_count`、`test_page_projection_strips_original_thumbnail_excerpt_citation_and_hidden_counts` |
| S03 | 没有审阅实现 | 候选确认 HTTP 不存在。评审不能确认候选，因此也没有这条拒绝测试 |
| S04 | 有直接测试 | `test_cookie_write_rejects_missing_or_wrong_csrf_and_bad_origin`、`test_auth_responses_have_no_wildcard_cors` |
| S05 | 部分有测试 | SSE 撤权有 `test_sse_route_hides_event_text_without_current_read`。搜索撤权有检索测试。下载撤权有 `test_original_download_requires_grant_and_omits_disk_path`。旧导出不存在，所以没有导出旁路测试 |
| S06 | 没有工具白名单测试 | 假模型不接收外部提示去调用确认命令 |
| S07 | 有直接测试 | 路径穿越、声明长度、声明哈希、越权票据均有原件测试 |
| S08 | 有直接测试 | `test_upload_pin_before_gc_keeps_the_generation`、`test_gc_claim_before_pin_blocks_install`、`test_stale_gc_unlink_leaves_the_new_generation_download`、`test_rollback_after_install_cleans_only_the_unregistered_orphan` |
| S09 | 部分 | 超像素栅格和超像素 PDF 有测试。PDF 只渲染第 0 页并卡像素，源码里没有“超页数”限额。GLB 不调用 Blender。没有转换超时后杀掉子进程的测试 |
| S10 | 有直接测试 | `test_same_name_new_bytes_are_two_versions_and_reuse_stays_in_project` |
| S11 | 部分 | 中文摘录、无命中、未索引、其他项目不泄露有 `test_search_evidence_returns_the_authorized_version_only`。没有旧版默认排除，也没有 FTS5 分词 |
| S12 | 没有导出和索引旁路 | 墓碑表在，`test_version_rows_and_tombstones_reject_raw_sql_mutation` 只拒绝直接改版本行。没有隐私删除后搜索、预览、导出只见墓碑的测试 |
| R01 | 没有实现 | 无审阅 HTTP |
| R02 | 部分有测试 | off/manual 不建 Job、event 去重、digest 窗口有建议偏好测试。原文还要求“直接请求可运行”和“权限/完整性告警独立”。这两句没有对应测试 |
| R03–R07 | 没有实现 | 无导出，无备份，无恢复 |

## 五个只读工具

出处：技术实施规范 v1.1 的工具表。

| 工具 | 结论 |
| --- | --- |
| `search_evidence` | 有。先授权再匹配摘录。`limit` 允许 1 到 20，规范表写不超过 12 |
| `get_page_snapshot` | 有。先 `page.read`。无权、撤权、其他项目、缺失版本返回空快照，不含标题和正文。请求哪一版返回哪一版 |
| `get_graph_neighborhood` | 有。只走三种谱系边。`depth` 0 到 2，`max_nodes` 1 到 30。无权节点不进入结果，也不从它继续走 |
| `lookup_method` | 没有函数，没有方法卡表 |
| `compare_versions` | 没有函数 |

`run_fake_chain` 会调用前两个，不会调用后两个。`read_fake_chain` 只读已经写好的运行，不再检索。

## 任务卡

| 任务 | 状态 | 和原卡的差距 |
| --- | --- | --- |
| T01–T04 | done | 维持任务卡。本轮没有把它们打回 |
| T05 | implementing | 非 3D 的原件、pin、审计、票据过期、PNG/PDF 预览有测试。验收 5 的 GLB 夹具按用户决定不做 |
| T06 | implementing | 取消、SSE、预算、建议频率、Outbox、派生文件、`run_tasks.retry_wait` 有测试。真实模型没有。`jobs` 没有 `retry_wait` |
| T07 | implementing | 三个只读工具有。方法卡、版本对比、FTS5 没有 |
| T08 | implementing | 一条固定的假模型链和只读页面有。任务卡的七条验收没有齐。没有会发请求的 DeepSeek 适配器。`tests/acceptance/test_agent_runs.py` 现有 5 个测试 |
| T09 | planned | 只有 `consume_candidate` 条件更新。没有预览哈希，没有确认建页 |
| T10–T11 | planned | 没有审阅、导出、备份恢复、纵向验收 |

`07` 的最终交接清单：生成的 OpenAPI 只有上面 16 条；没有可重演演示脚本；没有备份恢复报告；没有真实模型记录；没有 C/S/W/R 全表证据。客观模型评测的 10–20 题、B0/B1/M1 比较没有做。

UI 白皮书和 UI 页面规范里的 R05 工作台、R10 运行页、R11 记忆目录没有做。`00` 把产品页面放在 G4 之后。现在的 HTML 只是假模型链只读页，确认和拒绝只改浏览器本地文字。

## 下一窗不要做的事

不要重做 `read_fake_chain` 和两条 fake-chain 路由。不要再发“只读函数”和“演示页”两路并行，那一刀已经在一个窗口里落地。不要挂后台子代理后结束回合。不要跑 `live_model`。不要做 3D。不要 git commit。

若继续施工，下一件具体的事仍是：一个可重复启动，在临时库种下合成课题「夜间取物辅助产品」，调用已有的 `run_fake_chain`，打印 view 地址。那不是新的 T 号，也不是 T09。
