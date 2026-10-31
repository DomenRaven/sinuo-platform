# T06 偏好变更取消自动运行

日期：2026-10-31。T06 状态保持 implementing。本切片不把任务标成 done。

## 对照

冻结合同 `workpool/handoffs/T06_POLICY_CANCEL_CONTRACT.md`，版本 `T06-POLICY-CANCEL-1`。施工文档 `docs/construction/05_WORKFLOW_AND_CONCURRENCY.md` 的「自动运行期间更改偏好」：自动运行保存 `origin=policy` 和 `preference_revision`；偏好修改事务使尚未结束的自动运行停止接受结果并转为取消。手动运行不跟着取消。偏好修订不改变 `Project.revision`。

本仓库没有单独的文献库。上面这份合同和施工节就是本切片的依据。

## 选型

新迁移 `0007_run_origin`，`down_revision` 是 `0006_suggestion_preferences`。`0001` 到 `0006` 的 SQL 没有改。`SCHEMA_REVISION` 改为这一版。不新建表，产品表仍是 43 张。

`agent_runs.origin` 只允许 `manual` 或 `policy`，数据库默认 `manual`，ORM 的 `insert_default` 也是 `manual`。`preference_revision` 可空。`manual` 必须为空，`policy` 必须大于等于 1。原来的具名 `INSERT INTO agent_runs` 不列出这两列，仍然能插入。

`0003` 按当前 ORM 创建 `agent_runs`。全新升级时这两列已经在表里，`0007` 先看 `PRAGMA table_info`，缺了才 `ALTER TABLE`。已经停在 `0006` 的库没有这两列，会走 `ALTER`。没有把这对规则写成跨列 `CHECK`：SQLite 在约束还点着列名时 `DROP COLUMN` 会失败。规则放在本修订的两个触发器里。downgrade 先删这两个触发器，再删这两列。

取消写在 `save_suggestion_preference` 里，偏好修订条件更新成功之后、调用方 `write_uow` 提交之前。一条条件更新只匹配该项目、该 actor、`origin=policy`、状态为 `queued` 或 `running` 的行，把 `execution_status` 改成 `cancelled`，`accepting_results` 改成 0。没有这样的行时不发出 `agent_runs` 的 `UPDATE`。没有改 `cancel_run` 和 `publish_run`。不逐条调用 `cancel_run`，因为它在匹配不到开放行时抛 `StaleLease`，会把同一次偏好保存一起回滚，而且它不区分 `origin`。

## 行为

成功保存并把偏好修订加 1 之后，该项目、该 actor 仍在排队或运行中的 policy 运行变为 `cancelled`，`accepting_results` 为 0。同一 actor 的 manual 运行保持 `queued` 且 `accepting_results` 为 1。另一个 actor 的 policy 运行保持 `queued`。`Project.revision` 不变。

对已经取消的 policy 运行调用 `publish_run` 抛 `StaleLease`，该行不变。下一次成功保存不再发出碰这条已取消运行的 `UPDATE`。

现有偏好 HTTP 调用的是同一个 `save_suggestion_preference`，因此会走进这次的取消。这次没有改路由，授权测试也没有跑 HTTP。

## 证据

先创建 `--basetemp` 的父目录 `_施工文件/2026-10-31_grok-dev-b3/99_临时文件`，再从仓库根执行：

```text
.venv/Scripts/python.exe -m pytest -q tests/acceptance/test_policy_cancel.py tests/acceptance/test_suggestion_preferences.py tests/acceptance/test_domain_schema.py tests/acceptance/test_assets_derivatives.py::test_head_includes_stored_object_tables -o cache_dir=_施工文件/2026-10-31_grok-dev-b3/99_临时文件/pytest-cache --basetemp=_施工文件/2026-10-31_grok-dev-b3/99_临时文件/pytest
```

退出码 0。37 passed。

`.venv/Scripts/python.exe -m alembic heads` 退出码 0。标准输出只有一行：`0007_run_origin (head)`。这是一个 head。

## 还没做

不创建自动运行，不调用模型，不取消已经发出的网络请求。晚到结果只留诊断、以及按新策略重新排队，都没有做。T06 不标 done。
