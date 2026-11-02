# 施工波次合同（2026-11-01）

主控冻结。子代理只在自己的写入范围内施工，不改本文件，不改 `BOARD.md`、`SESSION.md`、`app.py`。不 git commit。不打印密钥，不把 `.env` 读进日志。不改 `fake_chain.py`、`read_chain.py`、假模型链路由、`scripts/seed_night_demo.py`、`workpool/demo-5min/`。不编辑已有迁移。新迁移只允许 F03 代理追加为 `0011`，且保持单头。不把 T05–T11 标成 done，不签 G0–G4。

需求原文在 `USER_SCOPE_2026-11-01.md`。解释器：`.venv/Scripts/python.exe`。仓库根：`D:\AAA学习\大三上\设计大数据基础\大作业\sinuo-platform`。

开工先读 `C:\Users\MAC\.cursor\skills\eight-honors-eight-gates\SKILL.md`，按八闸门做自己那一个模块。闸门 1.5 只读与本模块有关的白皮书段落，不把课件抄进仓库。

## 硬合同

- 前缀 `/api/v1`。人确认之前，模型不写正式页、不写决定、不翻阶段。
- F01：创建项目时写入固定八个阶段，名字依次是：任务与情境、用户与场景研究、问题及机会定义、设计要求、概念探索、深化与原型、测试与迭代、表达与复盘。响应里的 `stage_id` 和 `page_id` 仍是第一阶段「任务与情境」及其首页。
- F02 总览画面是界面，本波次不做。
- F03 节点种类只用这些键：`problem`、`evidence`、`insight`、`requirement`、`artifact`、`test`、`decision`。显示名是问题、证据、洞察、要求、作品、测试、决定。
- 职责：澄清管课题边界；证据管证据和洞察；方法管方法卡、概念路径和测试计划；过程批评管要求是否可测和阶段建议。作品、测试记录、决定只在人确认后写入。
- F07：不做 3D，不把缩略图当必须项。文件名要能看出版本。
- 真实能力的证明要调用 DeepSeek。假模型只证明控制逻辑。密钥只从环境来。
- 理论增补只允许 CC0、CC BY 4.0、Open Government Licence。不下载 IDEO Design Kit 正文、Hyper Island、Norman、Ulrich 与 Eppinger、Delft Design Guide、ISO。

## 写入范围

| 代理 | 可写 | 交接文件 |
| --- | --- | --- |
| F01 | `src/sinuo/application/versions/projects.py`，`tests/acceptance/test_product_stage_template.py`，以及因「创建时变成八个阶段」而失败的既有测试。禁止 `test_end_to_end.py` | `workpool/handoffs/WAVE_F01_TEMPLATE.md` |
| F03 | `src/sinuo/domain/page_nodes.py`（新），`src/sinuo/application/versions/drafts.py`，`tests/acceptance/test_page_nodes.py`。必要时只新增迁移 `0011` | `workpool/handoffs/WAVE_F03_NODES.md` |
| 路径 | `src/sinuo/application/versions/branches.py`，`history.py`，`src/sinuo/application/candidates/`，对应测试。禁止 `execute.py` | `workpool/handoffs/WAVE_PATH_COMMIT.md` |
| 回链 | `src/sinuo/application/trace/`（新），`tests/acceptance/test_trace_requirements.py`。不改 `app.py`，路由函数交主控挂载 | `workpool/handoffs/WAVE_F08_TRACE.md` |
| 智能体 | `src/sinuo/application/agents/execute.py`，`intent.py`，`model_chain.py`，`src/sinuo/api/agents/start.py`，`src/sinuo/workers/suggestions.py`，智能体相关测试。可以跑 `live_model` | `workpool/handoffs/WAVE_AGENTS_LIVE.md` |
| 理论 | `scripts/import_open_methods.py`（新），`tests/acceptance/test_open_methods_import.py`。产物只进 `var/` | `workpool/handoffs/WAVE_THEORY_OPEN.md` |
| 可见 | 只新建 `web/workbench.html`。只读已有 HTTP，不改后端 | `workpool/handoffs/WAVE_VISUAL.md` |

每个代理结束时在自己的交接文件写：做了什么、测试命令和退出码、没完成的条款、需要主控挂到 `app.py` 的那一行。不改别人的文件。
