# T05 上传票据过期再审

这次只读了 `workpool/handoffs/T05_TICKET_EXPIRY_CONTRACT.md`、`workpool/handoffs/T05_TICKET_EXPIRY_SLICE.md`、`src/sinuo/application/assets/uploads.py` 里 `request_upload_ticket` 对 `expires_at` 的检查和 `register_original` 里的过期分支、`tests/acceptance/test_ticket_expiry.py`、`migrations/versions/0008_ticket_expiry.py` 的 `revision` 与 `down_revision`，以及 `src/sinuo/infrastructure/models.py` 的 `SCHEMA_REVISION`。未改源码、测试、迁移、SESSION、BOARD 或任务卡，未做 git commit。

## 命令与退出码

仓库根先创建 `_施工文件/2026-10-31_grok-review-expiry/99_临时文件`，然后执行：

```text
.venv/Scripts/python.exe -m pytest -q tests/acceptance/test_ticket_expiry.py -o cache_dir=_施工文件/2026-10-31_grok-review-expiry/99_临时文件/pytest-cache --basetemp=_施工文件/2026-10-31_grok-review-expiry/99_临时文件/pytest
```

退出码：0

pytest 摘要：`5 passed in 0.69s`。进度行是 `.....`。

随后执行：

```text
.venv/Scripts/python.exe -m alembic heads
```

退出码：0

标准输出只有一行：`0008_ticket_expiry (head)`。

## 测试锁住的过期行为

文件里有五条集成测试。作品版本数和审计行数来自 `_version_and_audit_counts`：登记用的 `write_uow` 结束之后，新的 `Session` 上用 `func.count()` 分别数 `ArtifactVersion` 和 `AuditEvent`。

`test_omitted_expires_at_stays_null_and_registers` 用 `_ticket` 开票。`_ticket` 调用 `request_upload_ticket` 时没有传入 `expires_at`。登记的 `now` 是 `NOW + 1.0`，`converter_installed=False`。断言：

- `row is not None`
- `row.expires_at is None`
- `outcome.registered is True`
- `outcome.artifact_version_id is not None`
- `_version_and_audit_counts(assets) == (1, 1)`

`test_expires_at_equal_to_now_leaves_no_version_or_audit` 里 `moment = NOW + 1.0`，开票 `expires_at=moment`，登记 `now=moment`。断言：

- `outcome.registered is False`
- `outcome.reason == "ticket_expired"`
- `outcome.artifact_version_id is None`
- `_version_and_audit_counts(assets) == (0, 0)`
- `row is not None`
- `row.status == "rejected"`
- `row.failure_reason == "ticket_expired"`
- `row.artifact_version_id is None`
- `row.closed_at == moment`

`test_expires_at_before_now_leaves_no_version_or_audit` 开票 `expires_at=NOW`，登记 `now=NOW + 1.0`。断言：

- `outcome.registered is False`
- `outcome.reason == "ticket_expired"`
- `outcome.artifact_version_id is None`
- `_version_and_audit_counts(assets) == (0, 0)`

这条没有断言票据行的 `status`、`failure_reason`、`closed_at`。

`test_future_expires_at_still_registers` 开票 `expires_at=NOW + 10.0`，登记 `now=NOW + 1.0`。断言：

- `outcome.registered is True`
- `outcome.reason is None`
- `outcome.artifact_version_id is not None`
- `_version_and_audit_counts(assets) == (1, 1)`
- `row is not None`
- `row.expires_at == expires_at`
- `row.status == "registered"`

`test_non_float_expires_at_is_rejected_before_insert` 把 `expires_at=1` 传给 `request_upload_ticket`。断言：

- `pytest.raises(AssetRejected)`
- `rejected.value.reason == "expires_at"`
- `select(func.count()).select_from(UploadTicketRow)` 的 `count == 0`

这条没有调用 `register_original`，也没有数作品版本或审计行。

这次测试文件没有 Alembic head 的断言，没有升级或降级，没有把过期和授权、字节上限或哈希不符放在同一条用例里。

## 这次读到的检查

`request_upload_ticket` 的 `expires_at` 默认是 `None`。`expires_at is not None and type(expires_at) is not float` 时 `raise AssetRejected("expires_at")`，这行在 `session.add` 之前。插入行写入 `expires_at=expires_at`。

`register_original` 在票据状态不是 `open` 的返回之后、字节上限和哈希比较之前：

`if ticket.expires_at is not None and ticket.expires_at <= now: return _reject(session, ticket, "ticket_expired", now)`

`_reject` 把 `status` 写成 `rejected`，`failure_reason` 写成传入的原因，`closed_at` 写成 `now`，然后 `flush`，返回 `_unregistered`。这个函数体里没有 `add` `ArtifactVersion` 或 `AuditEvent`。

`migrations/versions/0008_ticket_expiry.py` 里 `revision = "0008_ticket_expiry"`，`down_revision = "0007_run_origin"`。`SCHEMA_REVISION = "0008_ticket_expiry"`。

## 阻塞数

阻塞数：0。

过期票据仍写出作品版本或审计行：等于 `now` 和早于 `now` 的两条断言都是版本数 0、审计行数 0，这次 pytest 通过。空的 `expires_at` 被当成已经过期：不传 `expires_at` 的那条断言 `row.expires_at is None`，并且 `registered is True`、计数 `(1, 1)`，这次 pytest 通过。读到的条件是 `expires_at is not None` 才进入 `<= now`。Alembic 多于一个 head：`alembic heads` 退出码 0，标准输出只有 `0008_ticket_expiry (head)` 这一行。

预览不计入这次阻塞。这次阅读和这次运行没有把预览写成已经结束。

## 状态

T05 仍是 implementing。本文件不是完成声明，也不把 T05 标成完成。
