# T09 HTTP 与三个草稿动作

- 上游任务 / owner：T09。本切片不标 done。
- 集成基线：没有新迁移。`alembic heads` 退出码 0，只有 `0010_evidence_index (head)`。没有 Git commit。没有再发 `live_model`。
- 消费者：确认 UI。OpenAPI 由 FastAPI 从路由生成。
- 契约 / schema 版本：沿用现有候选表和草稿 JSON。没有新错误码。
- 实现位置：`src/sinuo/api/candidates/`、`src/sinuo/api/app.py` 的路由挂载、`src/sinuo/application/candidates/` 的三个动作。仍走 `preview_candidate`、`commit_candidate`、`reject_candidate`。
- 权限动作：三个动作要 `page.draft.write` 和来源页的 `page.read`。带作品版本的还要 `preview.read`，作品版本未墓碑。actor 只来自会话。
- 事务与幂等边界：TX-05。消费、草稿、处置、审计和回执在调用方的 `write_uow` 里。不新建正式 `PageVersion`，不推进 `Project.revision`。确认和拒绝带 `Idempotency-Key`。预览每次新插一行，不做同键回放。
- 成功响应 / 失败响应：预览返回已保存的十六进制 `preview_hash`，没有 `sha256:` 前缀。确认首次 201，同键同体 200，`meta.idempotency_replayed` 标明是否回放。同键异体是 `IDEMPOTENCY_KEY_REUSED`。`not_implemented` 映射为 422 `INVALID_SCHEMA`。
- 最小调用示例或夹具：`tests/acceptance/test_candidate_http.py`、`tests/acceptance/test_candidate_commit.py`。
- 真实验证命令与结果：见下。
- 未验证项 / 已知限制：全库 pytest 没跑。`add_node` 的空提案 `{}` 仍是 `not_implemented`。个人布局以外的半投影、空白页中途失败注入没有补。T10、T11、3D、假模型链没做。
- 下游第一次应运行的命令：下面同一条 pytest。不要把 T09 标成 done。

下游确认：主控已跑下面两条命令，结果为 pytest 退出码 0、28 passed，heads 退出码 0、只有 `0010_evidence_index`。

## 路径

```text
POST /api/v1/candidates/{candidate_id}/preview
POST /api/v1/candidates/{candidate_id}/commit
POST /api/v1/candidates/{candidate_id}/reject
```

项目号取候选行。Origin 和 CSRF 沿用版本路由。

## 三个动作

`add_node` 增加节点。`add_typed_edge` 增加只有 `edge_id`、`source_node_id`、`target_node_id` 的边，两端必须已在这份草稿里。`attach_artifact_version` 给已有节点写上确切作品版本。正文经过 `canonical_document`。`user_patch` 不能改动作种类。布局修订变化不使内容候选失效。草稿内容变化后旧预览返回 `CANDIDATE_STALE`。`suggest_stage_transition` 仍走已有的空白页确认，只补了这个动作名的测试。

## 命令

工作目录是仓库根。

```text
.venv/Scripts/python.exe -m pytest -q tests/acceptance/test_candidate_commit.py tests/acceptance/test_candidate_http.py -o cache_dir=_施工文件/2026-11-01_t09http/99_临时文件/pytest-cache --basetemp=_施工文件/2026-11-01_t09http/99_临时文件/pytest
.venv/Scripts/python.exe -m alembic heads
```

pytest 退出码 0。`28 passed, 1 warning in 3.02s`。警告是既有的 httpx 弃用。`alembic heads` 退出码 0，只有一行 `0010_evidence_index (head)`。
