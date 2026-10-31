# D 版智能体、工作流、通信与存储只读核验

核验日期：2026-10-29。代码基准：`D:\AAA学习\大三上\设计大数据基础\大作业\edu-src-20261009-020001\edu-platform`。本记录依据静态代码阅读，未运行系统，未读取 `deepseek api.txt`，未修改 D 版。

## 实际智能体和流程

| 模块 | 实际职责 | 证据 |
|---|---|---|
| 问题填充 | 对问题树节点补解释等内容 | `teacher/src/data/aiInitAgents.ts:44`；调用见 `teacher/src/data/aiInitPipeline.ts:180` |
| 知识提取 | 从问题内容抽术语、补解释并挂载到问题节点 | `teacher/src/data/aiInitAgents.ts:168-220,448-592` |
| 方法提取 | 分析所需方法/工具、整理信息并挂载 | `teacher/src/data/aiInitAgents.ts:273-383,448-592` |
| 审校 | 对问题和概念文字提出并直接应用修订 | `teacher/src/data/aiInitAgents.ts:638-726` |
| 提示词教练 | 为用户形成可复制的启动提示词 | `student/src/lib/researchPromptAgent.ts:43-100` |
| 研究/论文辅助 | 问答、术语解释、论文阅读等按场景调用 LLM 的函数 | `student/src/lib/researchAi.ts`、`student/src/lib/paperReading.ts`、`student/src/components/ResearchAiPanel.tsx:319-379` |

教师端初始化明确按 `problem → knowledge → method → review` 串行运行（`teacher/src/data/aiInitPipeline.ts:15,141-245`）。后台任务接口仅接受 `knowledge`、`method`（`server/src/routes/aiJobs.js:18-25,70-72`），worker 按这两个类型顺序运行（`server/src/aiJobsWorker.js:113-166`）。D 版属于预设流水线与多项 LLM 功能，不是具有独立权限、可互相协商和动态调度的完整多智能体架构；文档需区分“现有实现”和“目标设计”。

## 权限与通信

- 后台 AI job 的查询、创建、取消均要求登录和 `teacher` / `superadmin` 角色，并按 `ownerTeamMemberId` 隔离 job：`server/src/routes/aiJobs.js:30-50,53-139`。
- 通用 AI 代理仅执行 `requireAuth`，接收客户端消息后请求 DeepSeek，再记录用量：`server/src/routes/studentInterprets.js:308-353`。这里没有按智能体分别定义服务端工具能力或可读取的数据集合。
- 前端教师端的 API 配置保存在 `localStorage`，包括 API key：`teacher/src/data/aiConfig.ts:20-78`；请求可经 `X-AI-Api-Key` 请求头送后端代理，也可能在无代理条件下直连上游：`teacher/src/data/aiClient.ts:75-112`。新平台应仅在服务器保存密钥、由服务器代理模型请求。
- 后端固定 DeepSeek base URL 和 model：`server/src/deepseek.js:1-13`；代理再强制指定 model：`server/src/routes/studentInterprets.js:308-324`。实际 model 标识及账户可用性需接入时以官方 API 或实测核对。

## 并发与版本风险

- 单 owner 最多 2 个后台 job；同课程同 agent 类型冲突时拒绝：`server/src/aiJobsStore.js:10`、`server/src/routes/aiJobs.js:79-100`。该检查与创建分开执行，跨进程情况下不是原子约束。
- worker 的 `saveLibraryWithRetry` 先读 revision、再读一次确认、随后写整个文档：`server/src/aiJobsWorker.js:14-39`。SQLite KV 层只有主键 upsert，未提供带 revision 条件的 compare-and-swap：`server/src/sqlKv.js:20-30,105-130`。因此不能把当前策略当作多写者事务保证。
- job 对概念树的局部保存先删除该课程同类型旧概念，再写新概念：`server/src/aiJobsWorker.js:80-105`。这与新产品“旧节点可回溯、非删除”的约束相冲突。运行中还会写部分结果，异常时可能留下部分新树：`server/src/aiJobsWorker.js:143-166`。
- 课程库使用 `baseRevision` 和 409 冲突响应，但整份 `library` 替换：`server/src/routes/courses.js:553-585`。可借鉴冲突提示，不能直接作为节点级协作写入机制。
- 学生工作区合并对个人节点使用字段时间戳及子节点增删时间戳：`server/src/studentWorkspaceStore.js:105-203`；知识、方法、资源、workbench 条目采用 overlay 覆盖：`server/src/studentWorkspaceStore.js:341-372`。保存时整体合并、整体写入：`server/src/studentWorkspaceStore.js:380-392`。部分合并思路可参考，但仍需明确并发冲突语义。
- AI 初始化运行记录和微调快照在浏览器 `localStorage`：`teacher/src/data/aiInitStore.ts:10-67`。无法直接支撑跨设备可审计的项目版本图。

## 可复用判断

可参考：问题/知识/方法关联结构（`teacher/src/data/aiInitAgents.ts:393-445`）；XMind/Excel/Markdown 树解析（`shared/parseXmindTree.ts` 等）；研究问答树交互（`student/src/components/ConversationTree.tsx`、`ResearchAiPanel.tsx`）；阶段栏与研究布局（`student/src/pages/ResearchPage.tsx:140-150`）；任务进度显示（`teacher/src/components/AiInitProgressPanel.tsx`）。这些主要是领域数据处理和交互原型，迁入新平台前需去除课程身份和旧状态模型耦合。

需重做：智能体工具权限与服务端校验、通信信封、任务协调与幂等、细粒度实体版本/事务、不可变产物与版本指针、跨设备记忆/事件日志、模型密钥管理和审计。新平台多模块并发应采用“快照读取 → 候选写入 → 人审/策略验证 → 原子提交”，每次提交保留来源、父版本与回滚指针。
