# AGENTS.md — 编码代理常驻规则

适用于在本仓库写代码、测试、迁移或文档的任何代理。本文件只列硬规则；细节见引用文档。

## 1 开工顺序

1. 先读 `docs/construction/00_施工总览与完成标准.md`，再读 `docs/construction/02_基线裁决与勘误.md`。
2. 按任务卡读对应合同：`docs/construction/03_DOMAIN_AND_TRANSACTIONS.md` 至 `docs/construction/07_BACKEND_ACCEPTANCE.md`。
3. 读 `workpool/BOARD.md` 和自己认领的 `workpool/tasks/T*.md`。未认领、未写明写入范围，不改代码。
4. 依据冲突时按 00 第 3 节的优先级裁决；02 已裁决的条目不得重新解释。

## 2 文件所有权

- 一个文件同一时段只有一位写入 owner。认领时把精确文件或子目录写进任务卡。
- 以下共享文件只由唯一集成 owner 修改；其他代理提交具体 diff 提案：
  - `pyproject.toml` 与依赖锁文件
  - `src/sinuo/api/app.py`
  - `migrations/versions/` 下的 Alembic revisions（保持单一 head）
  - `src/sinuo/infrastructure/models.py` 与公共 schema（事件信封、错误码、动作枚举、候选动作、运行状态枚举）
- 修改别人拥有的实现，退回 owner 或重新认领。审查者只读。
- 编码代理不得修改 `docs/reference/`。架构负责人仅在用户授权的文档修复中，备份后定向同步未锁定原文、更新哈希及变更记录；禁止以此改动产品锁定稿。锁定白皮书 `docs/reference/思诺智能体辅助设计平台产品白皮书_定稿.md` 的 SHA-256 必须保持 `CE92954FFBD12D4D7278ED240A759CF7DDAD31DB60F1F3B88F3C7472EAA8CF86`。

## 3 不可破的不变量

违反任一条即为阻断缺陷，不能以“后续修”合入。

1. 历史浏览零业务写入：查看项目/章/页/节点历史不改 HEAD、草稿、`ProjectRevision` 或任何共享“当前分支”指针。
2. `PageVersion` 与原件字节不可覆盖、不可普通 UPDATE/DELETE；更正追加新记录；合法删除只走墓碑流程。
3. 模型与专责能力只读业务域、只写运行域。智能体代码拿不到业务写 Session，不能调用确认命令。
4. 每次工具调用、模型外发前、SSE 建立/重连/回放、原件下载、导出生成与下载、候选预览与提交、幂等回执重放，都按**当前**权限重新授权。权限快照只解释历史输入。
5. 候选预览由服务器生成并绑定：actor、候选修订、规范化 `user_patch`、全部源版本与草稿修订、`access_epoch`、`preview_hash`、到期时间。提交只引用服务器保存的预览。
6. 候选只能消费一次：`WHERE status='pending' AND revision=:r` 的条件更新。不同幂等键、不同操作者的双确认也只能成功一次。
7. 候选消费、业务对象、审计、Outbox、幂等回执在同一短事务提交；任一步失败全部回滚，不留成功回执。
8. 迟到结果不覆盖：运行封口后到达的任务输出、旧租约的 finalize/renew 一律拒绝，只记安全诊断与实际用量。
9. 单个 `AgentRun` 最多 3 种职责（clarification/evidence/method/critic 中选）。四职责链拆成 `parent_run_id` 关联的两次运行。不能用“子角色”“合成角色”绕过。
10. CAS 必须落在条件 UPDATE 里，不能先读再普通 UPDATE。进程内锁只能辅助。
11. 模型、网络、文件转换调用一律在数据库事务外。

## 4 测试与临时文件

- 测试只用独立临时数据库和对象目录。不得指向 D/E 盘旧项目、`docs/reference/`、用户真实资料或 `var/` 下的运行库。
- 测试数据全部合成，标 `synthetic=true`；安全测试用合成 canary 字符串，不用真实密钥或访谈。
- 默认不跑 `live_model`。真实模型测试需显式开启、服务端配置密钥并设本地预算。
- 临时文件、pytest 缓存与 basetemp 放 `_施工文件/<执行日期>_<代理名>/99_临时文件/`（已被 `.gitignore` 忽略）。完成后清理自己创建的临时文件。
- 并发测试用 barrier 或显式调度让独立连接真正重叠；不能用 `sleep` 假设竞态发生，不能用 mock 替代事务与文件行为。

## 5 禁止的捷径

- 不删测试、不放宽断言、不加 `skip`/`xfail` 掩盖实现缺陷。改断言须先证明原断言与冻结合同不符，并交集成 owner。
- 不把 FakeProvider、fake consumer、占位端点或固定示例输出写成“真实接入”或“已完成”。未实现返回 `not_implemented` 或安全不可用状态。
- 不在任务卡写“基本完成”。`done` 只由集成 owner 在集成基线上验证后标记。
- 只写实际运行过的命令与结果；没测的写“待测”。

## 6 失败处理

记录每次失败：观察到什么、支持哪种假设、下次改变哪一个条件。**同一失败连续三次且没有新证据**，停止重复同一做法，按 `prompts/04_GROK_诊断与修复.md` 转入诊断：缩小复现、检查上游假设、查官方资料。仍需外部输入则标 `blocked`，继续其他 ready 任务。

## 7 自主权边界

- 可自主决定：函数结构、算法、局部重构、查询优化、索引、测试夹具、调试路径。
- 须先写 ADR：改变公共合同（API、事件、错误码、schema、状态枚举）、替换技术栈、引入新基础设施、做不兼容依赖升级。ADR 放 `workpool/decisions/`，按 `workpool/ADR模板.md` 写；目录不存在时由写第一份 ADR 的代理创建。ADR-001 至 ADR-006 已使用。新 ADR 从 ADR-007 起。
- 改变锁定产品定义（白皮书 2.1）不在代理权限内，须用户决定。

## 8 密钥与外发

- 不读取、不复制、不打印任何 `deepseek api.txt` 或其他密钥文件。
- 密钥只在服务端环境变量（`SINUO_DEEPSEEK_API_KEY`，类型 `SecretStr`）。不得进入前端、代码、提示词、日志、测试夹具、文档、Git 或证据附件。日志只输出“是否已配置”。
- `SINUO_MODEL_ENABLED` 默认 `false`。真实外发资料与付费调用只在用户授权范围内进行。

## 9 常用命令

完整步骤见 `README.md`“本地启动”一节。在仓库根的 PowerShell 中：

```powershell
.\.venv\Scripts\python -m pip install -e ".[dev]"
.\.venv\Scripts\alembic upgrade head          # 只对明确配置的库执行
.\.venv\Scripts\python -m pytest -q -m "not live_model"
.\.venv\Scripts\python -m ruff check src tests migrations
.\.venv\Scripts\uvicorn sinuo.api.app:create_app --factory --host 127.0.0.1 --port 8000
```

pytest 的缓存与临时目录参数按 `README.md` 指向第 4 节的施工临时目录。

## 10 交付格式

更新自己的任务卡：实现文件、合同变化、实际命令与退出码、失败/恢复测试、未验证项、下游用法。交接用 `workpool/交接模板.md`，放 `workpool/handoffs/`（不存在时创建）。提交审查前检查 diff 中没有密钥、真实用户原文、无关生成物或未说明的破坏性迁移。

## 11 接续施工说明（2026-10-30）

ADR-002 已用于基础加固，ADR-003 已用于验收归属与文档同步，ADR-004 已用于审查意见分层，ADR-005 已用于偏好修订冲突码，ADR-006 已用于方法卡表。后续 ADR 从 ADR-007 起。UoW 内只能用托管 Session 的数据操作，禁止原始 COMMIT/ROLLBACK/BEGIN SQL、原生 DBAPI 或另开连接分拆同一命令；当前提前提交保护面向标准 Session/Connection 接口，不是任意 Python 执行沙箱。T05 必须落实原件 generation/pin/GC 协议；T06→T08 必须落实建议频率与受限后台主体。以最新交接记录识别已做/待做边界。


## 12 当前职责与开工入口

用户指定：Codex担任项目总监和架构工程师，维护产品/架构文档与验收监督；Grok主控担任唯一产品代码集成owner，负责实现、调试、依赖、迁移及代码测试。普通实现选择在合同内自行推进。开发代理/审查代理的并发槽位不是产品AgentRun的三职责上限。

开工先读 `GROK_START_HERE.md` 与 `docs/construction/53_GROK多代理全流程施工手册.md`。先探测真实文件/终端/子代理能力并记录；有真实子代理才称多代理，没有则顺序执行并如实说明。2026-10-30 文档修复当时代码任务尚未认领；现行入口以 `workpool/SESSION.md` 为准。T02 与 T03 已 done，T04 正在实现，T05–T11 仍未实现。
