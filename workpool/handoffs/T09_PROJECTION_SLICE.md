# T09 回执投影切片

- 上游任务 / owner：T09。本切片不标 done。
- 集成基线：没有新迁移。没有 Git commit。没有再发 `live_model`。没有改 `commit.py`、`proposal.py`、`gate.py`、路由、`SESSION.md`、`BOARD.md`、任务卡、`AGENTS.md` 或假模型链。空提案 `{}` 的行为未动。
- 消费者：确认回执的当前权限投影。T10、T11、3D 没做。
- 契约 / schema 版本：沿用现有幂等回执。`project_receipt` 只保留 `_RECEIPT_KEYS` 和 `id` / `*_id` / `*_ids`。不把投影写回库。
- 实现位置：只新增 `tests/acceptance/test_candidate_projection.py`。产品代码未改。
- 权限动作：本条用 `draft_decision`。重放仍先走 `authorize_proposal`，需要 `page.draft.write` 和来源页版本的 `page.read`。种子里的 `decision.confirm` 保持 active，这条提案不调用它。撤掉的只有 `artifact.original.read`。
- 事务与幂等边界：TX-05。首次确认把带 `title` 和 `summary` 的回执写入 `idempotency_records`。同键同体再进入 `_run`：先 `authorize_proposal`，再对已存回执调用 `project_receipt`。候选保持 `committed`、修订 2、一条处置，页和草稿不新增。
- 成功响应 / 失败响应：首次 201。撤原文读取权后的同键同体是 200，返回体等于 `project_receipt(已存回执)`。返回体没有 `title`、`summary`、正文，也没有草稿 `content_json`。库内回执仍留着 `title` 和 `summary`。
- 最小调用示例或夹具：`test_replay_projects_stored_receipt_after_original_read_is_revoked`。种子和 `_preview` / `_commit` 复用 `tests/acceptance/test_candidate_commit.py`，该文件未改。合成数据，没有 sleep。
- 真实验证命令与结果：见下。
- 未验证项 / 已知限制：证据锚点重查的路径不存在，所以墓碑和撤 `evidence.read` 都没有测，也没有用 skip。全库 pytest 没跑。T09 仍是 implementing。
- 下游第一次应运行的命令：下面同一条 pytest。不要把 T09 标成 done。

下游确认：已在本工作区执行下面的命令，结果为退出码 0、1 passed。接手范围只到这条回执投影。证据锚点确认重查仍缺调用点。

## 已测

`test_replay_projects_stored_receipt_after_original_read_is_revoked`

先给账号 A 一条项目级 `artifact.original.read`，再成功确认一次。库里的 `response_json` 仍有 `title` 和 `summary`，`response_status` 为 201。然后只把这一条改为 revoked。`page.draft.write`、`page.read`、`decision.confirm` 仍是 active。同键同体重放返回 200，`replayed` 为真。返回体只剩 `project_receipt` 留下的字段。候选仍是消费过的那一次：状态 `committed`，修订 2，处置 1 条。页、草稿、候选、处置的计数在重放前后相同。

## 证据锚点

路径不存在，未测。

确认路径里没有把 `evidence_anchor` 交给 `authorize` 的调用。墓碑或撤掉 `evidence.read` 因此不会让这次确认失败。按约定不改产品代码，也不写必然失败的测试。

缺的调用点：

- `src/sinuo/application/candidates/gate.py` 的 `authorize_proposal`。草稿只授权 `page.draft.write`、`page.read`，引用作品只授权 `preview.read`。历史分支授权 `page.read`、`page.branch`、`page.create`。空白页授权 `decision.confirm`、`page.create`、来源页的 `page.read`，引用作品仍是 `preview.read`。没有 `Action.EVIDENCE_READ`，也没有 `scope_type="evidence_anchor"`。
- `src/sinuo/application/candidates/commit.py` 的 `_run`。重放先调用上面的 `authorize_proposal`，再把已存回执交给 `project_receipt`。
- 同文件 `_commit` 会再调用一次 `authorize_proposal`。`_authorize_referenced_artifacts` 只在历史分支上对正文节点的 `artifact_version_id` 做 `preview.read`。
- `src/sinuo/application/candidates/proposal.py` 的 `ParsedProposal` 没有证据锚点字段。`citations` 被当成作品版本。

`src/sinuo/application/auth/authorizer.py` 里，若对 `scope_type="evidence_anchor"` 调用 `authorize`，`_evidence` 会把已有墓碑标成 `tombstoned`，`decide` 给出 `REDACTED`，`_raise_for` 抛 `SourceRedacted`。确认路径没有走到这里。

## 命令

工作目录是仓库根 `sinuo-platform`。

```text
.venv/Scripts/python.exe -m pytest -q tests/acceptance/test_candidate_projection.py -o cache_dir=_施工文件/2026-11-01_t09close/99_临时文件/pytest-cache-b --basetemp=_施工文件/2026-11-01_t09close/99_临时文件/pytest-b
```

退出码 0。`1 passed, 1 warning in 0.51s`。警告是既有的 httpx 弃用。证据锚点：路径不存在，未测。
