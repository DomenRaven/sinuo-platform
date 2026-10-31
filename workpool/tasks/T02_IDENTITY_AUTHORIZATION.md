# T02 身份、成员与对象授权

- 状态：done
- 依赖：T01 done
- Owner：Grok 开发A（实现 195e7f24-e144-4156-a172-56fd9488be83；独立审查未做）
- 基线提交或快照：无 Git commit。2026-10-30 基线 pytest 66 passed、ruff 通过。合同 T02-T03-SHARED-1
- 写入范围：`domain/auth/`（动作字面量只从 `public_contract.py` 导入）、`application/auth/`、`infrastructure/auth/`（不建 `throttle_model.py`）、`api/auth/`、`tests/acceptance/test_identity_authorization.py`、`tests/acceptance/identity_fixtures.py`。不写配置、入口、迁移、`models.py`、`domain_models.py`
- 对应门禁：G1；消费者：T04、T05、T06、T07、T08、T09、T10

## 目标

把经验证的账号转换成可信 principal；每项动作同时检查项目成员、对象范围和字段敏感度。角色名称只给默认权限，不能越过对象授权。用户能登录、登出、管理获授权的成员范围；撤权后新的受控读取和写入立即拒绝。

## 输入与交付

按安全合同实现本地账号、Argon2 密码哈希、数据库持久 opaque session、HttpOnly/SameSite Cookie 与适合 Cookie 写请求的 CSRF/Origin 校验。初始账号通过本地初始化命令建立，无默认公开密码；会话秘密使用安全随机值，数据库保存不可逆校验值，退出/到期可撤销。未来接外部身份提供方应通过适配层。

冻结 `Principal`、动作枚举、对象投影和 `authorize(action, resource, principal)` 合同，供所有模块调用。API 不信任客户端的 actor/role/allowed_tools。权限修改在事务中增加 `access_epoch`，权威 ACL 立即生效；异步缓存失效只负责加速传播。保留给 HTTP、SSE、工具、下载、搜索、回执重放和导出的统一检查入口。

## 验收

本任务创建并运行 `python -m pytest -q tests/acceptance/test_identity_authorization.py`，覆盖以下可观察行为：

1. 无凭据、过期或撤销会话拒绝；登录后的主体由服务端决定。
2. 同一项目可读页而无原件权时，原件、缩略图、片段和引用详情均被裁剪。
3. 把别的项目对象 ID 放进已授权 URL，仍拒绝；不回传隐藏标题和数量。
4. Cookie 写请求缺失/错误 CSRF 或不合法 Origin 被拒绝；登出使会话失效。
5. 只有相应管理权限能授予动作；请求正文自报角色无效。
6. 撤权后原有会话、幂等回执读取和授权缓存不能继续取得受限内容。
7. 承接原 T01 的生产认证隔离：生产实例不得注册 pytest 身份替换，开发身份头/参数不能认证；若存在开发认证开关，生产启用必须拒绝。不得为满足测试而新增登录旁路。当前该条未实现/未测，须在本卡提供真实配置/HTTP反例。

## 交接与证据

交出两项目、四种身份（主设计师、团队成员、评审、另一项目成员）的合成夹具，动作矩阵及成功/失败 API 样例。向 T03 交接授权表要求，向其余消费者交接可注入的真实授权接口；测试替身只在测试依赖中出现。记录会话/CSRF 方案和未实现的账号恢复能力，避免将本地认证扩写成企业 SSO 已完成。
