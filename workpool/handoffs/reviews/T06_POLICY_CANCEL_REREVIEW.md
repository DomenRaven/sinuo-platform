# T06 策略运行取消复审

日期：2026-10-31。本次只读了指定交接、`migrations/versions/0007_run_origin.py`、`save_suggestion_preference` 里的取消更新，以及 `tests/acceptance/test_policy_cancel.py`，并核对 `SCHEMA_REVISION`。没有打开 `0001` 到 `0006`。没有改源码、测试、迁移、SESSION、BOARD 或任务卡。

## 命令与退出码

从仓库根执行：

```text
.venv/Scripts/python.exe -m pytest -q tests/acceptance/test_policy_cancel.py -o cache_dir=_施工文件/2026-10-31_grok-review-policy/99_临时文件/pytest-cache --basetemp=_施工文件/2026-10-31_grok-review-policy/99_临时文件/pytest
```

标准输出：`3 passed in 0.40s`。退出码 0。

```text
.venv/Scripts/python.exe -m alembic heads
```

标准输出只有一行：`0007_run_origin (head)`。退出码 0。这次输出是一个 head。

`0007_run_origin.py` 里 `revision = "0007_run_origin"`，`down_revision = "0006_suggestion_preferences"`。两列是 `origin VARCHAR(16) NOT NULL DEFAULT 'manual'` 和可空的 `preference_revision INTEGER`。两个触发器是 `agent_runs_origin_before_insert` 和 `agent_runs_origin_before_update`，插入和更新都要求 `manual` 且 `preference_revision` 为空，或 `policy` 且 `preference_revision` 非空并且大于等于 1。`downgrade` 调用的 `remove` 先删除这两个触发器，再在列还在时删除 `preference_revision` 和 `origin`。这个函数里没有删除其他表或其他列。`src/sinuo/infrastructure/models.py` 的 `SCHEMA_REVISION` 是 `0007_run_origin`。

`save_suggestion_preference` 在偏好行条件更新的 `rowcount == 1` 之后调用 `_cancel_open_policy_runs`。那次更新的条件是同一 `project_id`、同一 `initiated_by_account_id`、`origin == "policy"`、`execution_status` 属于 `("queued", "running")`，并把 `execution_status` 写成 `cancelled`、`accepting_results` 写成 0。没有匹配行时，函数在发出这条 `UPDATE` 之前返回。这段读取里的更新没有写 `Project.revision`。

## 测试锁住的取消行为

`test_preference_save_cancels_that_actors_queued_policy_run` 把下面这些结果钉在断言上：

- 保存前 `assert _revision(session) == 7`。
- 保存前该 actor 的 manual 行：`origin == "manual"`，`preference_revision is None`，`execution_status == "queued"`，`accepting_results == 1`。
- 保存前另一 actor 的 policy 行：`execution_status == "queued"`，`accepting_results == 1`。
- 保存前该 actor 的 policy 行：`origin == "policy"`，`preference_revision == 1`。
- `_save(..., expected_revision=1)` 之后 `saved.revision == 2`。这里的 `revision` 来自 `save_suggestion_preference` 的返回值。
- 保存后该 actor 的 policy 行：`execution_status == "cancelled"`，`accepting_results == 0`。除这两列以外，该行其余列与保存前相等。
- 保存后该 actor 的 manual 行与保存前整行相等。
- 保存后另一 actor 的 policy 行与保存前整行相等。
- 保存后 `assert _revision(session) == 7`。
- 对这条已取消的 policy 运行调用 `publish_run`，断言抛出 `StaleLease`。随后该行与取消后的快照相等，并且 `assert _revision(session) == 7`。
- 下一次 `_save(..., expected_revision=2, cooldown_seconds=30, now=NOW + 5)` 之后 `again.revision == 3`。`_updates_touching_run(statements, POLICY_RUN_ID) == []`，所以这次成功保存没有再发出碰到该运行的 `UPDATE`。随后该 policy 行、manual 行、另一 actor 的 policy 行仍与对应的保存前快照相等，并且 `assert _revision(session) == 7`。

同一次 pytest 里另外两个测试也通过。它们锁的是插入默认值和触发器，不是取消：`test_named_insert_defaults_origin_to_manual` 断言不写这两列的具名 `INSERT` 得到 `origin == "manual"`、`preference_revision is None`，并且 `execution_status == "queued"`、`accepting_results == 1`。`test_origin_pair_rejects_manual_revision_and_policy_without_one` 断言 `origin="policy"` 且 `preference_revision is None`、以及 `origin="manual"` 且 `preference_revision == 1` 都抛出 `IntegrityError`，两行随后都不存在。

本测试文件没有 `running` 状态的运行，也没有对应断言。本次运行没有覆盖创建自动运行、真实模型调用、取消已经发出的网络请求、HTTP 路由，以及按新偏好重新排队。

## 阻塞计数

阻塞 0。

这次阅读和这次运行里，被断言的那条排队中的 policy 运行在偏好保存后是 `cancelled` 且 `accepting_results == 0`。同一 actor 的 manual 行保持保存前的整行。另一 actor 的 policy 行保持保存前的整行。`Project.revision` 在保存前、第一次保存后、`publish_run` 抛错后、第二次保存后都是 7。按本次给定的阻塞含义，这四项没有出现。

创建自动运行、真实模型调用、取消已经发出的网络请求、HTTP 路由变化、按新偏好重新排队，这次都没有被跑到，也不记成已关闭。代码过滤条件包含 `running`，本测试文件没有锁住一条 `running` 行。

## 任务状态

T06 仍是 implementing，本文件不是完成声明。
