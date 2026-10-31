# T02/T03 交接

- 上游任务 / owner：T02 身份、T03 领域。集成 owner：Grok 主控。
- 集成基线：无 Git commit。迁移 head `0004_repair_invariants`。合同 `T02-T03-SHARED-2`。ADR-004。
- 消费者：T04 已开始使用 `write_uow` 与领域表。T09 必须调用 `consume_candidate`。
- 契约 / schema 版本：`SCHEMA_REVISION=0004_repair_invariants`。动作与错误码在 `sinuo.domain.public_contract`。
- 实现位置：`src/sinuo/api/auth/`、`src/sinuo/application/auth/`、`src/sinuo/application/cas.py`、`src/sinuo/infrastructure/domain_models.py`、`migrations/versions/0003_domain_identity.py`、`migrations/versions/0004_repair_invariants.py`。
- 权限动作：`authorize(action, resource, principal)`。生产应用不注册身份替换。
- 事务与幂等边界：Argon2 在写事务外。签发前条件核对验密时的口令哈希和认证纪元。候选单行消费不在触发器里承诺。登录失败次数在验密前的同一笔事务记下。
- 成功响应 / 失败响应：登录成功 204 并设置 Cookie。失败为 401 `AUTH_REQUIRED`、403 `ACTION_FORBIDDEN`、404 `NOT_FOUND_OR_HIDDEN`、409 `HEAD_CHANGED`（`details.category=access_epoch`）、429 `REQUEST_BUDGET_EXCEEDED`。
- 最小调用示例或夹具：`tests/acceptance/identity_fixtures.py`、`tests/acceptance/test_identity_authorization.py`、`tests/acceptance/test_domain_schema.py`。
- 真实验证命令与结果：冻结环境 `lock-venv` 的 `python -m pytest -q` 退出码 0，113 passed。解释器 3.14.3，SQLite 3.50.4，argon2-cffi 25.1.0，cffi 2.1.1。`python -m alembic heads` 退出码 0，单一 head。uv 0.13.6，`uv lock` 退出码 0，`uv sync --frozen --extra dev` 退出码 0。3.12 本轮未跑。
- 未验证项 / 已知限制：不是企业 SSO，没有邮件找回。`consume_candidate` 不是 T09 确认命令。原始 SQL 仍可能一次更新多条仍为 pending 的候选。`/readyz` 的 scope 仍是 foundation。真实模型未调用。
- 下游第一次应运行的命令：`python -m pytest -q tests/acceptance/test_identity_authorization.py tests/acceptance/test_domain_schema.py`

审查：T02 [身份复审](8d378391-2d55-47fb-964f-881805c0f53b) blocking 0，限速 note 已复核关闭。T03 [修复复审](aede6c81-0b03-4c7a-82af-db94027b6af6) blocking 0。上一轮审查代理在创建完成前被宿主中止，没有写出文件，也没有留下 agent id。
