# T06 派生文件与旧租约

版本：`T06-DERIVATIVE-ATTEMPT-1`。日期：2026-10-31。重启投递复审 blocking 0 之后才做。

转换写文件发生在数据库事务外。文件只能落在调用方给出的根目录下的 `jobs/<job_id>/<fencing_token>/result`。路径段拒绝 `..`。

登记有效结果仍用现有的 `finalize_job`。`output_ref` 是这条相对路径。旧 fencing token 的 finalize 失败，不能改掉新 token 已经写下的 `output_ref`。旧文件可以留在自己的 token 目录里，但不能成为新的有效指针。

不改 `finalize_job` 的条件，不改迁移，不调用模型。
