# T06 建议频率切片

日期：2026-10-31。T06 状态保持 implementing。本切片不把任务标成 done。

## 对照

冻结合同 `workpool/handoffs/T06_SUGGESTION_CONTRACT.md`，版本 `T06-SUGGESTION-1`。施工文档 `docs/construction/05_WORKFLOW_AND_CONCURRENCY.md` 的「主动建议频率与触发合同」：偏好属于 `(project_id, actor_id)`，CAS 只增加这条偏好自己的修订；事件只消费白名单；digest 使用 UTC 时间窗，轮询迟到时不补历史窗口；触发主键是 `(project_id, actor_id, preference_revision, trigger_kind, trigger_key)`。

本仓库没有单独的文献库。上面这份冻结合同和施工节就是本切片的依据。

## 选型

新迁移 `0006_suggestion_preferences`，`down_revision` 是 `0005_stored_objects`。`0001` 到 `0005` 的 SQL 没有改。`SCHEMA_REVISION` 改为这一版。downgrade 只删除 `suggestion_triggers` 和 `suggestion_preferences`。

ORM 在 `src/sinuo/infrastructure/suggestions.py`。`models.py` 在领域表清单检查通过之后导入该模块，`Base.metadata` 因此包含这两张表，`/readyz` 能发现缺表。若 `domain_models` 先被导入，清单检查仍按原来的 38 张表执行，避免启动时把这两张表当成清单漂移。

行为在 `src/sinuo/workers/suggestions.py`。调用方持有 `write_uow`，函数内部不提交。本切片没有新增 HTTP 路由。

## 行为

没有偏好行时按 manual 处理。白名单事件不写 Job，不写触发记录，也不补插偏好。

`save_suggestion_preference` 在没有行、且 `expected_revision` 为 1 时，先插入默认行：manual、修订 1、冷却 0、digest 间隔 86400、范围 `{}`。随后按 `expected_revision` 做条件更新，成功后修订变为 2。之后每次成功保存都把对上的修订加 1。对不上时抛 `SuggestionPreferenceConflict`。没有行时不会留下半截默认行；已有行时列保持原样。这个异常要让 `write_uow` 退出，不要在同一个写单元里接住它。保存不读、不写 `Project.revision`。

`off` 和 `manual` 不建 `suggestion_trigger` Job，也不写触发记录。`event` 在冷却外为白名单事件写一条 `job_kind=suggestion_trigger`、`status=queued` 的 Job，以及一条 `outcome=queued` 的触发记录，并把 `next_due_at` 设为 `now + cooldown_seconds`。同一主键再次进入不建第二个 Job，已有的 `queued` 记录保持不变。冷却期内另一条白名单事件写 `outcome=suppressed`，`job_id` 为空，不建 Job。两个连接抢同一个事件，只有一个 Job。

白名单是 `page.version_created`、`artifact.version_registered`、`test.record_confirmed`。以 `run`、`candidate`、`stream` 开头的事件不建 Job。

`digest` 的 `trigger_key` 是当前 UTC 窗口起点的整数秒，窗口长度是 `digest_interval_seconds`。迟到两档只为当前窗口建一条 Job，不为跳过的窗口补 Job。同一窗口里的下一次轮询不建另一条 Job。

Job 载荷和同一事务里的 Outbox（`event_type=suggestion.trigger_queued`，`schema_version=1`）都只有 `project_id`、`actor_id`、`preference_revision`、`trigger_kind`、`trigger_key`。事务提交后，新的 session 读到同一条 Job。

`coalesced` 仍是触发结果的合法取值。本切片的冷却路径写 `suppressed`。同一主键不能再插一行，所以重投不把已有的 `queued` 改成 `coalesced`。

## 证据

先创建 `--basetemp` 的父目录 `_施工文件/2026-10-31_grok-dev-b2/99_临时文件`，再从仓库根执行：

```text
.venv/Scripts/python.exe -m pytest -q tests/acceptance/test_suggestion_preferences.py tests/acceptance/test_domain_schema.py tests/acceptance/test_assets_derivatives.py::test_head_includes_stored_object_tables -o cache_dir=_施工文件/2026-10-31_grok-dev-b2/99_临时文件/pytest-cache --basetemp=_施工文件/2026-10-31_grok-dev-b2/99_临时文件/pytest
```

退出码 0。34 passed。

## 还没做

建议偏好的 HTTP 路由、偏好变更时取消已经排队的自动运行、真实模型调用。T06 不标 done。
