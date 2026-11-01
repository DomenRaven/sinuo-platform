# HANDOFF-T09 三个普通候选写入草稿 → T10 / 集成

- 上游任务 / owner：T09 候选预览与确认。本切片不标 done。
- 集成基线：无新迁移。头仍是 `0010_evidence_index`。`SCHEMA_REVISION` 未改。0001 到 0010 的 SQL 未改。
- 消费者：T10 与未来确认 UI。本切片没有 HTTP 路由。
- 契约 / schema 版本：沿用现有 `candidate_previews`、`candidate_preview_sources`、`candidate_dispositions`、`page_drafts`。草稿正文仍是 `canonical_document` 的 schema_version 1。
- 实现位置：`src/sinuo/application/candidates/proposal.py`、`preview.py`、`commit.py`。仍走现有 `preview_candidate` / `commit_candidate`。没有第二套提交函数。调用方持有 `write_uow`。
- 权限动作：三个动作与 `draft_decision` 一样要 `page.draft.write` 和来源页版本的 `page.read`。`attach_artifact_version` 以及带 `artifact_version_id` 的 `add_node` 还要该作品版本的 `preview.read`，并且作品版本未墓碑。`suggest_stage_transition` 仍走已有的 `stage_blank` 确认，没有新命令。
- 事务与幂等边界：TX-05。`commit_candidate` 用 `consume_candidate` 消费候选，条件更新草稿，并在同一 `write_uow` 写处置、审计、Outbox、运行事件和回执。不新建 `PageVersion`，不递增 `Project.revision`。个人 `layout_revision` 不进入预览来源，变化后旧预览仍可确认。草稿 `content_revision` 或 `content_hash` 变化后，旧预览提交返回 `CANDIDATE_STALE`，必须按当前草稿重新预览。
- 成功响应 / 失败响应：首次确认 `201`，回执经 `project_receipt`。正文带 `page_id`、`content_revision`、`content_hash`，不带 `page_version_id` 和 `project_revision`。Outbox 类型是 `candidate.committed`。来源不符、撤权、过期预览、墓碑仍走原来的 `CANDIDATE_STALE`、`PREVIEW_EXPIRED`、`ActionForbidden`、`SourceRedacted`，失败事务回滚，业务行不增加。`user_patch` 里的 `action` 或 `page_mode` 与候选不同时，预览阶段 `InvalidSchema`，不插预览行。

## 提案字段

三个动作的 `page_mode` 必须为空。`schema_version` 必须是整数 1。动作种类只认候选行，不认补丁。

| 动作 | 必填 | 可选 | user_patch 可改的内容 |
| --- | --- | --- | --- |
| `add_node` | `page_id`、`base_version_id`、`node_id` | `artifact_version_id` | `node_id`、`artifact_version_id` |
| `add_typed_edge` | `page_id`、`base_version_id`、`edge_id`、`source_node_id`、`target_node_id` | 无 | 这三个边字段 |
| `attach_artifact_version` | `page_id`、`base_version_id`、`node_id`、`artifact_version_id` | 无 | `node_id`、`artifact_version_id` |

边写入草稿后只有 `edge_id`、`source_node_id`、`target_node_id`，两端都必须已经在这份草稿里。没有类型字段。节点只有 `node_id`，挂了作品版本时才多 `artifact_version_id`。

`preview_candidate` 新增两个带默认值的关键字参数：`expected_page_version_id: str | None = None`，`expected_draft_content_revision: int | None = None`。不传时行为和原来一样。传了且页版本对不上当前来源，返回 `PREVIEW_CHANGED` 并不插预览行。传了且草稿修订对不上当前草稿，返回 `CANDIDATE_STALE` 并不插预览行。

种子候选 `synthetic-candidate` 的提案仍是 `{}`。这条空提案继续返回 `not_implemented` 且不写预览，所以原有那条测试不用改口径。带齐上面字段的三个动作会真正写入草稿。

## 最小调用

```python
view = preview_candidate(
    session,
    principal=principal,
    project_id=project_id,
    candidate_id=candidate_id,
    now=now,
    user_patch={"node_id": "synthetic-added-node", "action": "add_node"},
    expected_page_version_id=page_version_id,
    expected_draft_content_revision=draft_revision,
)
response, replayed = commit_candidate(
    session,
    principal=principal,
    project_id=project_id,
    candidate_id=candidate_id,
    preview_id=view.preview_id,
    preview_hash=view.preview_hash,
    expected_page_version_id=page_version_id,
    expected_draft_content_revision=draft_revision,
    request_key=request_key,
    now=now,
    user_patch={"node_id": "synthetic-added-node", "action": "add_node"},
)
```

`add_typed_edge` 把补丁换成边的三个字段。`attach_artifact_version` 把补丁换成已有 `node_id` 和确切 `artifact_version_id`。

## 验证

```text
.venv/Scripts/python.exe -m pytest -q tests/acceptance/test_candidate_commit.py -o cache_dir=_施工文件/2026-11-01_t09http/99_临时文件/pytest-cache-b --basetemp=_施工文件/2026-11-01_t09http/99_临时文件/pytest-b
```

退出码 0。`22 passed`，1 条既有 Starlette/httpx 弃用警告，2.28s。原有 13 条测试都还在，断言没有放宽，也没有删。新的 9 条覆盖三个动作的成功确认和失败零新增、布局修订不失效、草稿内容变化重检、`suggest_stage_transition` 走 `stage_blank`。合成数据，没有 sleep。未跑全库。未对 `var/sinuo.db` 做 upgrade。未发网络。未做 HTTP、3D、T10、T11。未 git commit。

- 未验证项 / 已知限制：没有 HTTP。空提案 `{}` 仍是 `not_implemented`。没有单独再测过期预览和撤 `page.draft.write` 在这三个动作上的组合；这两条仍由原来的 `commit_candidate` 绑定和授权路径处理，既有测试仍覆盖 `draft_decision` 与阶段页。T09 不标 done。
- 下游第一次应运行的命令：上面的 `test_candidate_commit.py`。不要把 T09 标成 done。
