# T03 修复复审

日期：2026-10-30。只读。对照 ADR-004、共享合同 `T02-T03-SHARED-2` 第 1 节第 14–17 条和第 6 节、`migrations/versions/0004_repair_invariants.py`、`migrations/versions/0003_domain_identity.py` 里被 0004 重装的触发器、`src/sinuo/application/cas.py`、`src/sinuo/infrastructure/database.py` 的登录名函数注册，以及 `tests/acceptance/test_domain_schema.py` 的终态、登录名、候选消费测试。

`T03_SCHEMA_REVIEW.md` 保留。本文不改代码、测试或迁移。

临时库在 `_施工文件/2026-10-30_grok/99_临时文件/`。验收命令使用项目 `.venv` 的解释器：

`.venv\Scripts\python.exe -m pytest tests/acceptance/test_domain_schema.py --basetemp <上述目录>\pytest`

退出码 **0**。23 passed。一条 Starlette/httpx 弃用警告，不改变退出码。

另用 `build_engine` 建了两份库：空库升到 head；先升到 `0003_domain_identity`，把五条触发器换成上一份审查记录里的旧正文，再升到 head。下面的行值和错误字符串来自这两次执行。

结论：**blocking 0 条，note 6 条。** 普通 UPDATE 不能把终态 `run_tasks` 绕回 `running`。

## note

### 1. `run_tasks` 终态不能再经 `retry_wait` 或 `blocked` 绕回，也不能改 fencing token

- 标记：note
- 文件：`migrations/versions/0003_domain_identity.py` 第 343–348 行。`0004` 在第 77–81 行按这个正文重装 `run_tasks_before_update`。
- 合同：共享合同第 6 节。终态是 `succeeded`、`failed`、`cancelled`。终态上的状态、fencing token 和租约都不能再改。`retry_wait` 回到 `queued` 仍允许。

触发器在旧状态属于这三个终态，且新状态、fencing token、`lease_owner`、`lease_until` 任一与旧值不同时 `RAISE(ABORT, 'run_tasks_terminal')`。`retry_wait` 和 `blocked` 都算状态变化。

head 库上，`synthetic-task-1` 先进入 `succeeded`，随后四条普通 UPDATE 都中止，错误都是 `run_tasks_terminal`：

- `status = 'retry_wait'`
- `status = 'blocked'`
- `status = 'running'`，同时写 `fencing_token = 4`、`lease_owner`、`lease_until`
- 只写 `fencing_token = 4`

行保持 `succeeded`，`fencing_token` 为 0，租约两列为空。

`synthetic-task-2` 从 `queued` 进入 `retry_wait`，再回到 `queued`，两步都成功。合同里的重试路径还在。

验收 `test_terminal_task_and_draft_content_reject_ordinary_updates` 覆盖了 `retry_wait` 和带新 token 的 `running`。`blocked` 和单独改 token 由上面的临时库补上，结果与触发器正文一致。

把库里的触发器换成旧正文后，`succeeded` → `retry_wait` → `running`（token 为 4）两步都成功。升到 head 之后，另一条在升级前写成 `succeeded` 的任务走同样的四条语句，全部 `run_tasks_terminal`，行保持 `succeeded`、token 0。升级替换的是触发器正文，不把已经写成 `running` 的旧行改回去。当前仓库里的 `0003` 源文件已经是新正文；ADR-004 写明没有带着旧正文部署过的库。

### 2. 草稿正文要伴随后修订加 1，头指针不能清空，项目两个计数器不能在同一条 UPDATE 里一起加 1

- 标记：note
- 文件：`page_drafts_before_update` 第 260–264 行，`pages_before_update` 第 245–246 行，`stages_before_update` 第 223–225 行，`projects_before_update` 第 161–163 行。`0004` 重装这四条。
- 合同：共享合同第 1 节第 12 条。`access_epoch + 1` 用于已有项目的授权变更，不加 `projects.revision`。

head 库上的结果：

- 只改 `page_drafts.content_json` 和 `content_hash`，中止，错误 `page_drafts_content_revision`。只改 `base_version_id`，同一错误。同一条语句里把 `content_revision` 加 1 并改正文，成功，修订从 5 变成 6。
- `pages.head_version_id` 和 `stages.head_config_version_id` 改成 NULL，分别中止于 `pages_head_required`、`stages_head_required`。头指针仍是 `synthetic-pv-a2` 和 `synthetic-cfg-3`。
- `projects` 同一条语句把 `revision` 和 `access_epoch` 都加 1，中止，错误 `projects_counters_coupled`。随后只加 `revision` 成功，再只加 `access_epoch` 成功，计数从 `(4, 1)` 变成 `(5, 2)`。

验收测试覆盖了草稿正文不加修订，以及页头指针改 NULL。阶段头指针和两个项目计数器由临时库补上。

旧正文下，草稿可以在修订仍为 5 时改掉，两个头指针可以改成 NULL，两个项目计数器可以在同一条 UPDATE 里从 `(4, 1)` 变成 `(5, 2)`。升到 head 之后这三组语句分别中止于 `page_drafts_content_revision`、`pages_head_required`、`stages_head_required`、`projects_counters_coupled`。已写下的旧值保持原样，修订停在 5，项目计数停在 `(5, 2)`，头指针在升级前已恢复，升级后仍是原来的版本 id。

### 3. N2：无 WHERE 的更新在每一行都合法时仍会改多行；`consume_candidate` 只改一行，失败由 `write_uow` 回滚

- 标记：note
- 文件：`candidates_before_update` 仍是行级条件，见 `0003_domain_identity.py` 第 380–400 行。帮助函数在 `src/sinuo/application/cas.py` 第 13–36 行。回滚在 `src/sinuo/infrastructure/database.py` 第 73–75 行。
- 合同：ADR-004 第 1 条，共享合同第 1 节第 14 条。触发器看不见 SQL 有没有 WHERE。单目标消费走 `consume_candidate`。影响行数不是 1 就抛 `CasMismatch`，由 `write_uow` 回滚整笔事务。

T09 仍未实现候选确认命令。`consume_candidate` 是这条共享写入边界，供 T09 调用。

head 库上两行都是 `pending` 时，执行：

```sql
UPDATE candidates SET status = 'committed', revision = revision + 1
```

`synthetic-candidate` 和 `synthetic-candidate-2` 都变成 `committed`、`revision = 2`。

其后库里已有终态行，再执行同一条无 WHERE 语句，整句中止，错误 `candidate_update_forbidden`。新建的两行保持 `pending`、`revision = 1`。无 WHERE 会改多行，发生在被更新的每一行都满足行级条件的时候。

验收 `test_consume_candidate_targets_one_row_and_rolls_back` 用的是 `WHERE status = 'pending'`，没有候选 ID。这条语句把当时所有 pending 行一起提交。测试断言两行都是 `committed`，退出码 0，与这次无 WHERE 的结果一致。

`consume_candidate` 的 WHERE 带候选 ID、项目、`pending` 和预期修订，并把修订写成预期值加 1。临时库里它把 `synthetic-candidate-3` 收成 `committed`、`revision = 2`，`synthetic-candidate-4` 保持 `pending`、`revision = 1`。

同一 `write_uow` 里先插入 `outbox_events.event_id = 'synthetic-cas-event'`，再按预期修订 1 消费已经提交的候选。函数抛出 `CasMismatch: synthetic-candidate-3`。事务结束后该事件行数是 0，候选仍是 `committed`、`revision = 2`。

### 4. N4：未规范化登录名的直接插入被拒绝；并发插入同一个规范名只留一行

- 标记：note
- 文件：`0004_repair_invariants.py` 第 28–44 行。函数注册在 `database.py` 第 27–32 行，函数本体是 `login_name_for_sql`。列上的 `UNIQUE` 在 `domain_models.py` 的 `accounts.login_name`。
- 合同：ADR-004 第 3 条，共享合同第 1 节第 16 条。入库值是 NFKC、去首尾空白、casefold 之后的 1 到 64 个码位。触发器拒绝与函数结果不同的写入。直接 SQL 保持原样，不改写成规范值。没有注册这个函数的连接在写入时失败。

`build_engine` 的连接上，这些直接插入中止，错误都是 `accounts_login_name`：`Synthetic-Lead`、`Ωmega-user`、前导空格、全角 `ｓynthetic-wide`。`ωmega-user` 插入成功。库里相应名字只有种子行 `synthetic-lead` 和 `ωmega-user`。

两个线程同时插入规范名 `synthetic-race`。一行留下，另一行是 `UNIQUE constraint failed: accounts.login_name`。验收 `test_equivalent_canonical_login_names_race` 用同一字符串，结果相同，包含在退出码 0 里。

未调用 `build_engine` 的 `sqlite3` 连接上，未规范化名字和已经是规范形的名字都插不进去，错误都是 `no such function: sinuo_login_name_canonical`。这两行的计数是 0。

升到 head 不改写升级前已经写下的行。停在 `0003` 时插入的 `Synthetic-Lead` 升级后仍是 1 行。ADR-004 写明没有已部署的真实库，测试库每次重建，所以本轮没有回填。升级之后新的未规范化插入中止，错误 `accounts_login_name`。

### 5. 已标 `0003` 的库升到 head 后，登录名触发器和 jobs 终态触发器都在

- 标记：note
- 文件：`0004_repair_invariants.py` 第 77–83 行创建 `accounts_login_name_before_insert`、`accounts_login_name_before_update`、`jobs_before_update`。降级函数第 87–89 行只删除这三条。
- 合同：共享合同第 1 节第 17 条，ADR-004 迁移段。

停在 `0003_domain_identity` 时，`sqlite_master` 里没有登录名插入触发器，也没有 `jobs_before_update`。升到 head 后版本号是 `0004_repair_invariants`，这三条都在。验收 `test_database_stamped_0003_receives_repair` 断言了插入触发器和 `jobs_before_update` 的名字，包含在退出码 0 里。

当前 `0003` 源文件里的 `run_tasks_before_update` 已经包含 fencing token 条件，不是旧的 `NEW.status IN ('queued', 'running')`。探针把 `run_tasks`、`page_drafts`、`pages`、`stages`、`projects` 五条更新触发器换成旧正文后再升级。升级后的正文重新出现 `NEW.fencing_token IS NOT OLD.fencing_token`、`page_drafts_content_revision`、`pages_head_required`、`stages_head_required`、`projects_counters_coupled`。旧的状态目标列表不在新的 `run_tasks` 触发器里。

这三条新触发器在升级后的库上会执行：未规范化登录名插入中止于 `accounts_login_name`；终态 job 改回 `running` 或 `queued` 中止于 `jobs_terminal`。

### 6. `jobs` 终态不能改回 `running`

- 标记：note
- 文件：`0004_repair_invariants.py` 第 46–58 行。
- 合同：共享合同第 6 节。`succeeded`、`failed`、`cancelled`、`superseded` 之后，状态、fencing token、租约和 attempt 都不能再改。

head 库上，`status = 'succeeded'` 的 job 执行下列语句都中止，错误都是 `jobs_terminal`：

- `status = 'running'`，同时写租约和 `fencing_token = 2`
- `status = 'queued'`
- 只改 `fencing_token = 9`
- 只改 `attempt_no = 2`

行保持 `succeeded`，token 为 1，租约为空，`attempt_no` 为 1。因此没有先改到 `queued` 再进入 `running` 的第二步。

停在 `0003`、尚未安装 `jobs_before_update` 时，同一条改回 `running` 的语句成功。升到 head 之后，这条语句和改成 `queued` 的语句都是 `jobs_terminal`，行保持 `succeeded`、token 1、attempt 1、租约为空。

验收 `test_login_name_and_terminal_job_are_enforced` 覆盖了 `succeeded` 改回带租约和新 token 的 `running`，包含在退出码 0 里。这次执行的终态是 `succeeded`。触发器的 `OLD.status` 列表同时写了 `failed`、`cancelled`、`superseded`。
