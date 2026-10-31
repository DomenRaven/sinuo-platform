# T02 身份与授权提案

状态：提案。未改业务代码，未写迁移，未改 ORM 注册，未跑测试，未做 git commit。本文不是任务完成证据，也不是 T02 完成证据。

读者：集成 owner（合入依赖、配置、入口、表注册与迁移），以及随后按本提案实现的 T02 编码。共享文件仍只由集成 owner 修改。

基线（已读源码，不是新实现）：

- `SCHEMA_REVISION = 0002_lease_invariants`。库内表只有 `idempotency_records`、`outbox_events`、`outbox_deliveries`。
- `create_app` 只注册 `/healthz` 与 `/readyz`。`dependency_overrides` 在工厂内没有写入。
- `Settings` 只有环境、数据库、模型与运行限额。没有 Argon2、会话或 Cookie 字段。`extra="ignore"`。
- `execute_idempotent` 在回放前调用 `authorize(session)`，然后原样返回已保存的 `response_json`。
- `domain/errors.py` 只有幂等冲突、过期租约和模型未启用。
- 动作字面量、成员表和授权表都还没有代码。T03 提案已经描述这些表的物理约束；本提案不修改该文件。

`/readyz` 的 `scope` 保持 `foundation`。挂上登录路由不扩大就绪范围。

## 1. Principal 稳定 ID 与会话解析

稳定 ID 是 `accounts.id`：36 字符 UUID，列宽 `String(64)`。它同时是审计和幂等回执里的 `actor_id`。会话行的 `id`、Cookie 原文、密码哈希都不是主体 ID。

```python
@dataclass(frozen=True)
class Principal:
    account_id: str
    session_id: str
    auth_epoch: int
```

`Principal` 上没有 `role`、`role_label`、`allowed_tools`、授权列表或 `access_epoch`。权限快照只解释历史输入，不放进主体。

生产解析只有一条路径，函数名 `resolve_principal`：

1. 只读取当前部署对应的那一个会话 Cookie。HTTPS 应用不接受 HTTP Cookie 名，HTTP 应用不接受 `__Host-` Cookie 名。
2. 用 `hashlib.sha256` 对 Cookie 值的 UTF-8 字节取摘要，得到 64 位小写十六进制，再按 `sessions.token_sha256` 查行。比较摘要时用 `hmac.compare_digest`。
3. 行必须满足下面的有效会话谓词。`now` 由调用方传入，默认 `time.time()`，单位是 Unix UTC 秒。测试注入合成时钟。谓词不读取 SQLite 的 `datetime('now')`。
4. 用该行的 `account_id` 装入 `Principal`。`auth_epoch` 取账号上的当前值，并且必须与会话行相等。

```text
revoked_at IS NULL
AND sessions.auth_epoch = accounts.auth_epoch
AND accounts.status = 'active'
AND now < idle_expires_at
AND now < absolute_expires_at
```

客户端以下字段全部忽略，不能用来选择账号、会话或角色：JSON 里的 `actor_id`、`owner_id`、`role`、`role_label`、`allowed_tools`；查询参数里的同名键；请求头 `X-Actor-Id`、`X-Role`、`X-Sinuo-Account-Id`、`X-Dev-User`、`X-Session-Id`、`Authorization`。业务命令写入 `CommandScope.actor_id` 时，只能抄 `principal.account_id`。

登录成功新发会话。服务端不接受客户端事先指定的令牌或 `session_id`。登录不撤销该账号的其他会话。退出只撤销当前 Cookie 对应的那一行。禁用账号和重置密码在同一事务把 `accounts.auth_epoch` 加 1，旧会话即使漏写 `revoked_at` 也过不了谓词。

同一请求里的授权与业务写入共用调用方已经打开的 `Session`。`resolve_principal` 与 `authorize` 不另开连接，不在 `write_uow` 里调用网络或密码以外的慢操作。Argon2 验证放在登录事务之外；验证成功后再进短事务写会话或按需重哈希。

## 2. 动作枚举、常量名、authorize 与 ResourceRef

合同 06 的核心动作一共 **23** 个。Python 名是 `Action` 这个 `StrEnum` 的成员。调用处使用成员，不把任意字符串送进 `authorize`。未知字符串在进入枚举时就失败。

| 常量名 | 字面量 | 能否写入 `object_grants.action` |
| --- | --- | --- |
| `PROJECT_READ` | `project.read` | 能 |
| `PROJECT_CREATE` | `project.create` | 不能。只看 `accounts.can_create_project` |
| `STAGE_CONFIGURE` | `stage.configure` | 能 |
| `PAGE_CREATE` | `page.create` | 能 |
| `PAGE_READ` | `page.read` | 能 |
| `PAGE_DRAFT_WRITE` | `page.draft.write` | 能 |
| `PAGE_CHECKPOINT` | `page.checkpoint` | 能 |
| `PAGE_BRANCH` | `page.branch` | 能 |
| `DECISION_CONFIRM` | `decision.confirm` | 能 |
| `ARTIFACT_UPLOAD` | `artifact.upload` | 能 |
| `ARTIFACT_ORIGINAL_READ` | `artifact.original.read` | 能 |
| `PREVIEW_READ` | `preview.read` | 能 |
| `EVIDENCE_READ` | `evidence.read` | 能 |
| `EVIDENCE_CREATE` | `evidence.create` | 能 |
| `RUN_CREATE` | `run.create` | 能 |
| `RUN_READ` | `run.read` | 能 |
| `RUN_CANCEL` | `run.cancel` | 能 |
| `TEST_RECORD` | `test.record` | 能 |
| `OPINION_CREATE` | `opinion.create` | 能 |
| `OPINION_EDIT_OWN` | `opinion.edit_own` | 能 |
| `REPORT_EXPORT` | `report.export` | 能 |
| `ACL_MANAGE` | `acl.manage` | 能 |
| `PRIVACY_DELETE` | `privacy.delete` | 能 |

`OBJECT_GRANT_ACTIONS` 是上表除 `PROJECT_CREATE` 以外的 22 个字面量，顺序固定。`ALL_ACTIONS` 是 23 个。

公开签名：

```python
class Authorizer(Protocol):
    def authorize(self, action: Action, resource: ResourceRef, principal: Principal) -> None: ...
```

生产实现类名 `SessionAuthorizer`。构造时绑定这一次命令的 `Session`，方法签名仍然是上面三个参数。方法内部读取当前成员、当前授权行、`projects.access_epoch` 和对象状态，再调用纯函数 `decide`。`decide` 放在 `domain/auth/decide.py`，不导入 FastAPI、SQLAlchemy、文件系统或模型 SDK。

`context_snapshots.authorization_scope_json`、模型输出、文件路径和请求正文都不能当作 `decide` 的事实来源。

```python
@dataclass(frozen=True)
class ResourceRef:
    project_id: str | None
    scope_type: str
    stage_id: str | None = None
    page_id: str | None = None
    page_version_id: str | None = None
    artifact_id: str | None = None
    artifact_version_id: str | None = None
    evidence_anchor_id: str | None = None
    sensitivity: str | None = None
    availability: str | None = None
    author_account_id: str | None = None
```

`scope_type` 只允许：`project`、`stage`、`page`、`page_version`、`artifact`、`artifact_version`、`evidence_anchor`、`account`。前七个与授权表的 `scope_type` 相同。`account` 只配合 `project.create`，此时 `project_id` 和六个目标都为空，并且不写授权行。

其余 `scope_type` 与 T03 提案的 CHECK 相同：对应的那一个目标非空，其余目标为空；`project` 范围六个目标都为空，对象就是 `project_id`。`sensitivity` 只允许 `project`、`sensitive`、`released`。`availability` 只允许 `available`、`tombstoned`、`abandoned`。`author_account_id` 只用于 `opinion.edit_own`。

`decide` 的通过条件是交集，默认拒绝：

1. 主体已经由有效会话得到，账号 `status='active'`。
2. `project.create`：`can_create_project=1`，资源范围是 `account`。不读取成员表。
3. 其他动作：`project_id` 必须存在；成员行 `status='active'`。没有成员行，或成员已撤销，结果是隐藏，不是 403。
4. 存在覆盖该资源的 `status='active'` 授权行，动作相等。项目级授权覆盖本项目内该动作的下级对象。阶段级授权只覆盖该 `stage_id`。页级授权只覆盖该 `page_id` 及其页版本。作品、作品版本、证据锚点只覆盖自己的 id。页的读权限不推导原件读权限。
5. 同时是两个项目的成员，也不把另一个项目的对象算进本次资源。URL 里的项目 id 必须等于资源的 `project_id`。
6. `opinion.edit_own` 还要求 `author_account_id == principal.account_id`。作者身份不够，当前授权也必须还在。
7. `sensitive` 的原文、摘录、缩略图和引用详情，只有打在该 `artifact_version` 或 `evidence_anchor` 上的 `artifact.original.read` / `evidence.read` 才能通过。项目级或页级的 `page.read`、`preview.read` 不够。
8. `released` 可以由覆盖该对象的 `preview.read` 或 `page.read` 看到。`project` 级敏感度跟随对应的普通读动作。
9. `tombstoned`：调用方本来就能看见父对象时返回已删除；调用方本来就看不见时返回隐藏。`abandoned` 的页仍然按 `page.read` 判断，不因此隐藏。

一个动作需要多份来源时，调用方对每一份资源各调用一次 `authorize`。有任何一份是跨项目或隐藏，整次调用按隐藏处理。辅助函数名 `authorize_many`，内部只循环 `authorize`。

缓存键函数名 `authorization_cache_key(account_id, project_id, object_version_id, access_epoch)`。P0 的 `SessionAuthorizer` 每次读库，不保存可绕过 ACL 的缓存。调用方若自建缓存，纪元与 `projects.access_epoch` 不一致就丢弃。缓存清理事件只加速传播。敏感响应带 `Cache-Control: no-store`。P0 不签发可长期绕过 ACL 的公共链接。

## 3. 账号、会话、CSRF、Cookie、期限与 Argon2

P0 是本地账号。邮件找回、第三方登录和企业 SSO 不在本提案里，也不把本地认证写成已经接通外部身份提供方。外部身份以后走单独适配层，不读取请求头里的用户名。

### 账号

登录名是新列 `accounts.login_name`，不是 `display_name`。`display_name` 可以重复，只用于显示。

- 入库前做 Unicode NFKC，再 `casefold`，去掉首尾空白。
- 长度 1 到 64 个码位。拒绝空白和控制字符。
- `UNIQUE`。没有 `password` 明文列，没有单独的盐列。
- `password_hash` 必须以 `$argon2id$` 开头。
- 首位维护者由本机 CLI 在账号表为空时创建，`can_create_project=1`。密码来自 `getpass.getpass`，确认输入两次。出现 `--password` 或把口令放进 argv 时，进程直接失败，不写库。
- 命令：`python -m sinuo.application.auth.bootstrap`。表非空时这条命令失败。验收夹具调用库函数插入合成账号，不开放 HTTP 注册。
- 禁用和重置密码只在 CLI：`disable`、`reset`。两者都把 `auth_epoch` 加 1。重置后的新口令同样不经过 argv。
- HTTP 没有创建账号、修改口令或找回口令的路由。

口令最短 12、最长 128。与规范化登录名相同则 CLI 拒绝。登录接口对外不区分“用户不存在”“口令错误”和“账号已禁用”。未知登录名仍对进程内的哑哈希做一次 Argon2 验证，再返回同一个 401。哑哈希在进程启动时生成，不对应任何账号，不写入日志。

### 会话

令牌是 `secrets.token_urlsafe(32)`，至少 32 字节熵。浏览器只持有 Cookie 原文。数据库保存 `token_sha256`、`account_id`、`auth_epoch`、`created_at`、`last_active_at`、`idle_expires_at`、`absolute_expires_at`、`revoked_at`，以及可空的 `csrf_sha256`。没有 `token`、`session_token` 或 `secret` 列。

| 期限 | 配置键 | 环境变量 | 默认秒数 |
| --- | --- | --- | --- |
| 空闲 | `session_idle_seconds` | `SINUO_SESSION_IDLE_SECONDS` | 28800（8 小时） |
| 绝对 | `session_absolute_seconds` | `SINUO_SESSION_ABSOLUTE_SECONDS` | 86400（24 小时） |

创建会话时：`absolute_expires_at = now + session_absolute_seconds`，此后不改；`idle_expires_at = now + session_idle_seconds`，并且不超过绝对期限；`last_active_at = now`。已认证请求在谓词通过之后，若 `last_active_at` 早于 `now - 60`，才把空闲期限向前滑到 `min(now + idle, absolute_expires_at)`。滑动失败不放宽谓词。`now` 已经达到任一期限时返回 401，不滑动。

退出把 `revoked_at` 从 NULL 写成一次。重复退出没有副作用：没有 Cookie，或 Cookie 已经无效，都返回 204，不新建会话，不改其他行。

登录、登出和 `GET` 会话不调用 `execute_idempotent`，不读不写 `idempotency_records`。请求里若带 `Idempotency-Key`，身份路由忽略它。其余修改命令仍用 03 的幂等合同。`PATCH` 访问控制就是这样的业务命令。

### CSRF 与 Origin

CSRF 令牌同样是 `secrets.token_urlsafe(32)`。登录响应不带这个明文。`GET /api/v1/session` 在已有 Cookie 时返回明文，数据库只存 `csrf_sha256`。客户端把它放在请求头 `X-CSRF-Token`。它不是第二枚 Cookie。

`POST`、`PUT`、`PATCH`、`DELETE` 在携带会话 Cookie 时，同时检查 Origin 和 CSRF。登录是例外：当时还没有 CSRF，只检查 Origin 并限速。`GET /api/v1/session` 不检查 CSRF。

Origin 必须与配置的允许列表逐字相等。缺 Origin、列表外的 Origin、以及 `*` 都拒绝，并且不创建、不撤销会话。CSRF 缺失和 CSRF 不匹配对外是同一个 403 正文。拒绝时不撤销会话，避免跨站请求把人登出。日志和错误正文不回显 Cookie、口令、CSRF 明文或 Origin 原文。

### Cookie

两种 Cookie 都是 host-only（不写 `Domain`）、`HttpOnly`、`SameSite=Lax`、`Path=/`。名称是代码常量，不是配置开关：

| 部署 | Cookie 名 | `Secure` |
| --- | --- | --- |
| `public_url` 方案为 `http`，主机必须是 `127.0.0.1` | `sinuo_session` | 否 |
| `public_url` 方案为 `https` | `__Host-sinuo_session` | 是 |

HTTP 模式不使用 `__Host-` 前缀。退出时用同一名称、同一 `Path` 和同一 `Secure` 标志发送 `Max-Age=0`。`Max-Age` 在登录时取剩余绝对期限，不长于 86400 秒的默认绝对期限。

`SINUO_PUBLIC_URL` 默认 `http://127.0.0.1:8000`。`SINUO_ALLOWED_ORIGINS` 是逗号分隔列表，默认 `http://127.0.0.1:8000`。列表里出现空项或 `*` 时，配置对象拒绝启动。`environment=production` 且方案不是 `https` 时拒绝启动。`http` 且主机不是 `127.0.0.1` 时拒绝启动。空闲秒数大于绝对秒数时拒绝启动。

本机监听保持现有的 `127.0.0.1`。P0 不注册 `CORSMiddleware`，响应不带 `Access-Control-Allow-Origin: *`，也不把通配 Origin 和 Cookie 一起使用。

### Argon2id

实现库是 `argon2-cffi` 的 `PasswordHasher`，`type` 固定为 `Type.ID`。不提供 Argon2i 或 Argon2d 的配置项。参数映射：

| 配置键 | 环境变量 | 传入 `PasswordHasher` | 默认 | 下限 |
| --- | --- | --- | --- | --- |
| `argon2_time_cost` | `SINUO_ARGON2_TIME_COST` | `time_cost` | 3 | 3 |
| `argon2_memory_kib` | `SINUO_ARGON2_MEMORY_KIB` | `memory_cost` | 65536 | 19456 |
| `argon2_parallelism` | `SINUO_ARGON2_PARALLELISM` | `parallelism` | 4 | 1 |
| `argon2_hash_len` | `SINUO_ARGON2_HASH_LEN` | `hash_len` | 32 | 16 |
| `argon2_salt_len` | `SINUO_ARGON2_SALT_LEN` | `salt_len` | 16 | 16 |

默认值取 RFC 9106 的第二组参数（64 MiB、`t=3`、`p=4`）。内存下限是 19 MiB，避免配置被降到该公开下限以下。登录验证成功后，若 `check_needs_rehash` 为真，在随后的短事务里更新 `password_hash`。重哈希不增加 `auth_epoch`，不撤销刚刚签发的会话。哈希串不是 `$argon2id$` 开头时，按登录失败处理，不用另一种算法验证。

### 登录限速

限速同时看账号和来源。来源只取 `request.client.host`，不信任 `X-Forwarded-For`。

表 `login_throttles`：

| 列 | 约束 |
| --- | --- |
| `bucket_key` | `String(128)` 主键。`acct:` 或 `src:` 再加 SHA-256 十六进制 |
| `window_started_at` | Float，Unix UTC 秒 |
| `attempts` | Integer，`>= 0` |
| `updated_at` | Float |

表内没有口令、令牌、Cookie 或未哈希的登录名。窗口默认 300 秒，失败上限默认 5 次，配置键 `login_rate_limit_window_seconds`、`login_rate_limit_attempts`。两个桶都未超限才验证口令。超限返回 429，并带 `Retry-After`。Origin 不合法时不增加计数。登录成功删除账号桶。过期窗口可以更新或删除桶。这张表不是业务历史。

### 本任务拥有的路由

路由模块自带前缀 `/api/v1`。

| 方法与路径 | 行为 |
| --- | --- |
| `POST /session` | JSON：`login_name`、`password`。`extra=forbid`。成功 204 并设置 Cookie。失败 401。不返回 CSRF 明文 |
| `GET /session` | 200，`data` 只有 `account_id`、`display_name`、`csrf_token`。没有角色和授权列表 |
| `DELETE /session` | 退出。成功或重复调用都是 204。坏的 CSRF 为 403，会话保持有效 |
| `PATCH /projects/{project_id}/access` | 合同 04 的访问控制修改。需要 `acl.manage`、Origin、CSRF 和 `Idempotency-Key` |

`PATCH` 正文 `extra=forbid`。出现 `role`、`actor_id`、`owner_id`、`allowed_tools` 时返回 422 `INVALID_SCHEMA`。`role_template` 只接受 `lead_designer`、`team_member`、`reviewer`。它在服务端展开成授权行，展开之后授权仍只读授权表。

成功包络沿用 `data` 与 `meta`。身份失败包络见第 5 节。会话与访问控制响应设置 `Cache-Control: no-store`。

## 4. memberships、object_grants、项目隔离与 access_epoch

物理列、外键和部分唯一索引以 T03 提案第 6 节为准。本节固定授权语义。T02 不另起一套表名，不把页范围塞进成员 JSON。

### memberships

| 列 | 语义 |
| --- | --- |
| `id` | 主键，UUID 文本 |
| `project_id` | 所属项目 |
| `account_id` | 账号 |
| `status` | `active` 或 `revoked` |
| `role_label` | 可空显示文本。`authorize` 不读它 |
| `created_at` | Float |
| `revoked_at` | 可空。重新加入时写回 NULL |

唯一键：`(project_id, account_id)` 和 `(project_id, id)`。外键：`project_id → projects.id`，`account_id → accounts.id`，都是 `ON DELETE RESTRICT ON UPDATE RESTRICT`。禁止物理删除。同一人重新加入是把这一行改回 `active`，不插入第二行。`project_id`、`account_id`、`id` 冻结。

### object_grants

列：`id`、`project_id`、`account_id`、`action`、`scope_type`、`status`、`created_at`、`revoked_at`、`created_by_account_id`，以及可空目标 `stage_id`、`page_id`、`page_version_id`、`artifact_id`、`artifact_version_id`、`evidence_anchor_id`。

- `action` 只能是 22 个对象动作。`project.create` 没有授权行。
- `status` 只有 `active` 和 `revoked`。没有 `effect='deny'`。没有行就是拒绝。
- `created_by_account_id` 是当时的 `principal.account_id`，外键指向 `accounts.id`。
- 每个非空目标使用 `(project_id, 目标 id)` 复合外键指向父表的 `(project_id, id)`。写错项目的页、阶段、作品或证据，由复合外键拒绝。
- 唯一键不能靠含空列的普通 UNIQUE。每种 `scope_type` 在 `status='active'` 上使用部分唯一索引：项目级为 `(project_id, account_id, action)`；页级再加 `page_id`；其余级别加上自己的目标列。名称与定义权在 T03 物理提案，本提案不另起索引名。
- 禁止物理删除。撤销只把 `status` 改为 `revoked` 并写 `revoked_at`。不能从 `revoked` 改回 `active`。再次允许是插入新行。

成员已撤销时，即使旧授权行仍是 `active`，`authorize` 也拒绝。两条都有效才通过。

### 角色只展开默认动作

展开函数名 `grants_for_template`。它只在 `acl.manage` 命令里把模板写成授权行。`authorize`、`decide` 和会话解析都不调用它。改 `role_label` 而不写授权行，权限不变。

任何模板都不包含 `project.create`、`acl.manage`、`privacy.delete`。

| `role_template` | 夹具身份 | 默认动作 | 范围 |
| --- | --- | --- | --- |
| `lead_designer` | 主设计师 | `project.read`、`stage.configure`、`page.create`、`page.read`、`page.draft.write`、`page.checkpoint`、`page.branch`、`decision.confirm`、`artifact.upload`、`artifact.original.read`、`preview.read`、`evidence.read`、`evidence.create`、`run.create`、`run.read`、`run.cancel`、`test.record`、`opinion.create`、`opinion.edit_own`、`report.export` | 项目级。共 20 个动作 |
| `team_member` | 团队成员 | `project.read`；以及每个被分配页面上的 `page.read`、`page.draft.write` | `project.read` 为项目级。页动作必须带非空 `page_ids`，按页写授权。不含确认、分支、原件、预览、证据、运行和意见 |
| `reviewer` | 评审 | `project.read`；共享页上的 `page.read`、`preview.read`、`opinion.create`、`opinion.edit_own` | 页级，必须带 `page_ids`。不含 `artifact.original.read`，不含项目级 `evidence.read`。敏感访谈不随页读出现 |

创建项目的发起者另外得到一行项目级 `acl.manage`。这是创建事务里的种子函数 `seed_creator_grants`，不是角色名推导。种子不包含 `privacy.delete`。`can_create_project` 仍是账号列，不能由 `PATCH` 访问控制修改。

`commit_acl` 在写入后统计该项目仍有效的项目级 `acl.manage`。结果为 0 则整笔回滚，避免把项目管理员全部撤销。

评审确认候选、团队成员修改别人的意见，都由动作授权和 `author_account_id` 拒绝。前端是否画出按钮不参与判断。

### 项目隔离与 access_epoch

嵌套 URL 先绑定服务端对象，再授权。把另一个项目的页、作品或证据 id 放进已授权项目的 URL，结果是隐藏。两个项目的成员身份不能合并成一次运行的范围。

`projects.access_epoch` 从 1 起。已有项目上的成员状态变化、授权插入和授权撤销，在同一 `write_uow` 里执行：

```sql
UPDATE projects
SET access_epoch = access_epoch + 1
WHERE id = :project_id AND access_epoch = :expected_access_epoch;
```

受影响行数必须为 1。同一条命令里多行授权只加 1，不加 `projects.revision`。行数不是 1 则整笔回滚，对外使用 409 `HEAD_CHANGED`，`details` 只有 `{"category":"access_epoch"}`。不新增错误码。

这次事务同时写审计和 Outbox。Outbox 的 `event_type` 是 `project.access_changed`。`payload` 只有 `project_id`、`access_epoch`、`subject_account_id`、`grant_ids`。审计命令名是 `acl.manage`。两边都不写标题、口令、令牌或摘录。

新纪元在本事务提交后立即用于下一次 `authorize`。读路径不等待缓存失效事件。

创建项目时的插入把 `access_epoch` 设为 1，并在同一事务调用 `seed_creator_grants`。那时还没有旧纪元可加。`access_epoch` 加 1 的语句只用于已经存在的项目。

## 5. 统一错误码与安全投影

不新增错误码。域异常在 `domain/auth/errors.py`：`AuthRequired`、`ActionForbidden`、`NotFoundOrHidden`、`SourceRedacted`。HTTP 映射在 `api/auth/errors.py`。限速异常 `RateLimited` 在应用层。映射结果：

| 情况 | HTTP | `error.code` | `retryable` |
| --- | --- | --- | --- |
| 无 Cookie、摘要无行、空闲到期、绝对到期、已撤销、认证纪元不一致、登录失败、账号禁用 | 401 | `AUTH_REQUIRED` | false |
| 已认证，对象在其可见范围内，但动作不被允许；CSRF 失败；Origin 失败 | 403 | `ACTION_FORBIDDEN` | false |
| 无成员、跨项目、对象不在分配范围内、猜 ID | 404 | `NOT_FOUND_OR_HIDDEN` | false |
| 父对象仍可见，目标已有墓碑 | 410 | `SOURCE_REDACTED` | false |
| 登录超过限速 | 429 | `REQUEST_BUDGET_EXCEEDED` | true |
| 访问控制正文出现 `role` 等禁止字段，或模板不合法 | 422 | `INVALID_SCHEMA` | false |
| 同一幂等键不同正文 | 409 | `IDEMPOTENCY_KEY_REUSED` | false |
| `expected_access_epoch` 不匹配 | 409 | `HEAD_CHANGED` | false |

401 的对外正文不区分失败原因。403 的 `details` 只允许 `action` 或 `category`。`category` 只允许 `csrf` 或 `origin`。404 的 `details` 是空对象。错误正文、`message` 和 `details` 都不含标题、计数、磁盘路径、SQL、口令、Cookie 或令牌。

包络：

```json
{
  "error": {
    "code": "AUTH_REQUIRED",
    "message": "Authentication required.",
    "details": {},
    "retryable": false,
    "request_id": "request-id"
  }
}
```

`message` 使用固定英文短句：401 为 `Authentication required.`，403 为 `Action not allowed.`，404 为 `Not found.`。

无凭据的登录失败和业务请求失败都走 401。不返回“还差几次口令”。429 只额外带 `Retry-After`。

跨项目和不存在的 id 使用同一状态、同一 `code`、同一 `message`、同一空 `details`。除 `request_id` 外正文一致。响应里不出现对方标题，也不出现隐藏对象的 `count`、`total`、`hidden_count`、`redacted_count`、`omitted`。列表先按当前授权过滤，再对可见项分页；不做未过滤的 `COUNT(*)`。

同一项目里能读页、没有原件权时，投影函数 `project_visible_fields` 去掉这些内容：原件字节与 `object_key`、缩略图、摘录、引用定位，以及会暴露它们的标题。敏感对象的聚合计数一起去掉，不改成“已隐藏 N 条”。`preview.read` 只保留 `released` 与 `project` 敏感度的预览。这个函数是页、搜索、下载和导出的共同投影。T02 不实现那些业务路由；拥有路由的任务在返回前调用它。

撤权后的回执：

- 业务回执仍由现有 `execute_idempotent` 保存。T02 不修改该文件。
- T02 的命令包装 `execute_authorized_command` 先让 `authorize` 在回放前运行。授权失败时不把已保存正文交给客户端。
- 授权仍通过但字段已不可见时，`project_receipt` 按当前权限裁剪后再返回。没有对象 id 的标题字符串直接去掉。已保存的计数键全部去掉，不替换成隐藏数量。
- 主体本身已经不可见时，返回 404 或 410，不返回原 `data`。
- 不更新 `idempotency_records` 里的旧正文。权限恢复后，同一次重放可以再按当时的当前权限放出仍被允许的字段。这不是新的业务写入。
- 仍可留下的键只有调用方现在有权看见的 id、`status`、`project_revision`、`audit_event_id`、`access_epoch`。`idempotency_replayed` 只在真正返回已保存结果时为真。
- 登录和登出没有回执，撤权测试不从这两条路由读取旧正文。

`PATCH` 访问控制自己的回执只含新的 `access_epoch` 和授权 id。

检查顺序在访问控制路由上固定为：解析会话、校验 Origin 与 CSRF、确认是该项目的有效成员、确认 `acl.manage`、比较纪元、写入。非成员即使猜中纪元也得到 404，与项目不存在相同。成员缺少管理权时得到 403，正文仍不带标题。

## 6. 生产认证隔离与测试反例

不引入开发身份 HTTP 开关，也不在 `Settings` 里增加 `dev_auth`、`allow_identity_header`、`pytest_user` 或同类字段。因此不存在“生产环境开启该开关后被拒绝”的运行路径。生产实例要证明的是：工厂没有注册 pytest 替换，伪造的头和参数不能变成主体。

`create_app` 不接受主体覆盖参数，不写 `app.dependency_overrides`。`get_principal` 只调用 `resolve_principal`。pytest 里若要替换主体，只允许测试函数在自己的应用对象上设置 `dependency_overrides`。七条行为验收和下面的生产反例都不使用这个替换，它们走真实登录或无 Cookie 请求。

验收文件里的生产反例名：

| 测试函数 | 证明的行为 |
| --- | --- |
| `test_production_create_app_registers_no_pytest_identity_override` | `environment=production` 且 `public_url` 为 https 时，`create_app` 返回后 `dependency_overrides` 仍为空 |
| `test_forged_identity_header_and_query_do_not_authenticate` | `X-Actor-Id`、`X-Role`、`X-Sinuo-Account-Id`、`X-Dev-User`、`X-Session-Id`、`Authorization: Bearer dev`，以及查询参数 `actor_id`、`role`、`allowed_tools`，得到 401，并且不设置会话 Cookie |
| `test_forged_actor_and_role_body_do_not_select_principal` | 登录正文里的 `actor_id`、`role`、`owner_id`、`allowed_tools` 被拒绝，不能选出账号 |
| `test_dev_auth_env_ignored_and_does_not_authenticate` | 设置 `SINUO_DEV_AUTH=1` 与 `SINUO_ALLOW_IDENTITY_HEADER=1` 后，`Settings` 不因此获得对应属性；伪造头仍然 401 |
| `test_no_dev_login_route_on_production_app` | 生产应用没有 `/dev/login` 或同类旁路路径 |
| `test_production_http_public_url_is_rejected` | 生产环境配上 `http` 的 `public_url` 时，配置对象拒绝构造。这是 Cookie 策略，不是身份头开关 |

行为验收使用合成夹具，不用上述替换：

- `test_missing_expired_and_revoked_session_rejected`
- `test_login_principal_comes_from_server_session`
- `test_page_projection_strips_original_thumbnail_excerpt_citation_and_hidden_counts`
- `test_foreign_project_id_in_authorized_url_hides_title_and_count`
- `test_cookie_write_rejects_missing_or_wrong_csrf_and_bad_origin`
- `test_logout_revokes_session_and_repeat_is_safe`
- `test_only_acl_manage_can_grant_and_body_role_is_rejected`
- `test_revocation_blocks_session_receipt_replay_and_auth_cache`
- `test_login_and_logout_do_not_write_idempotency_records`
- `test_role_label_without_grants_does_not_authorize`
- `test_http_cookie_is_host_only_httponly_lax_without_host_prefix`
- `test_https_cookie_uses_host_prefix_secure_and_path_root`
- `test_auth_responses_have_no_wildcard_cors`
- `test_bootstrap_refuses_password_on_command_line`

夹具是两个合成项目和四种身份：主设计师、团队成员、评审、另一项目成员。显示名前缀 `synthetic-`。隐藏标题使用合成 canary。口令使用明显的合成字符串。数据标成合成，不读真实访谈，不指向 `var/` 或参考文档。

撤权反例的会话 Cookie 在撤权后仍然能通过身份解析，直到空闲期限、绝对期限或认证纪元变化。它不能再读到被撤销的标题、正文、缩略图或回执里的这些字段。缓存反例使用含旧 `access_epoch` 的键，断言该键不能通过当前纪元校验。

将来若有人要加开发认证配置，生产启动必须拒绝它。本提案的配置 diff 不加入这个字段。验收不能把“源码里没有开关”写成“已经测过生产拒绝该开关”。

待运行的验收命令（本提案没有执行）：

```powershell
python -m pytest -q tests/acceptance/test_identity_authorization.py
```

该测试依赖第 7 节的表和依赖已经合入。合入前失败时，不另写一套私有迁移把测试变成绿色。

## 7. 认领的写入文件与交给集成 owner 的 diff

### 7.1 本代理认领、实现时才写入

这些路径现在还不创建。公共枚举模块 `src/sinuo/domain/schema_types.py` 不在清单里。`domain/auth/actions.py` 是授权模块自己的常量，合入公共枚举后改为从那里导入，不把第三份字面量写进 `models.py`。

- `src/sinuo/domain/auth/__init__.py`
- `src/sinuo/domain/auth/principal.py`
- `src/sinuo/domain/auth/actions.py`
- `src/sinuo/domain/auth/resource.py`
- `src/sinuo/domain/auth/decide.py`
- `src/sinuo/domain/auth/roles.py`
- `src/sinuo/domain/auth/projection.py`
- `src/sinuo/domain/auth/errors.py`
- `src/sinuo/application/auth/__init__.py`
- `src/sinuo/application/auth/authorizer.py`
- `src/sinuo/application/auth/sessions.py`
- `src/sinuo/application/auth/passwords.py`
- `src/sinuo/application/auth/csrf.py`
- `src/sinuo/application/auth/acl.py`
- `src/sinuo/application/auth/receipts.py`
- `src/sinuo/application/auth/bootstrap.py`
- `src/sinuo/infrastructure/auth/__init__.py`
- `src/sinuo/infrastructure/auth/cookies.py`
- `src/sinuo/infrastructure/auth/throttle_model.py`
- `src/sinuo/infrastructure/auth/throttle.py`
- `src/sinuo/api/auth/__init__.py`
- `src/sinuo/api/auth/routes.py`
- `src/sinuo/api/auth/dependencies.py`
- `src/sinuo/api/auth/schemas.py`
- `src/sinuo/api/auth/errors.py`
- `tests/acceptance/test_identity_authorization.py`
- `tests/acceptance/identity_fixtures.py`

不认领：`pyproject.toml`、锁文件、`src/sinuo/api/app.py`、`src/sinuo/config.py`、`src/sinuo/infrastructure/models.py`、`src/sinuo/domain/errors.py`、`src/sinuo/domain/schema_types.py`、`src/sinuo/application/idempotency.py`、`migrations/versions/`、`tests/conftest.py`、T03 提案。

下游从 `sinuo.domain.auth` 取 `Principal`、`Action`、`ResourceRef`，从 `sinuo.application.auth` 取 `SessionAuthorizer`。测试替身只出现在 `tests/` 的依赖里。验收测试使用真实的 `SessionAuthorizer` 和临时库。

### 7.2 交给集成 owner 的共享改动

T02 不直接修改这些文件。

**依赖。** 在 `pyproject.toml` 的运行依赖加入 `argon2-cffi>=23.1.0,<26`，并更新锁文件。它不是开发额外依赖。不加入 Redis、认证中间件包或 CORS 额外包。

**路由挂载点。** 在 `create_app` 里、应用构造完成之后：

```python
from sinuo.api.auth.routes import router as auth_router
from sinuo.api.auth.errors import register_auth_exception_handlers

app.include_router(auth_router)
register_auth_exception_handlers(app)
```

同时保持：不写 `dependency_overrides`；不增加主体覆盖参数；不注册 `/dev/login`；不添加 `CORSMiddleware`；不把 `/readyz` 的 `scope` 改成别的字面量；访问日志不记录 `Cookie`、`Authorization`、`X-CSRF-Token` 和口令字段。异常处理器只捕获第 5 节列出的异常和现有的 `IdempotencyConflict`。

**配置。** 在 `Settings` 增加第 3 节的键：`public_url`、`allowed_origins`、`session_idle_seconds`、`session_absolute_seconds`、`argon2_time_cost`、`argon2_memory_kib`、`argon2_parallelism`、`argon2_hash_len`、`argon2_salt_len`、`login_rate_limit_attempts`、`login_rate_limit_window_seconds`，以及对应的校验。Cookie 名不要做成配置。不要增加开发身份字段。

**表注册。** `login_throttles` 的 ORM 在 `infrastructure/auth/throttle_model.py`，使用现有 `Base`。`models.py` 在 `Base` 定义之后增加：

```python
import sinuo.infrastructure.auth.throttle_model  # noqa: F401
```

账号、会话、成员和对象授权表的 ORM 沿用 T03 的 `domain_models` 侧面导入，T02 不建第二套映射。`accounts` 需要补上 `login_name`。漏掉 throttle 导入时，`/readyz` 不会检查这张表，因此导入必须和迁移、`SCHEMA_REVISION` 同一次合入。

**迁移内容（owner 写入 `migrations/versions/`，保持单一 head）。** 在尚未合入的领域迁移里加上 `login_name` 和 `login_throttles`。若那一次修订已经冻结合入，则另开后续修订，不改 `0001` 与 `0002`。`login_throttles` 使 T03 提案的 37 张表再增加 1 张。降级时先删 `login_throttles`，再按 T03 的子表到父表顺序删除。不使用 batch 模式重建账号表。

## 8. 与 T03 的边界

T02 拥有授权语义：主体如何从会话得到、23 个动作如何判定、角色模板如何展开成授权行、会话和 CSRF 的期限、错误如何投影、回执如何按当前权限裁剪、生产进程如何拒绝伪造身份。

T03 拥有并交给集成 owner 合入的部分：表的物理列、CHECK、触发器、部分唯一索引、复合外键、删除顺序，以及 `schema_types.py` 里的闭集。`object_grants.action` 的 CHECK 使用 22 个对象动作，不包含 `project.create`。成员表的 `role_label` 不参与 CHECK 到动作的映射。

两边一致的部分直接沿用，不另起同义列：

- 会话有效谓词、令牌与 CSRF 只存 SHA-256、绝对期限列冻结、空闲期限可滑动但不超过绝对期限。
- 授权行只追加撤销，成员行在同一行上 `active` 与 `revoked` 之间变化。
- 外键 `RESTRICT`。项目隔离靠 `(project_id, id)` 复合外键，不靠应用层单独记住。
- 已有项目的授权变更把 `access_epoch` 加 1，不加 `revision`。
- `command_receipts` 不建表，业务回执就是 `idempotency_records`。
- 非账号主体不在 P0。`object_grants.account_id` 只指向 `accounts`。
- 敏感度词表沿用 `project` / `sensitive` / `released`。要改词只改这一闭集，列名 `sensitivity` 不变。

`domain/auth` 可以在集成后导入 `domain/schema_types` 的动作元组。`schema_types` 不导入 `domain/auth`。两处字面量不一致时，以合同 06 的 23 个动作和本节的 22 个对象动作为准，由集成 owner 在共享合同里收成一份。T02 不编辑 `schema_types.py` 来完成这种收束。

`acl_commit` 若放在 T03 的领域函数里，它检查的是已有项目的纪元有没有加 1。创建项目的种子写入不调用这条“旧值加 1”检查。触发器仍然不是权限边界；`authorize` 才是。

T04 及以后的读、工具、SSE、下载、导出、候选预览和提交，在各自的入口调用同一个 `SessionAuthorizer.authorize`。它们不从请求正文恢复角色。页投影和回执投影调用 `project_visible_fields` 与 `project_receipt`。

## 9. 未决点

1. `accounts.login_name` 的语义已经确定，T03 提案的 `accounts` 列里还没有它。物理上放进尚未合入的领域修订，还是放进下一次修订，由集成 owner 决定。实现不改用 `display_name` 登录。
2. `login_throttles` 会计入 T03 提案的 37 张表之外。它跟哪一个 Alembic revision 一起成为 head，由集成 owner 决定。T02 不创建 `migrations/versions/` 文件。
3. 创建项目时纪元从无到 1，并在同一事务种下创建者授权。这和“每次授权变更都要对旧纪元加 1”如果被写成同一个函数，创建会被误拒。需要共享合同写明：加 1 只约束已有项目的 `UPDATE`。在这份共享合同写明之前，两份提案的函数边界还没有被集成 owner 收束。
4. 访问纪元冲突本提案复用 `409 HEAD_CHANGED`，`details.category` 为 `access_epoch`，因而不另写 ADR。若集成 owner 认为这会把页头冲突和授权纪元混成同一个码，需要先有 ADR-004 再增加错误码。ADR 出现之前，实现按这个复用码编写，不私自增加码。

邮件找回、第三方登录、企业 SSO、服务账号和开发身份 HTTP 开关不是待选方案。前四项留在 P0 之外；开发身份 HTTP 开关不引入。
