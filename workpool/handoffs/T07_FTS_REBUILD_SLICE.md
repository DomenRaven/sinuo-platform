# T07 FTS5 与索引重建 → 下游集成

- 上游任务 / owner：T07 证据检索。本切片只补 FTS5 与从证据锚点重建索引。T07 仍是 implementing，不标 done。
- 集成基线：当前工作区，未做 git commit。唯一 Alembic head 是 `0010_evidence_index`，`down_revision` 是 `0009_method_cards`。没有对 `var/sinuo.db` 做 alembic upgrade。
- 消费者：`search_evidence` 的现有六参数调用（含 `execute.py`、`fake_chain.py`）。要命中，必须先由调用方在搜索前显式调用 `rebuild_evidence_index`。本切片没有改这两个调用方。
- 契约 / schema 版本：`SCHEMA_REVISION` 与 Alembic head 都是 `0010_evidence_index`。没有新错误码，没有新权限动作。`search_evidence` 增加仅关键字参数 `artifact_version_id`，默认 `None`。六参数位置调用不变。

- 实现位置：
  - `src/sinuo/application/retrieval/search.py`
  - `migrations/versions/0010_evidence_index.py`
  - `src/sinuo/infrastructure/models.py` 只改了 `SCHEMA_REVISION`
  - `tests/acceptance/test_retrieval_methods.py`
  - 三处 head / 表数量断言：`tests/acceptance/test_domain_schema.py`、`test_assets_derivatives.py`、`test_suggestion_preferences.py`。没有改这些文件里的其他行为断言。
  - 没有 ADR。ADR-007 未占用。
  - 重建函数：`rebuild_evidence_index(session) -> int`。返回本次新插入的索引行数。已有锚点的索引行直接跳过。
  - 搜索签名：

```python
def search_evidence(
    session,
    authorizer,
    principal,
    project_id,
    query,
    limit,
    *,
    artifact_version_id: str | None = None,
) -> SearchEvidenceResult
```

- 权限动作：仍先算当前 `evidence.read` 允许的锚点 id，再只在这个集合里做 FTS 或子串匹配。非项目成员直接空结果，不读对方摘录。无权锚点和其他项目不进入命中、版本状态和计数。`AuthRequired` 继续抛出。

- 事务与幂等边界：
  - 搜索只读，不插入、不更新、不删除。
  - 重建只读 `evidence_anchors`，只向 `evidence_excerpt_index` 插入尚不存在的锚点。不 UPDATE/DELETE 证据锚点或原件版本，也不修改已经写下的索引版本 id。版本 id 取该锚点自己的 `artifact_version_id`，不读取更新的 `version_no`。
  - 重建不自己提交，放在调用方的 `write_uow` 里。同一锚点再重建一次，插入数为 0，且不产生写入语句。
  - 有锚点但没有索引行时，该版本是 `not_indexed`，不算 `no_hit`，也不用锚点摘录的 LIKE 冒充已索引。
  - `downgrade` 只执行 `DROP TABLE IF EXISTS evidence_excerpt_index`。SQLite 连同本表的影子表一起删除。不删 `method_cards`、`evidence_anchors` 或 `artifact_versions`。

- 成功响应 / 失败响应：
  - 空查询、只含空白、超过 40 字，以及 `limit` 不是 1 到 20 的整数，仍抛 `ValueError`，消息分别是 `query` 和 `limit`。
  - `artifact_version_id` 为 `latest`、`head`、`current`、`newest`（大小写不敏感），或空白、未修剪、超过 64 字，抛 `ValueError`，消息是 `artifact_version_id`。不传这个参数时，不把旧摘录改报成更新版本。
  - 查询旧版本必须显式传入那个版本 id。传入项目里不存在的 id 时返回空结果。
  - 命中仍带回索引行上的原件版本 id 和当时的 locator。`counts` 的键仍是 `hit`、`no_hit`、`not_indexed`。

- 分词：三字及以上走 SQLite FTS5 trigram，查询先包成短语再 `MATCH`，因此 `100%` 里的 `%` 是字面字符。不足三字（夹具里的 `%` 和 `_`）只在已授权且已有索引行的集合里做 `LIKE`，`%`、`_` 和反斜杠按字面转义。不单用 unicode61：本机 SQLite 3.50.4 上，unicode61 能匹配整段「合成竹材弯曲」，不能匹配其中的「竹材」或「竹材弯」；trigram 短语能匹配「竹材弯」。裸 `MATCH '100%'` 会在 `%` 处报 FTS5 语法错误，所以三字及以上走短语，而不是裸查询串。这条混合路径第一次跑夹具就通过，没有改用另一种分词，因此没有 ADR。

- 产品表数字：`application_tables` 统计 `sqlite_master` 里 `type='table'`、名字不以 `sqlite_` 开头、再去掉 `alembic_version` 的全部表。`0009_method_cards` 时是 44，全是产品表。本版新增 1 张真实产品表，即 FTS5 虚表 `evidence_excerpt_index`。SQLite 同时建出 5 张影子表，不是业务表：`evidence_excerpt_index_config`、`_content`、`_data`、`_docsize`、`_idx`。断言里的 50 = 44 张原有产品表 + 1 张索引虚表 + 5 张影子表。这 50 里有 5 张是影子表。没有把断言改成 45 来躲开它们。

- 最小调用示例或夹具：`tests/acceptance/test_retrieval_methods.py` 的 `seed_search_world` 在搜索前调用 `rebuild_evidence_index`。现有中文夹具数字仍是查询「合成竹材弯曲」时 `hit` 1、`no_hit` 3、`not_indexed` 2。`%`、`_`、`100%` 和「竹」×40 的断言没有放宽。另有：未重建时命中锚点是 `not_indexed`；v1 重建后再追加 v2 并再次重建，锚点和索引里的版本 id 仍是 v1。

- 真实验证命令与结果：仓库根执行，退出码 0。36 passed，1 条既有 Starlette/httpx 弃用警告，2.48s。没有对 `var/sinuo.db` 做 alembic upgrade。

```text
.venv/Scripts/python.exe -m pytest -q tests/acceptance/test_retrieval_methods.py tests/acceptance/test_domain_schema.py -o cache_dir=_施工文件/2026-11-01_t08trigger/99_临时文件/pytest-cache-c --basetemp=_施工文件/2026-11-01_t08trigger/99_临时文件/pytest-c
```

  另两条只核对被改过的 head 断言，退出码 0，2 passed，0.50s：`test_assets_derivatives.py::test_head_includes_stored_object_tables`、`test_suggestion_preferences.py::test_revision_registers_suggestion_tables`。用 Alembic `ScriptDirectory` 读迁移脚本，head 只有 `0010_evidence_index`，这次检查没有连接运行库。

- 未验证项 / 已知限制：
  - 全库 pytest 未跑。`test_agent_runs.py`、`test_agent_policy_recheck.py` 以及假模型链仍在插入锚点后直接搜索，本切片没有改那些文件。按现在的合同，没有索引行就是 `not_indexed`，摘录不会被 LIKE 命中。这些测试待测；下游要在搜索前调用 `rebuild_evidence_index`。
  - 没有做向量、没有调模型、没有做 3D、T09、T10、T11。
  - 撤权后的异步清理没做。查询时无权锚点不进入结果；索引里留下的行不会被这次重建删掉。
  - `ruff check` 对改过的文件退出码 1。5 条都是 `test_retrieval_methods.py` 图邻域里原有的超长行，本切片没有改那些行。
  - T07 不标 done。

- 下游第一次应运行的命令：上面那条检索与 schema pytest。不要对 `var/sinuo.db` 做 upgrade。接假模型链或 agent run 之前，先在对应夹具里重建索引。

下游确认：已在基线上执行最小验证，结果为【 】；接手范围为【 】。
