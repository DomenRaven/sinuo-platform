# T09 候选预览、人工确认与原子提交

- 状态：review。2026-11-01 证据锚点确认重查已测，四文件 pytest 退出码 0，32 passed。不标 done。T08 仍是 implementing。
- 依赖：T04、T08 done
- Owner：候选与领域事务开发
- 基线提交或快照：认领时填写
- 写入范围：候选预览/处置/确认应用服务与 API、`tests/acceptance/test_candidate_commit.py`；共享候选 schema、迁移和错误码由集成 owner 合入
- 对应门禁：G3；消费者：T10

## 目标

用户能看见提案将改哪些对象、编辑其允许修改部分并明确确认；服务端提交的内容必须与该用户看到的预览一致。同一候选最多消费一次，任何并发、来源变化或权限变化都不会写出半张页或重复决定。

## 输入与交付

预览保存 actor、候选修订、当前 access_epoch、规范化 user_patch、effective_proposal、全部来源预条件、到期时间与 preview_hash。提交引用服务端预览；任何正文改变都重新预览。提交时检查当前身份/动作权限、候选状态、预览绑定、全部来源版本/草稿/原件有效性，然后候选状态 CAS 与目标效果同事务提交。

将候选消费、Page/PageVersion/来源边/Decision/Audit/Outbox/幂等回执放进一个短事务；普通节点候选只写草稿与处置，不伪造正式版。stage_blank 与 merge_blank 创建主体空白页，仅承接显式引用；history_branch 保留旧版可编辑起点、来源映射和历史测试限制。未确认草稿上的阶段建议要求先保存正式版并重新检查。

## 验收

创建并执行 `python -m pytest -q tests/acceptance/test_candidate_commit.py`，用真实并发连接验证：

1. 同一候选使用不同请求键、不同操作者同时确认，只有一个成功，另一个返回 CANDIDATE_ALREADY_RESOLVED。
2. 相同键重放返回同一结果；相同键改正文拒绝；回执按当前权限重新投影。
3. 预览后篡改 user_patch/actor/来源/哈希，或使用过期预览均拒绝；先前预览不因重新生成自动替换。
4. 按动作校验来源：stage_blank/merge_blank 的声明当前 HEAD 或所依赖草稿内容改变时拒绝；history_branch 校验确切旧版存在、可读、未墓碑，不因源页 HEAD 继续更新而失效。所有动作均重查证据有效性与当前权限；失败时确认对象零新增。
5. 在候选已消费后目标写入中途注入异常，候选与业务对象一起回滚。
6. 多来源阶段页主体空白并保留全部准确来源；拒绝候选仅保存处置，不改业务图。

## 交接与证据

提供完整 preview/commit/reject 示例和竞争时间线、数据库结果、失败包络。T10 与未来 UI 可基于预览做展示，但 HTTP 业务提交响应才是成功依据；SSE candidate.ready 不代表已写业务对象。

## 预览与确认第一段（2026-11-01）

状态仍是 implementing，不标 done。服务在 `src/sinuo/application/candidates/`。公开函数是 `preview_candidate`、`commit_candidate`、`reject_candidate`、`bound_preview_hash`。没有新迁移。没有 HTTP。主控重跑：

```text
.venv/Scripts/python.exe -m pytest -q tests/acceptance/test_candidate_commit.py -o cache_dir=_施工文件/2026-11-01_t09/99_临时文件/pytest-cache --basetemp=_施工文件/2026-11-01_t09/99_临时文件/pytest
```

退出码 0。13 passed，1 warning，1.55s。警告是既有的 httpx 弃用。`add_node`、`add_typed_edge`、`attach_artifact_version` 当时仍是 `not_implemented`。交接见 `workpool/handoffs/T09_PREVIEW_COMMIT_SLICE.md`。

## HTTP 与草稿动作（2026-11-01）

状态仍是 implementing，不标 done。三条路由在 `/api/v1/candidates/{candidate_id}/preview|commit|reject`。三个动作经现有 `commit_candidate` 写入草稿，正文经过 `canonical_document`，不新建正式页版本。没有新迁移。主控重跑：

```text
.venv/Scripts/python.exe -m pytest -q tests/acceptance/test_candidate_commit.py tests/acceptance/test_candidate_http.py -o cache_dir=_施工文件/2026-11-01_t09http/99_临时文件/pytest-cache --basetemp=_施工文件/2026-11-01_t09http/99_临时文件/pytest
.venv/Scripts/python.exe -m alembic heads
```

pytest 退出码 0。28 passed，1 warning，3.02s。heads 退出码 0，只有 `0010_evidence_index`。交接见 `workpool/handoffs/T09_HTTP_ACTIONS_SLICE.md`。

## 空白页回滚与回执投影（2026-11-01）

状态仍是 implementing，不标 review，也不标 done。`test_stage_blank_failure_after_page_insert_rolls_back` 用已有 `before_effect`，没有改 `commit.py`。`test_replay_projects_stored_receipt_after_original_read_is_revoked` 在 `artifact.original.read` 撤掉后回放，返回体没有标题和正文。证据锚点墓碑和撤 `evidence.read` 没有现成调用点，没有补测试。空提案 `{}` 仍不写预览。主控全量：

```text
.venv/Scripts/python.exe -m pytest -q -o cache_dir=_施工文件/2026-11-01_t09close/99_临时文件/pytest-cache --basetemp=_施工文件/2026-11-01_t09close/99_临时文件/pytest
.venv/Scripts/python.exe -m alembic heads
```

pytest 退出码 0。364 passed，2 deselected，2 warnings，23.23s。heads 退出码 0，只有 `0010_evidence_index`。交接见 `workpool/handoffs/T09_CLOSEOUT_2026-11-01.md`。

## 证据锚点确认重查（2026-11-01）

状态写到 review，不标 done。`authorize_proposal` 在 `consume_candidate` 之前，对提案已经点名的每个证据锚点重查 `evidence.read`，范围是 `evidence_anchor`。墓碑用已有 `SOURCE_REDACTED`。没有 `evidence.read` 时抛已有 `ActionForbidden`。没有点名锚点的旧候选不进入这次检查。空提案 `{}` 仍不写预览。没有新迁移，没有新错误码。没有改 `commit.py`。

```text
.venv/Scripts/python.exe -m pytest -q tests/acceptance/test_candidate_commit.py tests/acceptance/test_candidate_http.py tests/acceptance/test_candidate_projection.py tests/acceptance/test_candidate_evidence.py -o cache_dir=_施工文件/2026-11-01_t09evidence/99_临时文件/pytest-cache --basetemp=_施工文件/2026-11-01_t09evidence/99_临时文件/pytest
```

pytest 退出码 0。32 passed，1 warning，3.56s。警告是既有的 httpx 弃用。测例是 `test_tombstoned_evidence_anchor_rejects_commit_without_new_rows` 和 `test_revoked_evidence_read_rejects_commit_without_new_rows`。T08 仍是 implementing。交接见 `workpool/handoffs/T09_CLOSEOUT_2026-11-01.md`。
