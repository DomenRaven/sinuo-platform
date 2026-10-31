# T03 模式审查

对照共享合同 `T02-T03-SHARED-1`、提案第 6–10 节、`src/sinuo/infrastructure/domain_models.py`、`migrations/versions/0003_domain_identity.py`、`tests/acceptance/test_domain_schema.py`。安装的迁移与 `workpool/handoffs/proposals/0003_domain_identity.draft.py` 字节相同，行号两边通用。

触发器以提案第 6–10 节为准，这是共享合同第 3 节的收束。下面的语句在临时库升到 `0003_domain_identity` 并写入合成夹具后执行过。本文不把 T03 标成完成。

结论：**blocking 1 条，note 6 条。** 最严重的是 blocking：`run_tasks` 的终态触发器挡不住经 `retry_wait` 或 `blocked` 绕回 `queued` / `running` 的普通 UPDATE，绕回之后还能按新的 fencing token 插入 `published` 结果。

## blocking

### B1. `run_tasks` 终态可以被普通 UPDATE 绕回运行中

- 文件：`migrations/versions/0003_domain_identity.py` 第 321–334 行；状态闭集在 `src/sinuo/domain/schema_types.py` 第 46–53 行；租约 CHECK 在 `src/sinuo/infrastructure/domain_models.py` 第 157–164 行和第 867 行；结果插入触发器在迁移第 347–361 行。
- 合同：提案第 6 节 `run_tasks` 写明，进入终态后不能改回 `queued` / `running`。共享合同第 3 节把这节触发器定为实现依据。终态是 `succeeded`、`failed`、`cancelled`。闭集里另外还有 `blocked` 和 `retry_wait`。

触发器只在旧状态已经是终态、且新状态属于 `queued` 或 `running` 时 `RAISE`：

```sql
OLD.status IN ('succeeded', 'failed', 'cancelled')
AND NEW.status IN ('queued', 'running')
```

`blocked` 和 `retry_wait` 不在这个新状态列表里，租约 CHECK 又把除 `running` 以外的状态都当成空租约。因此终态行可以先走进这两个中间状态，下一步就不再被终态条件看见。

临时库里的结果：

- `succeeded` 直接改成 `running` 会中止，错误是 `run_tasks_terminal`。触发器存在。
- `succeeded` → `retry_wait` → `running`（同时写入 `lease_owner`、`lease_until`、`fencing_token=4`）两步都成功，行最终是 `running`。
- `succeeded` → `blocked` → `queued` 两步都成功，行最终是 `queued`。
- 绕回 `running` 且 token 为 4 之后，运行的 `accepting_results` 仍是插入时的 1。再插入 `disposition='published'`、`fencing_token=4` 的 `agent_results` 被接受。插入触发器只要求 token 等于任务当前值；任务 token 已经被普通 UPDATE 改掉了。

`tests/acceptance/test_domain_schema.py` 没有这条 UPDATE 序列的断言。文件里也没有 `pytest.skip`、`xfail` 或 `skipif`。这条绕过不会被现有断言拦住，也不是把失败改成跳过。

## note

### N1. 草稿正文可以在 `content_revision` 不变时被改写

- 文件：`migrations/versions/0003_domain_identity.py` 第 241–252 行。
- 合同：提案第 6 节 `page_drafts` 的触发器条文是 `page_id`、`project_id` 冻结，`content_revision` 只能保持或加 1。同节的 CAS 语句，以及“保存为版本时把 `base_version_id` 指到新版本，并把 `content_revision` 加 1”，要求正文变化推进这个计数器。

实现与前一条触发器条文一致：修订号不变时，`NEW.content_revision IS NOT OLD.content_revision` 不成立，`RAISE` 不触发。临时库里下面这条普通 UPDATE 成功，`content_revision` 仍是 5，哈希已换成另一串 64 位十六进制：

```sql
UPDATE page_drafts
SET content_json = '{"edges":[],"nodes":[],"schema_version":1}',
    content_hash = :other_hash
WHERE page_id = 'synthetic-page-a';
```

接着 `SET base_version_id = 'synthetic-pv-a1'` 也成功，修订号仍是 5。条件更新的 WHERE 没有被触发器守住。

### N2. 不带 WHERE 的候选更新会提交所有 pending 行

- 文件：`migrations/versions/0003_domain_identity.py` 第 363–384 行。
- 合同：提案第 6 节 `candidates` 写明，触发器挡住漏写 WHERE 的普通 UPDATE；应用层还要检查受影响行数是 1。

行级触发器只判断每一行是不是“pending、新状态为 committed/rejected/expired、revision 加 1、其余列 `IS` 相等”。它看不见语句有没有 WHERE。临时库里另插一条 pending 候选后执行：

```sql
UPDATE candidates SET status = 'committed', revision = revision + 1;
```

`synthetic-candidate` 和 `synthetic-candidate-2` 都变成 `committed`、`revision=2`。应用层若再漏掉行数检查，触发器不会把多行提交退回去。

### N3. 改 `password_hash` 不必增加 `auth_epoch`

- 文件：`migrations/versions/0003_domain_identity.py` 第 105–115 行。
- 合同：共享合同第 2 节要求禁用和重置密码把 `accounts.auth_epoch` 加 1。提案第 6 节的账号触发器只冻结 `id` 与 `created_at`，并要求 `auth_epoch` 保持或加 1。有效会话谓词还要求会话上的 `auth_epoch` 等于账号上的当前值。

实现与第 6 节的触发器清单一致，没有把 `password_hash` 的变化绑到纪元上。临时库执行 `UPDATE accounts SET password_hash = '$argon2id$other-synthetic'` 成功，`auth_epoch` 仍是 1。纪元不加，旧会话仍过得了纪元谓词。同一触发器会拒绝 `auth_epoch` 加 2，步进限制本身是在的。

### N4. `login_name` 的唯一约束不覆盖 casefold

- 文件：`src/sinuo/infrastructure/domain_models.py` 第 294–297 行和第 311 行。规范化函数在 `src/sinuo/domain/schema_types.py` 第 201–208 行。
- 合同：共享合同第 1 节要求入库值是 NFKC + casefold + 去首尾空白，1 到 64 个码位，并且 UNIQUE。第 3 节规定列类型 `String(128)`、非空、唯一。

列上的 UNIQUE 和 `length BETWEEN 1 AND 64 AND login_name = trim(login_name)` 都在。CHECK 不要求 casefold。夹具已有 `synthetic-lead` 时，再插入 `Synthetic-Lead` 成功，两条账号并存。`normalize_login_name` 会把两边收成同一个字符串，但表约束不调用它。验收只检查了这个 Python 函数（`tests/acceptance/test_domain_schema.py` 第 727 行），没有用原始 INSERT 锁住大小写。

### N5. 同一次 UPDATE 可以同时增加 `revision` 和 `access_epoch`

- 文件：`migrations/versions/0003_domain_identity.py` 第 146–160 行。
- 合同：共享合同第 1 节第 12 条要求，已经存在的项目上 `access_epoch + 1` 只用于授权变更，不加 `projects.revision`。提案第 6 节的触发器允许两个计数器各自保持或加 1；“只加纪元时标题、简介、模板键不变”只在 `revision` 保持原值时检查。

```sql
UPDATE projects
SET revision = revision + 1, access_epoch = access_epoch + 1
WHERE id = 'synthetic-project-alpha';
```

这条语句成功，计数从 `(revision=4, access_epoch=1)` 变成 `(5, 2)`。标题不变时，`projects_epoch_only` 的正文条件不成立。`acl_commit` 只检查纪元加 1，也不看 `revision` 有没有一起动。

### N6. 页和阶段的头指针可以被更新成 NULL

- 文件：`migrations/versions/0003_domain_identity.py` 第 203–208 行（阶段）和第 223–228 行（页）。
- 合同：提案第 6 节写明，页的 `head_version_id` 只在插入过程中为空，命令提交前必须写上。触发器条文是：头指针若变化，新版本必须属于本页、本项目。阶段头指针用同一类存在性检查。历史读取不得移动这两个指针。

两个触发器都是 `NEW.head_... IS NOT NULL AND NOT EXISTS (...)`。新值为 NULL 时存在性检查不执行。临时库里：

```sql
UPDATE pages SET head_version_id = NULL WHERE id = 'synthetic-page-a';
UPDATE stages SET head_config_version_id = NULL WHERE id = 'synthetic-stage';
```

两句都成功。跨项目的头指针会被拒绝（`pages_head_mismatch`、`stages_head_mismatch`），归属检查在非空时是生效的。清空是另一条口子。

## 核对过、不单列成意见

- 降级没有删掉或改写基元行。临时库在 0002 先写入回执、事件和合法投递，升到 0003 后再写入指向该事件的 `jobs` 与 `consumer_receipts`，并带上含确认版本的领域夹具。降回 `0002_lease_invariants` 后，应用表只剩 `idempotency_records`、`outbox_events`、`outbox_deliveries`，三张表的行与降级前逐列相同。
- `page_versions`、`artifact_versions` 以及提案第 7 节其余无条件不可变表，安装的触发器是 `BEFORE UPDATE` / `BEFORE DELETE` 加 `RAISE(ABORT)`，没有 `UPDATE OF`，也没有 `WHEN`。抽查改 `page_versions.content_json`、改 `decisions.body`、删除 `page_edges`、更新 `run_task_dependencies`，都被对应的 immutable 触发器拒绝。
- 部分唯一索引带 `status = 'active'`。同一项目、账号、动作的第二条 active 授权被 UNIQUE 拒绝；撤销后不能改回 active；再插入新的 active 行可以。
- 抽查过的跨项目写入被复合外键或墓碑触发器拒绝：决定指向另一项目的作品版本、意见、测试、预览来源、父运行、墓碑的 `project_id` 与目标不一致。
- `tests/acceptance/test_domain_schema.py` 没有 `pytest.skip`、`xfail` 或 `skipif`，也没有把不可变失败改成跳过。
- `.env.example` 里的 `SINUO_DEEPSEEK_API_KEY` 是空的。验收口令是合成前缀 `$argon2id$synthetic-hash`。没有发现可用密钥进仓库。
- `jobs` 可以从 `succeeded` 改回 `running`。提案第 6 节对这张表只要求禁止 DELETE、用终态结束，没有写出终态触发器，所以不单列。
