# T02/T03 共享合同

版本：`T02-T03-SHARED-2`。日期：2026-10-30。集成 owner：Grok 主控。这是实现依据。两份提案仍保留为阅读材料；下面的收束覆盖提案里的未决点。ADR-004 关闭 N2、N3、N4 的分层方式，并补上 jobs 终态。

提案：

- T02：`workpool/handoffs/proposals/T02_IDENTITY_PROPOSAL.md`（代理 `d3d3ce60-35ce-41ea-a593-30f24c72e1bc`）
- T03：`workpool/handoffs/proposals/T03_DOMAIN_PROPOSAL.md`（代理 `975ceb30-670f-4f9f-9f7b-c56038bb03d2`）

产品语义沿用施工 03、04、06。本文件不新增错误码，因此没有 ADR-004。

## 1. 已收束的决定

1. `accounts.login_name` 放进 `0003_domain_identity`，不登录 `display_name`。入库值是 NFKC + casefold + 去首尾空白，1 到 64 个码位，`UNIQUE`。
2. `login_throttles` 同属 `0003`。合入后一共 38 张表：既有 3 张基元表，T03 提案的 34 张新表，再加这张限速表。T03 提案第 14 节“限速表以后再建”作废。
3. 动作字面量、错误码、角色模板名、授权范围、敏感度、可用性、Cookie 名只定义在 `src/sinuo/domain/public_contract.py`。T02 与 T03 都从这里导入，禁止再写一份字面量。
4. 访问纪元冲突返回 `409`，`error.code` 为 `HEAD_CHANGED`，`details` 只有 `{"category":"access_epoch"}`。
5. 敏感度闭集：`project`、`sensitive`、`released`。
6. 墓碑不更新 `page_versions` 或 `artifact_versions`。读路径负责隐藏。不提供红acted UPDATE。
7. `project.create` 只看 `accounts.can_create_project`。不建 `account_capabilities`。
8. P0 主体只有账号。`memberships.account_id` 与 `object_grants.account_id` 外键指向 `accounts.id`。
9. 证据只存 `excerpt_hash` 与定位，不存摘录正文。
10. `jobs.subject_id` 多态，不建外键。
11. `/readyz` 的 `scope` 保持 `foundation`。`SCHEMA_REVISION` 只有在 `0003` 合入时才改成 `0003_domain_identity`。在此之前保持 `0002_lease_invariants`。
12. 新建项目插入 `revision=1`、`access_epoch=1`。`access_epoch + 1` 只用于已经存在的项目上的授权变更，不加 `projects.revision`。创建项目不调用这条加 1 检查。
13. 不修改 `0001_primitives.py` 与 `0002_lease_invariants.py`。不改 `execute_idempotent` 的签名。回执重放仍先授权；HTTP 层再用当前权限投影正文，撤权后不得把受限字段原样返回。
14. 候选的行级触发器不判断 SQL 是否漏写 WHERE。单目标消费只走 `consume_candidate`。影响行数不是 1 则抛错，整笔 `write_uow` 回滚。
15. 登录签发前必须条件核对验密时的 `password_hash` 与 `auth_epoch`。重哈希不能覆盖重置后的新口令。禁用和重置仍把 `auth_epoch` 加 1。
16. 登录名触发器调用 `sinuo_login_name_canonical`。未规范化的直接插入会被拒绝，不会被改写成规范值。
17. 迁移 head 是 `0004_repair_invariants`。已标 `0003` 的库执行 upgrade head 才会得到本轮触发器。

## 6. 终态

`run_tasks` 进入 `succeeded`、`failed` 或 `cancelled` 后，状态、fencing token 和租约都不能再改。`retry_wait` 可以回到 `queued`。`jobs` 进入 `succeeded`、`failed`、`cancelled` 或 `superseded` 后，状态、fencing token、租约和 attempt 都不能再改。认领只针对 queued，或租约已经过期的 running。这两条由 `0004` 的触发器执行。T06 实现 Worker 时不得绕开它们。

## 2. 身份与授权

`Principal` 只有 `account_id`、`session_id`、`auth_epoch`。没有角色、工具白名单或 `access_epoch`。生产解析只读当前部署那一枚会话 Cookie，对令牌做 SHA-256，再按 T02 提案第 1 节的有效会话谓词查库。客户端的 `actor_id`、`role`、`allowed_tools`、`X-Actor-Id`、`X-Role`、`X-Dev-User`、`Authorization` 一律忽略。

`authorize(action: Action, resource: ResourceRef, principal: Principal) -> None` 由 `SessionAuthorizer` 实现。纯函数 `decide` 不导入 FastAPI、SQLAlchemy 或模型 SDK。默认拒绝。`project.create` 不读成员表。没有成员或跨项目返回隐藏（404 `NOT_FOUND_OR_HIDDEN`），不返回标题和数量。页读权限不推出原件读权限。`opinion.edit_own` 还要求作者就是当前账号，并且当前授权仍在。

角色模板只在 `acl.manage` 里展开成授权行。`authorize` 不读 `role_label`。三种模板的动作集合以 T02 提案第 4 节为准，且都不含 `project.create`、`acl.manage`、`privacy.delete`。创建者在创建事务里另得一行项目级 `acl.manage`。该项目有效的项目级 `acl.manage` 不能被减到 0，否则整笔回滚。

会话：令牌 `secrets.token_urlsafe(32)`，库中只有 SHA-256。空闲默认 28800 秒，绝对默认 86400 秒。登录轮换新会话，不撤销该账号其他会话。退出只撤销当前行；重复退出 204。禁用和重置密码把 `accounts.auth_epoch` 加 1。Argon2id 参数见 `Settings`。验证在写事务之外。登录、登出、`GET /api/v1/session` 不写 `idempotency_records`。

Cookie 名由 `session_cookie_name(public_url)` 决定：HTTP 且主机为 `127.0.0.1` 时用 `sinuo_session` 且不设 `Secure`；HTTPS 用 `__Host-sinuo_session` 且 `Secure`。都是 host-only、`HttpOnly`、`SameSite=Lax`、`Path=/`。不注册 CORS。生产配置若 `public_url` 不是 https，启动失败。不引入开发身份 HTTP 开关，生产应用不写 `dependency_overrides`。

`PATCH /api/v1/projects/{project_id}/access` 需要 `acl.manage`、Origin、CSRF 与 `Idempotency-Key`。正文出现 `role`、`actor_id`、`allowed_tools` 时返回 422 `INVALID_SCHEMA`。

登录限速同时看账号桶和来源桶。来源只用 `request.client.host`。表内不存口令、令牌或明文登录名。

## 3. 领域表与迁移

物理列、唯一约束、复合外键、触发器和降级顺序以 T03 提案第 6–10 节为准，并加上：

- `accounts.login_name`：`String(128)`，非空，唯一。`password_hash` 必须以 `$argon2id$` 开头。
- `login_throttles`：`bucket_key` `String(128)` 主键，`window_started_at` Float，`attempts` Integer 且 `>= 0`，`updated_at` Float。允许更新和删除。无外键。

`page_versions` 与 `artifact_versions` 使用无条件 `BEFORE UPDATE` / `BEFORE DELETE` 触发器 `RAISE(ABORT)`。0003 不用 Alembic batch 模式。头指针不建外键，避免降级环。

迁移文件只由集成 owner 放入 `migrations/versions/0003_domain_identity.py`。`down_revision = 0002_lease_invariants`。升级不改写三张基元表的已有行。开发库可以从空的领域表降回 0002；含确认版本的库不用降级当恢复。

草稿修订、布局修订、页 `version_no`、项目 `revision`、阶段配置 `version_no`、作品 `version_no`、候选 `revision`、意见 `revision`、运行事件序号、`access_epoch`、`auth_epoch` 互不赋值。

## 4. 文件 owner

同一时段一个文件只有一个写入者。审查者只读。

| 写入者 | 路径 |
| --- | --- |
| 集成 owner | `pyproject.toml`、锁文件、`src/sinuo/config.py`、`src/sinuo/domain/public_contract.py`、`src/sinuo/infrastructure/models.py`、`src/sinuo/api/app.py`、`migrations/versions/0003_domain_identity.py`、工作池状态与本文件 |
| 开发 A（T02） | `src/sinuo/domain/auth/` 下除动作字面量定义以外的模块、`src/sinuo/application/auth/`、`src/sinuo/infrastructure/auth/`（不含 `throttle_model.py`）、`src/sinuo/api/auth/`、`tests/acceptance/test_identity_authorization.py`、`tests/acceptance/identity_fixtures.py` |
| 开发 B（T03） | `src/sinuo/domain/schema_types.py`、`src/sinuo/infrastructure/domain_models.py`、`tests/acceptance/test_domain_schema.py`、`workpool/handoffs/proposals/0003_domain_identity.draft.py` |

`domain_models.py` 使用已经存在的 `Base`，包含账号、会话、限速和全部领域表。`models.py` 只在合入时侧面导入它，并把 `SCHEMA_REVISION` 改成新 head。T02 不建第二套账号或授权映射。`login_throttles` 的 ORM 在 `domain_models.py`，不在 `throttle_model.py`。

`schema_types.py` 可以导入 `public_contract`，不能导入 `domain.auth`。`domain.auth` 可以导入 `public_contract` 与 `schema_types` 里的纯函数。

下游接口变化先改本文件的版本号，再改代码。T04 及以后调用 `SessionAuthorizer.authorize`，不从请求恢复角色。

## 5. 验收入口

T03：`python -m pytest -q tests/acceptance/test_domain_schema.py`。断言按 T03 提案第 13 节，表数量改为 38，修订号为 `0003_domain_identity`。

T02：`python -m pytest -q tests/acceptance/test_identity_authorization.py`。覆盖任务卡 7 条，包括生产应用不注册 pytest 身份替换、伪造开发头不能认证。测试数据合成，临时库放在当天施工目录。默认不跑 `live_model`。

两边都绿，并且基线 `pytest -m "not live_model"` 与 `ruff check src tests migrations` 仍通过之后，集成 owner 才把对应任务标成 `done`。
