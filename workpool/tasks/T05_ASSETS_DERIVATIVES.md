# T05 原件、作品版本与派生处理

- 状态：implementing
- 依赖：T04 done；与 T06 共享信封 T05-T06-ENVELOPE-1
- Owner：Grok 开发A
- 基线提交或快照：无 Git commit。全量 pytest 142 passed。迁移 head `0004_repair_invariants`
- 写入范围：资产/上传/对象存储/转换适配模块、已分配的转换 Worker、`tests/acceptance/test_assets_derivatives.py`；共享任务/迁移由集成 owner 管理
- 对应门禁：G2；消费者：T07、T10

## 目标

上传的资料与专业原件可以准确取回，更新形成新作品版本；预览失败不会损坏原件。图片、PDF 和至少一种选定 3D 格式具备真实的预览路径；其他格式展示可说明来源的封面和下载能力。测试与证据始终绑定确切作品版本。

## 输入与交付

实现票据→隔离字节暂存→服务端大小/MIME/哈希校验→不可变落盘→版本登记与 Outbox 的上传链。对象键由服务端生成且按项目隔离，不能把用户路径拼成磁盘路径；本地运行数据与源码、临时转换、派生结果分开。

与 T06 冻结 `artifact_version_registered` 及派生 Job 输入/输出合同；每次转换使用 attempt 专属临时目录，结果只能由当前 fencing token 登记。图片/PDF/3D 转换器按栈合同选择，隔离资源、超时和错误；依赖缺失显式 unavailable。不得把用户提供封面伪称工程预览，不执行原件中的脚本/宏。

## 验收

创建并执行 `python -m pytest -q tests/acceptance/test_assets_derivatives.py`。本卡先用确定性消费者验证资产行为，T06 完成后执行真实 Worker 联合测试：

1. 同名不同字节生成两个版本；下载哈希与上传原件一致。
2. 超大、伪造长度/哈希、越权票据和路径穿越输入被拒绝，失败不登记可引用原件。
3. 字节落盘后数据库失败，暂存孤儿可识别与安全清理；已登记原件不被清理。
4. 转换器崩溃/超时/格式不支持时原件仍可下载，各状态独立。
5. 三类 P0 格式各有真实小夹具与可检查预览；许可和转换器版本进入元数据。
6. 未授权主体无法读取原件、封面、预览、文件名或存储路径；旧版测试不漂移到新作品版。

## 交接与证据

交出格式能力表、对象键规则、下载授权接口、转换器版本/安装要求、完整性测试和失败恢复样例。附实际预览文件而不是仅有状态 JSON；跨模块 G2 证据由集成 owner 合并，fake consumer 的成功不能替代真实任务投递验证。

2026-10-31 pin 接入：状态仍是 implementing。仓库根命令 `.venv/Scripts/python.exe -m pytest -q tests/acceptance/test_assets_derivatives.py -o cache_dir=_施工文件/2026-10-31_grok-dev-a/99_临时文件/pytest-cache --basetemp=_施工文件/2026-10-31_grok-dev-a/99_临时文件/pytest`，退出码 0，33 passed。成功登记后 stored_objects 为 ready，上传 pin 已消费，ArtifactVersion.object_key 与安装键一致。真实 Pillow/PDF/GLB 渲染未做，下载路由仍未挂到 create_app。交接见 `workpool/handoffs/T05_PIN_SLICE.md`。

2026-10-31 栅格与 PDF 预览：状态仍是 implementing。`render_original_preview` 用已导入的 Pillow 11.3.0 解码非 SVG 栅格，用已导入的 pypdfium2 4.30.0 渲染 PDF 第 0 页，写出无 EXIF 的 PNG；路径走 `resolve_contained`。超过调用方像素上限、SVG，以及 Blender 不在时的 `model/gltf-binary`，都不写文件。`preview_disposition` 和 `run_preview_converter` 未改，注入转换器的正常返回仍是 not_run、generated false。仓库根命令 `.venv/Scripts/python.exe -m pytest -q tests/acceptance/test_preview_render.py tests/acceptance/test_assets_derivatives.py::test_missing_converter_does_not_fabricate_a_preview tests/acceptance/test_assets_derivatives.py::test_injected_converter_failures_leave_the_original_downloadable -o cache_dir=_施工文件/2026-10-31_grok-dev-a2/99_临时文件/pytest-cache --basetemp=_施工文件/2026-10-31_grok-dev-a2/99_临时文件/pytest`，退出码 0，9 passed。Blender 预览仍未做。交接见 `workpool/handoffs/T05_PREVIEW_RENDER_SLICE.md`。

2026-10-31 登记后渲染：状态仍是 implementing。`render_registered_preview` 在调用方的 `write_uow` 提交之后，用 `read_original` 读取已经落盘的原件，再调用 `render_original_preview`。函数本身不打开会话，也不提交。`register_original` 仍只调用 `preview_disposition`。小 PNG 在派生目录写出 PNG；超过像素上限时不写预览，原件仍可由 `download_original` 按原哈希下载。迁移和 `0007_run_origin` 的 head 断言未改。仓库根命令 `.venv/Scripts/python.exe -m pytest -q tests/acceptance/test_registered_preview.py tests/acceptance/test_preview_render.py -o cache_dir=_施工文件/2026-10-31_grok-dev-a3/99_临时文件/pytest-cache --basetemp=_施工文件/2026-10-31_grok-dev-a3/99_临时文件/pytest`，退出码 0，9 passed。Blender 预览仍未做。交接见 `workpool/handoffs/T05_REGISTER_PREVIEW_SLICE.md`。

2026-10-31 预览 HTTP：状态仍是 implementing。`POST /api/v1/artifact-versions/{artifact_version_id}/preview` 先在短只读会话里按 `preview.read` 授权，读出项目、哈希、generation 和媒体类型，关闭会话后再调用 `render_registered_preview`。这条路由不打开 `write_uow`。像素上限固定为 4194304。相对路径由服务端生成为 `previews/{artifact_version_id}.png`。响应只有 `generated`、`state`、`reason`、`generator_name`、`generator_version`。请求 JSON 里的路径和像素上限不参与渲染。缺少 `objects_root` 或 `derived_root` 时返回 `state=unavailable`、`reason=storage_not_configured`，不渲染，响应里没有磁盘路径。没有 `preview.read` 的调用方得不到生成器名称、版本或预览文件。匿名是 401。`register_original` 未改。迁移和 head 断言未改。仓库根命令 `.venv/Scripts/python.exe -m pytest -q tests/acceptance/test_preview_http.py tests/acceptance/test_registered_preview.py -o cache_dir=_施工文件/2026-10-31_grok-dev-a4/99_临时文件/pytest-cache --basetemp=_施工文件/2026-10-31_grok-dev-a4/99_临时文件/pytest`，退出码 0，3 passed。Blender 预览仍未做。交接见 `workpool/handoffs/T05_PREVIEW_HTTP_SLICE.md`。

2026-10-31 预览读取：状态仍是 implementing。`GET /api/v1/artifact-versions/{artifact_version_id}/preview` 先在短只读会话里按 `preview.read` 授权，关闭会话后只读取已经存在的 `previews/{artifact_version_id}.png`。不调用 `render_registered_preview`，不打开 `write_uow`，文件不存在时不创建。有权且文件存在时返回 PNG 字节，`nosniff`，`Cache-Control: no-store`，文件名固定为 `preview.png`，不用作品标题。没有 `preview.read` 是 403，正文不是 PNG。匿名是 401。缺失文件、隐藏或不存在的版本不返回标题或磁盘路径。POST、`register_original`、迁移未改。仓库根命令 `.venv/Scripts/python.exe -m pytest -q tests/acceptance/test_preview_http.py -o cache_dir=_施工文件/2026-10-31_grok-dev-a5/99_临时文件/pytest-cache --basetemp=_施工文件/2026-10-31_grok-dev-a5/99_临时文件/pytest`，退出码 0，1 passed。Blender 预览仍未做。交接见 `workpool/handoffs/T05_PREVIEW_GET_SLICE.md`。

2026-10-31 复用 pin 围栏：状态仍是 implementing。同项目复用 ready 对象时，`_consume_upload_pin` 的条件更新增加 `object_pins.fencing_token` 等于放置 pin 前从该 ready 对象读到的 token。对不上或 token 不是 int 就抛 `RegistrationConflict`，调用方 `write_uow` 回滚，不留下新作品版本，该次 pin 也不留下已消费行。成功复用仍消费 pin，且不链接第二份物理文件。`install_reserved_original`、迁移、预览路由未改。仓库根命令 `.venv/Scripts/python.exe -m pytest -q tests/acceptance/test_assets_derivatives.py -o cache_dir=_施工文件/2026-10-31_grok-dev-a6/99_临时文件/pytest-cache --basetemp=_施工文件/2026-10-31_grok-dev-a6/99_临时文件/pytest`，退出码 0，34 passed。交接见 `workpool/handoffs/T05_PIN_FENCE_SLICE.md`。

2026-10-31 登记审计：状态仍是 implementing。成功的 `register_original` 在返回前，于调用方的 `write_uow` 里写入一行 `audit_events`。`actor_id` 用票据的 `created_by_account_id`。`command` 是 `artifact.version_registered`。`subject_type` 是 `artifact_version`，`subject_id` 是新版本 id。`project_revision` 是这次登记推进后的项目修订。`payload` 只有 `project_id`、`artifact_id`、`artifact_version_id`、`content_hash`、`generation_id`、`byte_size`，不含磁盘路径和原件字节。哈希不符、长度不符、越权不写审计。迁移、预览路由、Worker 未改。仓库根命令 `.venv/Scripts/python.exe -m pytest -q tests/acceptance/test_registration_audit.py -o cache_dir=_施工文件/2026-10-31_grok-dev-a7/99_临时文件/pytest-cache --basetemp=_施工文件/2026-10-31_grok-dev-a7/99_临时文件/pytest`，退出码 0，2 passed。随后同一缓存目录跑 `tests/acceptance/test_assets_derivatives.py::test_same_bytes_round_trip_and_do_not_invent_a_preview`，退出码 0，1 passed。交接见 `workpool/handoffs/T05_REGISTER_AUDIT_SLICE.md`。

2026-10-31 登记审计复用与失败：状态仍是 implementing。同项目第二次登记相同字节时，再写一行 `audit_events`，`subject_id` 和 payload 里的 `artifact_version_id` 都是新版本 id，磁盘上仍只有一份名为 `original` 的物理原件，`stored_objects` 仍是一行。声明长度不符时审计行数和作品版本数都是 0。主体账户不是票据创建者时审计行数是 0，票据保持 `open`。`register_original` 未改。迁移、预览、Worker、SESSION、BOARD 未改。仓库根命令 `.venv/Scripts/python.exe -m pytest -q tests/acceptance/test_registration_audit.py -o cache_dir=_施工文件/2026-10-31_grok-dev-a8/99_临时文件/pytest-cache --basetemp=_施工文件/2026-10-31_grok-dev-a8/99_临时文件/pytest`，退出码 0，5 passed。交接见 `workpool/handoffs/T05_REGISTER_AUDIT_FAILURES.md`。

2026-10-31 票据过期：状态仍是 implementing。`request_upload_ticket` 增加可选的 `expires_at`，不传时该列为空，表示不过期。登记时 `expires_at` 有值且不晚于本次 `now`（等于 now 也算过期）则拒绝，原因是 `ticket_expired`，不产生作品版本，不写审计。未来的 `expires_at` 仍照常登记。新迁移 `0008_ticket_expiry`，`down_revision` 是 `0007_run_origin`。只给 `upload_tickets` 增加这一可空列。`0001` 到 `0007` 的 SQL 未改，`0007` 的 revision id 未改。产品表仍是 43 张。`SCHEMA_REVISION` 改为 `0008_ticket_expiry`。预览、Worker、pyproject.toml、SESSION、BOARD 未改。仓库根先建 `_施工文件/2026-10-31_grok-dev-a9/99_临时文件`，再执行 `.venv/Scripts/python.exe -m pytest -q tests/acceptance/test_ticket_expiry.py tests/acceptance/test_registration_audit.py tests/acceptance/test_assets_derivatives.py::test_head_includes_stored_object_tables -o cache_dir=_施工文件/2026-10-31_grok-dev-a9/99_临时文件/pytest-cache --basetemp=_施工文件/2026-10-31_grok-dev-a9/99_临时文件/pytest`，退出码 0，11 passed。`.venv/Scripts/python.exe -m alembic heads` 退出码 0，只有一行 `0008_ticket_expiry (head)`。交接见 `workpool/handoffs/T05_TICKET_EXPIRY_SLICE.md`。

## 并发补充（2026-10-30）

按 06 最后一节实现 StoredObject generation、上传 reservation/pin 与 GC claim；引用登记与 GC 必须在同一数据库协调。GC 不能只依赖文件年龄和复查引用。补真实双连接的“登记先赢/清理先赢”测试，以及旧 GC 迟到删除不影响新 generation 的故障例。此项属于本卡完成条件，对应 S08；当前仅补施工合同，未实现对象库。


长字节只写 attempt 暂存；pin/state/fence 有效性检查下的最终短原子安装与引用登记协调，禁止失效上传重新安装到已 deleted generation。补“GC 已完成后旧上传晚到”的故障测试。
