# T08 持久智能体编排与 DeepSeek 适配

- 状态：implementing。2026-11-01 假模型链可以走出四职责并留下 pending 候选。同一条链可以按 `run.read` 打开只读页面。确认和拒绝只改页面本地状态。不标 done。不跑 live_model，不建方法卡表。
- 依赖：T06、T07 done；可提前基于冻结接口编写 fake 测试，但不能提前完成集成验收
- Owner：Grok 主控
- 基线提交或快照：无 Git commit。2026-11-01 迁移 head `0008_ticket_expiry`
- 写入范围：`agents/` 专责实现、Provider 适配、运行应用服务/仓储和 `tests/acceptance/test_agent_runs.py`；共享事件/工具 schema、迁移由集成 owner 管理
- 对应门禁：G3；消费者：T09

## 目标

同一输入任务能够选择所需职责，完成有预算、有依赖和可追溯的执行，生成有来源的候选。运行过程中即使切页、改草稿、模型超时、撤权或 Worker 接管，结果仍绑定正确输入并保留故障边界。

## 输入与交付

四类能力是 clarification/evidence/method/critic，单个 AgentRun 最多三种。完整四职责链分为 parent_run_id 关联的两次运行，中间澄清结果由用户确认。代码调度器生成无环 RunPlan；独立任务可并行，critic 等待声明依赖。每个任务独立输出，合成层保留分歧，结束时原子关闭 accepting_results；晚到输出仅作安全诊断和用量记录。

冻结初始 ContextSnapshot 和物化草稿快照；运行中检索结果追加 RunReadRecord，候选绑定完整 manifest_hash。接入 T07 的五个只读工具，每次调用、模型外发和候选发布都重校当前权限。工具内容视作数据，不能改变工具白名单或调用领域提交。

提供 FakeProvider 与服务端 DeepSeekAdapter。后者模型名/超时/费用/并发可配置，使用本地 Schema 与锚点验证，处理空内容、JSON 错误、401/402/429/5xx及有限纠正。并行预算必须由运行级原子预留/结算共享；未知用量保留估计而非记零。根据人格规范输出统一声音，不伪造未调用角色。

## 验收

创建并执行 `python -m pytest -q tests/acceptance/test_agent_runs.py`，使用可控 FakeProvider 与真实持久库验证：

1. 单职责和依赖并行路径不同，第四职责与依赖环在执行前拒绝。
2. 运行开始后草稿变化不改变输入；候选因内容预条件改变而失效，布局变化不误失效。
3. 必需依赖失败/超时后不发布完整阶段推进候选，局部结果说明缺口。
4. 取消、租约接管或运行封口后的晚到输出不能覆盖候选。
5. 提示注入、伪造锚点、未知工具和越权参数无法触发业务写入或扩大读集。
6. 并行调用共同消耗预算，重试和纠正也计入；没有无限循环。
7. HTTP adapter 的供应商成功/空内容/错误响应通过受控 HTTP 测试验证。真实 DeepSeek 烟测只使用获授权的合成输入和预算，并单独记录是否实际进行。

## 交接与证据

交出完整运行账本样例、候选 Schema、只读工具记录、权限/预算/取消状态，以及 Fake 与真实提供方验证的明确区分。T09 只接收校验后候选引用；智能体不得获取业务写 Session 或拥有确认权限。

## 自动触发补充（2026-10-30）

承接 T06 的策略 Job，启动前重校策略修订/启用状态、当前账号/权限/范围、外发政策与共享预算；排队后关闭策略或撤权不得发生模型调用。冻结输入不依赖全局当前页。对应 R02 与 W11；主动建议仍只生成候选，决定权保留给用户。


运行期间的偏好变化也属于验收：自动运行绑定 preference_revision，每次工具读/模型外发/候选发布重校；设置事务取消旧修订自动运行并关闭 accepting_results。验证第一轮后关闭或缩范围，第二轮不外发。手动运行与已提交决定不被自动偏好开关撤销。

## 假模型链证据（2026-11-01）

状态是 implementing，不标 done。一次 `run_fake_chain` 先读授权证据和页面快照，再在写事务外调用 `FakeModelProvider`。内存调度一次最多三个职责，所以澄清、查证据、问方法写在父运行，批评写在 `parent_run_id` 指向父运行的第二次运行。批评留下一条 `draft_decision`、`status=pending` 的候选，不调用 `consume_candidate`。没有方法卡表。`DisabledModelProvider` 不落运行、不落候选。`live_model` 没跑。HTTP 适配、预算并行、撤权后的晚到输出和其余验收条都没有做。

```text
.venv/Scripts/python.exe -m pytest -q tests/acceptance/test_retrieval_methods.py tests/acceptance/test_agent_runs.py -o cache_dir=_施工文件/2026-11-01_grok/99_临时文件/pytest-cache --basetemp=_施工文件/2026-11-01_grok/99_临时文件/pytest
```

退出码 0。10 passed，0.92s。其中 `tests/acceptance/test_agent_runs.py` 4 个。交接见 `workpool/handoffs/T08_FAKE_CHAIN_SLICE.md`。

## 只读入口证据（2026-11-01）

状态仍是 implementing。`read_fake_chain` 先做项目范围的 `run.read`。通过后返回父运行和批评运行的职责与状态、这次证据命中、方法问句「没有方法卡可查。上面的证据是否够用？」、以及 `pending` 的 `draft_decision` 候选正文。无权、其他项目或缺失运行返回空结果，不带标题、正文和候选正文。`AuthRequired` 继续抛出。函数不调用模型，不写候选行。

页面 `GET /api/v1/projects/{project_id}/agent-runs/{parent_run_id}/fake-chain/view` 和同一条 JSON `GET .../fake-chain` 都走这个函数。页面写明页快照和检索来自已测后端，这次运行用的是 FakeModelProvider，不是真实模型。确认和拒绝只改页面上的本地文字，不调用 `consume_candidate`，不新建页版本。

```text
.venv/Scripts/python.exe -m pytest -q tests/acceptance/test_agent_runs.py -o cache_dir=_施工文件/2026-11-01_grok/99_临时文件/pytest-cache --basetemp=_施工文件/2026-11-01_grok/99_临时文件/pytest
```

退出码 0。5 passed，1 warning，0.90s。警告是 httpx 弃用。随后全量 `.venv/Scripts/python.exe -m pytest -q`，同一缓存目录，退出码 0。308 passed，2 warnings，17.91s。T08 不标 done。
