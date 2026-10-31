# 思诺后端施工骨架

Grok 从 `prompts/00_GROK_总控开工.md` 开工，先读 `docs/construction/00_施工总览与完成标准.md`。施工合同、任务卡和参考资料都在本仓库内。

本仓库提供后端开发起点。已完成的范围是配置、健康检查、数据库迁移与事务基元、幂等命令存储、Outbox 投递租约，以及可替换模型的有界任务执行器。项目、章页、作品、ACL、候选提交、SSE、搜索和导出仍需按施工文档实现。当前没有 UI。

## 技术栈

| 模块 | 选择 | 当前状态 |
| --- | --- | --- |
| 运行时 | Python 3.12–3.14 | 依赖范围见 pyproject.toml |
| HTTP | FastAPI + Uvicorn | `/healthz`、`/readyz` 可运行 |
| 配置 | Pydantic Settings | `.env` / `SINUO_` 环境变量，密钥使用 SecretStr |
| 关系存储 | SQLAlchemy 2 + SQLite WAL | FK、busy_timeout、短 `BEGIN IMMEDIATE` 写事务 |
| 迁移 | Alembic | `0001_primitives` 创建 3 张基础表，`0002_lease_invariants` 追加租约一致性约束 |
| 测试 | pytest + HTTPX TestClient | 真实 SQLite 迁移、双连接竞争、回滚、幂等与租约过期 |
| 模型 | `ModelProvider` 协议 | `DisabledModelProvider` 显式返回未接入状态，未调用 DeepSeek |
| 任务执行 | Python asyncio 有界 DAG | 输入值不可变、依赖等待、失败阻断、至多 3 个职责 |

包版本采用兼容区间，交付包含 `uv.lock`；优先按锁文件安装，更新依赖后重新核验。不要将“依赖可解析”当作所有业务功能已实现。

## 本地启动

在项目根打开 PowerShell。使用已有 Python 3.12 或以上虚拟环境；新的环境可按下列命令建立。依赖安装会访问包源。

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python -m pip install -e ".[dev]"
Copy-Item .env.example .env
.\.venv\Scripts\alembic upgrade head
.\.venv\Scripts\uvicorn sinuo.api.app:create_app --factory --host 127.0.0.1 --port 8000
```

若 `.env` 已存在，保留现有内容并逐项补字段。默认数据库为 `var/sinuo.db`，此目录属于应用运行数据并被 Git 忽略。`/healthz` 表示进程存活；`/readyz` 仅在当前基线迁移版本匹配时返回 `scope=foundation`。应用启动不会自动建表，也不会在迁移缺失时假报可用。

测试目录指向本任务施工目录，避免临时测试数据库落入用户资料目录。日期按实际执行日替换。

```powershell
New-Item -ItemType Directory -Force '_施工文件/2026-10-29_codex/99_临时文件' | Out-Null
$env:PYTHONDONTWRITEBYTECODE = '1'
.\.venv\Scripts\python -m pytest -q -o cache_dir='_施工文件/2026-10-29_codex/99_临时文件/pytest-cache' --basetemp='_施工文件/2026-10-29_codex/99_临时文件/pytest'
```

## 目录及所有权

| 路径 | 责任 |
| --- | --- |
| `src/sinuo/api` | HTTP 请求、响应、认证依赖与 SSE 适配器 |
| `src/sinuo/domain` | 领域类型、不变量、错误；不依赖 Web、模型和磁盘 |
| `src/sinuo/application` | 用例、确认提交、事务协调与授权调用 |
| `src/sinuo/infrastructure` | 数据库、原件库、外部服务适配 |
| `src/sinuo/agents` | 输入/输出合同、专责能力与模型接口 |
| `src/sinuo/workers` | Outbox、租约、派生任务执行 |
| `migrations` | 模型变更与迁移；由集成负责人统一合入 |
| `tests` | 对应领域约束、HTTP、并发与故障测试 |

## 当前基元的正确使用方式

`write_uow(engine)` 在 SQLite 上建立短写事务。`execute_idempotent` 必须在此边界内调用，并从可信认证上下文取得项目、操作者和动作。它首先调用当前授权检查，再按“项目 + 操作者 + 命令 + 请求键”查记录；同键异体抛 `IdempotencyConflict`。命令效果、Outbox 和幂等结果由调用者一次提交。授权回调在重放结果时同样执行。示例测试里的 `allow` 仅供测试使用，业务路由必须接入真实 ACL。

`claim_delivery` 用条件更新抢占待处理或已过期的租约，成功时递增 fencing token。工作在事务外执行；回写派生结果和 `complete_delivery` 必须处于同一短事务，后者按 token、owner、状态和截止时间拦截旧工作者。远端外部副作用还需要独立的对方幂等键；该基元不声称跨系统恰好一次。

`DagRunner` 是可测试的内存执行边界，尚未接入持久 AgentRun、任务恢复、读工具网关和候选发布。它只返回独立结果，不执行领域命令。开发完整编排时保留当前依赖与失败测试，并增加持久状态、取消、租约、晚到隔离和当前权限复核。`model_enabled=true` 只验证配置，不会自动把未实现的 DeepSeek 传输变成可用服务。

## 下一步开发边界

先实现领域 schema、身份与对象 ACL、版本/草稿 CAS、不可变作品原件、确认提交事务，再接持久编排和只读工具。对外发布业务路由前，其授权、重放和越权测试必须通过。测试日志只描述已实际运行的命令与结果；本骨架自身不能证明完整产品通过验收。


## 当前施工状态（2026-10-30）

T01 / G0 已验收；T02 身份与授权、T03 领域模型与迁移可并行认领。完整范围、66 项测试与迁移/启动证据见 [基础交接](workpool/handoffs/T01_BASELINE_20261030.md)。Grok 继续使用总控提示词并以工作池最新状态推进，不从零重做施工包。整体进度见 [续工验收记录](施工续工验收记录_20261030.md)。

若已安装 uv，可用 `uv sync --frozen --extra dev` 按交付锁文件建立环境；将 UV_PROJECT_ENVIRONMENT/UV_CACHE_DIR 指向 AGENTS 规定的施工临时目录。README 中的 pip 安装是兼容路径，不能据此宣称使用了锁定版本。


## 文档勘误后的入口

当前状态以 [当前施工基线](当前施工基线.md) 和工作池为准。具体代码由Grok施工，Codex负责架构/文档与验收监督；T01 done只指G0基础范围，生产认证隔离在T02待实现。技术/UI原文已受控勘误，旧报告与旧83项清单保留为历史。

立即开工请读 [Grok开工入口](GROK_START_HERE.md)；真实多代理要求编码宿主具备文件、终端和子代理工具，提示词本身不会赋予这些能力。
