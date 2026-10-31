# T05 登记审计的复用与失败路径

状态保持 implementing。这次只给登记审计补上复用成功和两条失败路径的断言，不是 T05 完成。

## 对照

合同 `workpool/handoffs/T05_REGISTER_AUDIT_CONTRACT.md`（`T05-REGISTER-AUDIT-1`）。成功登记写一行审计。哈希、长度或越权失败不写。同项目相同字节复用 ready 对象时，版本 id 始终是新的，因此再写一行指向新版本的审计，不另存一份物理原件。

## 补上的断言

`tests/acceptance/test_registration_audit.py` 仍使用 `test_assets_derivatives.py` 里的 `_seed`、`_ticket`、`_account`、`_membership`、`_project_grant`、`_principal`、`_files_named`。审计计数都在调用方 `write_uow` 结束之后的新 `Session` 上。

1. `test_reused_bytes_write_a_second_audit_for_the_new_version`：同一项目用相同字节再开一张票据并登记。第二次 `registered is True`，新的 `artifact_version_id` 与第一次不同，`generation_id` 和 `object_key` 与第一次相同。`audit_events` 为 2 行，其中一行的 `subject_id` 是新版本 id，payload 的 `artifact_version_id` 也是该 id。名为 `original` 的文件只有 1 个，`stored_objects` 计数为 1，作品版本为 2。
2. `test_declared_length_mismatch_writes_no_audit_and_no_version`：声明长度比实际字节少 1。`reason` 为 `expected_size_mismatch`。审计行数为 0，作品版本数为 0。
3. `test_ticket_owner_mismatch_writes_no_audit_and_leaves_the_ticket_open`：主体持有 `artifact.upload`，但账户不是票据创建者。`reason` 为 `ticket_owner`。审计行数为 0，作品版本数为 0，票据 `status` 仍是 `open`，`closed_at` 为空。

## 生产代码

`register_original` 未改。新断言没有证明成功路径漏写审计，也没有证明失败路径写出审计。迁移、预览、Worker、SESSION、BOARD 未改。

## 命令

工作目录是仓库根 `sinuo-platform`。先建 `_施工文件/2026-10-31_grok-dev-a8/99_临时文件`，再执行：

```text
.venv/Scripts/python.exe -m pytest -q tests/acceptance/test_registration_audit.py -o cache_dir=_施工文件/2026-10-31_grok-dev-a8/99_临时文件/pytest-cache --basetemp=_施工文件/2026-10-31_grok-dev-a8/99_临时文件/pytest
```

## 退出码

0

## 通过数

5 passed。输出是 `.....`，然后是 `5 passed in 0.65s`。

## 仍未完成

真实 Blender 预览仍未做。T05 状态仍是 implementing。本文件不是完成声明。
