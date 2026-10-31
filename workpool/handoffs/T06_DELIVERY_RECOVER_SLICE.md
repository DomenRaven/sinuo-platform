# T06 提交后重启投递

日期：2026-10-31。T06 状态保持 implementing。本切片不把任务标成 done。

## 对照

冻结合同 `workpool/handoffs/T06_DELIVERY_RECOVER_CONTRACT.md`，版本 `T06-DELIVERY-RECOVER-1`。施工文档 `docs/construction/05_WORKFLOW_AND_CONCURRENCY.md` 的「Outbox 与至少一次消费」：业务事务可以只写下 `outbox_events` 就提交；通知前崩溃时，从已提交的 Outbox 重投。`docs/construction/02_基线裁决与勘误.md` 的 D04：至少一次投递，消费者幂等，不宣称跨系统恰好一次。

本仓库没有单独的文献库。上面这份合同和施工节就是本切片的依据。

## 选型

不新迁移。`outbox_deliveries` 的主键已经是 `(event_id, consumer_name)`。`recover_delivery` 在调用方的会话里执行一条 `INSERT ... ON CONFLICT DO NOTHING`，只有这一对还不存在时才插入 `state=pending`、`fencing_token=0`、`attempts=0`，租约字段为空。函数返回这次是否插入，自己不提交。

同一条语句在冲突时不更新已有行，所以 `delivered` 不会被改回 `pending`，`leased` 也不会被改写。放弃先查询再插入：主键冲突会中止当前 SQLite 事务，同一次 `write_uow` 里就不能接着 `claim_delivery`。不用 `INSERT OR IGNORE`，那会把外键失败也吞掉。`claim_delivery` 和 `complete_delivery` 的条件没有改。

## 行为

一次 `write_uow` 只提交 `outbox_events`，此时没有投递行。之后另一次 `write_uow` 调用 `recover_delivery`，插入一条 `pending`，同一事务里的第二次调用不再插入，随后现有的 `claim_delivery` 能认领这一行。认领完成之后再恢复，仍然只有这一行，状态保持 `delivered`，没有新的 `pending` 行。

两个线程在屏障后各自用 `write_uow` 恢复同一对事件和 consumer，提交结束后只有一行 `pending`。这不是跨系统恰好一次。

## 证据

先创建 `--basetemp` 的父目录 `_施工文件/2026-10-31_grok-dev-b4/99_临时文件`，再从仓库根执行：

```text
.venv/Scripts/python.exe -m pytest -q tests/acceptance/test_outbox_recovery.py -o cache_dir=_施工文件/2026-10-31_grok-dev-b4/99_临时文件/pytest-cache --basetemp=_施工文件/2026-10-31_grok-dev-b4/99_临时文件/pytest
```

退出码 0。2 passed。

## 还没做

没有常驻 consumer，也没有做派生文件写完、登记回执前崩溃的那一条重试。T06 不标 done。
