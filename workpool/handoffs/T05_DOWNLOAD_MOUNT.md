# T05 下载路由挂载

日期：2026-10-31。集成 owner。pin 复审 `T05_PIN_REREVIEW.md` 的 blocking 是 0 之后才改入口。

`create_app` 现在挂上原件下载路由。OpenAPI 含 `/api/v1/artifact-versions/{artifact_version_id}/content`。对象目录仍由调用方写到 `app.state.objects_root`；没有这个目录时，已授权读取得到不可读原件，不返回磁盘路径。

包含路由的 FastAPI 把路由放在 `_IncludedRouter` 上，顶层 `route.path` 不是这条 URL。测试改成断言 OpenAPI，并用 `create_app` 跑下载、403、404 和 401。

## 命令

工作目录 `sinuo-platform`。

```text
.venv/Scripts/python.exe -m ruff check src/sinuo/api/app.py src/sinuo/api/assets/routes.py tests/acceptance/test_assets_derivatives.py
.venv/Scripts/python.exe -m pytest -q tests/acceptance/test_assets_derivatives.py -o cache_dir=_施工文件/2026-10-31_grok-integrate/99_临时文件/pytest-cache-assets --basetemp=_施工文件/2026-10-31_grok-integrate/99_临时文件/pytest-assets
```

ruff 退出码 0。pytest 退出码 0，33 passed。另有既有的符号链接线程 GBK 警告和 Starlette 弃用警告。

真实 Pillow、PDF、GLB 预览仍未做。T05 仍是 implementing。
