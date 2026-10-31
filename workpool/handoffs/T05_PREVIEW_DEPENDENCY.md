# T05 预览依赖决定

日期：2026-10-31。集成 owner。本文件只决定这一刀不做什么。

`docs/construction/06_SECURITY_STORAGE_RECOVERY.md` 已指定三类 P0 预览：Pillow 栅格、`pypdfium2` 的 PDF、Blender CLI 的 GLB。缺工具时必须是 `unavailable`，不能用占位图冒充预览。

这一刀不把这三项写进 `pyproject.toml`，也不实现真实渲染。原因：登记还没接到 pin；依赖锁归集成 owner；Blender 不是 pip 包。真实小夹具预览留到 pin 登记回归通过之后的单独切片。

下载路由同样留到下一刀。`tests/acceptance/test_assets_derivatives.py` 里 `test_mounted_download_route_sets_nosniff_and_attachment` 当前断言 `create_app` 不含原件内容路径。挂路由必须和这条断言同一改动，且 `app.py` 只由集成 owner 改。
