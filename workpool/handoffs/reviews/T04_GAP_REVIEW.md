# T04 缺口复审

日期：2026-10-31。只读，只写本文件。不改代码，不把 T04 标成 done。任务卡 `workpool/tasks/T04_VERSIONS_COMMANDS.md` 的状态仍是 implementing。

对照的是当前命令实现和下面这次 pytest 实际跑过的测试。结论不沿用旧审查稿。

```
.\.venv\Scripts\python.exe -m pytest -q tests/acceptance/test_versions_commands.py tests/acceptance/test_versions_history.py tests/acceptance/test_versions_projects.py tests/acceptance/test_versions_http.py tests/acceptance/test_versions_checkpoint_commands.py -o cache_dir='_施工文件/2026-10-30_grok/99_临时文件/pytest-cache-gap' --basetemp='_施工文件/2026-10-30_grok/99_临时文件/pytest-gap'
```

退出码 **0**。**29 passed**。1 条 Starlette/httpx 弃用警告，不改变退出码。

结论：**blocking 0，note 4。** 验收 6 条都有直接测试。残留缺口都有另一条已经失败就会红的测试锁住对应行为，不升成 blocking。

## 验收 1

两请求基于同一草稿修订更新同一内容，仅一方成功，另一方 409；远端与失败方输入可复核。

### `test_two_connections_cannot_both_advance_the_same_draft`

两个线程各开一个 `write_uow`，都对 `PAGE_A`、基线 `synthetic-pv-a2`、`expected_revision=5` 调用 `update_draft`。正文一份是空节点，另一份带 `synthetic-other`。

实际断言：

- 两条线程都结束。
- 结果排序后是 `["conflict", "ok"]`，失败边是 `DraftConflict`。
- `page_drafts.content_revision == 6`。
- `content_json` 等于胜方正文，`content_hash` 等于胜方摘要。
- 库里的正文不等于败方正文。
- 测试自己留着的两份输入标签不同。

还缺什么：这里没有 HTTP 状态码，也没有 `DRAFT_REVISION_CONFLICT`。它不经过 `save_draft` 和幂等回执。

### `test_second_draft_write_on_the_same_revision_returns_409`

同一个 `TestClient` 上先后 `PATCH /api/v1/pages/{page_id}/draft`，`expected_revision` 都是 1，幂等键不同，节点分别是 `synthetic-winner` 和 `synthetic-loser`。

实际断言：

- 第一笔 200，`content_revision == 2`。
- 第二笔 409，`error.code == "DRAFT_REVISION_CONFLICT"`。
- 响应文本里没有 `synthetic-loser`。
- `page_drafts.content_json` 含 `synthetic-winner`，不含 `synthetic-loser`。

还缺什么：这是顺序请求，不是两条独立连接同时写。失败键有没有留下幂等行，这条测试没有再查。

两笔合在一起：并发时只有一份正文落库，由第一条锁住；同一修订的第二笔在 HTTP 上是 409，且败方正文不在响应里也不在库里，由第二条锁住。`patch_draft` 调用的就是 `save_draft` → `update_draft`，`DraftConflict` 在 `register_version_exception_handlers` 里映射成 409。没有一条测试同时做「两个连接 + HTTP 409」，这个拆开见 note 1，不升 blocking。

## 验收 2

相同请求键并发或重复 20 次只创建一个页或版本；相同键改正文报 409。

### `test_repeated_checkpoint_key_creates_one_version`

同一个 `CommandScope`（键 `synthetic-checkpoint-20`）顺序调用 `execute_idempotent` 20 次。

实际断言：

- 20 次响应里的 `page_version_id` 都等于第一次。
- `page_versions` 行数比调用前多 1。
- 幂等行存在，且 `response_json["page_version_id"]` 等于第一次的版本 id。

还缺什么：20 次是顺序的，不是 20 路并发。没有断言 HTTP 状态码。

### `test_checkpoint_key_with_a_different_body_conflicts`

同一键先按草稿哈希执行一次，再把 `expected_content_hash` 换成 64 个 `f`。

实际断言：第二次抛 `IdempotencyConflict`；抛出之后版本行数与冲突前相同。

还缺什么：没有 HTTP 409，也没有错误码 `IDEMPOTENCY_KEY_REUSED`。

### `test_repeated_branch_key_creates_one_page`

键 `synthetic-branch-20` 顺序 20 次。

实际断言：后 19 次的 `page_id` 等于第一次；`Page` 行数比调用前多 1。

还缺什么：没有并发，也没有同键异体用例。

### 建项与平行页

- `test_repeated_project_key_creates_one_blank_page`：顺序 20 次。`Project`、`Page`、`PageVersion`、`PageDraft`、`AuditEvent`、`OutboxEvent`、`IdempotencyRecord` 各 1。重放 `page_id` 相同，`project_revision` 与 `access_epoch` 都是 1。空白页 `parent_version_id` 为空，没有 `PageEdge`。阶段配置版本号 1、位置 0，`head_config_version_id` 指向该配置。
- `test_project_key_with_a_different_body_conflicts`：异体抛 `IdempotencyConflict`。结束后仍是 1 个项目、1 页，标题仍是 `synthetic-project`。
- `test_concurrent_project_key_creates_one_page`：两线程，不是 20 路。两边都没有异常，两个 `page_id` 相同，`Page` 与幂等行都是 1。
- `test_repeated_parallel_key_creates_one_page_without_next_edge`：顺序 20 次。`Page` 与 `PageVersion` 各 2，`PageEdge` 为 0，`kind == "next"` 的边为 0。新页 `parent_version_id` 为空，`page.create` 审计 1 条，项目修订为 2，`access_epoch` 仍是 1。
- `test_parallel_key_with_a_different_body_conflicts`：异体抛 `IdempotencyConflict`。页数仍是 2，标题集合里没有 `synthetic-parallel-other`，项目修订仍是 2。
- `test_concurrent_parallel_key_creates_one_page`：两线程。两个 `page_id` 相同，页数 2，项目修订 2，`access_epoch` 仍是 1。
- `test_project_command_replays_and_history_does_not_write`：同一 `Idempotency-Key` 把 title 改成 `synthetic-other-body`，HTTP 409，`error.code == "IDEMPOTENCY_KEY_REUSED"`。同键同体第二次 201，且 `meta.idempotency_replayed is True`。

还缺什么：checkpoint 和 branch 没有自己的并发用例，branch 没有同键异体用例，checkpoint 异体停在领域异常而不是 HTTP 409。这些见 note 2。`execute_idempotent` 在哈希不同时先抛 `IdempotencyConflict`，`IdempotencyConflict` 的 HTTP 映射是 409。建项的 HTTP 测试和 checkpoint 的领域冲突测试已经把这条映射和「异体不产生第二份版本」锁住。

## 验收 3

在目标写入后、审计 / Outbox / 回执前注入异常，整个事务回滚。

### `test_draft_failure_before_audit_rolls_back`

`save_draft` 的 `before_audit` 里读到草稿修订已是原值加 1，且 `page.draft.write` 的审计计数为 0，然后抛 `RuntimeError("injected")`。异常从 `write_uow` 里的 `execute_idempotent` 冒出。

实际断言：

- 回滚后 `content_revision` 和 `content_hash` 回到注入前。
- 键 `synthetic-draft-fail` 的 `IdempotencyRecord` 不存在。

还缺什么：回滚后没有再读 `content_json`，也没有再数审计行。`save_draft` 在钩子之后才插入审计，成功路径不插入 Outbox。钩子已经要求当时审计为 0，所以这次注入没有把审计写出去。

### `test_checkpoint_failure_before_audit_rolls_back`

钩子里断言：该页版本数已比注入前多 1，`page.version_created` 的 Outbox 计数仍等于注入前，`page.checkpoint` 审计为 0，然后抛 `RuntimeError("injected")`。

实际断言：回滚后版本行列表、HEAD、项目修订、Outbox 计数都回到注入前；键 `synthetic-checkpoint-fail` 没有幂等行。

还缺什么：回滚后没有再数审计行。钩子已经要求抛出时审计为 0，而审计插入在钩子之后。

### `test_branch_failure_before_audit_rolls_back`

钩子只断言 `Page` 计数比注入前多 1，然后抛 `RuntimeError("injected")`。

实际断言：回滚后 `Page` 计数和 `PageEdge` 计数回到注入前；键 `synthetic-branch-fail` 没有幂等行。

还缺什么：钩子没有看审计或 Outbox 是否仍为 0。回滚后没有数 Outbox、审计、项目修订，也没有重读源页。`branch_from_version` 把项目修订更新放在钩子之前，把审计和 `page.version_created` 放在钩子之后，都在同一个 `write_uow` 里。页和边已经回到原计数，说明这个事务没有提交。Outbox 计数本身没在这条测试里出现，见 note 3。

另外两条同类测试也跑过，但不替代上面三条：

- `test_project_failure_before_audit_drops_page_and_receipt`：钩子看到页和版本已在，审计与 Outbox 仍为 0。结束后 `Page`、`Project`、幂等行、审计都是 0。结束后没有再数 Outbox。
- `test_parallel_failure_before_audit_drops_page_and_receipt`：钩子看到新页、HEAD 版本、项目修订 2、`access_epoch` 仍为 1、`page.create` 审计为 0、边为 0。结束后页数 1、修订 1、`access_epoch` 1，这条 `page.create` 的幂等行和审计都是 0。钩子和结束时都没有数 Outbox。

## 验收 4

保存版本后编辑草稿，历史版哈希与内容不变；多次 GET 不改 HEAD 或 ProjectRevision。

### `test_history_keeps_confirmed_bytes_after_a_later_draft`

HTTP 上创建项目、写入确认正文、checkpoint，记下版本行的 `content_hash` 与 `content_json`、当时的项目修订和 HEAD。再把草稿改成带 `synthetic-later-draft` 的文档。然后对 `/api/v1/pages/{page_id}/history` 做两次 GET。

实际断言：

- 后一次改草稿返回 200。
- 两次 GET 都是 200，且 `data` 相等。
- 响应里该确认版本的 `content_hash` 等于保存后立刻读到的库内哈希。
- 两次 GET 之后，该版本行的 `content_hash` 与 `content_json` 仍等于保存时的快照。
- `content_json` 里没有 `synthetic-later-draft`。
- 两次 GET 之后的项目修订等于保存后的修订。
- 两次 GET 之后的 HEAD 等于保存后的 HEAD，并且等于这次确认出来的版本 id。

还缺什么：GET 体只有 `page_version_id`、`version_no`、`content_hash`，没有正文。正文不变是靠随后的 SQL，不是靠响应体。这条测试没有断言 `access_epoch`。验收句子要求的是哈希、正文、HEAD、项目修订，这四项都有断言。

旁边两条把「读取不写」再锁紧了，但不是这条 HTTP 场景本身：

- `test_read_history_selects_without_moving_pointers`：四次 `read_history`，语句都以 `SELECT` 开头且不含写关键字，会话没有 new/dirty/deleted，版本、页、草稿、项目快照不变。
- `test_checkpoint_appends_and_keeps_old_bytes`：确认后再改草稿，四次领域读取。旧两版整行不变，新版正文仍是确认时的草稿，HEAD 与项目修订停在确认后的值。

## 验收 5

从 V2 分支时原 V3 与后续路线保留，新页有独立 ID 与 origin 映射，旧测试不被重新确认。

### `test_branch_keeps_later_source_versions_and_skips_tests`

种子里 `synthetic-pv-a2` 是第 2 版，正文只有节点 `synthetic-node-1`（带 `synthetic-art-v1`），没有边。测试先把草稿改成两个节点加一条 `synthetic-edge-n1`，再确认出更高的一版，然后从 `synthetic-pv-a2` 分支。

从 V2 分出之后的实际断言：

- 源页整行和源页全部版本行等于分支前快照。
- 源页 HEAD 仍是那个更高版本。
- 新 `page_id` 不是 `PAGE_A` 或 `PAGE_B`，`source_page_id` 是 `PAGE_A`。
- 新版本落在新页上，`version_no == 1`，`parent_version_id` 为空，新页 HEAD 指向它。
- 克隆节点 id 的集合不等于 `{"synthetic-node-1"}`。
- 克隆后第一个节点仍引用 `synthetic-art-v1`。
- 新边的 `source_version_id` 是 `synthetic-pv-a2`，`target_version_id` 是新版本，`kind` 是 `branches_from`。
- `reason` 里 `nodes[0]["origin_node_id"] == "synthetic-node-1"`。
- `reason` 里 `nodes[0]["node_id"]` 等于克隆正文里第一个节点的 `node_id`。
- 项目修订等于更高版本的修订再加 1，`access_epoch` 仍是 1，源草稿的 `content_revision` 不变。

随后再从那个更高版本分出第二条路线。这里对 origin 的断言是集合，不是逐对：

- `reason.nodes` 里 `origin_node_id` 的集合等于源文档节点 id 的集合。
- `reason.nodes` 里 `node_id` 的集合等于克隆文档节点 id 的集合。
- `origin_edge_id` 的集合等于 `{"synthetic-edge-n1"}`。
- 克隆节点 id 与源节点 id 不相交，边 id 也不相交。
- 工件版本 id 的顺序与源文档相同，边的两端改接到新节点 id。
- 这条新版本的 `parent_version_id` 仍为空，`version_no` 仍是 1，边的 `kind` 仍是 `branches_from`。

旧测试：

- 两次分支之后，以及源页再确认出第 4 版之后，`test_records` 按 `id, artifact_version_id, observation, result, project_revision` 排序的结果都等于分支前的快照。
- 意见和决定的快照不变，工件版本行数不变，个人布局修订不变。
- 第 4 版接在原来的三行版本之后，两个分支页的 HEAD 仍指向各自的第 1 版。

还缺什么：

- 从更高版本分出的那条映射没有断言「某个 `origin_node_id` 配哪个新 `node_id`」。两个集合分开相等时，对调配对也会通过。V2 那条只有一个节点，`nodes[0]` 的配对已经被断言锁住，见 note 4。
- `test_records` 的查询没有包括 `conditions_json`、`source`、`supersedes_id`、`created_at`。行集合、`result` 和 `project_revision` 不变，已经能否定「多了一条测试」或「把原结果改掉」。`branch_from_version` 不写 `test_records`。
- 种子里已有的边 `synthetic-edge`（`synthetic-pv-a2` → `synthetic-pv-b1`）和 `PAGE_B` 的 HEAD，分支后没有再读。分支函数只插入新页、新版本、新草稿、新边，并条件更新项目修订，不改源页和其他边。

## 验收 6

当前权限变化后重放命令不泄露受限原响应；不允许通过客户端传 actor 提权。

### `test_revoked_membership_hides_draft_checkpoint_and_branch_replays`

先以会话主体做成草稿、checkpoint 和分支。三条命令的正文里额外放 `actor_id: "forged-actor"` 时，状态都是 422。成员被改成 `revoked` 之后，用原幂等键和原正文重放这三条。

实际断言：

- 三条重放都是 404，`error.code == "NOT_FOUND_OR_HIDDEN"`。
- 响应文本不含草稿 `content_hash`、确认版本 id、新分支页 id。
- 库里的幂等行仍然含这三份值：`page.draft.write` 含草稿哈希，`page.checkpoint` 含确认版本 id，`page.branch` 含分支页 id。

### `test_revoked_membership_replay_does_not_return_stored_page`

平行页创建的正文里额外放 `actor_id` 时返回 422。创建成功后撤销成员，再用原键重放。

实际断言：重放 404，`NOT_FOUND_OR_HIDDEN`；响应文本不含 `page_id`、`page_version_id`、`audit_event_id` 和标题 `synthetic-canary-parallel-page`。库里的 `page.create` 回执仍然含页 id 和版本 id。

### `test_project_command_replays_and_history_does_not_write` 里的 actor 片段

创建项目的 JSON 里放 `actor_id: "forged"` 返回 422。这条测试没有在撤销成员后重放 `project.create`。

还缺什么：没有测查询参数或请求头里的 actor。版本命令的 schema 都是 `extra="forbid"`，路由里的 `CommandScope.actor_id` 来自会话主体，不读正文里的 actor。`execute_authorized_command` 在把库存回执交出去之前调用 `authorize`。上面两条撤权测试锁住的是：授权失败时 HTTP 不带回库存回执，库里的回执还在。

`project.create` 的资源是账号，`SessionAuthorizer` 对 `PROJECT_CREATE` 只看账号是否仍可建项，不看项目成员。撤销成员不会让这条命令变成未授权，重放仍会返回当时的 id。这和「命令已经不被允许时不得吐出旧回执」不是同一条路径，页级四条命令的隐藏已经由测试锁住。

## blocking

无。

上面 6 条里，当前实现违反合同、并且没有任何一条测试会因此失败的情况，没有看到。草稿条件更新、同键一份效果、注入后整笔回滚、确认字节不被后写草稿改掉、从 V2 分支保留更高版本并写下 `origin_node_id`、撤权后重放不返回库存 id，都有失败就会红的断言。

## note

### 1. 并发胜负和 HTTP 409 拆在两条测试里

验收 1 的并发正文由 `test_two_connections_cannot_both_advance_the_same_draft` 锁住，409 和败方正文不落库由 `test_second_draft_write_on_the_same_revision_returns_409` 锁住。缺的是同一次「两个连接打到 HTTP」。不升 blocking。

### 2. 同键 20 次与异体 409 没有在每条命令上各做一遍

checkpoint 与 branch 的 20 次是顺序重放。并发只创建一页，由建项和平行页的两线程测试锁住，用的是同一个 `execute_idempotent` 和 `BEGIN IMMEDIATE`。checkpoint 异体锁住的是 `IdempotencyConflict` 且版本数不增加。HTTP 409 / `IDEMPOTENCY_KEY_REUSED` 由建项测试锁住。branch 没有单独的异体测试。不升 blocking。

### 3. 分支注入回滚没有数 Outbox

`test_branch_failure_before_audit_rolls_back` 只锁页数、边数和回执。checkpoint 的失败测试已经锁住「钩子里 Outbox 计数不变，回滚后仍不变」。分支的 Outbox 插入在同一个 `before_audit` 之后、同一个 `write_uow` 里。不升 blocking。

### 4. 多节点 origin 配对只锁了集合

从更高版本分出的 `origin_node_id` 与新 `node_id` 是两个集合分别相等。从 V2 分出的边已经锁住 `nodes[0].origin_node_id == "synthetic-node-1"`，且 `nodes[0].node_id` 等于克隆后的那个节点 id。不升 blocking。

## 六条是否都有直接测试

有。

| 验收 | 直接测试 |
| --- | --- |
| 1 | `test_two_connections_cannot_both_advance_the_same_draft`；`test_second_draft_write_on_the_same_revision_returns_409` |
| 2 | `test_repeated_checkpoint_key_creates_one_version`；`test_repeated_branch_key_creates_one_page`；`test_repeated_project_key_creates_one_blank_page`；`test_repeated_parallel_key_creates_one_page_without_next_edge`；以及对应的异体 / 并发 / 建项 HTTP 409 |
| 3 | `test_draft_failure_before_audit_rolls_back`；`test_checkpoint_failure_before_audit_rolls_back`；`test_branch_failure_before_audit_rolls_back`；建项与平行页各有一条同类注入 |
| 4 | `test_history_keeps_confirmed_bytes_after_a_later_draft` |
| 5 | `test_branch_keeps_later_source_versions_and_skips_tests` |
| 6 | `test_revoked_membership_hides_draft_checkpoint_and_branch_replays`；`test_revoked_membership_replay_does_not_return_stored_page` |
