# GAP 智能体启动切片

- 上游：白皮书 7.1 调度段，04 `POST /projects/{p}/agent-runs`。本切片不标 done。
- 未改 `execute.py`、`fake_chain.py`、`agents/routes.py`、`app.py`、`deepseek.py`。未 git commit。未跑 `live_model`。
- `create_app()` 仍不挂这条路由。验收测试在 `create_app()` 之后自己 `include_router`。

## 改动文件

- `src/sinuo/application/agents/intent.py`
- `src/sinuo/api/agents/start.py`
- `tests/acceptance/test_agent_start_http.py`
- 本交接。

## 四种问法选中的职责

调度在代码里完成，不把计划交给模型。一句话只命中一个家族；同时命中两个家族则 422，不开运行、不调用模型。一次计划最多三个职责。没进计划的职责没有 `RunTask`，也不调用模型。

| 问法 | 选中的职责 | 依赖 |
| --- | --- | --- |
| 问资料 | 只选查证据 `evidence` | 无 |
| 问方法 | 只选问方法 `method` | 无 |
| 问下一步 | 查证据 `evidence`，过程批评 `critic` | 批评等待证据 |
| 简报变化 | 只选澄清 `clarification` | 无 |

同一规则还覆盖这些句子：问文献 → 查证据；怎么访谈 → 问方法；下一步做什么、检查进度 → 查证据和过程批评；任务书刚变化 → 澄清。

问下一步在组计划之前先做确定性缺口检查，记下「阶段判断等待证据」。这句话不另写一张表；能看到的结果是批评任务依赖证据任务。

## 路由

`POST /api/v1/projects/{project_id}/agent-runs`。权限 `run.create`。成功 202，`data` 为 `run_id`、`snapshot_id`、`events_url`。`events_url` 是 `/api/v1/agent-runs/{run_id}/events`。

调用现有 `open_agent_run` 和 `finish_agent_run`。来源是 `manual`，`preference_revision` 为空。测试传入 `FakeModelProvider`。设置里 `model_enabled=True` 且带了一把合成密钥；`DeepSeekAdapter` 的构造、`from_settings` 和 `execute` 被换成会抛错的替身。测试通过，说明没有走真实客户端。

`finish_agent_run` 冻结的快照没有 `outputs`。启动层只在提供者是 `FakeModelProvider` 时，给当前这一个已选职责补上回声槽，再调用这个假模型。未选中的职责到不了这一步。

`input_mode` 只校验 `confirmed | draft_preview | historical_readonly`。`open_agent_run` 没有这个字段，快照里不另存一份。可选的 `draft_content_revision` 在打开运行前核对；没有草稿或修订不一致时 409，不调用模型。

客户端不能在正文里提交职责名单。`roles` 这类多余字段是 422。

## 验证

```text
.venv/Scripts/python.exe -m pytest -q tests/acceptance/test_agent_start_http.py -o cache_dir=_施工文件/2026-11-01_gap_run/99_临时文件/pytest-cache --basetemp=_施工文件/2026-11-01_gap_run/99_临时文件/pytest
```

退出码 0。`2 passed`，1 条既有 Starlette/httpx 弃用警告。未跑全库。未跑 `live_model`。

## 未做

路由没有挂进 `create_app`。幂等记录写在 `finish_agent_run` 返回之后；这两步之间若进程退出，同一把幂等键重试会再开一次运行。本切片不标 done。
