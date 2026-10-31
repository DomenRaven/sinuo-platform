# T05 复用 pin fencing token 再审

这次只读了 `workpool/handoffs/T05_PIN_FENCE_CONTRACT.md`、`workpool/handoffs/T05_PIN_FENCE_SLICE.md`、`src/sinuo/application/assets/uploads.py` 里 `register_original` 的同项目 ready 复用分支和 `_consume_upload_pin`，以及 `tests/acceptance/test_assets_derivatives.py` 里 `test_reuse_consume_requires_the_captured_fencing_token` 与 `test_same_name_new_bytes_are_two_versions_and_reuse_stays_in_project`。未改源码、测试、迁移、SESSION、BOARD 或任务卡，未做 git commit。

## 命令与退出码

仓库根先创建 `workpool/handoffs/reviews` 和 `_施工文件/2026-10-31_grok-review-pinfence/99_临时文件`，然后执行：

```text
.venv/Scripts/python.exe -m pytest -q tests/acceptance/test_assets_derivatives.py::test_reuse_consume_requires_the_captured_fencing_token tests/acceptance/test_assets_derivatives.py::test_same_name_new_bytes_are_two_versions_and_reuse_stays_in_project -o cache_dir=_施工文件/2026-10-31_grok-review-pinfence/99_临时文件/pytest-cache --basetemp=_施工文件/2026-10-31_grok-review-pinfence/99_临时文件/pytest
```

退出码：0

pytest 摘要：`2 passed in 0.47s`。输出是两条通过，没有 failed，没有 skipped，这次输出里也没有 warning。

## 测试锁住的消费行为

读到的复用分支在 `place_upload_pin` 之前把 `ready.fencing_token` 记到局部变量，写出版本之后把这个值传给 `_consume_upload_pin`。该函数在 token 的类型不是 `int` 时抛 `RegistrationConflict`；否则更新条件包含 `pin_id`、`object_id`、`purpose == "upload"`、`consumed_at` 为空、`expires_at > now`，以及 `object_pins.fencing_token` 等于传入的 token。`rowcount` 不是 1 时抛 `RegistrationConflict`。下面只列这两次测试里的断言实际锁住的结果。

`test_reuse_consume_requires_the_captured_fencing_token` 的成功复用：

- `reused.registered is True`，登记时刻是 `NOW + 3.0`。
- `reused.generation_id == first.generation_id`，`reused.object_key == first.object_key`，`reused.artifact_version_id != first.artifact_version_id`，`reused.content_hash == digest`。
- 该 generation 的原件文件存在，字节等于 payload，哈希等于 digest；`_files_named(objects, "original")` 只有这一个文件。
- 本项目 `StoredObjectRow` 只有一行，`ready.state == "ready"`。
- 该复用上传的 pin 只有一行：`reuse_pin.consumed_at == NOW + 3.0`，并且 `reuse_pin.fencing_token == ready.fencing_token`。
- `ArtifactVersion` 行数等于 2。

同一测试的错位路径：先从 `sqlite_master` 读出名为 `object_pins_before_update` 的触发器语句，断言它是字符串且含有 `object_pin_identity`，再执行 `DROP TRIGGER object_pins_before_update`。随后在 `after_flush_postexec` 里把这次上传、`consumed_at` 为空的 pin 的 `fencing_token` 改成原值加 1。断言与结果：

- `pytest.raises(RegistrationConflict)` 包住这次 `register_original`。
- `shifted["done"] is True`，说明监听器确实改到了那枚未消费 pin 的 token。
- `_files_named(objects, "original")` 仍只有成功复用之前的那一个文件。
- 版本 id 集合等于 `{first.artifact_version_id, reused.artifact_version_id}`。
- 这次票据 `status == "open"`，`artifact_version_id is None`。
- 该上传的 pin 满足 `pins == [] or all(pin.consumed_at is None for pin in pins)`，并且 `all(pin.consumed_at is None for pin in pins)`。锁住的是：没有一行该上传的 pin 带着已写入的 `consumed_at`。断言同时允许 pin 行不存在，也允许行还在而 `consumed_at` 为空；这次运行没有把这两种情形分开打印。

这个错位测试会丢掉 `object_pins_before_update`。`DROP TRIGGER` 写在 `try` 之前。`finally` 用 `write_uow` 执行从 `sqlite_master` 保存下来的那条语句。这次 pytest 退出码是 0，该测试通过，所以恢复语句在测试函数返回前执行，并且没有把测试打失败。测试在恢复之后没有再查 `sqlite_master`。丢掉触发器、把已放下 pin 的 token 加一、再执行保存的建触发器语句，都出现在这个测试里。`register_original` 和 `_consume_upload_pin` 的正文里没有 `DROP TRIGGER`，也没有装回触发器，所以这段摘除和恢复不是产品路径。

`test_same_name_new_bytes_are_two_versions_and_reuse_stays_in_project` 里同项目第三次登记（与第一次相同字节）锁住的是：`registered is True`；四个版本 id 互不相同；`first.object_key == third.object_key`；`first.generation_id == third.generation_id`；第二次不同字节的 `object_key` 与第一次不同；另一项目的同字节 `object_key` 与第一次不同，且两条 key 分别以各自项目前缀开头；本项目该哈希目录下只有第一次的 generation；两个项目的原件都是文件、不是同一文件、字节都是 `BYTES_A`；名为 original 的文件共 3 个；第三次下载的正文是 `BYTES_A`、哈希与第一次相同、版本 id 不同；同一作品的 `version_no` 是 `[1, 2, 3]`，第 1 版和第 3 版的 `object_key` 相同、`content_hash` 都是第一次的哈希。这次读到的该测试断言里没有 `ObjectPinRow`，没有 `consumed_at`，也没有 `fencing_token`。

这两次测试没有断言 `_consume_upload_pin` 在 token 类型不是 `int` 时抛出，也没有分别断言 `expires_at`、`purpose`、`pin_id`、`object_id` 或已经消费过的 pin 各自单独失败。

## 阻塞数

阻塞数：0。

这次阅读和这次运行没有显示：fencing token 对不上时仍能消费 pin 并提交新的作品版本。错位测试把已放下 pin 的 token 加一之后，登记抛出 `RegistrationConflict`，版本集合仍是此前两版，该上传没有已消费的 pin。也没有显示对得上的复用不再消费 pin：成功复用断言 `reuse_pin.consumed_at == NOW + 3.0`，且该 pin 的 `fencing_token` 等于当时 ready 对象的 token。测试暂时丢掉触发器并在退出前装回，不计入阻塞。

## 状态

T05 仍是 implementing。本文件不是完成声明，也不把 T05 标成完成。
