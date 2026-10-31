# T05 登记后渲染切片

状态保持 implementing。这只是登记提交之后再渲染的一刀，不是 T05 完成。

## 对照

合同 `workpool/handoffs/T05_REGISTER_PREVIEW_CONTRACT.md`（`T05-REGISTER-PREVIEW-1`）和 `docs/construction/06_SECURITY_STORAGE_RECOVERY.md`。06 写明数据库与文件系统不能组成单一事务，事务成功后才可引用，预览失败不影响原件取回。本仓库没有单独的预览理论条目。

新函数是 `render_registered_preview`，放在 `src/sinuo/application/assets/registered_preview.py`。它复用 `read_original` 和 `render_original_preview`。不改 `register_original`，因此登记仍然只调用 `preview_disposition`。不改 `preview_disposition` 和 `run_preview_converter`。不改迁移，也不改已有的 `0007_run_origin` head 断言。

## 行为

调用方先在 `write_uow` 里完成登记并退出，事务提交之后再调用新函数。渲染不在 `write_uow` 里面。新函数不打开数据库会话，也不提交。它按项目、内容哈希和 generation 读取已经落盘的原件字节，再交给 `render_original_preview`。返回值就是那次渲染的结果。

小 PNG 成功时，派生目录里有一张 PNG。超过调用方像素上限时不写预览文件。SVG 拒绝和没有 Blender 的 GLB 仍由 `render_original_preview` 返回，同样不写文件。原件字节只读，仍可经现有的 `download_original` 按原哈希下载。不画占位图，不启动 shell。

## 命令

工作目录是仓库根 `sinuo-platform`。先建 `_施工文件/2026-10-31_grok-dev-a3/99_临时文件`，再执行：

```text
.venv/Scripts/python.exe -m pytest -q tests/acceptance/test_registered_preview.py tests/acceptance/test_preview_render.py -o cache_dir=_施工文件/2026-10-31_grok-dev-a3/99_临时文件/pytest-cache --basetemp=_施工文件/2026-10-31_grok-dev-a3/99_临时文件/pytest
```

新测试使用既有的 `assets` 夹具。`write_uow` 只用于登记。渲染调用写在该块退出之后。一条是小 PNG 变成 PNG 预览，一条是超限 PNG：派生目录为空，原件仍可下载。没有断言新的 Alembic head。

## 退出码

0

## 通过数

9 passed。输出里没有 failed，也没有 skipped。

## 仍未完成

Blender 预览仍未做。没有占位图。T05 状态仍是 implementing。
