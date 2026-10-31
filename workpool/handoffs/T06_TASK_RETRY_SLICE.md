# T06 任务重试等待

日期：2026-10-31。T06 状态保持 implementing。本切片不把任务标成 done。

## 对照

冻结合同 `workpool/handoffs/T06_TASK_RETRY_CONTRACT.md`，版本 `T06-TASK-RETRY-1`。

施工文档 `docs/construction/05_WORKFLOW_AND_CONCURRENCY.md` 的「三套状态机」：`RunTask.status` 可以是 `retry_wait`，从 `retry_wait` 回到 `queued` 要符合次数、截止和预算。同一文档「预算与 DeepSeek 适配」：401、402、无权限、无效 Schema 不盲重试；429 和暂时不可用只做有界退避。

`migrations/versions/0003_domain_identity.py` 的 `run_tasks_before_update`：`succeeded`、`failed`、`cancelled` 之后，不能再改状态、fencing token 或租约。已经 `failed` 的行若再 UPDATE 成别的状态，终态触发器会拒绝，所以第二次结束不发 UPDATE。

`lease_rule(TASK_STATUSES)` 写在 `ck_run_tasks_lease` 上。只有 `running` 可以带租约，并且 `lease_owner` 非空、`lease_until` 有限、`fencing_token > 0`。`retry_wait`、`failed`、`queued` 都要求 `lease_owner` 和 `lease_until` 同时为空。进入等待或失败时清空这两列，行仍然满足约束。fencing token 和 attempt 保持原值。

`JOB_STATUSES` 没有 `retry_wait`。本切片不加。

本仓库没有单独的文献库。上面这份合同、触发器、租约约束和施工节就是本切片的依据。

## 选型

新模块 `src/sinuo/workers/run_tasks.py`。条件更新的写法沿用 `finish_job_attempt`：调用方的 `write_uow` 里只发一条 UPDATE，函数自己不 `commit`。可重试的任务结果是 `retry_wait`，因为任务状态表里有这个值，Job 的等待仍是 `queued` 加更晚的 `available_at`。

放行是另一条 UPDATE，条件是 `status = 'retry_wait'` 且 `available_at` 不晚于同一次读到的数据库时钟。未到时间就直接返回，避免发出会被终态或零行匹配以外的写入。

放弃给 `jobs` 增加这个状态，放弃新迁移，也放弃先改对象再无条件 flush。次数和 fencing token 留给以后的认领，这里不加。

## 行为

`finish_task_attempt` 只匹配 `status = running` 的一行。

- `budget_exhausted`、`unauthorized`、`payment_required`、`invalid_schema`：进入 `failed`，清空租约，`last_error` 写成该类别。这条优先于次数和截止。`available_at`、fencing token、attempt、input hash 保持不变。再次调用读到 `failed` 就返回，不发 UPDATE。
- `rate_limited`、`unavailable`，并且 `attempt_no` 小于调用方给出的 `max_attempts`，调用方给出的 `deadline` 仍晚于数据库时钟：进入 `retry_wait`，清空租约，`available_at` 写成调用方给出的更晚时间，`last_error` 写成该类别。fencing token 和 attempt 不加。
- 可重试但次数已经达到 `max_attempts`：`failed`，`last_error` 为 `attempts_exhausted`。次数先于截止判断。截止已经不晚于数据库时钟：`failed`，`last_error` 为 `deadline`。再次调用不发 UPDATE。
- 仍可等待，但给出的 `available_at` 不晚于数据库时钟：抛出 `ValueError`，不发 UPDATE，行保持 `running`，租约还在。

`release_retry_wait` 在 `available_at` 仍晚于数据库时钟时不发 UPDATE，状态保持 `retry_wait`。到点后一条条件更新把状态改成 `queued`。租约保持为空，fencing token、attempt、`available_at` 和 `last_error` 不动。两个线程在屏障之后各自用 `write_uow` 放行同一条到点任务，只有一次把行改成 `queued`。

进入 `retry_wait` 之前已经把预算耗尽、次数到顶和截止已过写成 `failed`。放行这一条不再读运行预算。不调用模型。

## 证据

先创建目录 `_施工文件/2026-10-31_grok-dev-b7/99_临时文件`，再从仓库根执行：

```text
.venv/Scripts/python.exe -m pytest -q tests/acceptance/test_run_task_retry.py -o cache_dir=_施工文件/2026-10-31_grok-dev-b7/99_临时文件/pytest-cache --basetemp=_施工文件/2026-10-31_grok-dev-b7/99_临时文件/pytest
```

退出码 0。12 passed。

## 还没做

`run_tasks` 的认领、依赖失败后的取消，以及放行时再读运行预算。真实模型不跑。GLB 仍不做。T06 不标 done。
