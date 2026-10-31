# T06 派生预览执行

版本：`T06-DERIVATIVE-PREVIEW-1`。日期：2026-10-31。派生文件复审 blocking 0 之后才做。

一条 `job_kind=derivative` 的任务在认领提交之后，于事务外读取已落盘原件并渲染。成功时把 PNG 写进 `jobs/<job_id>/<fencing_token>/result`，再在调用方的 `write_uow` 里用现有 `finalize_job` 发布这条相对路径。

渲染失败不发布成功指针，也不写占位图。像素超限或 SVG 用现有的不可重试结束。没有 Blender 的 GLB 用现有的 `unavailable` 结束，测试把次数上限设成当前 attempt，使它失败而不是无限排队。

不改 `finalize_job` 的 WHERE，不改迁移，不调用模型。
