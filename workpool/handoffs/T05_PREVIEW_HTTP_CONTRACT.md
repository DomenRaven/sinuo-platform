# T05 预览 HTTP 合同

版本：`T05-PREVIEW-HTTP-1`。日期：2026-10-31。登记后渲染复审 blocking 0 之后才做。

`create_app` 已挂资产路由。新增 `POST /api/v1/artifact-versions/{artifact_version_id}/preview`。先在短只读会话里按当前 `preview.read` 授权，读出版本的项目、哈希、generation 和媒体类型，然后关闭会话。渲染调用 `render_registered_preview`，不打开 `write_uow`。

像素上限由服务端固定，不接受客户端传入。派生相对路径由服务端生成，不能含用户提供的路径。响应只含 `generated`、`state`、`reason`、`generator_name`、`generator_version`，不含磁盘路径。没有 `objects_root` 或 `derived_root` 时不渲染，返回不可用，也不泄露路径。

不改 `register_original`，不改迁移。Blender 仍不安装。
