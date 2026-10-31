# T05 登记审计

版本：`T05-REGISTER-AUDIT-1`。日期：2026-10-31。派生预览复审 blocking 0 之后才做。

成功的 `register_original` 在同一次调用方事务里写 `audit_events`。`actor_id` 用票据上的 `created_by_account_id`。`command` 是 `artifact.version_registered`。`subject_type` 是 `artifact_version`，`subject_id` 是新版本 id。`project_revision` 是这次登记推进后的修订。`payload` 只有 `project_id`、`artifact_id`、`artifact_version_id`、`content_hash`、`generation_id`、`byte_size`，不含磁盘路径和原件字节。

哈希、长度或越权失败不写审计。不改迁移，不改预览和任务代码。
