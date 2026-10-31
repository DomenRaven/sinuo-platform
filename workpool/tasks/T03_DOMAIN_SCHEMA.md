# T03 领域模型、约束与迁移

- 状态：done
- 依赖：T01 done；与 T02 的共享边界已冻结为 T02-T03-SHARED-1
- Owner：Grok 开发B（实现 ec12d5ab-6ce4-47f9-aa95-978fb9e957d9；审查未结束）
- 基线提交或快照：无 Git commit。2026-10-30 基线 pytest 66 passed、ruff 通过。合同 T02-T03-SHARED-1
- 写入范围：`src/sinuo/domain/schema_types.py`、`src/sinuo/infrastructure/domain_models.py`、`tests/acceptance/test_domain_schema.py`、`workpool/handoffs/proposals/0003_domain_identity.draft.py`。不写 `migrations/versions/`、`models.py`、`public_contract.py`
- 对应门禁：G1；消费者：T04、T05、T06、T07、T08、T09、T10

## 目标

为项目、阶段、页草稿、确认版本、谱系、原件、证据、决定、测试、意见、运行和消息提供明确的数据身份、约束和可升级 schema。即使应用某一层遗漏检查，唯一键、外键、条件更新和不可变写入边界也应阻止核心损坏。

## 输入与交付

按领域合同建立稳定 ID 与版本 ID，落实项目作用域、版本序号唯一、来源端点、幂等作用域、候选消费状态、Outbox 去重与任务 fencing 所需字段。节点与边可用受严格 Schema 约束的版本 JSON；投影有独立重建入口。布局放个人 `view_states`，与草稿内容分离。

统一 UTC 时间、整数修订、规范化请求/内容哈希和错误码。为每张核心表说明写入 owner、唯一约束、索引与删除/墓碑策略。读取模型不能回填最新版到历史快照。迁移链从现有 primitives 起扩展，集成 owner 保持单一 head；不随意删库重建规避升级问题。

## 验收

创建并执行 `python -m pytest -q tests/acceptance/test_domain_schema.py`。验证：

1. 从空库和已有初始 primitives 数据库均可迁移到 head，已有回执/事件保留。
2. 跨项目外键/引用在数据库可表达范围内拒绝，剩余约束有领域层反例测试。
3. 同页版本号、同作品版本号、同命令作用域键不能重复。
4. 正式页版本与原件元数据的业务内容没有普通 UPDATE/DELETE 路径；受控墓碑仍可审计。
5. 历史查询只读；草稿修订、个人布局、正式版和项目修订不会误用同一计数器。
6. 迁移失败后可按记录恢复，降级能力与不能降级的迁移均明确说明。

## 交接与证据

交出 schema 图或表清单、迁移命令、各聚合最小夹具、约束异常映射和 UoW 使用示例。保留模型/迁移 schema 版本，消费者不得各自另造同义字段。任务完成以真实数据库结果为准，单纯 ORM 对象创建或类型检查不够。
