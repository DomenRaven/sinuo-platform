# T08 自动触发重校 → 下游集成

- 上游任务 / owner：T06 偏好取消与 `suggestion_trigger` Job；T08 持久运行。本切片实现代理只写自动触发重校，不把 T08 标成 done。
- 集成基线：当前工作区，未新做提交。`tests/acceptance/test_suggestion_preferences.py` 仍断言 `SCHEMA_REVISION == 0009_method_cards`，该测试本轮通过。没有新建迁移，head 仍是 `0009_method_cards`。产品表数量不变。
- 消费者：后续要接自动运行的调度，以及仍使用 `open_agent_run` / `finish_agent_run` 的手动运行。
- 契约 / schema 版本：不新增 schema。沿用 `0007_run_origin` 的 `origin`、`preference_revision`，以及现有 `suggestion_preferences.scope_json`。T08 不标 done。

- 实现位置：
  - `src/sinuo/application/agents/execute.py`。`src/sinuo/workers/suggestions.py` 零改动。没有调用 `consume_candidate`。没有新做 `jobs.retry_wait`，也没有新的项目级预算表。
  - 公开入口 `open_policy_run(engine, principal, request) -> PolicyRunOutcome`。`request` 是 `PolicyRunRequest`。`principal.account_id` 必须是这条 Job 的 actor，不用另一个 `can_create_project` 账号代替。
  - `PolicyRunRequest` 字段：`job_id`、`intent`、`tasks`、`question`、`situation`、`constraints`、`now`，可选 `max_model_calls`（默认 6）、`deadline_seconds`（默认 30）。页版本和检索词不从这里读。
  - `PolicyRunOutcome` 字段：`run`（成功时为 `OpenedRun`，否则 `None`）、`job_status`、`reason`。
  - `open_agent_run` 仍写 `origin="manual"`、`preference_revision=None`。`finish_agent_run` 的签名不变。模型调用仍在 `write_uow` 外，预算仍是 `reserve_model_call` / `settle_model_call`。
  - `scope_json` 本切片读取的字段：
    - `page_version_id`：可选字符串。缺键或 JSON null 时冻结为 `None`。这是 policy 快照唯一的页版本来源，不读 `Page.head_version_id`，不读 `ViewState`。
    - `page_version_ids`：可选字符串列表。冻结页非空时，当前范围仍覆盖它的条件是等于 `page_version_id`，或出现在这个列表里。
    - `query`：可选字符串。证据职责要求去掉空白后非空，且长度不超过 40。它会被冻结进快照。
    - `queries`：可选字符串列表。冻结 query 非空时，当前范围仍覆盖它的条件是等于 `query`，或出现在这个列表里。
    - `artifact_version_ids`：可选字符串列表。出现时，每一项都必须是本项目里 `ai_processing_allowed=1` 的原件版本，否则不创建运行。缺省不因此拒绝启动；运行中的 policy 摘录仍会按原件该列过滤。
  - 其它键可以留在 JSON 里，本切片不解释。

- 权限动作：启动和每一轮模型调用前重查当前 actor 自己的 `run.create`。证据读、页快照、方法卡仍走原有 `evidence.read` / `page.read` / 方法检索授权。账号必须 `status=active` 且 `disabled_at` 为空。`off` / `manual` 不启动。`event` / `digest` 才启动。

- 事务与幂等边界：
  - 计划不合法（第四职责、环）在碰 Job 之前拒绝，不建运行。
  - 调用者不是 Job 的 actor：不改 Job，不建运行。
  - 其余启动失败在同一 `write_uow` 里条件更新这条仍为 `queued`、租约为空的 Job，不插入 `AgentRun`。`mode` 不是 `event`/`digest`、修订对不上、范围不合法、外发清单含禁止原件：标 `superseded`。账号失效或当前没有 `run.create`：标 `cancelled`。成功则插入 `origin=policy` 的运行，并把同一条 Job 标 `succeeded`。更新不把 Job 改成 `running`，`lease_owner` 和 `lease_until` 保持 NULL。
  - `save_suggestion_preference` 仍在它自己的事务里取消该项目、该 actor 未结束的 policy 运行，不改 manual，不改 `Project.revision`，也不回滚已提交的 `Decision`。
  - policy 运行在每次 `search_evidence`、`lookup_method`、`get_page_snapshot`、每次 `provider.execute`、以及插入候选之前重查：运行仍 `running` 且 `accepting_results=1`，偏好修订仍等于运行绑定修订，mode 仍是 `event` 或 `digest`，范围仍覆盖冻结的 `page_version_id` 和 `query`，账号仍有效，当前 `run.create` 仍在。失败则不调该工具、不调模型、不插入候选。
  - manual 运行不因为自动建议开关做偏好重校。它仍在每次工具读和每次模型调用前重查自己的当前 `run.create` 和既有预算。

- 成功响应 / 失败响应：
  - 成功：`reason="started"`，`job_status="succeeded"`，`run.origin="policy"`，`run.preference_revision` 为 Job 载荷里的修订。随后 `finish_agent_run` 按现有规则结束；批评职责仍可留下 `pending` 候选，不消费。
  - 失败原因字符串：`actor`、`account`、`grant`、`mode`、`revision`、`scope`、`outbound`、`job`。前七类不调用 Provider。`job` 表示 Job 不存在、种类不对，或已经不是可启动的 queued。
  - 只对 `origin=policy` 过滤摘录：进入 provider 上下文的摘录必须来自 `ai_processing_allowed=1` 的原件版本。manual 仍把现有授权摘录交给 Provider，包括 `ai_processing_allowed=0` 的摘录。

- 最小调用示例或夹具：`tests/acceptance/test_agent_policy_recheck.py`。先 `save_suggestion_preference` 把 mode 设为 `event` 或 `digest` 并写入上面的 scope，再用现有 `consider_suggestion_event` 或 `poll_suggestion_digest` 排 `suggestion_trigger`，然后：

```python
outcome = open_policy_run(engine, principal, PolicyRunRequest(
    job_id=job_id,
    intent="合成自动运行",
    tasks=(
        RunTaskSpec("clarify", "clarification"),
        RunTaskSpec("evidence", "evidence", ("clarify",)),
    ),
    question="合成方法问题",
    situation="家庭厨房",
    constraints={"consent": True},
    now=now,
))
if outcome.run is not None:
    finish_agent_run(engine, principal, outcome.run.run_id, provider, now=later)
```

- 真实验证命令与结果：在仓库根执行，退出码 0。37 passed，1 条既有 Starlette/httpx 弃用警告，3.79s。没有对 `var/sinuo.db` 做 alembic upgrade。没有发网络。

```text
.venv/Scripts/python.exe -m pytest -q tests/acceptance/test_agent_policy_recheck.py tests/acceptance/test_agent_runs.py tests/acceptance/test_policy_cancel.py tests/acceptance/test_suggestion_preferences.py -o cache_dir=_施工文件/2026-11-01_t08trigger/99_临时文件/pytest-cache-a --basetemp=_施工文件/2026-11-01_t08trigger/99_临时文件/pytest-a
```

  其中手动运行在自动建议改成 `off` 之后仍是 `running` 且 `accepting_results=1`，随后 finish 仍调用 Provider 并按现有规则结束。没有调用 `consume_candidate`；成功路径的候选保持 `pending`。

- 未验证项 / 已知限制：
  - 全库 pytest、真实 DeepSeek、HTTP 路由、`var/sinuo.db` 升级都没有跑。
  - 两条并发 `open_policy_run` 抢同一条 Job 没有用 barrier 重叠；单线程第二次打开已成功的 Job 不会再建运行。
  - policy 运行中途只撤 `run.create`、不改偏好，没有单独一条测试。启动前撤权和手动运行中途撤权已测；policy 的每轮重校走同一套 `run.create` 检查。
  - 第一轮任务已经认领之后，若 Provider 返回前偏好保存取消了运行，该 `RunTask` 走现有迟到路径，可能仍为 `running` 并带着租约。第二轮不再认领，不调工具，不调模型，不插入候选。Job 不会被改成 `running`。
  - T09 预览哈希和确认建页、T10、T11 没做。T08 不标 done。

- 下游第一次应运行的命令：上面同一条 pytest。不要对 `var/sinuo.db` 做 upgrade。

下游确认：已在基线上执行最小验证，结果为【 】；接手范围为【 】。
