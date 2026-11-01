# T11 门禁账 2026-11-01

第一段可执行验收。T11 写到 implementing，不标 done。T05、T06、T08、T10 不在这里改成 done。G0 到 G4 都不写通过。

判定依据是 `docs/construction/00_施工总览与完成标准.md` 的门禁表，以及 `docs/construction/07_BACKEND_ACCEPTANCE.md` 的完成定义。本仓库没有另设理论库。施工合同就是这条行为的依据。

## 本次命令

临时目录的父路径原先不存在，pytest 在收集阶段报错，测试体没有跑。建好 `_施工文件/2026-11-01_t11/99_临时文件` 之后，同一命令：

```text
.venv/Scripts/python.exe -m pytest -q tests/acceptance/test_end_to_end.py -o cache_dir=_施工文件/2026-11-01_t11/99_临时文件/pytest-cache --basetemp=_施工文件/2026-11-01_t11/99_临时文件/pytest
```

退出码 0。1 passed，1 warning。警告是既有的 Starlette/httpx 弃用。未跑全库。未跑 `live_model`。没有 Git commit。

空文件上的迁移来自 pytest 的 `engine` 夹具，`alembic_version` 为 `0010_evidence_index`。这不是业务 HTTP，也不够 G0。

## 这次走了 HTTP 的步骤

测试是 `tests/acceptance/test_end_to_end.py`。课题名「夜间取物辅助产品」，`brief` 为 `synthetic=true`。每步核对了响应里的 id 和数据库里的同一 id。没有用 sleep。

- 登录：`POST /api/v1/session`，`GET /api/v1/session`。会话账号与未撤销会话数对得上。
- 建项目：`POST /api/v1/projects`。项目、阶段、第一页和审计 id 对得上。
- 同一阶段第二张平行页：`POST /api/v1/projects/{project_id}/pages`。两页同一个 `stage_id`，该项目的 `page_edges` 这时是 0。
- 改草稿：`PATCH /api/v1/pages/{page_id}/draft`。草稿修订和内容哈希对得上，第一版哈希还在。
- 检查点：`POST /api/v1/pages/{page_id}/checkpoints`。新页版本、父版本和 HEAD 对得上。
- 查看历史：`GET /api/v1/pages/{page_id}/history`。读完之后 HEAD 和项目修订不变。
- 创建分支：`POST /api/v1/pages/{page_id}/branches`。一条 `branches_from`，源页 HEAD 仍是检查点版本。
- 审阅意见：`POST /api/v1/projects/{project_id}/review-opinions`。作者是这次登录的主设计师，创建项目时已有 `opinion.create`。意见 id 锚在检查点页版本。
- 导出登记和状态：`POST /api/v1/projects/{project_id}/exports`，`GET /api/v1/exports/{export_id}`。任务是 `queued`，`latest` 为 false，job id 和 `input_snapshot_id` 对得上。

## 这次调用的恢复函数

没有备份或恢复 HTTP。测试在 HTTP 结束后调用 `backup_snapshot`，再调用 `restore_snapshot`，写到另一个目录。隔离库里的项目、两张页、分支边、审阅意见，以及仍为 `queued` 的导出任务，和源库一致。源库仍有未撤销会话。隔离库的会话全部撤销。报告里 `page_history`、`branches`、`permissions` 是 `match`。预览在 `not_restored` 里。这次成功只说明这一份合成小库的这一次调用，不把 G4 写成通过。

## 记成未执行的步骤

原因写在 `tests/acceptance/test_end_to_end.py` 的 `NOT_EXECUTED`。测试确认这个新项目没有作品、原件版本、证据锚点、测试记录、智能体运行和候选。

- 上传原件。没有上传 HTTP。不调用 `request_upload_ticket` 或 `register_original`。
- 创建作品。没有创建 Artifact 的 HTTP。不插入作品行。
- 写入证据锚点。没有写入 HTTP，也没有写入函数。不调用检索或重建索引来冒充已经定位。
- 登记测试记录。没有登记 HTTP，也没有写入函数。
- 启动智能体运行。`open_agent_run` 和 `run_fake_chain` 没有启动 HTTP。假模型链只有读取端点。不读取一个不存在的 run。
- 预览并确认阶段空白页。预览和确认端点存在，但没有候选。不插入候选再调用它们。
- 上传作品新版本并保留旧测试。依赖上面没有 HTTP 的上传和测试登记。
- 导出包生成与下载。POST 只登记 `queued`。不调用 `run_export_job`。`GET /content` 在 queued 时是 404，只说明包还没生成。
- 3D。不做 GLB 和其他 3D。
- 真实模型。`live_model` 这次不发。

## G0–G4

每条只有一个结果。已有测试文件本次没有重跑，只列出来说明缺口在哪里，不作为通过。

| 门禁 | 结果 | 指向 |
| --- | --- | --- |
| G0 可启动 | 未执行 | 本次命令只跑了上面这一个文件，退出码 0，1 passed。夹具把空文件迁到 `0010_evidence_index`。没有干净环境安装，没有单独请求 `/healthz` 或 `/readyz`。 |
| G1 写入可信 | 未执行 | 本次命令只有成功路径上的草稿、检查点和一次历史读取。没有在这次命令里做越权拒绝、CAS 冲突、幂等重放、事务回滚。相关文件本次未跑：`tests/acceptance/test_versions_http.py`、`tests/acceptance/test_versions_checkpoint_commands.py`、`tests/acceptance/test_identity_authorization.py`。 |
| G2 资料与任务可靠 | 未执行 | 本次命令没有原件、预览失败、重复投递、租约或撤权后的搜索。相关文件本次未跑：`tests/acceptance/test_registration_audit.py`、`tests/acceptance/test_assets_derivatives.py`、`tests/acceptance/test_outbox_recovery.py`、`tests/acceptance/test_durable_tasks_events.py`。 |
| G3 候选可信 | 未执行 | 本次命令没有启动假模型链，没有候选预览或确认，没有预算停止，也没有发 `live_model`。相关文件本次未跑：`tests/acceptance/test_agent_runs.py`、`tests/acceptance/test_candidate_commit.py`。`tests/acceptance/test_live_deepseek.py` 本次没有跑。 |
| G4 可交接 | 未执行 | 纵向场景上面那些步仍是未执行。并发故障、OpenAPI 和限制清单这次没有做。隔离恢复只在 `tests/acceptance/test_end_to_end.py` 里成功了一次。`tests/acceptance/test_review_export_recovery.py` 本次没有重跑。 |
