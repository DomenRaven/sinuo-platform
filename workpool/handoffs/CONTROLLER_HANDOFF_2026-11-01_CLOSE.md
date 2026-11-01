# 总控交接（2026-11-01 晚）

给下一个总控窗口。先读本文件，再读 `workpool/SESSION.md` 和 `workpool/BOARD.md`。更早的 `CONTROLLER_HANDOFF_2026-11-01.md`、`GROK_START_HERE.md` 开头那段、`prompts/00_GROK_总控开工.md` 描述的是 10 月 30 日的开工，当时 T02 还没做完。不要从那里重做 T02。

本文件只记录这次核对过的命令和源码。聊天记忆不能覆盖仓库里的符号。

## 不要做

- 不要把 T05、T06、T07、T08、T09、T10、T11 改成 done。T07 和 T09 保持 review。其余保持 implementing。
- 不要签 G0–G4。
- 不要做 GLB 或其他 3D。豁免只取消 3D，不把 T05 或白皮书 F07 算完成。见 `workpool/handoffs/USER_WAIVE_3D.md`。
- 不要改 `run_fake_chain`、`read_fake_chain`、两条 fake-chain 路由、`scripts/seed_night_demo.py`、`workpool/demo-5min/`，除非用户明确要求。
- 不要 git commit 或 git push，除非用户明确要求。不要改 git config。不要对 main 强推。
- 不要把 `.env` 加进提交。不要在交接、日志、测试输出里打印密钥。本机 `.env` 已打开模型并写入过密钥，文件在 `.gitignore`。测试构造 `Settings` 时继续用 `_env_file=None`。没有用户要求时不要再跑 `live_model`。
- 不要把 `F:/工业设计理论库` 的原始课件复制进 git。不要下载受版权限制的教材来充语料。
- 同一时间一个文件只给一个写入者。子代理可以并行，主控收回后再改共享入口 `src/sinuo/api/app.py` 和迁移。

## 仓库

- 根目录：`D:\AAA学习\大三上\设计大数据基础\大作业\sinuo-platform`
- 远程：`https://github.com/DomenRaven/sinuo-platform.git`，分支 `main`，私有库。
- 解释器：`.venv/Scripts/python.exe`。前缀 `/api/v1`。
- 迁移 head：`0010_evidence_index`。`alembic heads` 这次退出码 0，只有这一行。不要改旧迁移文件。新迁移只能接在这一头后面，并且保持单头。
- 全量：`pytest -q` 退出码 0。406 passed，2 deselected，2 warnings，30.75s。被排除的两条是 `tests/acceptance/test_live_deepseek.py` 的 `live_model`。
- 这次 OpenAPI 路径 30 条，由 `create_app(Settings(_env_file=None))` 打印。

```text
GET /api/v1/agent-runs/{run_id}/events
GET /api/v1/artifact-versions/{artifact_version_id}/content
GET,POST /api/v1/artifact-versions/{artifact_version_id}/preview
POST /api/v1/candidates/{candidate_id}/commit
POST /api/v1/candidates/{candidate_id}/preview
POST /api/v1/candidates/{candidate_id}/reject
GET /api/v1/exports/{export_id}
GET /api/v1/exports/{export_id}/content
POST /api/v1/pages/{page_id}/branches
POST /api/v1/pages/{page_id}/checkpoints
PATCH /api/v1/pages/{page_id}/draft
GET /api/v1/pages/{page_id}/history
POST /api/v1/projects
PATCH /api/v1/projects/{project_id}/access
POST /api/v1/projects/{project_id}/agent-runs
GET /api/v1/projects/{project_id}/agent-runs/{parent_run_id}/fake-chain
GET /api/v1/projects/{project_id}/agent-runs/{parent_run_id}/fake-chain/view
POST /api/v1/projects/{project_id}/artifacts/uploads
POST /api/v1/projects/{project_id}/evidence-anchors
POST /api/v1/projects/{project_id}/exports
POST /api/v1/projects/{project_id}/pages
POST /api/v1/projects/{project_id}/review-opinions
GET,PATCH /api/v1/projects/{project_id}/suggestion-preferences/me
POST /api/v1/projects/{project_id}/test-records
PATCH /api/v1/review-opinions/{opinion_id}
DELETE,GET,POST /api/v1/session
POST /api/v1/uploads/{ticket_id}/complete
PUT /api/v1/uploads/{ticket_id}/content
GET /healthz
GET /readyz
```

## 这次瘦身

删除了整个 `_施工文件`。里面是历次 pytest 库、uv 缓存、冻结环境，以及 2026-10-30 文档修补用的一次性脚本。约 1.6 GB。这些文件本来就被忽略，删掉不影响测试，需要时 pytest 会再建缓存。同时删了 `.pytest_cache`、`.ruff_cache` 和 `__pycache__`。

留下的：`.venv`、`var/sinuo.db`、`scripts/seed_night_demo.py`、`workpool/handoffs/` 里的切片记录、`workpool/demo-5min/`。

## 已经挂上、还没串成纵向场景

`src/sinuo/api/app.py` 现在 include 这些路由：假模型链、启动运行、身份、版本、审阅、事件、导出、证据、测试记录、原件下载与预览、上传、建议偏好、候选。

- 上传：`src/sinuo/api/assets/uploads.py`。没有同名作品时先建 `Artifact`。同名新字节是新版本，旧 `content_hash` 不变。测试 `tests/acceptance/test_upload_http.py`，3 passed。
- 证据与测试记录：`src/sinuo/api/evidence/routes.py`、`src/sinuo/api/records/routes.py`。测试 `tests/acceptance/test_evidence_records_http.py`，3 passed。
- 启动运行：`src/sinuo/api/agents/start.py` 的 `start_router()`。`app.state.agent_provider` 优先。没设置时，`model_enabled` 为真才 `DeepSeekAdapter.from_settings`，否则 `DisabledModelProvider`。测试 `tests/acceptance/test_agent_start_http.py`，2 passed。
- 导出：`POST /projects/{id}/exports` 把任务记成 queued。`drain_export_jobs` 在 `src/sinuo/application/exports/drain.py`，没有单独的 HTTP。下载仍是 `GET /exports/{id}/content`。测试 `tests/acceptance/test_export_drain.py`，1 passed。
- 候选：预览、确认、驳回。确认前 `authorize_proposal` 会按 `evidence.read` 重查锚点。三个草稿动作只改草稿，不建 `PageVersion`。

`tests/acceptance/test_end_to_end.py` 的 `NOT_EXECUTED` 仍写着「没有上传 HTTP」「没有启动用的 HTTP」。这是这段测试写完之后接口才挂上，正文还没改。全量能通过，只说明它仍按旧清单把这些步骤记成未执行。下一窗口的第一件事就是改这份测试去调用现有 HTTP，并用假模型注入，不要为了让清单变绿去插库。3D 和真实模型继续留在未执行。改完也不签门禁。

## 智能体

四个职责：clarification、evidence、method、critic。一次运行最多三个。`plan_duties` 用句子里的固定说法选职责：问资料只查证据，问方法只问方法，问下一步是证据加过程批评，简报变化只做澄清。一句话命中两族就拒绝，不调用模型。这不是模型做的意图分类。

四角色的完整链仍是假模型链的两次运行，靠 `parent_run_id`。启动接口一次只开一条运行。

模型不提交业务。`commit_candidate` 仍要人确认。

DeepSeek 适配器在 `src/sinuo/agents/deepseek.py`。澄清和证据用 `SINUO_MODEL_NAME`，仓库示例是 `deepseek-flash`。方法和批评用 `SINUO_MODEL_NAME_REASONING`，示例是 `deepseek-v4-pro`。请求带 `response_format: json_object`，并关闭 thinking。密钥只从环境来。`.env.example` 里密钥是空的，`SINUO_MODEL_ENABLED=false`。

课程理论：`execute.py` 在项目检索之后读 `SINUO_THEORY_ROOT` 的 `chunks.json` 和 `graph.json`。目录不存在就给空理论，不去读 F 盘。向量模块返回的编码器名是 `char-ngram-2-3`。本机有 BGE 快照，当前虚拟环境不能导入 sentence_transformers。图谱是调用方给的链接上走两跳，摘要引用页 id。F 盘 `F:/工业设计理论库` 的概念页和方法页还没导出成这两个 JSON，所以真实运行看不到那两门课。合同在 `workpool/handoffs/THEORY_RETRIEVAL_CONTRACT.md`，盘点在 `THEORY_CORPUS_MANIFEST.md`。

## 任务卡

| 任务 | 状态 | 还缺什么 |
| --- | --- | --- |
| T01–T04 | done | 不重做 |
| T05 | implementing | PNG/PDF 和上传 HTTP 已有。3D 按用户决定不做。封面仍未做 |
| T06 | implementing | `jobs` 没有 `retry_wait`。用户说过这里少花精力 |
| T07 | review | FTS5、重建索引、方法卡、版本对比已有。运行时语料未导出 |
| T08 | implementing | 启动 HTTP 和适配器已有。T06、T07 未签收。真实运行还看不到课程卡片 |
| T09 | review | 预览、确认、驳回、草稿动作、证据重查已有。依赖 T08，不标 done |
| T10 | implementing | 审阅、导出、排空、备份已有。依赖 T05 和 T09 |
| T11 | implementing | 纵向测试还在用过时的未执行清单 |

## 演示

`scripts/seed_night_demo.py` 会重建临时库并调用 `run_fake_chain`，登录 `synthetic-night` / `synthetic-night-pass`。它不重建证据索引，摘录可能显示未索引。汇报若要走这条假模型链，进程里把模型关掉，避免启动别的路径时打到 DeepSeek。两屏兜底在 `workpool/demo-5min/`。

## 下一窗口顺序

1. 改纵向测试，调用已挂上的 HTTP。启动运行注入假模型。导出调用 `drain_export_jobs`。3D 与真实模型保持未执行。不签门禁，不把任务卡改成 done。
2. 从 F 盘理论库导出 `chunks.json` 和 `graph.json` 到 git 忽略的运行目录，用 `SINUO_THEORY_ROOT` 接上。用合成片段测过的向量和图谱函数不要改成去读 F 盘路径。
3. 有余力再看白皮书里仍缺的页内节点类型、阶段模板和封面。T06 的 `jobs.retry_wait` 放后面。
