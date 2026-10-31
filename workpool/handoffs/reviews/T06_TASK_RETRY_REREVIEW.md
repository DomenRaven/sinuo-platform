# T06 任务重试等待再审

这次只读了 `workpool/handoffs/T06_TASK_RETRY_CONTRACT.md`、`workpool/handoffs/T06_TASK_RETRY_SLICE.md`、`src/sinuo/workers/run_tasks.py`、`tests/acceptance/test_run_task_retry.py`。未改源码、测试、迁移、SESSION、BOARD 或任务卡，未做 git commit。未把 T06 标成 done。

## 命令与退出码

仓库根先创建 `_施工文件/2026-10-31_grok-review-taskretry/99_临时文件`，然后执行：

```text
.venv/Scripts/python.exe -m pytest -q tests/acceptance/test_run_task_retry.py -o cache_dir=_施工文件/2026-10-31_grok-review-taskretry/99_临时文件/pytest-cache --basetemp=_施工文件/2026-10-31_grok-review-taskretry/99_临时文件/pytest
```

退出码：0

pytest 摘要：`12 passed in 1.01s`。输出先是 `............`，然后是 `12 passed in 1.01s`。

## 测试锁住的转移

`assert_session_writes_nothing` 比较调用前后的 `total_changes()`，并断言 sqlite trace 里没有含 `UPDATE` 的语句。`assert_failed_finish_is_quiet` 用 `category="rate_limited"`、`max_attempts=8`、`deadline=FUTURE_DEADLINE`、`available_at=LATER_AVAILABLE` 再调 `finish_task_attempt`，并套上这个检查。

- 可重试、次数未到顶、截止仍晚于数据库时钟、给出的 `available_at` 更晚：`running` 变成 `retry_wait`。`test_retryable_running_task_waits_and_clears_the_lease` 参数是 `rate_limited` 与 `unavailable`，种子 `attempt_no` 为 1，调用 `max_attempts=8`、`deadline=FUTURE_DEADLINE`、`available_at=LATER_AVAILABLE`。断言 `finished.already_finished is False`，`finished.status == "retry_wait"`，`task.status == "retry_wait"`，`finished.last_error == category`，`task.last_error == category`，`finished.available_at == LATER_AVAILABLE`，`task.available_at == LATER_AVAILABLE`，`task.lease_owner is None`，`task.lease_until is None`，`finished.fencing_token == before.fencing_token == 1`，`finished.attempt_no == before.attempt_no == 1`，`task.fencing_token == 1`，`task.attempt_no == 1`，`task.input_hash == INPUT_HASH`，`task.output_ref is None`，并且 `role`、`run_id`、`project_id`、`mandatory` 与调用前相同。
- 已经是 `retry_wait` 时再次结束：同一测试用 `pytest.raises(StaleLease)` 包住 `assert_session_writes_nothing`，之后 `fetch_task(engine) == task`。
- 不可重试类别进入 `failed`，第二次不发 UPDATE。`test_non_retryable_task_fails_and_a_second_call_does_not_update` 参数是 `budget_exhausted`、`unauthorized`、`payment_required`、`invalid_schema`，调用参数同时是 `max_attempts=1`、`deadline=1.0`、`available_at=1.0`。断言 `finished.status == "failed"`，`finished.last_error == category`，`task.status == "failed"`，`task.last_error == category`，`finished.available_at == 0.0`，`task.available_at == 0.0`，`task.lease_owner is None`，`task.lease_until is None`，`task.fencing_token == 1`，`task.attempt_no == 1`，`task.input_hash == INPUT_HASH`，`task.output_ref is None`。第二次 `again.already_finished is True`，`again.status == "failed"`，`again.last_error == category`，`again.available_at == 0.0`，`fetch_task(engine) == task`。第二次经过 `assert_failed_finish_is_quiet`。
- 次数到顶先于截止，进入 `failed`，`last_error` 为 `attempts_exhausted`，第二次不发 UPDATE。`test_attempt_cap_fails_before_the_deadline_and_a_second_call_does_not_update` 使用 `category="rate_limited"`、`max_attempts=1`、`deadline=1.0`、`available_at=LATER_AVAILABLE`，种子 `attempt_no` 为 1。断言 `after_changes == before_changes + 1`，`first.status == "failed"`，`first.last_error == "attempts_exhausted"`，`first.available_at == 0.0`，`first.fencing_token == 1`，`first.attempt_no == 1`，`task.status == "failed"`，`task.last_error == "attempts_exhausted"`，`task.status != "retry_wait"`，`task.lease_owner is None`，`task.lease_until is None`，`task.available_at == 0.0`。第二次 `again.already_finished is True`，`again.last_error == "attempts_exhausted"`，`fetch_task(engine) == task`，并经过 `assert_failed_finish_is_quiet`。
- 截止已经不晚于数据库时钟，进入 `failed`，`last_error` 为 `deadline`，第二次不发 UPDATE。`test_passed_deadline_fails_and_a_second_call_does_not_update` 使用 `category="unavailable"`、`max_attempts=8`、`deadline=1.0`、`available_at=LATER_AVAILABLE`。断言 `finished.status == "failed"`，`finished.last_error == "deadline"`，`finished.available_at == 0.0`，`finished.attempt_no == 1`，`task.status == "failed"`，`task.last_error == "deadline"`，`task.lease_owner is None`，`task.lease_until is None`，`task.available_at == 0.0`，`task.fencing_token == 1`。第二次 `again.already_finished is True`，`again.last_error == "deadline"`，`fetch_task(engine) == task`，并经过 `assert_failed_finish_is_quiet`。
- 仍可等待，但给出的 `available_at` 不晚于数据库时钟：抛出 `ValueError`，不写，行保持 `running`。`test_retryable_available_at_must_be_later_and_writes_nothing` 使用 `category="unavailable"`、`max_attempts=8`、`deadline=FUTURE_DEADLINE`、`available_at=1.0`。断言调用前 `before.status == "running"`，`before.lease_owner == "worker-1"`，`before.lease_until == LEASE_UNTIL`；`pytest.raises(ValueError, match="available_at")` 包住 `assert_session_writes_nothing`；之后 `fetch_task(engine) == before`。
- 未到 `available_at` 的放行不改行。`test_release_before_available_at_changes_nothing` 先把任务收成 `retry_wait`。断言 `finished.status == "retry_wait"`，`waiting.status == "retry_wait"`，`waiting.available_at == LATER_AVAILABLE`，`waiting.lease_owner is None`，`waiting.lease_until is None`。`release_retry_wait` 包在 `assert_session_writes_nothing` 里。断言 `released.released is False`，`released.status == "retry_wait"`，`released.available_at == LATER_AVAILABLE`，`released.fencing_token == 1`，`released.attempt_no == 1`，`fetch_task(engine) == waiting`。
- 到点后两个连接放行，只有一次把行改成 `queued`。`test_two_threads_release_one_due_task` 先以 `unavailable` 收成等待，`make_due` 把 `available_at` 写成 1.0。断言放行前 `due.status == "retry_wait"`，`due.available_at == 1.0`，租约两列为空。两个线程在 `Barrier(2)` 后各自用 `write_uow` 调用 `release_retry_wait`。断言 `errors == []`，`sorted(released) == [False, True]`，`task.status == "queued"`，`task.available_at == 1.0`，`task.lease_owner is None`，`task.lease_until is None`，`task.fencing_token == due.fencing_token == 1`，`task.attempt_no == due.attempt_no == 1`，`task.input_hash == INPUT_HASH`，`task.last_error == "unavailable"`，`task.output_ref is None`，`task.role == due.role`。第三次放行包在 `assert_session_writes_nothing` 里，断言 `again.released is False`，`again.status == "queued"`，`fetch_task(engine) == task`。

`test_jobs_have_no_retry_wait_status` 断言 `"retry_wait" not in JOB_STATUSES`，以及 `"retry_wait" in TASK_STATUSES`。这次运行通过了这两条。该测试只比较状态常量，没有插入 job 行，也没有对 job 发 UPDATE。jobs 获得 `retry_wait` 不计入下面的阻塞，本文件也不把它写成已经关闭。

测试文件导入的是 `finish_task_attempt` 和 `release_retry_wait`。这次阅读和这次运行里没有真实模型调用。真实模型调用不计入下面的阻塞，本文件也不把它写成已经关闭。

## 阻塞数

阻塞数：0。

`finish_task_attempt` 在类别属于 `rate_limited` 或 `unavailable`、`attempt_no` 小于 `max_attempts`、`deadline` 仍晚于数据库时钟、给出的 `available_at` 也晚于数据库时钟时，发一条 `status == "running"` 的条件更新，目标是 `retry_wait`，并清空 `lease_owner` 与 `lease_until`。对应断言是 `finished.status == "retry_wait"` 与 `task.status == "retry_wait"`。这次运行通过，没有出现可重试且未超次数的任务停在别的状态。

已经 `failed` 时，函数读到 `current.status == "failed"` 后返回 `already_finished=True`，该分支没有 UPDATE。不可重试、次数到顶、截止已过这三条测试的第二次调用都经过 `assert_session_writes_nothing`，并断言 `already_finished is True` 且 `fetch_task` 与第一次结束后的行相等。这次运行通过，没有出现失败行被再次 UPDATE。

`release_retry_wait` 在状态不是 `retry_wait` 或 `available_at` 仍晚于数据库时钟时返回 `released=False`，不执行更新。`test_release_before_available_at_changes_nothing` 用 `assert_session_writes_nothing` 包住这次放行，并断言 `fetch_task(engine) == waiting`。这次运行通过，没有出现提前放行改动行。

到点放行的更新条件是 `status == "retry_wait"` 且 `available_at` 不晚于同一次读到的数据库时钟，只把状态写成 `queued`。`rowcount` 不是 1 且另一侧已经是同一 fencing token、attempt、input hash、`available_at`、`last_error` 的 `queued` 时，返回 `released=False`。两个线程的断言是 `sorted(released) == [False, True]`，结束后 `task.status == "queued"`。这次运行通过，没有出现两个连接都把同一条任务改成 `queued`。

## 状态

T06 仍是 implementing。本文件不是完成声明，也不把 T06 标成完成。
