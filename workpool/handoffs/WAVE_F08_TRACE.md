# HANDOFF F08 回链

- 上游：白皮书 F08「从方案能回到要求和原始证据；测试指向特定作品版本」。节点种类键用已冻结的 `requirement`、`evidence`、`test`。没有等 F03，没有改 `drafts.py`。
- 理论：产品白皮书已把 QOC（问题、方案、准则）写成页内回看依据的启发，技术规范把 F08 记在 R05/R08/R09，测试锁定作品版本。本波次不发网络，没有另下 MacLean 原文。缺的是论文原文，不是这条读法的借口。
- 没改 `app.py`，没有迁移，没有 commit，没有打模型。

## 做了什么

只读函数 `trace_requirements`。给定项目，再给一个页版本或一个要求节点，返回证据锚点和钉在作品版本上的测试。

页版本沿用节点里已经写下的 `artifact_version_id`，收集该版本上的证据锚点和测试记录。锚点带作品版本，以及页码、区域或时间点里实际存着的那一项。没有这三项时，锚点仍返回，`gaps` 写 `locator.place`，不编页码。更正行留在原来的作品版本上，不改绑到新版本。

要求节点只认 `kind` 为 `requirement` 的节点。它点名的 `evidence_anchor_id` / `evidence_anchor_ids`，以及 `supports`、`references`、`derived_from` 边上的 `evidence` 节点，才算它依据的锚点。这些锚点的后继更正行一并返回，作品版本不变。`kind` 还没写上时，要求节点这条返回空列表，`gaps` 写 `node.kind`，不用作品版本去猜它是要求。`kind` 是 `evidence` 或 `test` 时同样不当成要求。

`excerpt` 只在当前调用对这个锚点有 `evidence.read`、锚点和所在作品版本都没有墓碑时返回。否则 `excerpt` 为 null，`excerpt_visible` 为 false。测试的观察、结果、来源和条件只在有项目级 `test.record` 且该测试行没有墓碑时返回。没有单独的 `test.read`。审阅者和团队成员能看到页码、区域、时间点、锚点 id 和测试钉住的作品版本，看不到正文。读不到页版本的人得到空列表。`latest`、`head`、`current`、`newest` 直接 422，不解析成当前头版本。

这次只读，不写锚点、测试、页或项目修订。

## 测试

```text
.venv\Scripts\python.exe -m pytest -q tests/acceptance/test_trace_requirements.py
```

退出码 0。5 passed，1 warning。警告是既有的 Starlette/httpx 弃用。未跑全库，未跑 `live_model`。

## 没完成的条款

- `drafts.py` 的页文档仍然只有 `node_id` 和可选的 `artifact_version_id`。正式保存的页还写不进 `kind`。回链对这种页的要求列表为空，并写明 `node.kind`。要让要求节点出现在正式草稿里，仍是 F03 的事。
- 路由没有挂进 `create_app`。验收测试在应用上自行 `include_router`，主控挂上之后这条测试仍能过。
- 测试正文用 `test.record` 把关，因为授权表里没有 `test.read`。

## 主控要挂的那一行

在 `src/sinuo/api/app.py` 的 import 区增加：

```python
from sinuo.application.trace.routes import router as trace_router
```

在 `create_app` 里、其他 `include_router` 旁边增加这一行：

```python
app.include_router(trace_router)
```

路由是 `GET /api/v1/projects/{project_id}/trace`。查询参数恰好一个：`page_version_id` 或 `node_id`。函数是 `get_requirement_trace`。
