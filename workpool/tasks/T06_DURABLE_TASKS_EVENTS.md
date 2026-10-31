# T06 持久任务、Outbox、租约与 SSE

- 状态：implementing
- 依赖：T04 done；与 T05 共享信封 T05-T06-ENVELOPE-1
- Owner：Grok 开发B
- 基线提交或快照：无 Git commit。全量 pytest 142 passed。迁移 head `0004_repair_invariants`
- 写入范围：`workers/` 中调度/投递模块、运行事件仓储与 SSE API、`tests/acceptance/test_durable_tasks_events.py`；共享 schema、迁移和入口由集成 owner 合入
- 对应门禁：G2；消费者：T08、T11

## 目标

业务提交后进程崩溃、任务重复投递、租约过期或客户端断线，后台工作和进度仍可恢复。只有当前有效租约可以发布结果；SSE 能重连和授权回放，不能跨项目或主体广播敏感内容。

## 输入与交付

使用数据库 Job/Outbox 作为持久事实源。claim/renew/finalize 各在短事务中以条件更新验证 state、owner、token、期限和输入哈希；执行模型或转换留在事务外。消费者去重键为 event_id+consumer；本地效果与消费回执同事务，外部副作用采用自身幂等策略。

实现有界退避、attempt/deadline 上限、取消、失败分类和人工可诊断的任务状态。事件持久化后再推送；进程内队列仅负责唤醒。run 内 seq 单调且唯一，SSE 校验 Last-Event-ID 归属，游标失效提供重新读取快照的安全出口。每次订阅、回放和新数据发送检查当前权限；撤权停止后续受控读取。

## 验收

创建并执行 `python -m pytest -q tests/acceptance/test_durable_tasks_events.py`，至少覆盖：

1. 业务事务提交后通知前崩溃，重启仍能投递；至少一次投递不产生重复业务效果。
2. 两 Worker 同时 claim，只有一者获得有效 token；过期后新 Worker 接管，旧者 finalize/renew 被拒绝。
3. 文件转换完成但登记回执前崩溃，可重试登记；旧 attempt 不能覆盖新派生结果。
4. 事件重复与断线重连能去重补读；非法/跨 run 游标不泄露其他事件。
5. 运行中撤权，现存连接的后续受控输出和新连接均按新权限裁剪/拒绝。
6. 持久任务取消、预算耗尽和不可重试错误结束，不形成无限 retry_wait 循环。

## 交接与证据

给 T08 可调用的持久 Job 接口、独立结果槽、事件写入/读取合同和故障夹具；给 T05 真实转换 Job 接线样例。日志区分“接收、认领、外部执行、有效发布、重投/迟到丢弃”，不能宣称跨外部系统 exactly-once。

## 建议频率施工归属（2026-10-30）

本卡额外拥有本人建议偏好 API/仓储、事件/定时触发记录、冷却与去重，合同见 04 新端点及 05 最后一节。验收 R02 的 off/manual 零自动任务、event 去重、digest 时间窗、策略 CAS、重启恢复。与 T08 冻结受限 actor/scope/策略修订的 Job 信封；后台不能使用管理员凭据替代发起者。调度 Job 不等于已获模型外发许可。

## 预算切片证据（2026-10-31）

状态仍是 implementing。本轮只做运行预算的原子预留/结算，以及一次 running 尝试的失败或回到 queued。建议频率、事件去重、digest 时间窗和真实模型调用都没有做。

```text
.venv/Scripts/python.exe -m pytest -q tests/acceptance/test_durable_tasks_events.py -o cache_dir=_施工文件/2026-10-31_grok-dev-b/99_临时文件/pytest-cache --basetemp=_施工文件/2026-10-31_grok-dev-b/99_临时文件/pytest
```

退出码 0。57 passed。交接见 `workpool/handoffs/T06_BUDGET_SLICE.md`。

## 建议频率切片证据（2026-10-31）

状态仍是 implementing。本轮做了建议偏好的持久化和触发去重：没有偏好行按 manual，保存先写默认行再条件更新，off/manual 不建 Job，event 去重与冷却抑制，digest 只处理当前窗口，提交后新 session 仍看到那一条 Job。HTTP 路由、取消已有自动运行、真实模型都没有做。T06 不标 done。

```text
.venv/Scripts/python.exe -m pytest -q tests/acceptance/test_suggestion_preferences.py tests/acceptance/test_domain_schema.py tests/acceptance/test_assets_derivatives.py::test_head_includes_stored_object_tables -o cache_dir=_施工文件/2026-10-31_grok-dev-b2/99_临时文件/pytest-cache --basetemp=_施工文件/2026-10-31_grok-dev-b2/99_临时文件/pytest
```

退出码 0。34 passed。交接见 `workpool/handoffs/T06_SUGGESTION_SLICE.md`。

## 偏好变更取消自动运行证据（2026-10-31）

状态仍是 implementing。本轮在偏好修订增加的同一事务里，取消该项目、该 actor 仍在排队或运行中的 policy 运行。manual 运行、其他 actor 和 `Project.revision` 不动。不创建自动运行，不调用模型，不改 HTTP 路由。T06 不标 done。

```text
.venv/Scripts/python.exe -m pytest -q tests/acceptance/test_policy_cancel.py tests/acceptance/test_suggestion_preferences.py tests/acceptance/test_domain_schema.py tests/acceptance/test_assets_derivatives.py::test_head_includes_stored_object_tables -o cache_dir=_施工文件/2026-10-31_grok-dev-b3/99_临时文件/pytest-cache --basetemp=_施工文件/2026-10-31_grok-dev-b3/99_临时文件/pytest
```

退出码 0。37 passed。`alembic heads` 只有 `0007_run_origin (head)`。交接见 `workpool/handoffs/T06_POLICY_CANCEL_SLICE.md`。

## 提交后重启投递证据（2026-10-31）

状态仍是 implementing。本轮为已提交、尚无投递行的 outbox 事件补一条 `pending` 的 `outbox_deliveries`。`recover_delivery` 不提交。同一 `(event_id, consumer_name)` 再调用一次，或两个连接同时恢复，仍然只有一行。完成认领后再恢复，不把 `delivered` 改回 `pending`，也不另插一行。`claim_delivery` 与 `complete_delivery` 的条件未改。这不是跨系统恰好一次。T06 不标 done。

```text
.venv/Scripts/python.exe -m pytest -q tests/acceptance/test_outbox_recovery.py -o cache_dir=_施工文件/2026-10-31_grok-dev-b4/99_临时文件/pytest-cache --basetemp=_施工文件/2026-10-31_grok-dev-b4/99_临时文件/pytest
```

退出码 0。2 passed。交接见 `workpool/handoffs/T06_DELIVERY_RECOVER_SLICE.md`。

## 派生尝试文件证据（2026-10-31）

状态仍是 implementing。本轮把一次派生尝试的字节写在数据库事务外，路径只允许 `jobs/<job_id>/<fencing_token>/result`。会逃出根目录的路径直接拒绝，并且不写文件。写文件不打开数据库会话。登记仍调用现有的 `finalize_job`，`output_ref` 是这条相对路径；`finalize_job` 的 WHERE 没有改。登记前崩溃时 `output_ref` 仍为空，文件留在原地，同一个 token 可以随后 finalize 一次。租约过期后新 token 发布另一份文件，旧 token 的 finalize 被拒绝，`output_ref` 保持新路径。旧文件可以留下，但不是已发布指针。不调用模型，不新迁移。T06 不标 done。

```text
.venv/Scripts/python.exe -m pytest -q tests/acceptance/test_derivative_attempt.py -o cache_dir=_施工文件/2026-10-31_grok-dev-b5/99_临时文件/pytest-cache --basetemp=_施工文件/2026-10-31_grok-dev-b5/99_临时文件/pytest
```

退出码 0。11 passed。交接见 `workpool/handoffs/T06_DERIVATIVE_ATTEMPT_SLICE.md`。

## 派生预览接线证据（2026-10-31）

状态仍是 implementing。本轮把一条 `job_kind=derivative` 的任务在认领提交之后交给现有预览渲染。渲染发生在事务外，不打开数据库会话。小 PNG 原件写成 `jobs/<job_id>/<fencing_token>/result`，随后在另一次 `write_uow` 里用现有 `finalize_job` 发布这条相对路径，文件是 PNG。旧 token 的 finalize 被拒绝，`output_ref` 仍是新路径。没有 Blender 的 GLB 不发布成功 `output_ref`，也不写占位图；`finish_job_attempt` 用 `unavailable`，次数上限设成当前 attempt，任务失败而不是回到 queued。像素超限和 SVG 不写文件，用现有的 `invalid_schema` 结束。不调用模型，不新迁移，不改 `finalize_job` 的 WHERE，不改预览 HTTP。T06 不标 done。

```text
.venv/Scripts/python.exe -m pytest -q tests/acceptance/test_derivative_preview.py -o cache_dir=_施工文件/2026-10-31_grok-dev-b6/99_临时文件/pytest-cache --basetemp=_施工文件/2026-10-31_grok-dev-b6/99_临时文件/pytest
```

退出码 0。5 passed。交接见 `workpool/handoffs/T06_DERIVATIVE_PREVIEW_SLICE.md`。

## 任务重试等待证据（2026-10-31）

状态仍是 implementing。本轮只做 `run_tasks` 的 `retry_wait`。可重试、次数未到、截止仍晚于数据库时钟时，一条条件更新从 `running` 进入 `retry_wait`，清空租约，`available_at` 写成调用方给出的更晚时间。不可重试、次数到顶或截止已过则进入 `failed`。已经 `failed` 的第二次调用不发 UPDATE。未到 `available_at` 的放行不改行。两个连接同时放行同一条到点任务，只有一次变成 `queued`。`jobs` 没有 `retry_wait`。不调用模型，不改迁移。T06 不标 done。

```text
.venv/Scripts/python.exe -m pytest -q tests/acceptance/test_run_task_retry.py -o cache_dir=_施工文件/2026-10-31_grok-dev-b7/99_临时文件/pytest-cache --basetemp=_施工文件/2026-10-31_grok-dev-b7/99_临时文件/pytest
```

退出码 0。12 passed。交接见 `workpool/handoffs/T06_TASK_RETRY_SLICE.md`。

