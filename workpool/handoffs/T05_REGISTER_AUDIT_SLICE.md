# T05 登记审计

状态保持 implementing。成功登记原件时，在同一次调用方事务里写一行审计。这只覆盖登记审计，不是 T05 完成。

## 对照

合同 `workpool/handoffs/T05_REGISTER_AUDIT_CONTRACT.md`（`T05-REGISTER-AUDIT-1`）。成功的 `register_original` 写 `audit_events`。失败的哈希、长度、越权不写。

## 行为

`register_original` 在作品版本和票据都已写入当前会话之后、函数返回之前，插入一行 `audit_events`。函数自己不提交。调用方的 `write_uow` 提交时，审计行和版本一起提交。

- `actor_id` 是票据上的 `created_by_account_id`
- `command` 是 `artifact.version_registered`
- `subject_type` 是 `artifact_version`
- `subject_id` 是新的作品版本 id
- `project_revision` 是这次登记推进后的项目修订
- `payload` 只有 `project_id`、`artifact_id`、`artifact_version_id`、`content_hash`、`generation_id`、`byte_size`

payload 不含磁盘路径，也不含原件字节。哈希不符、声明长度不符、超过调用方字节上限、授权失败，以及票据作者不符，都不插入审计行。

首次安装和同项目复用 ready 对象这两条成功路径都会写这一行。`actor_id` 用的是票据账户，既有成功登记已经插入该账户。`install_reserved_original`、迁移、预览路由和 Worker 未改。

## 命令

工作目录是仓库根 `sinuo-platform`。先建 `_施工文件/2026-10-31_grok-dev-a7/99_临时文件`，再执行：

```text
.venv/Scripts/python.exe -m pytest -q tests/acceptance/test_registration_audit.py -o cache_dir=_施工文件/2026-10-31_grok-dev-a7/99_临时文件/pytest-cache --basetemp=_施工文件/2026-10-31_grok-dev-a7/99_临时文件/pytest
```

随后：

```text
.venv/Scripts/python.exe -m pytest -q tests/acceptance/test_assets_derivatives.py::test_same_bytes_round_trip_and_do_not_invent_a_preview -o cache_dir=_施工文件/2026-10-31_grok-dev-a7/99_临时文件/pytest-cache --basetemp=_施工文件/2026-10-31_grok-dev-a7/99_临时文件/pytest
```

## 退出码

两条命令都是 0。

## 通过数

登记审计 2 passed。同字节往返 1 passed。

## 仍未完成

真实 Blender 预览仍未做。T05 状态仍是 implementing。
