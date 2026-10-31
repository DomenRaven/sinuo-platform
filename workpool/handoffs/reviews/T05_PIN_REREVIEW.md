# T05 pin 再审

这次只读了 T05 pin 切片的交接、`register_original` / `_ready_original` / `_consume_upload_pin` / `_write_version`、`install_reserved_original` / `place_upload_pin`、安全文档里 generation/pin/GC 一节，以及 `tests/acceptance/test_assets_derivatives.py` 中涉及 stored object state、pin `consumed_at`、`ticket_owner` 的断言。预览测试只核对函数仍在。未改源码、测试、迁移、SESSION、BOARD 或任务卡，未做 git commit。

## 命令与退出码

仓库根先创建 `_施工文件/2026-10-31_grok-review-t05/99_临时文件`，然后执行：

```text
.venv/Scripts/python.exe -m pytest -q tests/acceptance/test_assets_derivatives.py -o cache_dir=_施工文件/2026-10-31_grok-review-t05/99_临时文件/pytest-cache --basetemp=_施工文件/2026-10-31_grok-review-t05/99_临时文件/pytest
```

退出码：0

pytest 摘要：`33 passed, 2 warnings in 2.19s`。两条警告是 `test_symlink_escape_writes_nothing` 里符号链接探测线程的 GBK 解码，以及 `test_mounted_download_route_sets_nosniff_and_attachment` 上 Starlette 对 httpx 的弃用提示。

## 测试锁住的登记行为

1. `test_same_bytes_round_trip_and_do_not_invent_a_preview` 调用 `register_original(..., now=NOW + 1)`，断言 `registered is True`。另开会话后断言：该 `generation_id` 的 `StoredObjectRow.state == "ready"`；同一 object 上 `purpose == "upload"` 的 pin 存在，且 `consumed_at == NOW + 1`。版本 `object_key` 等于返回的 object_key。原件字节与声明哈希一致。`preview_generated is False`，`preview_state == "unavailable"`，`preview_reason == "converter_not_installed"`。派生目录为空，名为 preview 的文件为空。

2. `test_upload_pin_before_gc_keeps_the_generation`：交错后 `pinned.placed is True`，`claimed.claimed is False`，`claimed.reason == "pinned"`。安装前对象 `state == "reserved"`，`fencing_token == 0`；pin `consumed_at is None`，`purpose == "upload"`，`expires_at > NOW`。`install_reserved_original(..., now=NOW + 1.0)` 之后断言 `installed is True`，对象 `state == "ready"` 且 `fencing_token == 1`，pin `consumed_at == NOW + 1.0`。原件字节与 payload 一致，attempt 文件已不在。

3. `test_gc_claim_before_pin_blocks_install`：GC 先赢时 `claimed.claimed is True`，`claimed.fencing_token == 1`。随后 `pinned.placed is False`，`pinned.reason == "generation_unavailable"`；`installed.installed is False`，`installed.object_key is None`，`installed.reason == "generation_unavailable"`。对象 `state == "gc_claimed"`，`fencing_token == 1`。objects 下没有名为 original 的文件，attempt 字节仍在。`fencing_token=0` 的删除断言 `deleted is False`、`reason == "stale_token"`。用 token 1 删除成功后，再次 `place_upload_pin` / `install_reserved_original` 仍是 `placed is False`、`installed is False`。最终 `state == "deleted"`，该 object 的 pin 列表为 `[]`。

4. `test_stale_gc_unlink_leaves_the_new_generation_download`：`register_original` 成功后，活 generation 的 stored object `state == "ready"`，其 upload pin `consumed_at is not None`。对该 object 的 `claim_generation` 断言 `claimed is False`、`reason == "referenced"`。用当前 token 调用 `delete_claimed_generation` 断言 `deleted is False`、`reason == "not_claimed"`。另一条 generation 上，错误 token 的删除断言 `stale_token`，当前 token 的删除只去掉那条 generation；活原件字节和哈希仍在。

5. `test_mismatched_ticket_owner_leaves_the_ticket_open`：传入与票据创建者不同的 principal 时，`registered is False`，`reason == "ticket_owner"`，`artifact_version_id is None`，`object_key is None`。票据 `status == "open"`，`artifact_version_id is None`，`generation_id is None`，`closed_at is None`。`ArtifactVersion` 行数为 0，`StoredObjectRow` 行数为 0。attempt 字节仍在，objects 下没有 original。同一票据随后不传 principal 再登记，断言 `registered is True`。

6. `test_rollback_after_install_cleans_only_the_unregistered_orphan`：已提交登记的 stored object `state == "ready"`，upload pin `consumed_at is not None`，版本 object_key 等于 `original_key(...)`。调用方在 `register_original` 已返回 `registered is True` 之后抛错；回滚后版本表只剩已提交的那一行。`discard_unregistered_original` 对已引用 generation 断言 `deleted is False`、`reason == "referenced"`；对未登记孤儿断言 `deleted is True`。孤儿行 `state == "deleted"`。已提交原件仍可下载，字节与哈希匹配。

7. `test_gc_claimed_generation_reuploads_on_a_new_generation`：旧 generation 被 claim 之后 `state` 仍是 `gc_claimed`，`generation_id` 仍是被认领的那一个。另一组字节的 `register_original` 断言 `registered is True`，其 `generation_id` 与旧 reservation、与 claim 返回的 generation 都不同。下载正文是新字节。该测试没有断言新对象的 `state`，也没有断言新 pin 的 `consumed_at`。

`test_same_name_new_bytes_are_two_versions_and_reuse_stays_in_project` 断言同项目同字节的第三次登记与第一次共用 `object_key` 和 `generation_id`，另一项目的同字节使用另一条 object_key。这次读到的断言里没有该复用 pin 的 `consumed_at`，也没有 `fencing_token`。

## 阻塞数

阻塞数：0。

这次读到的代码与上列断言对得上，三条阻塞条件都没有出现：

- 新字节要返回成功，必须先经过 `install_reserved_original`。该函数要求对象 `state == "reserved"`，并且 pin 的 object、purpose、未消费、未过期、`fencing_token` 都匹配；然后把状态改成 `ready`、fencing token 加一，并写入 `consumed_at`。两处 `rowcount` 不是 1 就抛出。`register_original` 在安装结果不为真或 object_key 不一致时抛 `RegistrationConflict`，之后才 `_write_version`。函数自己不提交。往返测试在提交后的新会话里看到 `ready` 和 `consumed_at == NOW + 1`。
- 复用只选取 `state == "ready"` 的对象。pin 放上之后写版本，再 `_consume_upload_pin`；`rowcount` 不是 1 就抛 `RegistrationConflict`。成功返回发生在这次消费之后。`place_upload_pin` 看到的状态若不在 `reserved` / `ready`，返回 `generation_unavailable`，不插入 pin。
- `test_gc_claim_before_pin_blocks_install` 把 `gc_claimed` 和随后的 `deleted` 上的 pin 与 install 断言为失败，并且没有 original 文件。
- 票据归属不符在预留之前返回 `ticket_owner`。对应测试断言未登记、票据仍 open、版本数和 stored object 数都是 0。

因此这次阅读和这次运行没有显示：一次成功登记仍能提交可引用的 ArtifactVersion，而同时缺少 ready 的 stored object 和已消费的 upload pin；没有显示 `gc_claimed` / `deleted` generation 能被安装；没有显示传入 principal 时归属不符的票据能登记成功。

## 非阻塞缺口

下面四项这次仍然开着：

- 真实 Pillow、PDF、GLB 渲染。交接写明没做。往返测试仍断言 `preview_generated is False`、`preview_state == "unavailable"`、`preview_reason == "converter_not_installed"`，并且没有 preview 文件。预览测试函数仍在：`test_missing_converter_does_not_fabricate_a_preview`、`test_injected_converter_failures_leave_the_original_downloadable`、`test_preview_metadata_stops_at_the_new_version_grant`。`preview.py` 里仍是对 `pypdfium2`、`PIL`、`blender` 的探测。
- 下载路由仍不在 `create_app` 上。`create_app` 只挂了 auth、version、events 三个 router。
- 哈希仍发生在调用方事务里。`register_original` 使用调用方已经打开的 session，在 `reserve_generation` 之前对 attempt 调用 `digest_file`。`install_reserved_original` 的正文里没有另一次哈希。安全文档要求长哈希在事务外完成。
- 复用 pin 消费不比较 `fencing_token`。`_consume_upload_pin` 的条件是 pin_id、object_id、`purpose == "upload"`、`consumed_at` 为空、`expires_at > now`。新 generation 的 `install_reserved_original` 在消费条件里比较 `fencing_token == token`。复用走的是前者。

## 状态

T05 仍是 implementing。本文件不是完成声明，也不把 T05 标成完成。

安全文档「原件登记与清理的并发协议（T05）」末句仍写：本节是待 T05 实现的合同，当前骨架未实现对象库。这次读到的登记和 GC 代码已经有 reservation、pin 和 install。该句本次没有改。
