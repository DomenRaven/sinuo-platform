# T04 复审

日期：2026-10-30。只读。对照 `workpool/tasks/T04_VERSIONS_COMMANDS.md` 验收 6 条，以及当前的 `src/sinuo/domain/auth/projection.py`、`src/sinuo/application/auth/receipts.py`、`src/sinuo/application/idempotency.py`、`src/sinuo/api/versions/routes.py`、`src/sinuo/api/versions/schemas.py`、`tests/acceptance/test_versions_commands.py`、`tests/acceptance/test_versions_history.py`、`tests/acceptance/test_versions_projects.py`、`tests/acceptance/test_versions_http.py`。

`T04_VERSIONS_REVIEW.md` 不作为本次结论来源。本文不改代码、测试或任务状态。T04 不标 done。任务文件里的状态仍是 implementing。

验收命令在 `sinuo-platform` 用项目 `.venv` 执行：

`.venv\Scripts\python.exe -m pytest -q tests/acceptance/test_versions_commands.py tests/acceptance/test_versions_history.py tests/acceptance/test_versions_projects.py tests/acceptance/test_versions_http.py -o cache_dir='_施工文件/2026-10-30_grok/99_临时文件/pytest-cache-rereview' --basetemp='_施工文件/2026-10-30_grok/99_临时文件/pytest-rereview'`

退出码 **0**。**20 passed**，1 条 Starlette/httpx 弃用警告，不改变退出码。下面每条都以这次跑过的测试和当前源码为准。

结论：**blocking 1 条，note 5 条。** 验收第 6 条现在有测试。

## blocking

### 1. 同一草稿修订的并发更新没有把 409 和双方输入对上

- 标记：blocking
- 测试：`test_two_connections_cannot_both_advance_the_same_draft`
- 验收原文：两请求基于同一草稿修订更新同一内容，仅一方成功，另一方 409；远端与失败方输入可复核。

两个线程各开一个 `write_uow`，都对 `PAGE_A`、基线 `synthetic-pv-a2`、`expected_revision=5` 调用 `update_draft`，正文不同。测试断言两边都结束，结果排序后是 `["conflict", "ok"]`，失败边捕获的是 `DraftConflict`，然后 `page_drafts.content_revision == 6`。

没有断言 HTTP 409，也没有断言错误码 `DRAFT_REVISION_CONFLICT`。四份验收测试都没有请求 `PATCH /pages/{page_id}/draft`。`register_version_exception_handlers` 会把 `DraftConflict` 映射成 409，这次 pytest 没有走到那条映射。

没有读回 `content_json` 或 `content_hash`。revision 变成 6 只说明条件更新只前进了一次，看不出库里留下的是哪一份正文，也看不出失败方的输入没有被写进去。

## note

### 2. 同键二十次和异体冲突有测试，409 只在创建项目的 HTTP 上

- 标记：note
- 测试：`test_repeated_project_key_creates_one_blank_page`、`test_concurrent_project_key_creates_one_page`、`test_project_key_with_a_different_body_conflicts`、`test_repeated_parallel_key_creates_one_page_without_next_edge`、`test_concurrent_parallel_key_creates_one_page`、`test_parallel_key_with_a_different_body_conflicts`、`test_project_command_replays_and_history_does_not_write`
- 验收原文：相同请求键并发/重复 20 次只创建一个页或版本；相同键改正文报 409。

建项同一个键顺序调用 20 次：`Project`、`Page`、`PageVersion`、`PageDraft`、`AuditEvent`、`OutboxEvent`、`IdempotencyRecord` 各 1，重放的 `page_id` 与第一次相同。平行页同一个键顺序 20 次：`Page` 与 `PageVersion` 各为 2（原页加新页），`page.create` 审计 1 条，重放 `page_id` 相同，且没有 `next` 边。

并发是两线程，不是 20 路。建项并发断言 `Page == 1` 且幂等记录 1 条，两个返回的 `page_id` 相同。平行页并发断言 `Page == 2`、修订仍是 2、两个 `page_id` 相同。并发用例没有再数 `PageVersion`。

异体：建项和平行页都 `pytest.raises(IdempotencyConflict)`，并检查没有多出一页、标题没有变成另一份正文。HTTP 409 和 `IDEMPOTENCY_KEY_REUSED` 只出现在创建项目：同一 `Idempotency-Key` 把 title 改成 `synthetic-other-body`。平行页异体没有 HTTP 状态码断言。

### 3. 注入回滚覆盖建项和平行页，回滚后没有再数 Outbox

- 标记：note
- 测试：`test_project_failure_before_audit_drops_page_and_receipt`、`test_parallel_failure_before_audit_drops_page_and_receipt`
- 验收原文：在目标写入后、审计/Outbox/回执前注入异常，整个事务回滚。

建项的 `before_audit` 在页和版本已经能查到、`AuditEvent` 与 `OutboxEvent` 仍为 0 时抛 `RuntimeError`。事务结束后新会话看到 `Page`、`Project`、`IdempotencyRecord`、`AuditEvent` 都是 0。结束时没有再数 `OutboxEvent`；注入点当时已经是 0，而且抛出发生在 `_record_page_version` 之前。

平行页的注入点断言新页和版本已在、项目修订已是 2、`page.create` 审计仍是 0、边是 0，然后抛错。回滚后页数回到 1，项目修订回到 1，`page.create` 的幂等记录和审计都是 0。注入点没有断言 `OutboxEvent`，回滚后也没有按这次失败命令去数 Outbox。

checkpoint、branch、draft 没有同类注入。这三条命令的审计和 Outbox 写在同一函数后段，没有 `before_audit` 缝。

### 4. 历史哈希在领域读取里保持不变；HTTP 查库只覆盖新建之后

- 标记：note
- 测试：`test_checkpoint_appends_and_keeps_old_bytes`、`test_read_history_selects_without_moving_pointers`、`test_project_command_replays_and_history_does_not_write`
- 验收原文：保存版本后编辑草稿，历史版哈希与内容不变；多次 GET 不改 HEAD 或 ProjectRevision。

`test_checkpoint_appends_and_keeps_old_bytes` 先确认一版，再改草稿，然后在 `write_uow` 里调用 `read_history` 四次。它断言前两版整行仍等于确认前的快照，新版的 `content_json`/`content_hash` 仍是确认时的草稿，HEAD 仍是这次确认的版本，项目修订仍是 5。这是领域函数，不是 HTTP GET。

`test_read_history_selects_without_moving_pointers` 再读四次，要求语句以 `SELECT` 开头且不匹配写语句，版本、页、草稿、项目的快照不变。场景是种子数据，不是「确认之后再改草稿」。

HTTP 测试在创建项目之后对 `/api/v1/pages/{page_id}/history` 做了两次 GET，两次 `data` 相同，`version_no == 1`。随后用另一条连接查询：

- `SELECT revision FROM projects WHERE id = :project_id`，断言等于 1
- `SELECT head_version_id FROM pages WHERE id = :page_id`，断言等于创建响应里的 `page_version_id`

这次查库没有读 `content_hash` 或 `content_json`，也没有先保存新版本再改草稿。所以「确认后改草稿，历史字节不变」和「HTTP GET 不推动 HEAD / 项目修订」拆在两条路径上，没有合成同一次 HTTP 场景。

### 5. 从 V2 分出的那条边没有读取 origin 映射

- 标记：note
- 测试：`test_branch_keeps_later_source_versions_and_skips_tests`
- 验收原文：从 V2 分支时原 V3 与后续路线保留，新页有独立 ID 与 origin 映射，旧测试不被重新确认。

测试先把草稿改掉并确认，使源页在 `synthetic-pv-a1`、`synthetic-pv-a2` 之上多出一个更高版本，再从 `synthetic-pv-a2` 分支。源页整行和源版本行保持分支前快照，HEAD 仍是那个更高版本。新 `page_id` 不是 `PAGE_A` 或 `PAGE_B`。新版本 `version_no == 1`、`parent_version_id` 为空，HEAD 指向自己。边的 `source_version_id` 是 `synthetic-pv-a2`，`kind` 是 `branches_from`。

origin 映射读的是下一次从那个更高版本分出的边：`reason` 里的 `origin_node_id`、`node_id`、`origin_edge_id`。从 V2 分出的那条边没有读 `reason`。`_clone_document` 对每次分支都写这份映射，但 V2 这次的映射没有被测试读出来。

`test_records` 在分支前、两次分支后、以及源页再确认出第 4 版之后，按 `id, artifact_version_id, observation, result, project_revision` 整表相等。随后源页前三版仍等于分支前快照，第 4 版追加在源页上，两条新页的 HEAD 仍指向各自的版本。评审意见、决策、制品数量、布局修订也不变。

### 6. 撤权重放有测试，范围是 page.create 的成员资格撤销

- 标记：note
- 测试：`test_revoked_membership_replay_does_not_return_stored_page`；`actor_id` 的 422 还在 `test_project_command_replays_and_history_does_not_write`
- 验收原文：当前权限变化后重放命令不泄露受限原响应；不允许通过客户端传 actor 提权。

这条现在有测试。撤权用例的实际断言：

- 创建平行页时带 `actor_id: forged-actor`，状态码 422。这发生在成功创建之前，解析失败，还没有这条命令的回执。
- 同一幂等键成功创建后，把该项目成员改成 `revoked`。
- 用同一键、同一正文再 POST，状态码 404，`error.code` 为 `NOT_FOUND_OR_HIDDEN`。
- 响应文本不含这次的 `page_id`、`page_version_id`、`audit_event_id`，也不含标题 `synthetic-canary-parallel-page`。
- 库存 `idempotency_records.response_json` 仍含 `page_id` 和 `page_version_id`。

标题不在这条 HTTP 回执里。路由写回的正文只有 `page_id`、`page_version_id`、`project_revision`、`audit_event_id`。所以「标题不在响应里」不能单独证明库存正文被挡住；能证明的是上述三个标识没有出现在 404 正文里，同时库里仍有它们。

返回顺序在 `execute_idempotent`：先 `authorize(session)`，授权通过后才 `session.get` 幂等行，哈希一致才 `return` 库存的 `response_json`。成员不是 `active` 时 `decide` 返回 `HIDDEN`，再变成 `NotFoundOrHidden`。因此撤权后的拒绝发生在把库存正文放进返回值之前。`execute_authorized_command` 在调用执行器之前用 `session.get(...) is not None` 只判断是否重放，不把 `response_json` 交给客户端；授权失败时走不到 `project_receipt(response.body)`。测试断言的是响应和库，没有对调用顺序做探针。

没有覆盖的部分：draft、checkpoint、branch、project.create 在撤权或撤掉单条 grant 之后的重放；成员仍为 active、只丢掉 `page.create` 授权时的重放。这几条写路由都进 `execute_authorized_command`，但测试只打了平行页这一条。

客户端 `actor_id`：`ProjectCreate`、`ParallelPageCreate`、`CheckpointBody`、`BranchBody`、`DraftPatch` 都是 `extra="forbid"`，模型字段里没有 `actor_id`。路由上的 `actor_id` 来自 `principal.account_id`。HTTP 422 只打了创建项目和创建平行页。draft、checkpoint、branch 没有再送一次多余的 `actor_id`。

草稿回执不再丢掉 `content_revision`。`_RECEIPT_KEYS` 含 `content_revision` 和 `content_hash`。`test_draft_receipt_keeps_revision_and_hash` 直接调用 `project_receipt`，断言修订号 6 和哈希都在，`excerpt` 不在。这不是一次 HTTP PATCH。`PATCH /pages/{page_id}/draft` 的成功正文就是这两个字段，再经 `execute_authorized_command` 做同一次投影。

`test_save_layout_advances_only_layout_revision` 在这次 20 条里面，但不对应上述 6 条验收。

## 草稿冲突复核

日期仍是 2026-10-30。只读当前测试，没有改测试或实现。上文的 blocking 1 针对的是：并发草稿测试不读回正文，也没有 HTTP 409。

`test_two_connections_cannot_both_advance_the_same_draft` 现在按线程结果区分胜方和败方，并查询 `content_revision`、`content_json`、`content_hash`。它断言修订号为 6，库里的正文和哈希等于胜方，正文不等于败方。

`test_second_draft_write_on_the_same_revision_returns_409` 对同一 `expected_revision` 做第二次 PATCH，断言状态码 409、错误码 `DRAFT_REVISION_CONFLICT`。随后只查询 `content_json`：其中有 `synthetic-winner`，没有 `synthetic-loser`。响应文本里也没有败方节点。这条 HTTP 测试没有读 `content_hash`。

复核命令：

`.venv\Scripts\python.exe -m pytest -q tests/acceptance/test_versions_commands.py::test_two_connections_cannot_both_advance_the_same_draft tests/acceptance/test_versions_http.py::test_second_draft_write_on_the_same_revision_returns_409 -o cache_dir='_施工文件/2026-10-30_grok/99_临时文件/pytest-draft-recheck/cache' --basetemp='_施工文件/2026-10-30_grok/99_临时文件/pytest-draft-recheck'`

退出码 **0**。2 passed，1 条 Starlette/httpx 弃用警告，不改变退出码。

原来的 blocking 1 **关闭**。当前 blocking **0** 条。前面 5 条 note 不动。T04 仍不标 done。
