# HANDOFF-T09 候选预览与确认提交切片 → T10 / 集成

- 上游任务 / owner：T09 候选预览与确认。本切片不标 done。
- 集成基线：无新迁移。头仍是 `0010_evidence_index`。`SCHEMA_REVISION` 未改。
- 消费者：T10 与未来确认 UI。本切片没有 HTTP 路由。
- 契约 / schema 版本：沿用现有 `candidate_previews`、`candidate_preview_sources`、`candidate_dispositions`。预览与来源行保持追加，触发器拒绝 UPDATE。
- 实现位置：`src/sinuo/application/candidates/`。调用方持有 `write_uow`，服务自己不提交。
- 权限动作：`draft_decision` 要 `page.draft.write` 与来源 `page.read`。`stage_blank` / `merge_blank`（含 `suggest_stage_transition`）要 `decision.confirm`、`page.create` 与每条来源的 `page.read`。`history_branch` 要确切旧版上的 `page.read`、`page.branch` 以及 `page.create`。引用的作品版本要 `preview.read`。提交和重放都重新授权。
- 事务与幂等边界：确认命令名 `candidate.commit`，拒绝命令名 `candidate.reject`。作用域是 `(project_id, actor_id, command, request_key)`。候选状态只经 `consume_candidate` 条件更新。消费、目标写入、处置、审计、Outbox、运行事件、幂等回执在同一个 `write_uow`。任一步异常由该单元回滚，不留成功回执。
- 成功响应 / 失败响应：首次 `201`，同键同体回放 `200`，返回体经 `project_receipt` 投影。预览绑定不符 `PREVIEW_CHANGED`，过期 `PREVIEW_EXPIRED`，来源或未确认草稿失效 `CANDIDATE_STALE`，另一键已消费 `CANDIDATE_ALREADY_RESOLVED`。同键异体是 `IdempotencyConflict`（`IDEMPOTENCY_KEY_REUSED`）。撤权是 `ActionForbidden`，墓碑是 `SourceRedacted`。`add_node`、`add_typed_edge`、`attach_artifact_version` 返回 `not_implemented`，不写预览。
- 最小调用示例或夹具：

```python
with write_uow(engine) as session:
    view = preview_candidate(
        session,
        principal=principal,
        project_id=project_id,
        candidate_id=candidate_id,
        now=now,
        user_patch={"target_title": "夜间取物需求定义"},
    )
with write_uow(engine) as session:
    response, replayed = commit_candidate(
        session,
        principal=principal,
        project_id=project_id,
        candidate_id=candidate_id,
        preview_id=view.preview_id,
        preview_hash=view.preview_hash,
        expected_page_version_id=version_id,
        expected_draft_content_revision=draft_revision,
        request_key=request_key,
        now=now,
        user_patch={"target_title": "夜间取物需求定义"},
    )
```

预览默认 600 秒。`preview_hash` 只由 `bound_preview_hash` 写入，不接受客户端自算的摘要。重新预览插入新行。`draft_decision` 只改草稿和处置。拒绝只写处置。`stage_blank` / `merge_blank` 建主体空白页，显式引用可进节点，来源边分别是 `stage_source` / `merge_source`，并写一条 Decision。`history_branch` 从确切旧版克隆新 ID，边为 `branches_from`，不因源页后来的 HEAD 失效，不复制旧决定或测试。

- 真实验证命令与结果：

```text
.\.venv\Scripts\python.exe -m pytest -q tests/acceptance/test_candidate_commit.py -o cache_dir=_施工文件/2026-11-01_t09/99_临时文件/pytest-cache --basetemp=_施工文件/2026-11-01_t09/99_临时文件/pytest
```

退出码 0。`13 passed`，1 条既有 Starlette/httpx 弃用警告。未跑全库。未对 `var/sinuo.db` 做 upgrade。未发网络，未跑 `live_model`。

- 未验证项 / 已知限制：
  - 没有 HTTP、OpenAPI 例子或 `app.py` 路由。路由模块还不存在。以后新增 `sinuo.api.candidates.routes` 时，在 `create_app` 里只加这一行：`app.include_router(candidate_router)`。
  - `suggest_stage_transition` 走与阶段/合流相同的确认快照规则，但没有单独用这个 action 名做测试。未确认草稿挡住阶段提交的测试用的是 `create_page_from_versions` + `stage_blank`。
  - `add_typed_edge` 与 `attach_artifact_version` 与 `add_node` 一样返回 `not_implemented`。直接测试只打了种子里的 `add_node`。
  - 没有测个人布局变化是否不影响内容候选。
  - 没有单独把作品版本或证据锚点墓碑放进确认路径；测了页版本墓碑和撤 `page.draft.write`。
  - 失败注入测的是草稿候选：消费和草稿 CAS 已经发生、处置尚未写入时抛错，候选与草稿一起回滚。没有在空白页插入中途再注入一次。
  - 回执投影测了去掉 `title` / `summary`，以及撤权后重放拒绝、不退回正文。没有测“命令权还在、只丢掉原文读取权”的半投影。
- 下游第一次应运行的命令：上面的 `test_candidate_commit.py`。不要把 T09 标成 done。

下游确认：主控已重跑上述 pytest，结果为退出码 0、13 passed、1.55s；接手范围为 HTTP 挂载，以及 `add_node`、`add_typed_edge`、`attach_artifact_version` 的真实写入。T09 不标 done。
