# T04 版本命令审查

日期：2026-10-30。只读。对照 `workpool/tasks/T04_VERSIONS_COMMANDS.md` 的 6 条验收，读了 `src/sinuo/application/versions/`、`src/sinuo/api/versions/`，以及 `tests/acceptance/` 下四份版本测试：`test_versions_commands.py`、`test_versions_history.py`、`test_versions_projects.py`、`test_versions_http.py`。任务卡点名的验收命令只有第一份。

本轮用项目 `.venv` 执行：

`.venv\Scripts\python.exe -m pytest -q tests/acceptance/test_versions_commands.py tests/acceptance/test_versions_history.py tests/acceptance/test_versions_projects.py tests/acceptance/test_versions_http.py`

退出码 **0**。18 passed。一条 Starlette/httpx 弃用警告，不改变退出码。这 18 条是文件里已经写下的断言。函数存在、以及这 18 条通过，都不等于 T04 的 6 条验收已经完成。

另用当前投影函数执行了一次调用。输入含 `content_revision`、`content_hash`、`page_version_id`、`version_no`、`project_revision`、`audit_event_id`。返回只剩下 `page_version_id`、`project_revision`、`audit_event_id`。

结论：**blocking 1 条，note 6 条。** 六条检查都在下面标了。blocking 是草稿 HTTP 成功体被回执投影清空，不是双写、回滚或提权已经在库上失败。

阶段配置没有单独命令。个人视图只有 `save_layout`，没有 HTTP，也没有幂等执行器。建项时插入的第一条阶段配置，以及 `save_layout` 的存在，都不算这两条命令已验收。`test_save_layout_advances_only_layout_revision` 核对了页版本、草稿、项目修订和 HEAD，没有核对候选行。

## blocking

### B1. 草稿 HTTP 成功体在回执投影后没有修订和哈希

- 标记：blocking
- 文件：`src/sinuo/api/versions/routes.py` 第 188–190 行把 `content_revision` 和 `content_hash` 放进 `CommandResponse`。`src/sinuo/application/auth/receipts.py` 第 38 行在返回前调用 `project_receipt`。白名单在 `src/sinuo/domain/auth/projection.py` 第 29–33 行和第 196–203 行：保留 `id`、`status`、`project_revision`、`audit_event_id`、`access_epoch`、`grant_ids`，以及名为 `id` 或以 `_id` / `_ids` 结尾的键。
- 验收：任务卡要求成功回执和业务效果在同一短事务里。草稿命令算出来的新修订要能回到调用方，下一次条件更新才对得上这次结果。

`execute_idempotent` 把处理函数的原始正文写入 `idempotency_records`。客户端拿到的是投影后的正文，投影结果不写回库存。

草稿处理函数的正文只有 `content_revision` 和 `content_hash`。这两个键都不在白名单里。投影结果是空对象。`PATCH /pages/{page_id}/draft` 成功时，`data` 里没有新修订，也没有哈希。同键重放再投影一次，客户端仍拿到空对象，库存 JSON 里那两字段还在。

确认版本的正文里，`page_version_id`、`project_revision`、`audit_event_id` 留得下，`version_no` 被去掉。建项、平行页和分支的返回键能留下。

四份测试都没有调用草稿 HTTP。18 passed 里没有这条空正文。

## note

### 1. 同一草稿修订的双写只有一方提交

- 标记：note
- 文件：`src/sinuo/application/versions/drafts.py` 第 26–42 行。`write_uow` 在 `src/sinuo/infrastructure/database.py` 第 61 行使用 `BEGIN IMMEDIATE`，异常路径在第 73–75 行回滚。HTTP 映射在 `src/sinuo/api/versions/errors.py` 第 16–24 行，状态 409，码 `DRAFT_REVISION_CONFLICT`，`details` 为空。
- 验收第 1 条：同一草稿修订上的两次更新只有一方成功，另一方 409；远端行和失败方输入可以对上。

条件更新要求 `page_id`、`project_id`、`base_version_id`、`content_revision` 同时匹配，命中行数不是 1 就抛 `DraftConflict`。后拿到写锁的连接会看见已经提交的新修订，条件对不上。

`test_two_connections_cannot_both_advance_the_same_draft` 用两条连接和两份不同正文。断言是一份 `DraftConflict`、一份成功，以及 `content_revision == 6`。它没有读回 `content_json` 或 `content_hash`，失败方那份正文没有和远端行比较。它也没有走 HTTP，因此没有 409。

### 2. 同键重复和并发只建一个页；异体键冲突

- 标记：note
- 文件：`src/sinuo/application/idempotency.py` 第 44–64 行。主键在 `src/sinuo/infrastructure/models.py` 第 15–18 行，包含 `project_id`、`actor_id`、`command`、`request_key`。冲突映射在 `src/sinuo/api/auth/errors.py` 第 145–152 行，状态 409，码 `IDEMPOTENCY_KEY_REUSED`。版本路由在 `routes.py` 里把建项、平行页、草稿、确认版本和分支都交给 `execute_authorized_command`。
- 验收第 2 条：同一请求键并发或重复 20 次只创建一个页或版本；同一键换正文得到 409。

同哈希重放直接返回已存正文，不再调用处理函数。异哈希在调用处理函数之前抛 `IdempotencyConflict`。`write_uow` 的 `BEGIN IMMEDIATE` 把同一库上的两个写事务排开，后进入的一方能看见已提交的回执行。

已有测试锁的是建项和平行页：各重复 20 次、各有一次双线程同键，以及同键换正文抛 `IdempotencyConflict`。建项的 HTTP 测试还有一次同键重放和一次异体 409。断言包括页计数、回执行数，以及并行场景下两个调用拿到同一个 `page_id`。

确认版本、分支和草稿的同键 20 次、并发和异体 409 都没有测试。`checkpoint_page` 和 `branch_from_version` 每次调用都会分配新的版本 id；同键只生效一次靠的是执行器，这两条领域函数自己没有请求键。

### 3. 建项和平行页在审计前失败会整笔回滚

- 标记：note
- 文件：`create_project` 的 `before_audit` 在 `src/sinuo/application/versions/projects.py` 第 137–138 行，位于 `_record_page_version` 之前。`create_parallel_page` 的钩子在 `pages.py` 第 88–89 行。审计和 `page.version_created` 的 Outbox 都在 `_record_page_version`（`pages.py` 第 219–243 行）里。回执在处理函数返回之后由 `execute_idempotent` 插入，仍在路由的 `write_uow` 里。
- 验收第 3 条：目标写入之后、审计 / Outbox / 回执之前注入异常，整笔事务回滚。

`test_project_failure_before_audit_drops_page_and_receipt` 在页和版本已经可见、审计和 Outbox 还是 0 时抛 `RuntimeError`。事务结束后 `Page`、`Project`、`IdempotencyRecord`、`AuditEvent` 的计数都是 0。`test_parallel_failure_before_audit_drops_page_and_receipt` 结束后页数回到 1，项目修订回到 1，这条 `page.create` 的回执和审计计数是 0。钩子抛在 Outbox 插入之前。这两个测试没有再数 `outbox_events`。

`checkpoint_page` 和 `branch_from_version` 把目标行、审计和 Outbox 写在同一个函数里，没有注入缝。异常仍会走出 `write_uow` 并回滚。没有测试在确认版本或分支的目标行已经写入、审计尚未写入时注入异常。`test_checkpoint_conflict_rolls_back_unless_one_row_moves` 覆盖的是错误 HEAD 和错误草稿修订：版本、页、草稿、项目修订、审计计数和 Outbox 计数保持原样。那是条件不匹配，不是验收写的注入点。错误的 `expected_content_hash` 走同一条 `rowcount != 1` 路径，没有单独的测试。

`update_draft` 只更新草稿行。`patch_draft` 不插入 `AuditEvent`，也不插入 Outbox。草稿这条命令的成功事务里有授权、草稿行和回执，没有审计和 Outbox。

### 4. 历史读取不推进 HEAD；HTTP GET 没有在库上复核指针

- 标记：note
- 文件：`read_history` 在 `src/sinuo/application/versions/history.py` 第 29–42 行，语句是按 `version_no` 排序的 `SELECT`，包在 `no_autoflush` 里。HTTP `get_history` 在 `routes.py` 第 315–343 行：解析会话、加载页、按 `page.read` 授权，然后调用 `read_history`。这个处理函数没有调用 `slide_session`，也没有 `write_uow` 或 `commit`。
- 验收第 4 条：保存版本后编辑草稿，历史版的哈希和内容不变；多次 GET 不改 HEAD 或 `ProjectRevision`。

`test_checkpoint_appends_and_keeps_old_bytes` 在确认版本之后改草稿，再调用四次 `read_history`。旧版本的哈希和正文保持确认前的字节，HEAD 停在新版本，项目 `revision` 停在 5，`access_epoch` 仍是 1。`test_read_history_selects_without_moving_pointers` 把 `read_history` 放进会提交的 `write_uow`，捕获到的语句都以 `SELECT` 开头，页、草稿、版本和项目修订与调用前相同。

`test_project_command_replays_and_history_does_not_write` 对同一页做了两次 HTTP GET，比较两次 JSON，并断言 `version_no == 1`。它没有查询 `pages.head_version_id`，也没有查询 `projects.revision`。

### 5. 从旧版分支留下源页、新 ID 和 origin 映射

- 标记：note
- 文件：`branch_from_version` 在 `src/sinuo/application/versions/branches.py` 第 91–96 行说明源页不更新；新版本的 `parent_version_id` 写为 NULL（第 149 行）；页头更新的 WHERE 用新页 id（第 162–168 行）；`page_edges.kind` 为 `branches_from`，`reason` 里是节点和边的 origin 映射（第 50–80 行、第 187–200 行）。
- 验收第 5 条：从 V2 分支时原来的 V3 和后续路线还在；新页有独立 ID 和 origin 映射；旧测试不被重新确认。

`test_branch_keeps_later_source_versions_and_skips_tests` 先在源页上确认出更高版本，再从 `synthetic-pv-a2` 分支。源页整行和源页全部版本与分支前相同，HEAD 仍是更高的那一版。新页 id 与源页不同，新版本号是 1，`parent_version_id` 为空。边的 `kind` 是 `branches_from`，`reason` 里的 `origin_node_id` / `origin_edge_id` 指向源文档。`test_records`、`review_opinions`、`decisions` 的列值与分支前相同，作品版本计数和布局修订也不变。之后源页还能再确认出下一版，已经分出去的两页 HEAD 仍指向各自的 v1。

HTTP `post_branch` 用 URL 里的页做 `page.branch` 授权，传给领域函数的是正文里的 `source_version_id`。领域函数没有源页 id 参数，只要求该版本属于同一个项目。主设计师的 `page.branch` 在 `roles.py` 第 66–70 行是项目级授权。URL 页和源版本可以不是同一页；这种调用仍然不更新源页那一行。默认模板里没有页级 `page.branch`。测试走的是领域函数，没有走 HTTP。

### 6. 重放前会按当前主体重新授权；版本命令没有撤权重放测试

- 标记：note
- 文件：`execute_authorized_command` 在 `receipts.py` 第 29–38 行。`scope.actor_id` 与当前主体不一致时抛 `AuthRequired`。`authorize` 在 `execute_idempotent` 返回库存正文之前执行。返回值再经过 `project_receipt`，裁剪结果不写回 `idempotency_records`。版本路由把 `CommandScope.actor_id` 和领域函数的 `account_id` / `actor_id` 都设为 `principal.account_id`。五个请求体都是 `extra=forbid`。
- 验收第 6 条：当前权限变化后，重放不得把受限的原响应交出去；客户端传 actor 不能提权。

建项 HTTP 测试用 `{"actor_id": "forged"}` 得到 422。解析发生在 `write_uow` 之前。随后同一 `Idempotency-Key` 的合法正文得到 201，说明这次 422 没有占用该键。测试没有查询伪造请求之后的项目计数，也没有在草稿、确认版本或分支上送 `actor_id`。请求体里没有可供客户端指定的 actor 字段；执行器使用的主体来自会话。

版本测试里没有先成功执行命令、再撤权、再重放，并断言客户端拿不到库存 JSON。身份验收里的撤权重放覆盖的是访问控制命令，不是这五条版本命令。

版本处理函数放进回执的正文没有标题、原件键或摘录。草稿库存里的 `content_revision` 和 `content_hash` 会被投影去掉，这是 B1。授权失败时异常在返回投影正文之前抛出；这个顺序没有被版本测试执行过。

## 验收覆盖

| 条 | 验收要求 | 现有测试锁住的部分 | 还没有测试的部分 |
| --- | --- | --- | --- |
| 1 | 同一草稿修订双写，一方成功，另一方 409；远端与失败方输入可复核 | 两条连接：一方 `DraftConflict`，一方成功，修订变为 6 | HTTP 409；读回远端正文并与失败方输入比较 |
| 2 | 同键并发或重复 20 次只建一个页或版本；同键换正文 409 | 建项和平行页的 20 次、并发、异体冲突；建项 HTTP 的重放和 409 | 确认版本、分支、草稿的同键 |
| 3 | 目标写入后、审计 / Outbox / 回执前注入异常，整笔回滚 | 建项、平行页的 `before_audit` 注入 | 确认版本、分支、草稿；草稿成功路径本身不写审计和 Outbox |
| 4 | 确认后改草稿，历史字节不变；多次 GET 不改 HEAD 或 ProjectRevision | 领域读取下历史字节不变；`read_history` 的 SQL 为 SELECT，指针不变 | HTTP GET 之后查询 `pages.head_version_id` 和 `projects.revision` |
| 5 | 从 V2 分支保留 V3 与后续路线，新 ID 与 origin 映射，旧测试不重新确认 | `test_branch_keeps_later_source_versions_and_skips_tests` | 无。HTTP 分支未测，源页不变已在领域测试里 |
| 6 | 权限变化后重放不泄露受限原响应；客户端 actor 不能提权 | 建项多一个 `actor_id` 字段得到 422，且没有占用幂等键 | 撤权后重放版本命令；草稿、确认、分支上的客户端 actor |

第 5 条有领域测试。第 6 条没有对着验收句子的测试。第 1 条没有测 409 和正文复核。第 4 条没有测 GET 之后的 HEAD 与 ProjectRevision。第 2 条和第 3 条没有测确认版本；第 3 条也没有测分支和草稿。
