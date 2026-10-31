# T06 取消复审

日期：2026-10-31。只读这一片。只写本文件。不改源代码、测试、迁移、SESSION、BOARD、任务卡。不做 git commit。

T06 仍是 implementing。本文件不是完成声明。

对照范围只到这些材料：`docs/construction/05_WORKFLOW_AND_CONCURRENCY.md` 的「合成、晚到结果与取消」；`src/sinuo/workers/runs.py`；`tests/acceptance/test_durable_tasks_events.py` 里的 `test_cancel_run_closes_status_and_rejects_late_publish`（2034–2126 行）和 `test_repeat_cancel_run_does_not_rewrite_execution_status`（2130–2156 行），以及这两条测试直接调用的助手。没有重读整个测试文件。上一份 `workpool/handoffs/reviews/T06_SLICE_REVIEW.md` 只重读了 blocking 一节。

## 命令与退出码

工作目录：`sinuo-platform`。命令原文：

```
.venv/Scripts/python.exe -m pytest -q tests/acceptance/test_durable_tasks_events.py::test_cancel_run_closes_status_and_rejects_late_publish tests/acceptance/test_durable_tasks_events.py::test_repeat_cancel_run_does_not_rewrite_execution_status -o cache_dir=_施工文件/2026-10-31_grok-review/99_临时文件/pytest-cache --basetemp=_施工文件/2026-10-31_grok-review/99_临时文件/pytest
```

同一条命令执行了两次。

第一次退出码 **1**。两条都在 setup 失败，测试函数没有执行。pytest 对 `--basetemp` 调用 `os.mkdir` 且 `parents=False`，当时父目录 `_施工文件/2026-10-31_grok-review/99_临时文件` 不存在，错误是 `FileNotFoundError` / `WinError 3`。同一次进程写下了 `pytest-cache`，父目录因此出现。

第二次退出码 **0**。输出是 `2 passed in 0.40s`。输出里没有 failed，也没有 skipped。下面的断言结论只来自这次退出码 0 的运行，加上这次读到的测试正文和 `runs.py`。

## 被断言锁住的取消行为

助手行为，供下面的断言落点使用：`insert_gate_run` 在 `write_uow` 里调用 `seed_reader(session, grant=False)`，再插入 `ContextSnapshot` 和 `AgentRun`。未传入时 `execution_status` 为 `queued`，`accepting_results` 为 1，`next_event_seq` 为 0，`closed_at` 为 `None`，`created_at` 为 `CREATED_AT`（`1_700_000_000.0`）。`cancel_open_run` / `publish_open_run` 各自在 `write_uow` 里调用 `cancel_run` / `publish_run`。`write_uow` 成功时提交，异常时回滚。`fetch_run_gate` 用新的 `Session` 读出 `SeenRunGate`：`execution_status`、`accepting_results`、`next_event_seq`、`closed_at`、`created_at`。`StaleLease` 是 `sinuo.domain.errors.StaleLease`。测试只用 `pytest.raises(StaleLease)`，没有 `match`。

`runs.py` 里与这些断言对应的写路径：`cancel_run` 若当前 `execution_status` 已是 `cancelled`，直接返回，不写更新。否则一条条件 `UPDATE`，条件是该 `id` 且 `execution_status` 为 `queued` 或 `running`，写入 `execution_status="cancelled"` 和 `accepting_results=0`。`rowcount` 不是 1 时抛 `StaleLease`。`publish_run` 一条条件 `UPDATE`，条件是 `accepting_results == 1` 且 `execution_status != "cancelled"`；写入 `accepting_results=0`，并把 `queued` / `running` 改为 `completed`，其他非取消状态保留原 `execution_status`。这条更新不写 `next_event_seq`。

### `test_cancel_run_closes_status_and_rejects_late_publish`

1. 排队行 `run-cancel-queued`（`next_event_seq=4`）。`cancel_open_run` 的返回值：`already_cancelled is False`，`execution_status == "cancelled"`，`accepting_results == 0`。提交后再读：`execution_status == "cancelled"`，`accepting_results == 0`，`next_event_seq == 4`，`closed_at is None`，`created_at == CREATED_AT`。随后 `publish_open_run` 抛 `StaleLease`。再读的 `SeenRunGate` 与取消后那一次相等，所以上面五列都没有被这次发布改掉。

2. 运行中行 `run-cancel-running`（`execution_status="running"`，`next_event_seq=4`）。返回值：`already_cancelled is False`，`execution_status == "cancelled"`，`accepting_results == 0`。提交后再读：`execution_status == "cancelled"`，`accepting_results == 0`，`next_event_seq == 4`。这条路径没有再调用 `publish_open_run`，也没有单独断言 `closed_at` 或 `created_at`。

3. 运行中且已经不接受结果的行 `run-closed-running`（`execution_status="running"`，`accepting_results=0`，`next_event_seq=2`）。先 `publish_open_run`，抛 `StaleLease`，再读与调用前的门闩相等。然后 `cancel_open_run`：返回值 `already_cancelled is False`，`execution_status == "cancelled"`，`accepting_results == 0`；再读同样是 `cancelled`、`0`，且 `next_event_seq == 2`。

4. 已完成且仍接受结果的行 `run-completed`（`execution_status="completed"`，`accepting_results=1`，`next_event_seq=3`）。`cancel_open_run` 抛 `StaleLease`，再读与调用前相等，所以取消没有改这行。随后 `publish_open_run` 的返回值和再读都是 `execution_status == "completed"`、`accepting_results == 0`，再读的 `next_event_seq == 3`。第二次 `publish_open_run` 抛 `StaleLease`，再读与第一次发布后的门闩相等。

5. 失败且仍接受结果的行 `run-failed`（`execution_status="failed"`，`accepting_results=1`，序号用助手默认值 0）。测试先发布。返回值和再读都是 `execution_status == "failed"`、`accepting_results == 0`，再读的 `next_event_seq == 0`。然后 `cancel_open_run` 抛 `StaleLease`，再读与发布后的门闩相等。取消拒绝锁住的是发布之后的这行（`accepting_results` 已经是 0），不是发布前那行。

6. 已经取消且 `accepting_results` 仍为 1 的行 `run-already-cancelled`（`next_event_seq=5`）。`publish_open_run` 抛 `StaleLease`，再读与调用前相等，因此 `accepting_results` 仍是 1。这条没有调用 `cancel_run`。

7. 不存在的 id `run-missing`。`cancel_open_run` 抛 `StaleLease`。抛出之后没有再 `fetch_run_gate`。

### `test_repeat_cancel_run_does_not_rewrite_execution_status`

行 `run-repeat-cancel` 插入时为 `running`，`next_event_seq=4`。第一次经 `cancel_open_run`：返回值 `already_cancelled is False`，`execution_status == "cancelled"`，`accepting_results == 0`。再读：`execution_status == "cancelled"`，`accepting_results == 0`。

第二次在同一个 `write_uow` 里直接调用 `cancel_run`。调用前后 `SELECT total_changes()` 相等。`raw_sqlite` 装上的 `set_trace_callback` 收到的语句里，没有包含 `UPDATE` 的语句。返回值 `already_cancelled is True`，`execution_status == "cancelled"`，`accepting_results == 0`。这个 `write_uow` 结束后再读，`SeenRunGate` 与第二次调用前相等，因此 `next_event_seq`、`closed_at`、`created_at` 也与第二次调用前相同。这次重复取消发生在第一次取消已经把 `accepting_results` 写成 0 之后。

## blocking

**0**

上一份切片审查的 blocking 1 写的是：测试没有读取消之后的 `AgentRun.execution_status` 或 `accepting_results`，晚到结果不发布也没有对应断言。这次这两条测试已经锁住三件事：排队和运行中的取消把 `execution_status` 写成 `cancelled`、把 `accepting_results` 写成 0；`run-cancel-queued` 取消后的 `publish_run` 抛 `StaleLease`，门闩行不变；已经取消的行再次 `cancel_run` 不发出 `UPDATE`，`execution_status` 保持 `cancelled`。第二次 pytest 退出码 0，这些断言通过。按这次复审的口径，这一项不再记 blocking。

预算、重启后再投递、文件派生、建议频率仍是不阻断缺口。这次没有重读那几块实现，也没有跑对应用例。本文件不把它们记成已关闭。上一份切片审查里的其他不阻断缺口，这次没有复审。

这两条测试没有断言、因此也不能记成已关闭的合同句子还有：取消之后尽力取消网络和子进程（`cancel_run` 的函数体里没有这类调用）；记下已发出请求的实际用量或结果；候选、`result_manifest` 或用户预览有没有被晚到结果改掉（断言只打到 `publish_run` 的 `StaleLease` 和 `SeenRunGate`）；已提交的业务决定保持有效；必需步骤超时后进入 `partial` 或 `failed`。另外，`execution_status` 已是 `cancelled` 且 `accepting_results` 仍为 1 时，再次 `cancel_run` 会不会改写这两列，这两条测试没有走这条调用。同文件后面的 `test_publish_run_seals_open_run_and_blocks_cancel` 和 `test_two_connections_only_one_of_publish_or_cancel_succeeds` 不在这次命令里，不计入上面的锁。

T06 仍是 implementing。本文件不是完成声明。
