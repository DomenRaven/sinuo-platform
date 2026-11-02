# 思诺（Seno）—— AI 辅助设计过程管理平台

> 面向工业设计课程项目的 **AI 辅助设计与过程管理平台**。
> 学生用「书—章—页—节点」保存设计过程；四个智能体分工**查证据、选方法、检查过程、提议下一步**；但**所有决定与确认权始终在人手里**。

思诺做的事：把设计过程中「为什么要这么做、依据是什么、试过哪些、怎么决定的」这条推理链，结构化和可回溯地记录下来，并让 AI 在每一步帮你查证、把关、建议。它**不是**训练平台，**不是** CAD/Figma 的替代品，**不做** 3D 建模。

---

## 核心功能

### 三层组织：书 → 章 → 页 → 节点 → 版本

| 层级 | 含义 |
|---|---|
| **书** | 一个设计项目 |
| **章** | 一个设计阶段（固定 8 个模板） |
| **页** | 阶段下的具体议题，同阶段可有平行页 |
| **节点** | 页内的一条推理记录（7 类） |
| **边** | 节点间的关系连线（4 类） |
| **版本** | 每次「固化/确认」产生的快照 |

- **八阶段（固定模板）**：任务与情境 → 用户与场景研究 → 问题及机会定义 → 设计要求 → 概念探索 → 深化与原型 → 测试与迭代 → 表达与复盘。
- **七类节点**：问题、证据、洞察、要求、作品、测试、决定（后三类由人确认）。
- **四类关系边**：承接（derives）、支持（supports）、反例（refutes）、引用（cites）—— 可在页内**拖拽连线**。

### 四个智能体（一次运行至多三个职责）

| 职责 | 做什么 |
|---|---|
| 课题澄清 | 澄清任务目标、汇总简报 |
| 证据查找 | 查证据、给依据、报缺口 |
| 方法建议 | 匹配方法卡 |
| 过程检查 | 读节点与阶段表、判断能否进入下一阶段、提议新节点或关系边 |

- **意图分流**：`查证据`→证据查找；`选方法`→方法建议；`检查下一步`→证据+过程检查；`简报变化`→课题澄清。
- **候选确认机制**：智能体的提议一律先变成「待确认」候选卡，人「预览→确认/拒绝」后才生效。
- **证据可回溯**：每条证据锚定到上传原件（页码/区间），结论能点回出处。

### 三屏界面

| 路由 | 界面 |
|---|---|
| `/projects` | 项目书架（登录、项目列表、新建项目） |
| `/projects/{项目id}` | 项目地图（8 章泳道 + 页缩略图 + 跨章承接连线 + 页拖拽重排） |
| `/projects/{项目id}/pages/{页id}` | 单页工作台（写节点、页内推理图、关系边、中间交付物、对话、读集勾选、候选确认） |

---

## 技术栈

| 模块 | 选择 |
|---|---|
| 运行时 | Python 3.12–3.14 |
| HTTP | FastAPI + Uvicorn |
| 存储 | SQLAlchemy 2 + SQLite（WAL），Alembic 迁移（44 张业务表） |
| 检索 | SQLite trigram 全文索引（证据）+ 字符 n-gram（理论库/方法库） |
| 大模型 | DeepSeek（澄清/证据走 `SINUO_MODEL_NAME`，方法/过程检查走 `SINUO_MODEL_NAME_REASONING`） |
| 智能体编排 | 自研 asyncio 有界 DAG runner（输入不可变、依赖等待、失败阻断、至多 3 职责） |
| 前端 | 单文件 SPA `web/desk.html`（无构建依赖，苹果式视觉） |
| 测试 | pytest + HTTPX TestClient（455 通过 / 2 真实模型测试排除） |

---

## 本地启动

在项目根目录执行（Windows PowerShell）：

```powershell
# 1. 建立虚拟环境（Python 3.12 或以上）
py -3.12 -m venv .venv

# 2. 安装依赖
.\.venv\Scripts\python -m pip install -e ".[dev]"

# 3. 配置环境（如已存在则逐项补字段）
Copy-Item .env.example .env

# 4. 初始化数据库（迁移）
.\.venv\Scripts\alembic upgrade head

# 5. 启动服务
.\.venv\Scripts\uvicorn sinuo.api.app:create_app --factory --host 127.0.0.1 --port 8000
```

访问 **http://127.0.0.1:8000**，使用演示账号登录：

| 项 | 值 |
|---|---|
| 登录名 | `synthetic-lead` |
| 密码 | `synthetic-pass-lead` |

- `/healthz` 表示进程存活；`/readyz` 返回 `status=ready` 与当前迁移版本。
- 默认数据库 `var/sinuo.db`（应用运行数据，被 Git 忽略）。
- 需要真实大模型能力时，在 `.env` 中配置 DeepSeek 密钥与 `SINUO_MODEL_NAME` / `SINUO_MODEL_NAME_REASONING`；未配置时智能体返回「未接入」状态，不影响界面与流程演示。

### 运行测试

```powershell
.\.venv\Scripts\python -m pytest -q
```

### 演示案例

仓库含一套可复现的 **OXO Good Grips 削皮器** 完整设计过程演示（8 章 14 页，公开来源语料）。一键复刻脚本：

```bash
# 合并 OXO 理论语料 + 种子方法卡（幂等）
./.venv/Scripts/python scripts/build_oxo_theory.py
./.venv/Scripts/python scripts/seed_oxo_methods.py
# 全流程驱动（建项目→传证据→8 章→多次提问→候选确认）
./.venv/Scripts/python scripts/oxo_live_walk.py
```

---

## 仓库结构

| 路径 | 责任 |
|---|---|
| `src/sinuo/api` | HTTP 路由、认证依赖 |
| `src/sinuo/domain` | 领域类型、不变量、错误（不依赖 Web/模型/磁盘） |
| `src/sinuo/application` | 用例、确认提交、事务协调、智能体编排 |
| `src/sinuo/infrastructure` | 数据库、原件库、迁移、外部服务适配 |
| `src/sinuo/agents` | 输入/输出合同、专责能力与模型接口 |
| `web/desk.html` | 前端单页应用（书架 / 项目地图 / 单页工作台） |
| `tests/` | 领域约束、HTTP、并发与故障测试 |
| `docs/` | 用户手册、Demo 视频交接文档、权威白皮书 |
| `scripts/` | 演示数据生成、全流程驱动、种子脚本 |

---

## 权威依据与文档

- `docs/思诺用户使用手册.md` —— 终端用户完整手册。
- `docs/DEMO视频交接文档.md` —— 面向演示视频制作的交接说明。
- `docs/reference/` —— 产品白皮书、UI 白皮书、技术实施规范（权威定义）。

---

## 使用许可与来源说明

- OXO 削皮器演示语料来自公开资料（Cooper Hewitt 设计博物馆馆藏、美国专利 US4974286/USRE34194、Smart Design 公开记述、Santoprene 厂商指南、通用设计中心定义），均标注来源，且不含任何编造的样本量/通过率/临床数据。
- 项目为课程作业（《设计大数据基础》），采用 Vibe Coding 方式，借助 Cursor、WorkBuddy 等 AI 编程工具完成需求描述、调试与迭代。
