# T05 登记后渲染合同

版本：`T05-REGISTER-PREVIEW-1`。日期：2026-10-31。policy 取消复审 blocking 0 之后才做。

`register_original` 仍在调用方的 `write_uow` 里，只做登记和 pin。它继续调用 `preview_disposition`，不调用 `render_original_preview`。文件转换不能放进数据库事务。

新函数在事务提交之后读取已经落盘的原件，再调用 `render_original_preview`。成功时派生目录里有 PNG。渲染失败、超限或工具不可用时不写预览，原件仍可按原哈希下载。不改原件字节，不画占位图，不启动 shell。

不改迁移，不改 `0007` 的 head 断言。
