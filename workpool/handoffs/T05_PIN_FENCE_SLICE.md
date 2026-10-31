# T05 复用 pin 的 fencing token

状态保持 implementing。这只给同项目复用的上传 pin 补上放置时记下的 fencing token，不是 T05 完成。

## 对照

合同 `workpool/handoffs/T05_PIN_FENCE_CONTRACT.md`（`T05-PIN-FENCE-1`）。复用 ready 对象时，消费 pin 的条件更新必须再核对 `object_pins.fencing_token`。这个值是放置 pin 之前从该 ready 对象读到的 token。对不上就抛出，调用方事务回滚。新字节仍走 `install_reserved_original`，那条路径没有改。

## 行为

`register_original` 在同项目找到 ready 对象后，先把 `stored_objects.fencing_token` 记到局部变量，再 `place_upload_pin`。写出新的 ArtifactVersion 和 Outbox 之后，`_consume_upload_pin` 的更新条件是：

- `pin_id` 等于刚放下的 pin
- `object_id` 等于该 ready 对象
- `purpose` 为 `upload`
- `consumed_at` 为空
- `expires_at` 大于本次 `now`
- `fencing_token` 等于上面记下的 token

记下的值不是 int，或者更新 `rowcount` 不是 1，都抛 `RegistrationConflict`。函数自己不提交。调用方的 `write_uow` 回滚后，没有新的作品版本，这次 pin 也不会以已消费的状态留下。

成功复用仍把该 pin 的 `consumed_at` 写成本次 `now`。消费后的 pin token 等于 ready 对象当时的 token。不调用 `install_reserved_original`，不链接第二份物理文件。

`object_pins_before_update` 禁止事后修改 `fencing_token`。验收测试只在这一处暂时去掉该触发器，把已经放下的 pin token 加一，再走 `register_original`。登记抛出后版本数不变，该上传没有已消费的 pin。触发器在测试退出前按 `sqlite_master` 里的原语句装回。既有哈希、授权、路径和 GC 断言没有放宽。

## 命令

工作目录是仓库根 `sinuo-platform`。先建 `_施工文件/2026-10-31_grok-dev-a6/99_临时文件`，再执行：

```text
.venv/Scripts/python.exe -m pytest -q tests/acceptance/test_assets_derivatives.py -o cache_dir=_施工文件/2026-10-31_grok-dev-a6/99_临时文件/pytest-cache --basetemp=_施工文件/2026-10-31_grok-dev-a6/99_临时文件/pytest
```

## 退出码

0

## 通过数

34 passed。输出里没有 failed，也没有 skipped。另有 2 条既有警告（符号链接探测线程的 GBK 解码，以及 Starlette 对 httpx 的弃用提示），没有把测试打失败。

## 仍未完成

真实 Blender 预览仍未做。T05 状态仍是 implementing。
