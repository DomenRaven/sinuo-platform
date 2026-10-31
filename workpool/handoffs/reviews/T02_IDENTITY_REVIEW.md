# T02 身份复审

对照 ADR-004 第 2 条、共享合同 `T02-T03-SHARED-2` 第 2 节、`src/sinuo/application/auth/sessions.py` 的 `authenticate_login`、`src/sinuo/application/auth/bootstrap.py`、`src/sinuo/api/app.py` 的路由挂载，以及 `tests/acceptance/test_identity_authorization.py` 的竞态测试。只读。没有改代码、测试、迁移或其他文件。没有读密钥或 `.env`。

结论：**blocking 0 条，note 1 条。** R-AUTH-01 不能再签发新纪元会话。

本次没有执行 `python -m pytest`。下面凡是写到测试，都是在读断言，不是测试通过的记录。没有退出码。

## R-AUTH-01

验密之后、签发之前有条件核对。`authenticate_login` 在写事务外调用 `verify_password`；需要重哈希时，`hash_password` 也在这个间隙里（`sessions.py` 第 153–158 行）。随后的 `write_uow` 才执行更新（第 168–186 行）：

```text
WHERE id = 验密时的账号
  AND status = 'active'
  AND auth_epoch = 验密时的纪元
  AND password_hash = 验密时的哈希
SET password_hash = 重哈希结果，或仍是那条旧哈希
```

`rowcount != 1` 时抛 `AuthRequired`，到不了 `issue_session`。行数是 1 之后还要 `session.get`，并且 `account.auth_epoch` 必须仍等于验密时的纪元，否则同样抛 `AuthRequired`。`issue_session` 把会话上的 `auth_epoch` 写成这个账号对象上的值，因此写进去的纪元就是验密时的纪元。

重置若已把 `password_hash` 换成新口令并把 `auth_epoch` 加 1，这条 WHERE 对不上。重哈希字符串不会覆盖新口令，`issue_session` 不会被调用。即便略过行数检查，同一笔新事务里 `session.get` 读到的也是新纪元，第二道比较仍会拒绝。写事务在异常时回滚（`database.py` 的 `write_uow`），不会把半笔会话提交出去。

`test_password_reset_between_verify_and_issue_does_not_mint_session`（第 901–977 行）用 `Barrier(2)`，在 `verify_password` 返回前让另一线程提交重置，然后再放行登录线程。函数体内没有 `sleep`。`@pytest.mark.parametrize("rehash", [False, True])` 覆盖不重哈希和重哈希。两条都断言 `AuthRequired`、库中哈希仍是重置后的新哈希、`auth_epoch == 2`、会话表没有行。重哈希那一支把库存哈希做成另一组 Argon2 参数，重置哈希是另一条口令；若签发路径把重哈希写回去，`password_hash == replacement` 不会成立。

本机 Python 3.14 自带的 SQLite 3.50.4 上，用 `sqlite3` 对一行做“把 `password_hash` 写成原值”的 UPDATE，`rowcount` 和 `changes()` 都是 1；WHERE 对不上时是 0。这只说明这个运行时不会把不重哈希的核对更新看成 0 行。它不是验收测试，也没有经过本仓库的 pytest。

## 逐条

| 检查项 | 结论 |
| --- | --- |
| 会话解析 | 通过 |
| 生产身份隔离 | 通过 |
| Cookie / Origin / CSRF | 通过 |
| 退出与过期 | 通过 |
| 禁用 | 通过 |
| 密码重置 | 通过 |
| 限速 | note |
| ACL 条件更新 | 通过 |
| 当前权限下的幂等重放 | 通过 |
| CLI | 通过 |
| Argon2 在写事务外 | 通过 |

通过项不计入 note。

## note

### N1. 限速检查和记次分成两笔事务，并行失败可以在记次前一起验密

- 文件：`src/sinuo/application/auth/sessions.py` 第 143–166 行；`src/sinuo/infrastructure/auth/throttle.py` 第 27–72 行。
- 合同：提案第 3 节。两个桶都未超限才验证口令。窗口默认 300 秒，失败上限默认 5 次。超限返回 429 和 `Retry-After`。Origin 不合法不计数。登录成功删除账号桶。来源只用 `request.client.host`。

串行路径是按这个上限做的。`inspect_buckets` 在账号桶或来源桶的当前窗口里看到 `attempts >= limit` 就返回限速，此时还没有调用 `verify_password`。验证失败后，另一笔 `write_uow` 对两个桶 `record_failure`。成功登录只 `clear_bucket` 账号桶（`sessions.py` 第 185 行）。桶键是 `acct:` 或 `src:` 加 SHA-256，不存口令、令牌或明文登录名。`client_host` 只读 `request.client.host`，应用代码不读 `X-Forwarded-For`。非法 Origin 在检查桶之前就抛 `ActionForbidden`。

空窗在两笔事务之间。检查事务提交并放开 `BEGIN IMMEDIATE` 之后才做 Argon2，失败记次更晚。同一窗口内同时到达、并且都在任一记次提交前读到 `attempts < limit` 的请求，会全部进入验密。记次本身会排队，不会把同一次加一丢掉；这一批结束后，次数会超过上限，再来的请求才被拒绝。所以上限约束的是“已经记入表的失败”，不是“同时在飞的验密次数”。

`tests/acceptance/test_identity_authorization.py` 没有 429、`Retry-After` 或 `login_throttles` 的断言。上面这段是读代码，不是跑出来的结果。

## 通过的检查

### 会话解析

`resolve_principal`（`sessions.py` 第 189–215 行）只接受调用方给出的令牌。HTTP 层的 `session_token` 只读 `session_cookie_name(public_url)` 那一枚 Cookie，不读 `Authorization`、`X-Dev-User` 或查询参数。库中按 SHA-256 查找，再用 `hmac.compare_digest` 核对摘要。谓词同时要求：行存在、`revoked_at` 为空、会话纪元等于账号当前纪元、账号 `status == active`、`now` 小于空闲期限并且小于绝对期限。返回的 `Principal` 只有 `account_id`、`session_id`、`auth_epoch`。

空闲滑动在谓词通过之后，由 `slide_session` 单独做。`now` 已经到达任一期限就直接返回，不把期限改大。新的空闲期限是 `min(now + idle, absolute_expires_at)`。`GET /api/v1/session` 和访问控制路由在 `resolve_principal` 之后才滑动。

### 生产身份隔离

`create_app` 只挂 `auth_router`（`app.py` 第 30 行），前缀 `/api/v1`。路由是 `POST/GET/DELETE /session` 和 `PATCH /projects/{project_id}/access`。没有 `/dev/login`，也没有第二套身份路由。工厂不写 `dependency_overrides`。`Settings` 使用 `extra="ignore"`，模型字段里没有 `dev_auth` 或 `allow_identity_header`。`environment == "production"` 且 `public_url` 不是 https 时，配置对象拒绝构造（`config.py` 第 54–55 行）。http 的主机只能是 `127.0.0.1`。应用没有注册 `CORSMiddleware`。

### Cookie / Origin / CSRF

`attach_session_cookie` 不写 `Domain`，`HttpOnly`，`SameSite=lax`，`Path=/`。http 用 `sinuo_session` 且 `secure=False`；https 用 `__Host-sinuo_session` 且 `secure=True`。退出用同一名称、同一 `Path` 和同一 `Secure` 清除。

登录只查 Origin，不要求 CSRF。Origin 必须与允许列表逐字节相等；空值、`*` 和不在列表中的值都拒绝，并且发生在签发之前。`PATCH` 的顺序是解析会话、Origin、CSRF、滑动、`acl.manage`，然后才解析正文和幂等键。`DELETE /session` 对仍有效的会话先查 Origin 和 CSRF，失败抛 `ActionForbidden`，不写 `revoked_at`。缺失的 CSRF 和错误的 CSRF 都走 `secret_matches` 的失败，对外同一类 403。CSRF 明文只在 `GET /api/v1/session` 返回，库存 SHA-256；登录响应不带这份明文。`GET` 每次轮换 CSRF。合同不要求 GET 携带旧 CSRF。

### 退出与过期

`logout_session` 只给当前解析出来的那一行写 `revoked_at`。没有 Cookie，或 Cookie 已经不能通过谓词，返回 `ABSENT`，不改其他行。重复退出再次解析时看到 `revoked_at`，仍是 `ABSENT`。路由对这两种情况都回答 204。坏的 Origin 或 CSRF 在写入之前抛出，会话保持有效。

到期由 `now < idle_expires_at and now < absolute_expires_at` 拒绝，并且不滑动。合同要求此时返回 401。退出只撤销当前仍能解析的那一行。

### 禁用

`disable_account` 在写事务里按账号 id、当前 `auth_epoch` 和 `status == active` 更新：纪元加 1，状态改为 `disabled`，写下 `disabled_at`。`rowcount != 1` 则失败并回滚。`resolve_principal` 在状态不是 `active` 或纪元不一致时拒绝。登录签发的 WHERE 同样要求 `status == active`，禁用账号对不上，不签发。

口令正确但状态已经不是 `active` 时，这次失败不调用 `record_failure`。口令错误仍会计次。这条路径不能把禁用账号登入。

### 密码重置

`reset_password` 先在事务外做 `hash_password`，再按 id 和当前纪元把 `auth_epoch` 加 1 并换上新哈希。`rowcount != 1` 则失败。它不把状态改回 `active`。旧会话的纪元对不上账号上的新纪元，`resolve_principal` 拒绝。同一口令的参数重哈希不在登录路径里加纪元，这和 ADR-004 第 2 条一致；重置和禁用才加纪元。

### ACL 条件更新

`bump_access_epoch` 先要求新纪元正好是旧纪元加 1，再执行：

```text
UPDATE projects
SET access_epoch = access_epoch + 1
WHERE id = :project_id AND access_epoch = :expected
```

`rowcount != 1` 抛 `AccessEpochConflict`，由外层 `write_uow` 回滚。这条语句不改 `revision`。授权插入、成员变更、撤销和“仍有一条项目级 `acl.manage`”的检查都在同一笔事务里；最后没有项目级管理员时抛 `ActionForbidden`，纪元加 1 一并回滚。`commit_acl` 在改纪元之前再次 `authorize(acl.manage)`。

### 当前权限下的幂等重放

`execute_authorized_command` 先核对 `scope.actor_id` 就是当前主体，再把 `authorizer.authorize` 交给 `execute_idempotent`。保存过的正文只在这次授权通过之后返回。授权失败时客户端拿不到库存 JSON。返回前用 `project_receipt` 裁剪，并且不把裁剪结果写回 `idempotency_records`。

`project_receipt` 去掉计数键，以及不在簿记白名单里、又不是 id 键的字段。标题、缩略图、摘录因此留不下来。白名单含 `id`、`status`、`project_revision`、`audit_event_id`、`access_epoch`、`grant_ids`，外加以 `_id` / `_ids` 结尾的键。访问控制路由实际放进响应的是 `access_epoch` 和 `grant_ids`。撤权后的页读重放会在 `authorize` 处失败；验收测试把这个顺序写成：先重放成功并断言标题不在投影里，撤权后再断言 `NotFoundOrHidden`，同时库存 JSON 里的标题还在。

`SessionAuthorizer` 每次读取当前成员和当前 `active` 授权行，实例上不保存授权结果。提案写过方法内部读取 `projects.access_epoch`。当前类没有读这一列，也没有可把旧纪元继续放行的缓存。调用方自建缓存时用 `cache_key_is_current` 比较纪元。判定本身来自当前行。

### CLI

`argv_supplies_password` 拒绝 `--password`、`-p`、`--pass`、`--pwd` 以及参数中含 `password=` 的形式，并且在读配置和写库之前就以状态 2 退出。口令来自两次 `getpass`。`init` 在 `BEGIN IMMEDIATE` 里看到任一账号就失败。`disable` 和 `reset` 都用上面的条件更新。错误输出是固定的 `bootstrap failed`，不回写口令。

### Argon2 在写事务外

登录的 `verify_password` 和按需的 `hash_password` 位于限速事务提交之后、签发事务开始之前。未知账号和非 `$argon2id$` 哈希改走进程内哑哈希，哑哈希不入库。`create_initial_account` 和 `reset_password` 都是先得到哈希，再进入 `write_uow`。签发事务里只有条件更新、插入会话和清除账号桶，没有 Argon2。

## 限速复核

原 N1 **关闭**。这次复核没有新的 blocking。上文开头的 “note 1 条” 指复核前的 N1，不再单独开放。

`authenticate_login` 现在把 `inspect_buckets` 和 `record_failure` 放进验密前的同一笔 `write_uow`（`sessions.py` 第 143–158 行）。这笔事务是 `BEGIN IMMEDIATE`，提交之后才调用 `verify_password`（第 161 行）。已经达到上限时，在 `record_failure` 之前抛 `RateLimited`，事务回滚，被拒绝的请求不会再记一次。两个并行的错误口令不能再一起读到旧次数：后拿到写锁的那一个会看见已经提交的次数。上限为 1 时，一个进入验密并因口令错误得到 `AuthRequired`，另一个得到 `RateLimited`。

成功签发仍只 `clear_bucket(keys[0])`（第 184 行），清的是账号桶。Argon2 仍在这笔记次事务之外。

`test_parallel_login_failures_cannot_both_pass_the_limit`（第 982–1025 行）把 `login_rate_limit_attempts` 设为 1，用 `Barrier(2)` 同时启动两个错误口令，断言结果恰好是 `auth` 和 `limited` 各一个。没有 `sleep`。这次没有重跑 pytest，没有退出码。
