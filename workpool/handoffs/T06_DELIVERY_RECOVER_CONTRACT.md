# T06 提交后重启投递

版本：`T06-DELIVERY-RECOVER-1`。日期：2026-10-31。pin fence 复审 blocking 0 之后才做。

业务事务可以只写下 `outbox_events` 就提交。通知前崩溃时，还没有 `outbox_deliveries` 行。重启后的恢复函数为指定 consumer 补一条 `pending` 投递，不提交。随后仍用现有的 `claim_delivery` 和 `complete_delivery`。

同一 `(event_id, consumer_name)` 只有一条投递。并发恢复最多插入一次。已经 `delivered` 的事件不会再变成 `pending`。这不是跨系统恰好一次。

不改迁移，不改资产和预览代码。
