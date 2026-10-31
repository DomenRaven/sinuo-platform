# T06 提交后重启投递复审

日期：2026-10-31。本次阅读范围是 `workpool/handoffs/T06_DELIVERY_RECOVER_CONTRACT.md`、`workpool/handoffs/T06_DELIVERY_RECOVER_SLICE.md`、`src/sinuo/workers/leases.py` 里的 `recover_delivery`，以及 `tests/acceptance/test_outbox_recovery.py`。下面只记录这次阅读和这次运行。没有改源码、测试、迁移、SESSION、BOARD 或任务卡。

## 命令与退出码

先创建 `_施工文件/2026-10-31_grok-review-delivery/99_临时文件`，再从仓库根执行：

```text
.venv/Scripts/python.exe -m pytest -q tests/acceptance/test_outbox_recovery.py -o cache_dir=_施工文件/2026-10-31_grok-review-delivery/99_临时文件/pytest-cache --basetemp=_施工文件/2026-10-31_grok-review-delivery/99_临时文件/pytest
```

标准输出：`2 passed in 0.28s`。退出码 0。

## 这次读到的恢复函数

`recover_delivery` 在调用方会话里执行插入。写入值是 `state="pending"`、`lease_owner=None`、`fencing_token=0`、`lease_until=None`、`attempts=0`。冲突列是 `event_id` 与 `consumer_name`，动作为 `ON CONFLICT DO NOTHING`。返回值是 `RETURNING event_id` 是否取到一行。函数体内没有 `commit`。

## 测试锁住的恢复行为

`test_committed_event_is_recovered_claimed_and_not_reset` 把下面这些结果钉在断言上。`CONSUMER` 是 `"preview"`。

- `commit_event` 之后，新的 `Session` 断言 `session.get(OutboxEvent, event_id) is not None`，并且 `session.get(OutboxDelivery, (event_id, CONSUMER)) is None`。
- 下一次 `write_uow` 里，第一次 `recover_delivery` 断言 `is True`。
- 同一次 `write_uow` 里，第二次 `recover_delivery` 断言 `is False`。
- 同一次 `write_uow` 里，`claim_delivery(..., owner="worker-1", now=0, ttl_seconds=10)` 的返回值断言 `lease is not None`。
- `complete_delivery(session, lease, now=1)` 之后，另一次 `write_uow` 里的 `recover_delivery` 断言 `is False`。
- 随后新的 `Session` 断言该行 `is not None`、`row.state == "delivered"`、`delivery_count(session, event_id) == 1`，以及 `state == "pending"` 的计数 `pending == 0`。

`test_two_connections_recover_one_delivery_row` 把下面这些结果钉在断言上。

- 两个线程各自在 `write_uow` 里对同一 `event_id` 和 `"preview"` 调用 `recover_delivery`。`join` 之后断言 `not first.is_alive()` 且 `not second.is_alive()`。
- `errors == []`。
- `sorted(inserted) == [False, True]`。
- 新的 `Session` 断言该行 `is not None`、`row.state == "pending"`、`row.lease_owner is None and row.lease_until is None`、`row.fencing_token == 0 and row.attempts == 0`、`delivery_count(session, event_id) == 1`。

## 阻塞计数

阻塞 0。

这次运行里，已提交且没有 `(event_id, "preview")` 投递行的事件，在 `recover_delivery` 返回真的同一次 `write_uow` 中被 `claim_delivery` 认领。两个连接的返回值是一个假和一个真，结束后行数是 1。`complete_delivery` 之后再恢复，返回值是假，该行的 `state` 仍是 `delivered`，`pending` 计数是 0。按本次给定的阻塞含义，这三项在这次阅读和这次运行里没有出现。

跨系统恰好一次、真实模型调用、派生文件登记不在这次断言里，不记成已关闭。测试写下的事件类型是 `example.confirmed`。两个测试都没有先放一行 `leased` 再调用 `recover_delivery`，租约中的行是否保持原样也不在这次断言里。

## 任务状态

T06 仍是 implementing，本文件不是完成声明。
