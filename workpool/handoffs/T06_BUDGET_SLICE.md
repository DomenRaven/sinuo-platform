# T06 预算预留切片

日期：2026-10-31。T06 状态保持 implementing，本切片不把任务标成 done。

## 对照

施工文档 `docs/construction/05_WORKFLOW_AND_CONCURRENCY.md` 的「预算与 DeepSeek 适配」：预算由运行侧原子预留，调用结束后按实际用量结算；未知用量先保留估算，不记成零。401、402、无权限、无效 Schema 不盲重试；429 和暂时不可用只做有界退避。

同一文档里 `RunTask.status` 才有 `retry_wait`。`JOB_STATUSES` 没有这个状态，本切片不加。Job 的等待是 `queued` 加上更晚的 `available_at`。`claim_job` 已经在认领时增加 `attempt_no` 和 `fencing_token`，这里不再加。

`migrations/versions/0004_repair_invariants.py` 的 `jobs_before_update`：失败、成功、取消、取代之后，不能再改状态、fencing token、租约或 attempt。所以第二次结束已经失败的任务时，不发出 UPDATE。

本仓库没有单独的文献库。上面两条施工合同就是本切片的依据，没有另引外部论文。

## 选型

预算放在已经存在的 `AgentRun.budget_json` 文本里，不建新表，不做迁移。预留和结算各用一次条件 UPDATE，WHERE 对上当前原文，沿用 `jobs.py` 的条件更新和调用方的 `write_uow`。失败终态加在 `jobs.py`，不改 claim、renew、finalize、cancel 的合同。

## 行为

文档只接受三个非负整数：`max_model_calls`、`spent_model_calls`、`reserved_model_calls`，并且 spent + reserved 不能超过 max。缺键或其他形状直接拒绝，存储文本不改写。已有的 `{}` 和 `{"kept":1}` 属于这种形状。

`reserve_model_call(session, run_id, estimate)`：`estimate` 是大于 0 的整数。只更新 `execution_status` 为 queued 或 running、且 `accepting_results` 为 1 的运行。成功时 `reserved_model_calls` 增加 `estimate`。余量不够则什么都不写，返回 `reserved=False`、`reason=budget_exhausted`。两个连接抢最后一个名额时，只有一次写入；另一个读到已提交的文档后不再预留。

`settle_model_call(session, run_id, estimate, actual)`：也是一次对上当前文本的条件 UPDATE。`actual` 为大于等于 0 的整数时，reserved 减少 `estimate`，spent 增加 `actual`。`actual` 为 `None` 时，两个计数都不变，返回 pending，不把未知调用记成零花费。对不上当前文本，或结算后会超出文档约束，则抛 `BudgetConflict`，不部分写入。

`finish_job_attempt` 结束一次 running 尝试：

- `budget_exhausted`、`unauthorized`、`payment_required`、`invalid_schema`：一次条件更新，从 running 到 failed，清空 `lease_owner` 和 `lease_until`，`last_error` 写成该类别。fencing token、attempt、input hash 保持不变。再次调用看到 failed，不发 UPDATE。
- `rate_limited`、`unavailable`，且 `attempt_no` 仍小于调用方给出的 `max_attempts`，调用方给出的 `deadline` 仍晚于数据库时钟：一次条件更新，从 running 回到 queued，清空租约，`available_at` 写成调用方给出的更晚时间，`last_error` 写成该类别。不增加 fencing token 或 attempt。
- 可重试，但次数已经达到 `max_attempts`：failed，`last_error` 为 `attempts_exhausted`。截止时间已经不在未来：failed，`last_error` 为 `deadline`。次数先判断。再次调用不发 UPDATE。
- 已经 failed 的任务不能再变回 queued。

本切片不取消运行，不发布候选，不调用模型或网络。

## 证据

先创建 `--basetemp` 目录，再从仓库根执行：

```text
.venv/Scripts/python.exe -m pytest -q tests/acceptance/test_durable_tasks_events.py -o cache_dir=_施工文件/2026-10-31_grok-dev-b/99_临时文件/pytest-cache --basetemp=_施工文件/2026-10-31_grok-dev-b/99_临时文件/pytest
```

退出码 0。57 passed。

## 还没做

建议频率、事件去重、digest 时间窗、真实模型调用。T06 不标 done。
