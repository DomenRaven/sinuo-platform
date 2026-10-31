# T06 自动运行取消合同

版本：`T06-POLICY-CANCEL-1`。日期：2026-10-31。偏好 HTTP 已经挂上之后才做这一刀。

## 迁移

新 revision `0007_run_origin`，`down_revision` 是 `0006_suggestion_preferences`。不改 `0001` 到 `0006` 的 SQL。`SCHEMA_REVISION` 改为这一版。不新建表，产品表数量仍是 43。

`agent_runs` 增加两列：

- `origin`：只允许 `manual` 或 `policy`。已有行默认 `manual`。数据库和 ORM 都要有这个默认，现有的具名 `INSERT INTO agent_runs (...)` 不用改。
- `preference_revision`：可空整数。`manual` 必须是空。`policy` 必须大于等于 1。

downgrade 只删除这两列。

当前 head 断言从 `0006_suggestion_preferences` 改到 `0007_run_origin`。`0006` 文件里的 revision id 不改。

## 行为

`save_suggestion_preference` 在同一调用方事务里，偏好修订增加之后，取消该项目、该 actor 仍在排队或运行中的 `origin=policy` 运行。取消是条件更新：`execution_status` 变成 `cancelled`，`accepting_results` 变成 0。不改 `manual` 运行，不改已经取消或完成的运行，不改 `Project.revision`。

这一刀不创建自动运行，不调用模型，不改 HTTP 路径。
