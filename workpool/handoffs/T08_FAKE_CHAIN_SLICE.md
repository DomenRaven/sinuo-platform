# T08 假模型链

日期：2026-11-01。T08 从 planned 改为 implementing。本切片不把任务标成 done。不跑 `live_model`，不新建迁移，不建方法卡表。

## 行为

`run_fake_chain` 做一次链，分成两段数据库事务，假模型夹在中间：

1. 短事务里先 `project.read`，再 `search_evidence` 和可选的 `get_page_snapshot`。这一段不写业务行。无权页的标题和正文不会进入读到的事实。
2. 事务外用 `DagRunner` 和 `FakeModelProvider`。父运行三个职责：clarification、evidence、method。批评单独一次运行，因为现有调度器拒绝把四个职责放进同一次内存运行。
3. 再开短事务。重读结果和第一次不一致就拒绝，不写候选。一致时写入父运行、批评运行、只读记录和一条候选。批评运行的 `parent_run_id` 指向父运行。候选 `action` 是 `draft_decision`，`status` 是 `pending`，`revision` 是 1。然后把两次运行收成 `completed` 且 `accepting_results=0`。

方法输出里的 `method_card` 固定是空。没有方法卡表。`DisabledModelProvider` 在落库前被拒绝。未知账号抛 `AuthRequired`，不写运行。

## 证据

```text
.venv/Scripts/python.exe -m pytest -q tests/acceptance/test_retrieval_methods.py tests/acceptance/test_agent_runs.py -o cache_dir=_施工文件/2026-11-01_grok/99_临时文件/pytest-cache --basetemp=_施工文件/2026-11-01_grok/99_临时文件/pytest
```

退出码 0。10 passed，0.92s。`tests/acceptance/test_agent_runs.py` 4 个：主链、禁用提供方、未知账号、空白意图。

主链断言父运行角色是 clarification、evidence、method，批评运行的父指针指向它，候选保持 pending。授权摘录「合成竹材弯曲」和页标题「合成可见页」在候选里。无权摘录、无权标题和无权正文不在。只读记录分别指向命中的作品版本和有权页版本。

## 还没做

真实 DeepSeek、HTTP 适配、单运行预算并行、取消后的晚到输出、提示注入、草稿变化失效。T09 的确认提交没做。T08 不标 done。
