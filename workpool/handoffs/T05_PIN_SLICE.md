# T05 pin 切片

状态保持 implementing。这只是登记接入 reservation / pin / install 的一刀，不是 T05 完成。

## 命令

工作目录是仓库根 `sinuo-platform`。pytest 不会创建 `--basetemp` 的父目录，先建 `_施工文件/2026-10-31_grok-dev-a/99_临时文件`，再执行：

```text
.venv/Scripts/python.exe -m pytest -q tests/acceptance/test_assets_derivatives.py -o cache_dir=_施工文件/2026-10-31_grok-dev-a/99_临时文件/pytest-cache --basetemp=_施工文件/2026-10-31_grok-dev-a/99_临时文件/pytest
```

## 退出码

0

## 通过数

33 passed。输出里没有 failed，也没有 skipped。另有 2 条既有警告（符号链接探测线程的 GBK 解码，以及 Starlette 对 httpx 的弃用提示），没有把测试打失败。

## 行为变化

`register_original` 的公开参数不变，函数自己不提交，用的是调用方已经打开的 `write_uow`。attempt 只在开头用现有 `digest_file` 哈希一次，不在 `install_reserved_original` 里再哈希。

新字节：`reserve_generation`，再 `place_upload_pin`（到期 `now + 3600.0`），再 `install_reserved_original`。安装没有成功就抛出，让调用方事务回滚，不返回可能被提交的软失败。安装成功后写入 ArtifactVersion 和原来的 Outbox 字段，然后关闭票据。对象键格式不变。

同项目复用：只复用 `state` 为 `ready` 的 stored object。先放上传 pin，写新的 ArtifactVersion 和 Outbox，再用条件更新消费该 pin（写 `consumed_at`；匹配 `pin_id`、`object_id`、`purpose=upload`、未消费、未过期）。`rowcount` 不是 1 就抛出。不链接第二份物理文件，也不调用只接受 `reserved` 的 `install_reserved_original`。对象不是 ready，或 pin 放不上去，就分配新 generation。不会安装到 `gc_claimed` 或 `deleted`。

传入 `principal` 时，`ticket.created_by_account_id` 必须等于该 principal 的 `account_id`。不符则未登记，原因 `ticket_owner`，票据保持 open，不插入版本。不传 principal 的既有调用仍会登记。

哈希、长度、票据状态、授权失败的含义不变：没有可引用的 ArtifactVersion，也没有 `stored_objects` 行。

调用方在 `register_original` 返回之后、提交之前抛出：数据库行回滚，`discard_unregistered_original` 仍能删除已经链接的孤儿字节。已经提交的 generation 因为被引用而留下，仍可下载。

## 仍未完成

真实 Pillow、PDF、GLB 渲染没有做，也没有新增依赖。下载路由没有挂到 `create_app`。预览仍只给出状态，不生成预览文件。
