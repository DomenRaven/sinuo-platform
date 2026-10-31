# T06 建议频率合同

版本：`T06-SUGGESTION-1`。日期：2026-10-31。预算复审 blocking 0 之后冻结。这一刀不挂 HTTP，不取消已有运行，不调用模型。

## 迁移

新 revision `0006_suggestion_preferences`，`down_revision` 是 `0005_stored_objects`。不改 `0001` 到 `0005` 的 SQL。`SCHEMA_REVISION` 改为这一版。downgrade 只删除本 revision 新建的表。

两张表：

- `suggestion_preferences`：主键 `(project_id, actor_id)`。列是 `mode`、`scope_json`、`cooldown_seconds`、`digest_interval_seconds`、`revision`、`next_due_at`、`updated_at`。`mode` 只允许 `off`、`manual`、`event`、`digest`。`revision >= 1`。`cooldown_seconds >= 0`。`digest_interval_seconds > 0`。`scope_json` 是 JSON 对象文本。
- `suggestion_triggers`：主键 `(project_id, actor_id, preference_revision, trigger_kind, trigger_key)`。列还有 `outcome`、`job_id`、`created_at`。`trigger_kind` 只允许 `event`、`digest`。`outcome` 只允许 `queued`、`coalesced`、`suppressed`。

当前 head 的测试期望从 `0005_stored_objects` 改到这一版，产品表数量从 41 改为 43。`0005` 文件本身和它的草案 revision 不改。

## 行为

没有偏好行时按 `manual` 处理，不写 Job，也不补插偏好。`save_suggestion_preference` 用 `expected_revision` 做条件更新；修订只加在偏好行上，不改 `Project.revision`。新行默认 `manual`、修订 1、冷却 0、digest 间隔 86400、范围 `{}`。

白名单事件只有 `page.version_created`、`artifact.version_registered`、`test.record_confirmed`。`run`、`candidate`、`stream` 开头的事件不建 Job。

`off` 和 `manual` 的自动触发不建 Job。`event` 在冷却外为白名单事件写一条 `suggestion_trigger` Job 和一条触发记录，`outcome=queued`，并把 `next_due_at` 设为 `now + cooldown_seconds`。同一主键再次触发不建第二个 Job。冷却期内另一条事件写 `suppressed`，不建 Job。并发同一键只有一个 Job。

`digest` 的 `trigger_key` 是当前 UTC 窗口起点的整数秒，窗口长度是 `digest_interval_seconds`。迟到轮询只处理当前窗口，不为漏掉的旧窗口补 Job。

Job 使用已有 `job_kind='suggestion_trigger'`，`status='queued'`。载荷只有 `project_id`、`actor_id`、`preference_revision`、`trigger_kind`、`trigger_key`。同一事务写 Outbox，`event_type='suggestion.trigger_queued'`，载荷同样只有这五项。
