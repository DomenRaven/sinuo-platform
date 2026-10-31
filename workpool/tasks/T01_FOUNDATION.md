# T01 基础、配置与可运行基线

- 状态：done
- 依赖：无
- Owner：Codex（历史 G0 基线交付）；后续代码维护由 Grok 集成 owner 认领
- 基线提交或快照：2026-10-30 12:30 D 盘交付；加固前源码快照见 `_施工文件/2026-10-30_codex/00_任务记录/加固前源码快照.zip`
- 写入范围：本轮集成加固额外拥有 `agents/runner.py`、`agents/contracts.py`、`workers/leases.py`、`infrastructure/database.py`、`infrastructure/models.py`、`migrations/versions/0002_lease_invariants.py`、`tests/test_foundation_boundaries.py` 与施工合同/任务状态一致性修订；原分配范围包括 `pyproject.toml`、依赖锁文件、`.env.example`、`.gitignore`、`src/sinuo/config.py`、`src/sinuo/api/app.py`、数据库连接配置、迁移入口、基础测试与开发命令文档；共同所有权由集成 owner 单独持有
- 对应门禁：G0；后续消费者：T02、T03

## 目标

在新的本地环境中安装、配置、迁移、启动和运行测试。已有骨架是输入材料；先验证实际行为，再决定哪些部分直接沿用。`/healthz` 只证明进程存活，`/readyz` 明示已经具备的基础范围和迁移状态。配置错误、缺迁移和缺必需工具应给出安全且可诊断的失败。

## 输入与交付

读取仓库 README、AGENTS 和施工总入口，核对 Python 3.12、FastAPI、Pydantic Settings、同步 SQLAlchemy 2、Alembic、SQLite WAL 的选型。锁定已经安装并验证的依赖，记录操作系统、Python/SQLite 版本与实际命令。提供 `.env.example` 占位符，默认绑定本机，模型默认关闭；密钥采用服务端秘密字段，日志只输出是否配置。

统一持久运行数据、测试数据和临时施工目录。请求/任务各有独立 Session；短写事务与外部调用的边界要有代码入口。迁移由显式命令执行，启动健康检查不得悄悄创建整套业务表。导出 OpenAPI 的工具可以此时准备，业务端点待对应任务完成后注册。

## 验收入口

以下是应在项目根实际运行的基础命令；主控可按已冻结包管理器调整等价命令并记录。

```powershell
python -m pip install -e ".[dev]"
python -m alembic upgrade head
python -m pytest -q tests/test_api.py tests/test_transactions.py tests/test_runner.py
python -m ruff check src tests migrations
python -m uvicorn sinuo.api.app:create_app --factory --host 127.0.0.1 --port 8000
```

测试临时目录和缓存应使用 AGENTS 规定的当日施工目录。本卡限定 G0 基础范围，至少验证：空库迁移成功、缺迁移时 readiness 失败、正确迁移后 foundation readiness 成功、非法基础配置拒绝、模型关闭时调用明确失败。旧卡的生产认证条目显式移交 T02，尚未实现/验证；本轮未以文档改名代替该验证。最后一项不能用假成功响应替代。

## 交接与证据

提交实际安装/迁移/测试退出码、基础端点响应、锁文件和版本清单；所有失败修复保留最小复现。向 T02/T03 交接配置、Session/UoW、应用注册和测试夹具接口。由集成 owner 在集成基线上重跑后将本卡标 done；此前状态停在 review/verify。

## 当前记录

实际执行结果、合入引用与未验证项由认领者填写。现有骨架声明不自动构成本卡通过证据。

## 集成验收（2026-10-30）

Codex 在 D 盘基线真实执行：uv.lock 冻结安装退出码 0；66 项测试通过；Ruff 与格式检查退出码 0；空库迁移到 0002、alembic check、降到 base 再升级均退出码 0；SQLite integrity_check=ok、foreign_key_check 无错误；真实 Uvicorn 的 healthz/readyz/OpenAPI 均 HTTP 200。独立代码复审已处理阻断项。G0 通过，T02/T03 变为 ready。

完整命令、Schema、告警和未完成边界见 `workpool/handoffs/T01_BASELINE_20261030.md`。未提供任何开发身份 HTTP 旁路；真实身份会话、生产会话配置和 ACL 在 T02 实现验收。此结果只证明 foundation，不代表 G1–G4 或完整 P0。


## 2026-10-30 范围勘误

依据 ADR-003，本卡 done 只表示 G0 基础可启动范围通过；原生产认证隔离验收已移交 T02 且未完成。历史66项基础测试保持其原实测时间，本轮只修文档，未添加或重跑业务测试。产品代码集成由 Grok 负责。
