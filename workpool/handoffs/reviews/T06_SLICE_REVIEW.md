# T06 切片审查

日期：2026-10-31。只读。只写本文件。不改源代码、测试、迁移，不做 git commit。

这是当前切片的审查。任务卡 `workpool/tasks/T06_DURABLE_TASKS_EVENTS.md` 的状态仍是 implementing。

对照材料：

- `docs/construction/04_API_AND_EVENTS.md` 的「事件信封与传递」和「SSE 重连与权限」。
- `docs/construction/05_WORKFLOW_AND_CONCURRENCY.md` 的「任务认领、租约与 fencing token」和「合成、晚到结果与取消」。
- `src/sinuo/workers/jobs.py`、`src/sinuo/api/events/` 下的 `routes.py`、`replay.py`、`sequence.py`。
- `tests/acceptance/test_durable_tasks_events.py`。
- `SessionAuthorizer.authorize`，以及 `AgentRun` / `RunEvent` 上的约束，用来核对访问纪元和 `UNIQUE(run_id, seq)` 有没有被这组测试打到。

## pytest

```
.\.venv\Scripts\python.exe -m pytest -q tests/acceptance/test_durable_tasks_events.py
```

工作目录：`sinuo-platform`。

退出码 **0**。**31 passed**，3.36s。输出里没有 failed，也没有 skipped。有 1 条 `StarletteDeprecationWarning`，出现在 `test_sse_route_returns_persisted_events_in_order` 加载 Starlette `TestClient` 时。

## 已覆盖的验收点

任务卡 6 条里，2、4、5 有对应用例。1 和 3 只打到消费回执。6 只打到 `jobs` 行取消。原子发号另有用例。下面只写这次读到的断言。

取消、SSE 授权、原子发号这三处按测试正文里的 `assert` 判断：任务行取消、`run.read` 撤权、`allocate_run_event` 的条件更新都核对了提交后的结果。运行级 `execution_status` / `accepting_results` 没有这种断言，记在 blocking。

### 验收 2：并发认领、过期接管、旧租约不能续租或发布

`test_two_connections_only_one_claim_succeeds`

两个线程在屏障后各自用 `write_uow` 调用 `claim_job`。结果长度为 2，恰好一个是 `JobAttempt`，另一个是 `StaleLease`。提交后的行是 `running`，`fencing_token` 为 1，`attempt_no` 为 1，`lease_owner` 等于胜者。胜者带回的 token 和 attempt 也是 1。

`test_expired_running_job_can_be_claimed`

worker-1 认领后，测试把 `lease_until` 写成 1.0。另一份 `input_hash` 的认领抛 `StaleLease`，行仍属于 worker-1，token、attempt 和 `lease_until` 不变。同一哈希由 worker-2 认领后，token 和 attempt 各加 1，`lease_owner` 变为 worker-2，新的 `lease_until` 大于 1.0。

`test_finalize_requires_live_lease_and_writes_one_receipt`

租约写成 1.0 之后，旧主 finalize 抛 `StaleLease`，回执为空，状态仍是 `running`。worker-2 接管之后，旧主续租、旧主用旧 token 或新 token 发布、新主用错哈希发布，都抛 `StaleLease`。此时 `output_ref` 为空，回执为空。新主用当前 token 发布后，状态为 `succeeded`，`output_ref` 为 `derivative-result-a`，两列租约为空，token 与 attempt 保持接管后的值。回执恰好一行：`(event-durable, derivative, project-durable, registered, derivative-result-a)`。之后新旧 token 再发布都抛 `StaleLease`，回执仍是这一行。

`test_renew_only_current_owner_and_token` 另锁定：错主、错 token、错哈希都不改变 `lease_until`；当前主用原 token 续租后 token 与 attempt 不变，`lease_until` 变大；过期后续租抛 `StaleLease`，行停在 `lease_until == 1.0`。

`test_claim_fails_unless_one_row_changes` 锁定错哈希、未来的 `available_at`、未过期的运行中租约、缺失 job 都不改行。`test_claim_queued_job_bumps_fence_and_attempt` 锁定首次认领把 token 和 attempt 从 0 加到 1，`lease_until` 落在 1.6e9 与 4e9 之间。`test_loaded_job_keeps_claim_through_commit` 先把已加载行的 `last_error` 写成 `stale local edit` 再认领，提交后状态为 `running`，token 与 attempt 为 1，`last_error` 为空。

`claim_job` 的条件是 `input_hash` 相同、`available_at` 不晚于数据库时钟，并且状态为 `queued`，或状态为 `running` 且 `lease_until` 早于该时钟。成功时 `fencing_token` 与 `attempt_no` 各加 1。时钟语句是 `julianday('now')` 换算成 Unix 秒。续租和发布走 `_live`：`running`、owner、token、`input_hash` 相同，且 `lease_until` 不早于同一时钟。`rowcount` 不是 1 就抛 `StaleLease`。

`test_terminal_job_trigger_rejects_return_to_running` 锁定 `succeeded` 之后再 claim 抛 `StaleLease`。直接把该行改回 `running` 抛 `IntegrityError`，文本含 `jobs_terminal`。`sqlite_master` 里 `jobs_before_update` 的 SQL 前后相同。行保持 `succeeded`，租约列为空，`output_ref` 仍在。

### 任务行取消

这些用例核对的是 `jobs` 行、回执行和 SQLite 变更计数。

| 用例 | 实际断言 |
| --- | --- |
| `test_cancel_queued_or_running_clears_lease` | 排队任务变为 `cancelled`，token 与 attempt 仍为 0，租约列为空。运行中任务取消后，token、attempt、`input_hash` 与认领时相同，`lease_owner` 和 `lease_until` 为空，回执为 0。`already_cancelled` 为假。 |
| `test_repeat_cancel_returns_without_updating_terminal_fields` | 第二次 `cancel_job` 前后 `total_changes()` 相等。跟踪到的语句里没有 `UPDATE`。`already_cancelled` 为真。状态、token、attempt 与第一次取消后的行相同。 |
| `test_cancel_rejects_unless_one_row_changes` | 已 `succeeded` 的任务、测试插入的 `failed`、`superseded`、缺失 id 都抛 `StaleLease`。前三行的 status、token、attempt 和租约列与调用前相同。 |
| `test_old_fence_cannot_finalize_or_renew_after_cancel` | 取消后原 token 的 renew、finalize 抛 `StaleLease`，再次 claim 也抛。`output_ref` 为空，回执为 0，token 与 attempt 不变。 |
| `test_two_connections_only_one_of_finalize_or_cancel_succeeds` | 屏障后一边 `finalize_job`、一边 `cancel_job`。恰好一方返回，另一方 `StaleLease`。取消胜出时状态为 `cancelled`，`output_ref` 为空，回执为 0。发布胜出时状态为 `succeeded`，`output_ref` 为 `derivative-result-a`，回执恰好一行。两种结果的 token 与 attempt 都等于认领值，租约列都为空。 |

`cancel_job` 见到已经是 `cancelled` 的行就返回。其他行只做一条条件更新：`queued` 或 `running` 改为 `cancelled`，并清空 `lease_owner`、`lease_until`。这条更新不写 `fencing_token`、`attempt_no`、`input_hash`。

### 回执：验收 1 和 3 里测试实际打到的部分

`test_same_consumer_effect_is_reused`：先插入同一 `effect_ref` 的回执再 finalize。job 变为 `succeeded`，回执仍是一行，`consumer_event_id` 为 `event-durable`。

`test_different_consumer_effect_rolls_back_finalize`：已有回执的 `effect_ref` 是 `derivative-result-b`，本次 finalize 要写 `derivative-result-a`。抛 `ConsumerEffectConflict`。回滚后的 job 仍是 `running`，`output_ref` 为空，租约与 token 仍是认领值，回执仍指向 `derivative-result-b`。

`test_finalize_without_source_event_stores_output_ref`：没有 `source_event_id` 时发布成功，`consumer_event_id` 为 `None`，回执表为空，`output_ref` 已写入。

`finalize_job` 在同一 session 里先条件更新 job，再按 `(event_id, consumer_name)` 读 `ConsumerReceipt`。project、effect_kind、effect_ref 都相同则复用；有一项不同就抛 `ConsumerEffectConflict`。异常穿出 `write_uow` 时，同一次 job 更新一起回滚。

### 验收 4：按序补读；非法或跨 run 游标不带出其他正文

`test_replay_returns_later_events_in_seq_order` 对合成表、`OutboxEvent`、`RunEvent` 各跑一遍。插入顺序是 seq 4、2、1。`replay_events` 返回的 seq 是 1、2、4，event_id 是 `sse-1`、`sse-2`、`sse-4`。`stream.heartbeat` 的 `progress` 为假。`is_task_progress` 对心跳和 `stream.reset_required` 为假，对 `run.received` 为真。进度事件只有 `sse-1` 和 `sse-4`。再读一次，结果相同，行数不变。游标 `sse-1` 之后是 `sse-2`、`sse-4`。游标 `sse-4` 得到空元组，`reset_required` 为假。游标 `missing-sse-cursor` 得到一条 `stream.reset_required`，载荷 `{}`，`progress` 为假，文本里没有三份正文。空串和前导空格抛 `EventCursorRejected`，文本里同样没有正文。`kind == "run"` 时，各 run 的 `execution_status` 仍是 `queued`，`next_event_seq` 仍是 0。这次回放插入走的是 `append_events`，没有调用发号函数。

`test_foreign_last_event_id_does_not_return_other_body`：无游标时只返回本 run 的 `sse-local`，载荷是本地正文，结果里没有另一 run 和另一项目的正文。游标 `sse-foreign` 或 `sse-other-run` 抛 `EventCursorRejected`。没有成员身份的另一个账号即使用 `sse-foreign`，得到的也是 `NotFoundOrHidden`。三份错误文本都不含这三份正文。库里的本地正文和外部正文都还在。

`test_sse_route_returns_persisted_events_in_order`：`GET /api/v1/agent-runs/run-sse/events` 为 200，`content-type` 以 `text/event-stream` 开头，`cache-control` 为 `no-store`。帧 seq 为 1、2、3，类型为 `run.received`、`task.started`、`stream.heartbeat`，progress 为真、真、假。`Last-Event-ID: order-received` 只返回后两帧。缺失游标返回一条 `stream.reset_required`，载荷 `{}`，响应文本不含三份正文。游标停在最后一帧时状态 200，正文为空。这里的心跳是 `append_events` 写入的 seq 3，断言 `next_event_seq` 仍为 2。

`replay_events` 在留下任何事件正文之前调用 `SessionAuthorizer.authorize(Action.RUN_READ, …)`。找不到的 `event_id` 返回 `stream.reset_required`。查到的行若 project 或 run 与请求不一致，抛 `EventCursorRejected`。路由把该异常写成 422，`code` 为 `INVALID_SCHEMA`，`message` 为 `Last-Event-ID is outside this run.`，`details.category` 为 `last_event_id`。

### 验收 5：SSE 授权，以及撤权后的后续输出

`test_sse_route_hides_event_text_without_current_read` 的断言是状态码、错误体和正文缺失：

- 无 cookie：401，`AUTH_REQUIRED`。`content-type` 不含 `text/event-stream`，`cache-control` 为 `no-store`，响应文本不含本地、另一 run、另一项目三份正文。
- 有会话、没有 `run.read`：403，`ACTION_FORBIDDEN`，`details` 为 `{"action": "run.read"}`，同样不含正文。
- 加上 grant 之后，另一 run、另一项目、空 `Last-Event-ID`、前导空格都是 422。`retryable` 为假，`request_id` 非空，不含正文。
- 不存在的 run：404，`NOT_FOUND_OR_HIDDEN`。
- grant 改为 `revoked` 后再 GET：403。成员改为 `revoked` 后再 GET：404。两份响应都不含正文。库中三份正文仍在。

`test_replay_checks_current_permission_before_each_read` 对三种事件源各跑一遍。无 grant 时得到 `ActionForbidden`，错误文本不含本地正文。写上 grant 之后，epoch 为 2 的 `Principal`（账号 `auth_epoch` 为 1）得到 `AuthRequired`。当前主体可以读到本地正文。grant 撤销后，带着已有 `sse-local` 再读得到 `ActionForbidden`。成员撤销后，同一游标和缺失游标都得到 `NotFoundOrHidden`。错误文本不含正文，库里的正文还在。

`test_sse_route_checks_permission_before_the_next_frame`

三条事件已经落库。ASGI 写出第一块正文时把 grant 标成 `revoked`。响应状态 200。第一帧是 `live-1`，载荷含本地正文，这一帧不含后两份正文。整段响应只有两条 `data:`。后面的帧是 `error`，`code` 为 `ACTION_FORBIDDEN`，不含后两份正文。整段文本也不含后两份正文。同一 cookie 再 GET 得到 403，响应不含三份正文。`execution_status` 仍为 `queued`，`next_event_seq` 为 3。

`_frames` 先交出打开连接时已经授权的第一帧。之后每一帧重新开 session，再走 `resolve_principal` 和 `replay_events`。授权失败时写出 `event: error` 并停止。`routes.py` 没有 Origin 判断，也没有设置 Cookie 的 SameSite。

`test_create_app_mounts_agent_run_events` 断言 `create_app` 的 OpenAPI 含有 `/api/v1/agent-runs/{run_id}/events`。未登录 GET 缺失 run 得到 401，响应文本不含 `payload`。带授权的 200 响应用的是测试自行挂上该 router 的 FastAPI。

### 原子发号

`allocate_run_event` 在调用方事务里做一条条件更新：`agent_runs.next_event_seq` 等于传入的 `expected_seq` 时加 1，`rowcount` 必须为 1，再插入 `seq = expected_seq + 1`。函数自身不提交。事件类型若是 `stream.heartbeat`，在写之前抛 `HeartbeatNotSequenced`。

`test_two_connections_do_not_share_or_skip_run_event_seq`：

- 两个连接同时用 `expected_seq=0`。恰好一个成功，序号为 1；另一个 `EventSequenceConflict`，错误文本不含正文。seq 列表是 `[1]`，败者的 `event_id` 不在表里。
- 再用 `expected_seq=1` 竞态一次。胜者序号为 2，计数连续到 2，败者 id 不在表里。
- 心跳抛 `HeartbeatNotSequenced`，不插入 `seq-heartbeat`，`next_event_seq` 仍为 2，类型里没有心跳。
- 分配得到 3 之后，同一 `write_uow` 抛异常。回滚后计数仍是 2，`seq-rollback` 不在表里。下一次 `expected_seq=2` 得到 3，seq 为 `[1, 2, 3]`。
- 项目 `revision` 和 `access_epoch` 仍为 1。run 的 `execution_status` 仍为 `queued`，`accepting_results` 仍为 1，`closed_at` 为空，`next_event_seq` 为 3。

`test_each_run_sequence_starts_at_one_and_ignores_heartbeats`：两个 run 各自从 0 起算。A 得到 1 和 2，B 得到 1。A 再用 `expected_seq=0` 抛 `EventSequenceConflict`，`seq-a-stale` 不在表里。不存在的 run 同样冲突，已有序号不变。已加载 run 的 `budget_json` 先改成 `{"kept":1}` 再分配，计数变为 3，这份 `budget_json` 还在。`execution_status` 仍为 `queued`，`accepting_results` 仍为 1。

`RunEvent` 模型有 `UniqueConstraint("run_id", "seq", name="uq_run_events_seq")`。上面的用例锁定条件更新的胜负和回滚，没有另插相同 seq 去触发这个约束。

## blocking

**1**

### 1. 运行取消没有被测试锁定

05「合成、晚到结果与取消」写的是：取消运行先原子改 `execution_status` 和 `accepting_results`，再尽力取消网络和子进程；已经发出的模型请求可以留下用量，不发布候选；已经提交的业务决定保持有效；必需步骤超时不得无限重试，并进入 `partial` 或 `failed`。

这个测试文件没有一处断言去读取消之后的 `AgentRun.execution_status` 或 `accepting_results`。发号用例断言的是分配之后这两列仍为 `queued` 和 `1`。`cancel_job` 的更新列只有 `jobs.status`、`lease_owner`、`lease_until`。`finalize_job` 的 `_live` 核对的是 job 状态、owner、token、`input_hash` 和 `lease_until`，不读 `AgentRun`。测试也没有把 `accepting_results` 写成 0 再调用发布。

验收 6 里被这 31 个测试锁定的，是 job 从 `queued` 或 `running` 变为 `cancelled`。运行级取消、晚到结果不发布候选、超时后的收束，没有对应断言。

## 不阻断的缺口

1. 验收 1 没有“业务事务提交后、通知前崩溃、重启再投递”的过程。`jobs.py` 的模块说明写着：调用放在调用方的 `write_uow` 里，本模块不提交；Outbox 投递助手是 `leases.py`；本模块不打开 SSE。这个测试文件没有导入投递函数。本次没有打开 `leases.py`。现有回执用例锁定的是同一次 finalize：相同 effect 仍是一行回执，不同 effect 让整个 `write_uow` 回滚。

2. 验收 3 没有文件。没有 attempt 目录，没有不可变派生文件，也没有旧路径覆盖新路径的断言。测试里的 `output_ref` 是标签 `derivative-result-a` 和 `derivative-result-b`。不同 `effect_ref` 不能替换已有回执，这一点已在回执用例里。

3. 验收 6 的预算耗尽和不可重试结束没有用例。`jobs.py` 不写 `retry_wait`，不读预算，也不给 `last_error` 写入失败类别。`attempt_no` 只在 claim 成功时加 1。测试没有断言重试次数、截止时间或退避后的 `available_at`。

4. 04 要求在建立、重连、批量回放和发出新数据前检查当前访问纪元。`SessionAuthorizer.authorize` 比对账号 `auth_epoch`、成员状态和有效 grant，函数里没有 `access_epoch`。测试把主体 epoch 设为 2 时得到 `AuthRequired`；撤权改的是 `ObjectGrant.status` 和 `Membership.status`。没有任何用例先增加 `Project.access_epoch` 再读 SSE。

5. `routes.py` 不检查 `Origin`，也不写 SameSite。测试用 Cookie 访问本机应用，没有带跨站 Origin 的请求。

6. 路由在没有下一行时结束生成器，中间不另发保活心跳。心跳断言针对的是已经写入的 `stream.heartbeat`：`progress` 为假，发号函数拒绝再给它编号。`stream.reset_required` 的载荷是空对象。测试没有在这帧之后请求运行快照。

7. HTTP 顺序用例用 `append_events` 把心跳写成 seq 3，同时断言 `next_event_seq` 仍为 2。发号竞态不走这条插入。本文件没有用重复 `(run_id, seq)` 触发 `uq_run_events_seq`。

8. 05 要求记下每次 `provider_request_id` / attempt 的用量。`jobs.py` 和这个测试文件都没有该字段。

9. 任务卡后半的建议频率（off/manual 零自动任务、event 去重、digest 时间窗、策略 CAS、重启恢复）不在 `test_durable_tasks_events.py`。这次没有读偏好 API。

租约、游标隔离、撤权后的新读取，以及 run 内条件发号，以这次 31 个测试为准。运行级取消，以及投递重启、文件派生、预算和重试上限，还没有被这组测试锁定。T06 保持 implementing。
