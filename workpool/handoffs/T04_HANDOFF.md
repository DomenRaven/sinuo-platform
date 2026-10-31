# T04 交接

- 上游任务 / owner：T04 草稿、确认版本与幂等命令。集成 owner：Grok 主控。
- 集成基线：无 Git commit。迁移 head 仍是 `0004_repair_invariants`。全量 pytest 142 passed，退出码 0。
- 消费者：T05、T06、T09。
- 契约 / schema 版本：沿用 `T02-T03-SHARED-2`。本段没有新迁移。
- 实现位置：`src/sinuo/application/versions/`、`src/sinuo/api/versions/`。草稿审计在 `save_draft`。确认版和分支在写入审计前留有 `before_audit`。
- 权限动作：`project.create`、`page.create`、`page.draft.write`、`page.checkpoint`、`page.branch`、`page.read`。主体只来自会话。
- 事务与幂等边界：领域函数不提交。路由用 `execute_authorized_command` 包在 `write_uow` 里。撤权后先授权失败，不返回库存正文。
- 成功响应 / 失败响应：草稿冲突 409 `DRAFT_REVISION_CONFLICT`。同键异体 409 `IDEMPOTENCY_KEY_REUSED`。成员撤销后重放 404 `NOT_FOUND_OR_HIDDEN`。自报 `actor_id` 为 422。
- 真实验证命令与结果：`.venv` 的 `python -m pytest -q` 退出码 0，142 passed。版本专项由复审代理再跑，退出码 0，29 passed。见 `workpool/handoffs/reviews/T04_GAP_REVIEW.md`。blocking 0。4 条 note 是覆盖拆分，不阻断。
- 未验证项：没有一条测试同时用两个 HTTP 连接打草稿 409。分支同键异体没有单独用例。真实模型未调用。
- 下游第一次应运行的命令：`python -m pytest -q tests/acceptance/test_versions_http.py tests/acceptance/test_versions_checkpoint_commands.py`
