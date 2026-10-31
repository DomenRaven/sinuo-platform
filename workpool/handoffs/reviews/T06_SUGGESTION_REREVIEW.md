# T06 建议频率切片再审

日期：2026-10-31。只读。只写本文件。没有改源代码、测试、迁移、SESSION、BOARD 或任务卡，没有 git commit，没有把 T06 标成 done。

T06 仍是 implementing。本文件不是完成声明。

对照范围：`workpool/handoffs/T06_SUGGESTION_CONTRACT.md`（版本 `T06-SUGGESTION-1`），`workpool/handoffs/T06_SUGGESTION_SLICE.md`，`migrations/versions/0006_suggestion_preferences.py` 的 revision、down_revision、两张新建表和 downgrade，`src/sinuo/workers/suggestions.py`，`tests/acceptance/test_suggestion_preferences.py`。`src/sinuo/infrastructure/models.py` 只核对了 `SCHEMA_REVISION`。`0001` 到 `0005` 只核对了 `revision` 与 `down_revision` 两行，没有重读这些文件的 SQL。

## 命令与退出码

先创建 `_施工文件/2026-10-31_grok-review-suggest/99_临时文件`。再从仓库根 `sinuo-platform` 执行：

```text
.venv/Scripts/python.exe -m pytest -q tests/acceptance/test_suggestion_preferences.py tests/acceptance/test_domain_schema.py tests/acceptance/test_assets_derivatives.py::test_head_includes_stored_object_tables -o cache_dir=_施工文件/2026-10-31_grok-review-suggest/99_临时文件/pytest-cache --basetemp=_施工文件/2026-10-31_grok-review-suggest/99_临时文件/pytest
```

退出码 **0**。输出是 **34 passed**，1 warning，2.73s。没有 failed，没有 skipped。那条 warning 是 `StarletteDeprecationWarning`，来自 `.venv/Lib/site-packages/fastapi/testclient.py` 加载 Starlette `TestClient`。`-q` 没有逐条打印用例名。34 是上面三条目标合在一起的通过数。建议文件里定义的是下面这 10 个测试；`test_domain_schema.py` 和 `test_head_includes_stored_object_tables` 的正文这次没有读，下面不转述它们的断言。

同一仓库根再执行：

```text
.venv/Scripts/python.exe -m alembic heads
```

退出码 **0**。标准输出只有一行：`0006_suggestion_preferences (head)`。这是一个 head。

阅读所见的迁移标识：`0006_suggestion_preferences.py` 的 `revision` 是 `0006_suggestion_preferences`，`down_revision` 是 `0005_stored_objects`。`upgrade` 经 `apply` 执行两段 `CREATE TABLE`，表名是 `suggestion_preferences` 和 `suggestion_triggers`。`DROP_TABLES` 只有 `suggestion_triggers`、`suggestion_preferences`；`downgrade` 只调用 `remove`，按这个元组 `DROP TABLE IF EXISTS`。`models.py` 的 `SCHEMA_REVISION` 是 `0006_suggestion_preferences`。`0001` 到 `0005` 的 revision 行仍是 `0001_primitives`、`0002_lease_invariants`、`0003_domain_identity`、`0004_repair_invariants`、`0005_stored_objects`，down_revision 依次是 `None`、`0001_primitives`、`0002_lease_invariants`、`0003_domain_identity`、`0004_repair_invariants`。这次核对没有看到它们被换成另一个 revision id。

## 测试锁住的行为

`test_revision_registers_suggestion_tables` 断言 `SCHEMA_REVISION == "0006_suggestion_preferences"`，`Base.metadata.tables` 含这两张表，连接上的 `alembic_version.version_num` 等于同一字符串，`sqlite_master` 里也有这两张表。

### 偏好

没有偏好行时，白名单事件不建 Job，也不补插偏好。`test_missing_preference_does_not_queue_a_whitelist_event` 对 `artifact.version_registered` 断言 `created_job is False`、`outcome is None`。提交后的新 session 断言偏好行是 `None`，`job_kind=suggestion_trigger` 的 Job、触发记录、`suggestion.trigger_queued` Outbox 都是空列表，`Project.revision` 仍是插入时的 7。

保存对不上修订时不留行。同一保存测试先用 `expected_revision=5` 包在 `pytest.raises(SuggestionPreferenceConflict)` 里；之后断言偏好行是 `None`，`Project.revision` 仍是 7。

第一次以 `expected_revision=1` 保存为 `event` 时，先插入默认行再条件更新。该测试断言返回值 `revision == 2`、`mode == "event"`。`before_cursor_execute` 记到的偏好写语句种类是 `["INSERT", "UPDATE"]`。插入参数里有 `"manual"`、`"{}"`、`0`、`86400`、`1`，并且 `"event"` 不在插入参数里。紧接着的 UPDATE 语句文本含 `suggestion_preferences`、`revision`、`where`。更新参数里有 `"event"`、`'{"page_ids":["synthetic-page"]}'`、`30`、`3600`、`2`、`1`。捕获到的语句里没有以 `UPDATE` 开头且含 ` projects` 的语句。提交后的行断言 `mode == "event"`、`revision == 2`、`cooldown_seconds == 30`、`digest_interval_seconds == 3600`、`scope_json` 等于上面的范围文本、`next_due_at is None`、`updated_at == NOW`（`1_700_000_000.0`），`Project.revision` 仍是 7，Job 列表为空。

已有行再用 `expected_revision=1` 保存，断言抛出 `SuggestionPreferenceConflict`；行仍是 `mode == "event"`、`revision == 2`、`updated_at == NOW`、`cooldown_seconds == 30`，`Project.revision` 仍是 7。随后 `expected_revision=2` 保存断言返回 `revision == 3`；行上 `revision == 3`、`cooldown_seconds == 40`，`Project.revision` 仍是 7。

`off` 和 `manual` 不建 Job。`test_off_and_manual_create_no_suggestion_job` 对这两个 actor 的白名单事件和 digest 轮询都断言 `created_job is False`。之后断言 Job、触发记录、建议 Outbox 都是空列表。

非法偏好行写不进去。`test_preference_checks_reject_illegal_rows` 对 `mode="always"`、`revision=0`、`cooldown_seconds=-1`、`digest_interval_seconds=0`、`scope_json="[]"` 逐个断言 `pytest.raises(IntegrityError)`。结束后偏好行是 `None`。

### 去重

冷却为 0 的 `event` 模式，同一事件只建一条 Job。`test_event_mode_queues_once_and_a_new_session_sees_that_job` 第一次断言 `created_job is True`、`outcome == "queued"`、`job_id is not None`。同一 `event_id` 再进入，断言 `created_job is False` 且 `job_id` 等于第一次的 id。`run.received` 断言 `created_job is False`、`outcome is None`。新 session 断言 `Project.revision` 仍是 7，Job id 列表等于 `[queued.job_id]`，`status == "queued"`，`job_kind` 等于 `JOB_KIND`，`lease_owner is None`。载荷键集合等于 `project_id`、`actor_id`、`preference_revision`、`trigger_kind`、`trigger_key`。载荷值是该项目、该 actor、`preference_revision == 2`、`trigger_kind == "event"`、`trigger_key` 等于这次的 artifact 事件 id。`input_hash` 等于该载荷 UTF-8 的 SHA-256。触发记录只有一条，`outcome == "queued"`，`job_id` 与 `trigger_key` 对上这条 Job。建议 Outbox 只有一条，`payload` 等于该 Job 载荷。

白名单三条各建一条，三个前缀不建。`test_whitelist_events_queue_and_prefixed_events_do_not` 对 `page.version_created`、`artifact.version_registered`、`test.record_confirmed` 各断言 `created_job is True` 且 `job_id is not None`。对 `run.received`、`candidate.ready`、`stream.reset_required` 各断言 `created_job is False`、`outcome is None`。之后 Job id 集合等于那三次返回的 id，触发记录长度是 3。

两个连接抢同一事件只有一条 Job。`test_two_connections_queue_one_job_for_the_same_event` 断言两个线程都已结束、`errors == []`、`created_job` 排序后是 `[False, True]`、两边的 `outcome` 集合是 `{"queued"}`。新 session 断言 Job 长度是 1、触发记录长度是 1，且这条 Job 的 id 等于 `created_job` 为 True 的那个 `job_id`。

这份测试文件里没有断言 `outcome == "coalesced"`。重复进入锁住的是已有 `queued` 记录还在，并且没有第二条 Job。

### 冷却

冷却期内另一条白名单事件写 `suppressed`，不建第二条 Job。`test_different_event_inside_cooldown_is_suppressed` 在 `cooldown_seconds=100` 下，`NOW + 10` 的第一条断言 `created_job is True`。`NOW + 50` 的另一条事件断言 `created_job is False`、`outcome == "suppressed"`、`job_id is None`。偏好行断言 `next_due_at == NOW + 110`。Job id 列表只有第一条。两条触发记录的 `outcome` 排序后是 `["queued", "suppressed"]`。建议 Outbox 长度是 1，其 `trigger_key` 等于第一条的 artifact 事件 id。

### digest

迟到轮询只为当前 UTC 窗口建一条 Job。`test_late_digest_poll_queues_only_the_current_window` 把间隔存成 100，在 `now=250.0` 断言 `created_job is True`、`trigger_key == "200"`。同一窗口的 `now=299.0` 断言 `created_job is False` 且 `job_id` 等于第一次。之后 Job id 列表只有这一条，触发记录的 `trigger_key` 列表是 `["200"]`，其中没有 `"0"`，也没有 `"100"`。载荷断言 `trigger_kind == "digest"`、`trigger_key == "200"`、`preference_revision == 2`。建议 Outbox 长度是 1。

## blocking 计数

**0**。

这次阅读和这次运行里，没有出现下面六条：

- `off` 或 `manual` 能创建 `suggestion_trigger` Job。`test_off_and_manual_create_no_suggestion_job` 对事件和 digest 都断言 `created_job is False`，Job 列表为空。
- 同一事件创建两条 Job。`test_event_mode_queues_once_and_a_new_session_sees_that_job` 断言第二次 `created_job is False`，Job id 列表只有一条。
- 两个连接都能创建一条 Job。`test_two_connections_queue_one_job_for_the_same_event` 断言只有一个 `created_job is True`，Job 长度是 1，触发记录长度是 1。
- 迟到的 digest 轮询为跳过的窗口创建 Job。`test_late_digest_poll_queues_only_the_current_window` 断言 `trigger_key == "200"`，触发键里没有 `"0"` 和 `"100"`，Job 只有一条。
- 偏好保存改写 `Project.revision`。`test_save_inserts_manual_default_then_updates_without_project_revision` 在冲突、首次保存和再次保存之后都断言 `Project.revision` 仍是 7，并断言捕获语句里没有更新 `projects` 的 UPDATE。缺偏好用例和事件去重用例也断言该修订仍是 7。
- Alembic 多于一个 head。`alembic heads` 退出码 0，输出只有 `0006_suggestion_preferences (head)`。

## 不计入 blocking、也没有收口

建议偏好的 HTTP 路由、偏好变更时取消已经排队的自动运行、真实模型调用、真实预览转换，不在上面的 blocking 定义里。这次读到的建议测试没有锁住它们，这次运行也没有把它们收口。

T06 仍是 implementing。本文件不是完成声明。
