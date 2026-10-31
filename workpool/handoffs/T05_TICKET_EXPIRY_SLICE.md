# T05 上传票据过期

状态保持 implementing。这次只给上传票据加可选过期时间，不是 T05 完成。

## 对照

合同 `workpool/handoffs/T05_TICKET_EXPIRY_CONTRACT.md`（`T05-TICKET-EXPIRY-1`）。`request_upload_ticket` 接受可选的 `expires_at`。`register_original` 在票据过期时走既有的拒绝返回，不产生作品版本，不写审计。

## 行为

新迁移 `0008_ticket_expiry`，`down_revision` 是 `0007_run_origin`。只给 `upload_tickets` 增加可空的 `expires_at`。`0001` 到 `0007` 的 SQL 没有改。`0007` 文件里的 revision id 仍是 `0007_run_origin`。`SCHEMA_REVISION` 改为 `0008_ticket_expiry`。不新建表，产品表仍是 43 张。已有插入不写这一列时，值为空，表示不过期。

`request_upload_ticket` 的新参数默认是空。不传时写入空值。传入的值必须是 float，否则 `AssetRejected`，原因是 `expires_at`，不插入票据。

登记时如果 `expires_at` 有值且不晚于本次 `now`，包括等于 `now`，票据改为 `rejected`，`failure_reason` 是 `ticket_expired`。作品版本数是 0，审计行数是 0。早于 `now` 同样拒绝。晚于 `now` 的票据照常登记，成功时仍写一行审计。

## 生产代码

预览、Worker、`pyproject.toml`、SESSION、BOARD 未改。

## 命令

工作目录是仓库根 `sinuo-platform`。先建 `_施工文件/2026-10-31_grok-dev-a9/99_临时文件`，再执行：

```text
.venv/Scripts/python.exe -m pytest -q tests/acceptance/test_ticket_expiry.py tests/acceptance/test_registration_audit.py tests/acceptance/test_assets_derivatives.py::test_head_includes_stored_object_tables -o cache_dir=_施工文件/2026-10-31_grok-dev-a9/99_临时文件/pytest-cache --basetemp=_施工文件/2026-10-31_grok-dev-a9/99_临时文件/pytest
```

随后：

```text
.venv/Scripts/python.exe -m alembic heads
```

## 退出码

pytest 是 0。`alembic heads` 是 0。

## 通过数

11 passed。输出是 `...........`，然后是 `11 passed in 1.23s`。

`alembic heads` 的标准输出只有一行：`0008_ticket_expiry (head)`。这是一个 head。

## 仍未完成

真实 Blender 预览仍未做。T05 状态仍是 implementing。本文件不是完成声明。
