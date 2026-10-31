# T05/T06 共享信封

版本：`T05-T06-ENVELOPE-1`。日期：2026-10-31。集成 owner 独占迁移、`models.py`、`app.py` 和依赖锁。

## 事件

原件登记成功时，在同一短事务写 Outbox：

- `event_type`: `artifact.version_registered`
- `schema_version`: 1
- `payload` 只有 `project_id`、`artifact_id`、`artifact_version_id`、`content_hash`、`generation_id`、`byte_size`
- 不放磁盘路径、口令或原文

派生任务用已经存在的 `jobs` 表，`job_kind='derivative'`。输入哈希是原件内容哈希。claim / renew / finalize 必须是条件更新，核对 owner、fencing token、期限和输入哈希。执行转换在事务外。过期租约不能写回。

## 文件

| 写入者 | 路径 |
| --- | --- |
| 开发 A（T05） | `src/sinuo/application/assets/`、`src/sinuo/infrastructure/assets/`、`tests/acceptance/test_assets_derivatives.py`、`workpool/handoffs/proposals/0005_stored_objects.draft.py` |
| 开发 B（T06） | `src/sinuo/workers/jobs.py`、`src/sinuo/api/events/`、`tests/acceptance/test_durable_tasks_events.py` |
| 集成 owner | `migrations/versions/`、`app.py`、公共路由注册 |

T05 可以先把上传票据和对象表写成迁移草稿，不要自己放进 `migrations/versions/`。字节写入必须在数据库事务外。路径含 `..` 或跳出配置根必须拒绝。
