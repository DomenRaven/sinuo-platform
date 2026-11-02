# 智能体运行侧交接（2026-11-01）

对照 F09、F10、F16、F17、F18、F20 和「真实模型」。依据是白皮书 7.2、7.4、7.5、7.6 里运行记录、出处和失败退路那几段。没有新下载文献。没有改 `fake_chain.py`、`app.py`、`projects.py`、`drafts.py`、`candidates/`、`.env`。没有把密钥写进仓库或这份交接。

## 补上的行为

已有调度、建议频率和「只留待确认候选、不建页」都复用，没有重写。

- 问资料、问方法、问下一步、简报变化仍走 `plan_duties`。没选中的职责不进计划，也不调用模型。问下一步先记下确定性缺口「阶段判断等待证据」，再只跑证据和过程批评。
- 证据判断带文件版本，以及页码、区域或时间点。没有可引用位置时写「尚无足够证据」或「引用缺少页码、区域或时间点」，并记下查询和输入页版本。
- 方法建议写出问题、输入、步骤、局限和来源。没有匹配的卡时写「没有匹配的方法」。
- 每次运行写 `run.planned`：输入页版本、路由、开口缺口。结束时再记工具名和状态、任务错误类别。失败任务的 `last_error` 只存类别，不存响应体。
- 被后一条决定取代的结论、被后一个锚点取代的摘录，退出这次默认上下文。原来的决定行和锚点行还在。
- 待确认建议带上缺口、引用和输入页版本。运行本身不新建页，也不改阶段。
- 关闭、手动、事件后、定时摘要没有改 `suggestions.py`。

## 测试

假模型（控制逻辑，含新合同、启动、持久运行、理论接入、选模型、模型链、阶段空白页、建议频率、策略重校、纵向）：

```text
.venv\Scripts\python.exe -m pytest -q tests/acceptance/test_agent_runtime_contract.py tests/acceptance/test_agent_start_http.py tests/acceptance/test_agent_runs.py tests/acceptance/test_theory_in_run.py tests/acceptance/test_model_select.py tests/acceptance/test_model_chain.py tests/acceptance/test_stage_blank_from_run.py tests/acceptance/test_suggestion_preferences.py tests/acceptance/test_agent_policy_recheck.py tests/acceptance/test_end_to_end.py --tb=line
```

退出码 0。52 passed，1 warning。警告是 httpx 弃用。

新文件在去掉未使用导入后再跑一次：`tests/acceptance/test_agent_runtime_contract.py`，退出码 0，3 passed。

真实调用（没有改成假模型）：

```text
.venv\Scripts\python.exe -m pytest -q tests/acceptance/test_live_deepseek.py -m live_model
```

退出码 0。2 passed。成功，没有错误类别。输出里没有写入密钥、请求头或响应体。

## 没完成

- F10 里「人的改正」没有新的运行账。采纳或拒绝仍在既有候选确认路径。本行不能改 `candidates/`。
- 这次真实调用是已有的合成烟测，不是带着项目资料走完一次设计运行。

## 主控

没有新路由。不用往 `app.py` 挂行。启动运行仍是 `POST /api/v1/projects/{project_id}/agent-runs`。
