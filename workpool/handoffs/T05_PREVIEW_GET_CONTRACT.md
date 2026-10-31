# T05 预览读取合同

版本：`T05-PREVIEW-GET-1`。日期：2026-10-31。预览 POST 复审 blocking 0 之后才做。

`GET /api/v1/artifact-versions/{artifact_version_id}/preview` 读取已经生成的派生 PNG。先按当前 `preview.read` 授权。GET 不调用渲染，不打开 `write_uow`，也不在文件缺失时补写。

有权且文件存在时返回 PNG 字节，`nosniff`，`Cache-Control: no-store`。响应头里的文件名固定为 `preview.png`，不使用作品标题。无权、匿名或版本不存在时，响应不含标题、预览字节和磁盘路径。文件不存在时同样不泄露标题。

不改 POST 的渲染行为，不改迁移。Blender 仍不安装。
