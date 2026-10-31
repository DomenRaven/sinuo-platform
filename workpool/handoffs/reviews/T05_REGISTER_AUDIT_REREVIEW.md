# T05 登记审计再审

这次只读了 `workpool/handoffs/T05_REGISTER_AUDIT_CONTRACT.md`、`workpool/handoffs/T05_REGISTER_AUDIT_SLICE.md`、`src/sinuo/application/assets/uploads.py` 里的 `_record_registration_audit` 以及 `register_original` 的两处调用，还有 `tests/acceptance/test_registration_audit.py`。未改源码、测试、迁移、SESSION、BOARD 或任务卡，未做 git commit。

## 命令与退出码

仓库根先创建 `_施工文件/2026-10-31_grok-review-audit/99_临时文件`，然后执行：

```text
.venv/Scripts/python.exe -m pytest -q tests/acceptance/test_registration_audit.py -o cache_dir=_施工文件/2026-10-31_grok-review-audit/99_临时文件/pytest-cache --basetemp=_施工文件/2026-10-31_grok-review-audit/99_临时文件/pytest
```

退出码：0

pytest 摘要：`2 passed in 0.39s`。输出先是 `..`，然后是 `2 passed in 0.39s`。这次输出里没有 failed，没有 skipped，也没有 warning。

## 测试锁住的审计行为

文件里有两条集成测试。审计表上的断言都写在 `with write_uow(...)` 结束之后的新 `Session` 上。

`test_successful_registration_writes_one_audit_row` 使用 `PAYLOAD = b"sinuo-audit-bytes"`。声明哈希是这段字节的 sha256，声明长度是 `len(PAYLOAD)`，`converter_installed=False`。登记前这个测试没有放入 ready 对象。断言与结果：

- `outcome.registered is True`
- `outcome.artifact_version_id is not None`
- `outcome.generation_id is not None`
- `select(AuditEvent)` 得到的 `len(rows) == 1`
- `project is not None and version is not None`。project 来自 `session.get(Project, PROJECT_ID)`，version 来自 `session.get(ArtifactVersion, outcome.artifact_version_id)`
- `project.revision == 2`。同一测试调用的 `_seed` 写入的项目修订是 1。这条断言锁住的是登记之后读到的修订等于 2
- `audit.actor_id == ACCOUNT_ID`。开票 `_ticket` 传入的 `account_id` 是这个 `ACCOUNT_ID`
- `audit.command == "artifact.version_registered"`
- `audit.subject_type == "artifact_version"`
- `audit.subject_id == outcome.artifact_version_id`
- `audit.project_revision == project.revision`
- `audit.project_revision == version.project_revision`
- `json.loads(audit.payload)` 的键集合等于 `_AUDIT_FIELDS`，即 `project_id`、`artifact_id`、`artifact_version_id`、`content_hash`、`generation_id`、`byte_size`
- `payload["project_id"] == PROJECT_ID`
- `payload["artifact_id"] == ARTIFACT_ID`
- `payload["artifact_version_id"] == outcome.artifact_version_id`
- `payload["content_hash"] == digest`。digest 是 `PAYLOAD` 的 sha256 十六进制
- `payload["generation_id"] == outcome.generation_id`
- `payload["byte_size"] == len(PAYLOAD)`
- `PAYLOAD.decode("ascii")` 不在 `audit.payload` 原文里
- `str(attempts)`、`str(objects)`、`relative` 都不在原文里
- 原文里没有 `\`
- 原文里没有 `/`

`test_rejected_registration_leaves_the_audit_table_empty` 把声明哈希换成另一段字节的 sha256，并有 `declared != digest`。暂存文件写入的仍是 `PAYLOAD`，声明长度仍是 `len(PAYLOAD)`。断言与结果：

- `outcome.registered is False`
- `outcome.reason == "declared_hash_mismatch"`
- `written.read_bytes() == PAYLOAD`
- `select(func.count()).select_from(AuditEvent)` 的 `count == 0`

这次测试文件没有同项目复用 ready 对象的成功登记。也没有声明长度不符、超过调用方字节上限、授权失败、票据不存在、票据已关闭、票据作者不符这些路径上的审计断言。文件里没有回滚之后再数审计行的断言。

## 阻塞数

阻塞数：0。

读到的 `_record_registration_audit` 向当前会话 `add` 一行 `AuditEvent`，然后 `flush`。`command` 用常量 `EVENT_TYPE`，值是 `artifact.version_registered`。`actor_id` 来自 `register_original` 里的 `ticket.created_by_account_id`。payload 先经过 `_event_payload`，再经过 `canonical_audit_payload`，字段是上面那六个。这个函数的正文里没有 commit。

两处调用都在对应的成功返回之前。第一处在 `ready is not None` 且 `pinned.placed and pin_id is not None` 的分支里，位于写出版本、消费 pin、关闭票据、释放暂存之后。该分支不返回时，函数继续走到 `reserve_generation` 和 `install_reserved_original`。第二处在这条路径写出版本并关闭票据之后、`return _registered_outcome(...)` 之前。`_reject` 和 `_unregistered` 不调用 `_record_registration_audit`。哈希不符、长度不符、字节上限、票据缺失、票据已关闭、票据作者不符这些返回都在这两处调用之前。

这次运行里，成功用例在调用方 `write_uow` 返回后能读到恰好一行审计。哈希拒绝后审计表计数是 0。该成功行的 payload 原文不含原件那串 ASCII、暂存目录、对象目录、相对路径、反斜杠和斜杠。这次阅读和这次运行没有出现成功登记提交后没有审计行，没有出现被拒绝的登记写出审计行，也没有出现 payload 含文件系统路径或原件字节。预览不计入这次阻塞。

## 状态

T05 仍是 implementing。本文件不是完成声明，也不把 T05 标成完成。
