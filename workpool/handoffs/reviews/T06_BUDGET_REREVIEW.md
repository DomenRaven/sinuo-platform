# T06 预算切片再审

日期：2026-10-31。只读。只写本文件。没有改源代码、测试、迁移、SESSION、BOARD 或任务卡，没有 git commit。

T06 仍是 implementing。本文件不是完成声明。

对照范围：`workpool/handoffs/T06_BUDGET_SLICE.md`，`src/sinuo/workers/budget.py`，`src/sinuo/workers/jobs.py` 的 `finish_job_attempt` 及其类别常量，`docs/construction/05_WORKFLOW_AND_CONCURRENCY.md` 的「预算与 DeepSeek 适配」以及 `RunTask.status` 含 `retry_wait` 的那一句，还有 `tests/acceptance/test_durable_tasks_events.py` 里名称包含 reserve、settle、budget、finish_job、retryable、non_retryable 的新测试，以及同段里紧挨着的已知零结算、次数封顶、已失败静默和非法参数用例。交接写明本仓库没有单独文献库，依据就是上述施工合同。旧的 SSE 与取消用例正文没有重读；下面只确认它们的函数仍在文件里。

## 命令与退出码

先创建 `workpool/handoffs/reviews`，以及 `_施工文件/2026-10-31_grok-review-t06/99_临时文件/pytest` 和同级的 `pytest-cache`。再从仓库根 `sinuo-platform` 执行：

```text
.venv/Scripts/python.exe -m pytest -q tests/acceptance/test_durable_tasks_events.py -o cache_dir=_施工文件/2026-10-31_grok-review-t06/99_临时文件/pytest-cache --basetemp=_施工文件/2026-10-31_grok-review-t06/99_临时文件/pytest
```

退出码 **0**。输出是 **57 passed**，1 warning，5.02s。没有 failed，没有 skipped。那条 warning 是 `StarletteDeprecationWarning`，挂在仍存在的 `test_sse_route_returns_persisted_events_in_order` 加载 Starlette `TestClient` 时。

`-q` 没有逐条打印用例名。57 是整份 `test_durable_tasks_events.py` 的通过数，文件里仍然定义着这些旧用例：`test_sse_route_returns_persisted_events_in_order`、`test_sse_route_hides_event_text_without_current_read`、`test_sse_route_checks_permission_before_the_next_frame`、`test_cancel_queued_or_running_clears_lease`、`test_repeat_cancel_returns_without_updating_terminal_fields`、`test_cancel_rejects_unless_one_row_changes`、`test_old_fence_cannot_finalize_or_renew_after_cancel`、`test_two_connections_only_one_of_finalize_or_cancel_succeeds`、`test_cancel_run_closes_status_and_rejects_late_publish`、`test_repeat_cancel_run_does_not_rewrite_execution_status`、`test_publish_run_seals_open_run_and_blocks_cancel`、`test_two_connections_only_one_of_publish_or_cancel_succeeds`。

## 预留、结算、结束：测试锁住的行为

施工文档「预算与 DeepSeek 适配」写的是：预算由运行侧原子预留，调用结束后按实际用量结算；未知用量先保留估算，不记成零；429 与暂时不可用有界退避；401、402、无权限、无效 Schema 不盲重试。同一文档写 `RunTask.status` 含 `retry_wait`。`jobs.py` 模块说明写明 Job 没有 `retry_wait`。`finish_job_attempt` 用的常量是 `_NON_RETRYABLE_JOB_ERRORS`（`budget_exhausted`、`unauthorized`、`payment_required`、`invalid_schema`）和 `_RETRYABLE_JOB_ERRORS`（`rate_limited`、`unavailable`）。

`reserve_model_call` 成功时只经过 `_swap_budget` 的一次 `UPDATE`。WHERE 含 `AgentRun.id == run_id` 和 `AgentRun.budget_json == current_text`，预留还加上 `execution_status` 属于 queued/running 且 `accepting_results == 1`。`rowcount != 1` 时不返回已预留。`settle_model_call` 同样对上当前原文；`actual is None` 时新文本就是当前文本，两个计数都不改，`pending=True`。`finish_job_attempt` 读到 `status == "failed"` 时在构造 UPDATE 之前返回。

### 预留

`test_reserve_and_settle_move_model_call_counts` 从 `model_budget(4, 1, 0)`、running 出发。`reserve_model_call(..., 2)` 之后断言 `reserved is True`、`reason is None`、`budget_json == model_budget(4, 1, 2)`，并且 `fetch_budget_text` 等于同一文本。`assert_budget_gate` 断言运行门闩仍是 `SeenRunGate("running", 1, 0, None, CREATED_AT)`，快照 `budget_json` 仍是 `"{}"`。

`test_reserve_matches_stored_budget_text` 插入带空格的当前原文。成功预留后断言返回文本和 `fetch_budget_text` 都是 `model_budget(3, 0, 1)`，并且 `fetch_budget_text != stored`。门闩断言为 queued、accepting 1。

`test_two_connections_only_one_reserves_the_last_model_call` 从 `model_budget(1, 0, 0)` 让两个连接各预留 1。断言 `len(outcomes) == 2`、`len(won) == 1`、`len(lost) == 1`、胜者 `reason is None`、败者 `reason == "budget_exhausted"`，两边的 `budget_json` 与 `fetch_budget_text` 相同，解析后为 `max_model_calls == 1`、`spent_model_calls == 0`、`reserved_model_calls == 1`。

余量不够时不写。同一条计数测试在 spent 2、reserved 2、max 4 上再预留 1，调用包在 `assert_session_writes_nothing` 里，断言 `reserved is False`、`reason == "budget_exhausted"`、返回文本和 `fetch_budget_text` 仍是 `model_budget(4, 2, 2)`。`assert_session_writes_nothing` 断言 `total_changes()` 不变，且 trace 里没有含 `UPDATE` 的语句。`test_reserve_rejects_bad_estimate_and_exhausted_budget` 在 `model_budget(1, 1, 0)` 上预留 1，同样包在该 helper 里，断言 `reserved is False`、`reason == "budget_exhausted"`、`fetch_budget_text == stored`。

非法估计不写：同一测试对 `0`、`-1`、`True`、`1.5` 断言 `pytest.raises(ValueError, match="estimate")`，并且每次都在 `assert_session_writes_nothing` 内。

形状不对不改写。`test_rejected_budget_shapes_are_not_rewritten` 遍历 `INVALID_BUDGETS`，其中有 `"{}"` 和 `'{"kept":1}'`，还有缺键、多键、负数、布尔、浮点、spent+reserved 超过 max、`"[]"`、`"null"`。每个样本的预留、`settle(..., 1)`、`settle(..., None)` 都断言 `pytest.raises(ValueError, match="budget_json shape is rejected")` 且 `assert_session_writes_nothing`，然后 `fetch_budget_text == sample`，门闩仍是 queued、accepting 1。

不在接受结果的运行不预留。`test_reserve_skips_runs_that_are_not_accepting` 覆盖 cancelled/0、running/0、completed/1。每次断言 `reserved is False`、`reason == "not_accepting"`、返回文本和 `fetch_budget_text` 都等于原 `model_budget(3, 0, 0)`，调用在 `assert_session_writes_nothing` 内，`assert_budget_gate` 对上插入时的 status 和 accepting。

缺运行。`test_missing_run_budget_is_a_conflict` 对不存在的 `run-budget-missing` 预留和结算都断言 `pytest.raises(BudgetConflict)`，且都在 `assert_session_writes_nothing` 内。

### 结算

已知用量会减预留、加花费。`test_reserve_and_settle_move_model_call_counts` 在预留 2 之后调用 `settle_model_call(..., 2, 1)`，断言 `pending is False`、`budget_json == model_budget(4, 2, 0)`、`fetch_budget_text` 相同，门闩仍是 running、accepting 1。随后再预留 2，断言 `reserved is True` 且文本变为 `model_budget(4, 2, 2)`。

已知 0 会释放预留、花费不加。`test_known_zero_actual_releases_the_reservation` 从 `model_budget(3, 1, 2)` 调用 `settle_model_call(..., 2, 0)`，断言 `pending is False`、返回文本和 `fetch_budget_text` 都是 `model_budget(3, 1, 0)`。

未知用量保持原预留。`test_unknown_actual_stays_reserved` 从 `model_budget(3, 0, 1)` 调用 `settle_model_call(..., 1, None)`。断言 `pending is True`、`pending.budget_json == stored`、`fetch_budget_text == stored`，并且 `loaded["spent_model_calls"] == 0`、`loaded["reserved_model_calls"] == 1`。同时断言 `after_changes == before_changes + 1` 和 `len(set(updates)) == 1`：有一次 UPDATE，写回的仍是结算前的文本，预留仍是 1。花费字段上的 0 是原文里的 spent，这次没有把预留释放掉。

放不下就不写。`test_settle_that_does_not_fit_leaves_the_text` 的原文是 `model_budget(3, 2, 1)`。`settle(..., 2, 0)`、`settle(..., 1, 2)`、`settle(..., 2, None)` 都断言 `pytest.raises(BudgetConflict)` 且 `assert_session_writes_nothing`；`actual=True` 断言 `pytest.raises(ValueError, match="actual")` 且不写。最后 `fetch_budget_text == stored`。

两个连接只有一次结算成功。`test_two_connections_one_settle_applies` 从 `model_budget(2, 0, 1)` 并发 `settle(..., 1, 1)`。断言 `len(won) == 1`、`lost == 1`、胜者 `pending is False`，存储文本等于胜者文本，解析后 `spent_model_calls == 1`、`reserved_model_calls == 0`。失败者进入 `except BudgetConflict`。

运行已经不接受结果时，已知用量仍可结算。`test_settle_records_usage_when_run_is_not_accepting` 在 cancelled、accepting 0、`model_budget(2, 0, 1)` 上调用 `settle(..., 1, 1)`，断言 `pending is False`、文本变为 `model_budget(2, 1, 0)`，`assert_budget_gate` 仍是 cancelled、accepting 0。阅读所见：结算的 `_swap_budget` 使用 `accepting_only=False`，WHERE 对上当前 `budget_json`，不再限制 execution_status 和 accepting_results。

### 结束一次尝试

不可重试类别一次写成 failed，再结束不写。`test_non_retryable_job_error_fails_once` 的参数是 `budget_exhausted`、`unauthorized`、`payment_required`、`invalid_schema`，`max_attempts=8`，`deadline=FUTURE_DEADLINE`，`available_at=LATER_AVAILABLE`。断言 `already_finished is False`、`status == "failed"`、`last_error == category`、`fencing_token == attempt.fencing_token == 1`、`attempt_no == 1`、`input_hash == INPUT_HASH`、`available_at == 0.0`。行上断言 `lease_owner is None`、`lease_until is None`、token 与 attempt 仍为 1、`input_hash == INPUT_HASH`、`fetch_available_at == 0.0`、`output_ref is None`、`fetch_receipts == []`。再次调用走 `assert_failed_finish_is_quiet`（内部是 `assert_session_writes_nothing`，类别为 `rate_limited`），断言 `already_finished is True`、`status == "failed"`、`last_error == category`、`fetch_job(engine) == job`。

可重试且次数未满、截止仍在将来：回到 queued，`available_at` 改为调用方给出的更晚时间。`test_retryable_job_returns_to_queued_until_later` 的参数是 `rate_limited` 和 `unavailable`。断言 `status == "queued"`、`last_error == category`、`available_at == LATER_AVAILABLE`（`3_800_000_000.0`）、`fetch_available_at == LATER_AVAILABLE`，fencing token、attempt、input hash 等于认领结果，租约两列为空，`output_ref is None`，`fetch_receipts == []`，并且 `job.status != "retry_wait"`。随后 `claim` 断言 `pytest.raises(StaleLease)` 且 `fetch_job == job`；再次 finish 同样断言 `StaleLease` 且行不变。Job 的等待是 `queued` 加上这个更晚的 `available_at`。

截止已过则 failed，原因是 `deadline`。`test_retryable_job_fails_when_deadline_has_passed` 使用 `unavailable`、`max_attempts=8`、`deadline=1.0`。断言 `status == "failed"`、`last_error == "deadline"`，token 与 attempt 等于认领，`available_at == 0.0`，租约为空，`fetch_available_at == 0.0`。`assert_failed_finish_is_quiet` 再断言 `already_finished is True`、`last_error == "deadline"`、`fetch_job == job`。

次数先到顶则 failed，原因是 `attempts_exhausted`，之后不能回到 queued。`test_failed_job_past_attempt_cap_does_not_return_to_queued` 在 `attempt_no == 1` 时用 `rate_limited` 和 `max_attempts=1`、`FUTURE_DEADLINE` 结束。断言 `after_first == before + 1`、`status == "failed"`、`last_error == "attempts_exhausted"`，token、attempt、hash 不变，`available_at == 0.0`。随后在同一会话里对 `unavailable`、`rate_limited`、`budget_exhausted` 再调用三次，`max_attempts=5`，更晚的 `available_at`，整个重复包在 `assert_session_writes_nothing` 里。断言 `len(repeats) == 3`，三次都是 `already_finished`、`status == "failed"`、`last_error == "attempts_exhausted"`、`available_at == 0.0`。提交后 `job.status == "failed"`、`fetch_available_at == 0.0`、租约为空、token/attempt/hash 不变、`output_ref is None`、`fetch_receipts == []`。阅读所见：`prior_attempt >= max_attempts` 写在 `deadline_at <= now` 之前。

已经失败的行不再更新。`test_already_failed_job_is_not_rewritten` 对预先失败的 `job-failed` 调用 `assert_failed_finish_is_quiet`，断言 `already_finished is True`、`status == "failed"`、`last_error is None`、`fencing_token == 4`、`attempt_no == 2`、`fetch_job == before`，并且 `before.lease_owner is None`、`before.lease_until is None`。

非法参数不写，running 行保持。`test_finish_rejects_bad_arguments_without_update` 覆盖 category `"retry_wait"`、`max_attempts` 为 `0` 和 `True`、`deadline` 为 `float("nan")`、`available_at` 为 `1.0`。每项都断言 `pytest.raises(ValueError)` 且 `assert_session_writes_nothing`。结束后断言 `status == "running"`、`lease_owner == "worker-1"`，租约、token、attempt、hash 等于认领，`last_error is None`，`fetch_available_at == 0.0`。

## blocking 计数

**0**。

这次阅读和这次运行里，没有出现下面四条：

- 成功预留不是对当前 `budget_json` 的条件更新。`_swap_budget` 的 WHERE 对上当前原文；`test_two_connections_only_one_reserves_the_last_model_call` 断言只有一个连接 `reserved is True`，存储的 `reserved_model_calls` 为 1。`test_reserve_matches_stored_budget_text` 断言带空格的当前原文被换成结算后的紧凑文本。
- 两个连接都能预留最后一个名额。同一并发测试断言 `len(won) == 1`、`len(lost) == 1`，败者 `reason == "budget_exhausted"`。
- 未知 actual 被存成花费 0。`test_unknown_actual_stays_reserved` 断言文本与结算前相同，`reserved_model_calls == 1`，`pending is True`。`test_known_zero_actual_releases_the_reservation` 才断言已知 actual 0 把 reserved 收到 0、spent 保持 1。
- 已失败任务被再次更新，或被改回 queued。`test_non_retryable_job_error_fails_once`、`test_retryable_job_fails_when_deadline_has_passed`、`test_failed_job_past_attempt_cap_does_not_return_to_queued`、`test_already_failed_job_is_not_rewritten` 都断言再次结束没有 UPDATE，`status` 保持 `failed`，`fetch_job` 与失败后的行相等。次数封顶那条还断言后来的 `rate_limited` 和 `budget_exhausted` 没有把 `last_error` 改掉，也没有把状态改成 queued。

## 不计入 blocking、也没有收口

建议频率、事件去重、digest 时间窗、真实模型调用、投递重启、文件派生，以及 `RunTask.retry_wait`，不在上面的 blocking 定义里。这次读到的预算与 `finish_job_attempt` 测试没有锁住它们，这次运行也没有把它们收口。

`RunTask.status` 的 `retry_wait` 仍只出现在施工文档那句任务状态里。Job 没有 `retry_wait` 状态。本切片的等待是 `queued` 加上更晚的 `available_at`，由 `test_retryable_job_returns_to_queued_until_later` 的 `status == "queued"`、`available_at == LATER_AVAILABLE`、`status != "retry_wait"` 锁住。

T06 仍是 implementing。本文件不是完成声明。
