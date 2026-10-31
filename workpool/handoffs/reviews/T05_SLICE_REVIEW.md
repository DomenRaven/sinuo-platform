# T05 切片审查

日期：2026-10-31。只读。只写本文件。不改源代码、测试、迁移，不做 git commit。

这是当前切片的审查，不是 T05 完成声明。任务卡 `workpool/tasks/T05_ASSETS_DERIVATIVES.md` 的状态仍是 implementing。

对照材料：

- `docs/construction/06_SECURITY_STORAGE_RECOVERY.md` 里的原件登记、下载授权，以及 generation / pin / GC 并发协议。预览一节只用来对照验收 5。
- `tests/acceptance/test_assets_derivatives.py`，以及它调用的 `src/sinuo/application/assets/` 与 `src/sinuo/infrastructure/assets/`。
- 测试要碰到的 `write_uow`、`ArtifactVersion` 列、`migrations/versions/0005_stored_objects.py`、草案 `workpool/handoffs/proposals/0005_stored_objects.draft.py` 的缺失列，以及 `SessionAuthorizer.authorize` 的入口。

06 末句和任务卡仍写“当前骨架未实现对象库”。下面以这次读到的代码和跑到的测试为准。

## pytest

```
.\.venv\Scripts\python.exe -m pytest -q tests/acceptance/test_assets_derivatives.py
```

工作目录：`sinuo-platform`。

退出码 **0**。**22 passed**，1.51s。输出里没有 failed，也没有 skipped。

## 已覆盖的验收点

任务卡 6 条里，1、2、3、4、6 的核心断言有对应用例。验收 5 没有覆盖，记在缺口。并发协议的四种顺序有对应用例，登记函数本身仍不走 pin。

### 验收 1：同名不同字节是两个版本，下载哈希等于原件

`test_same_name_new_bytes_are_two_versions_and_reuse_stays_in_project`

同一作品标题下先后写入 `BYTES_A`、`BYTES_B`、再一次 `BYTES_A`，另一项目再写一份 `BYTES_A`。

实际断言：

- 四个 `artifact_version_id` 互不相同。同项目版本号是 1、2、3。
- 同项目两份相同字节共用 `object_key` 和 `generation_id`。不同字节的键不同。
- 另一项目的键以 `objects/<other-project>/` 开头，且和本项目文件 `samefile` 为假。磁盘上有 3 个 `original`。
- 四次 `download_original` 的 `body` 与对应上传字节一致，`server_hash` 等于该 body 的 sha256。

同项目复用走 `register_original` 里的 `_reusable_original`：按本项目的 `ArtifactVersion` 找键，再流式重算已落盘文件的哈希和长度。另一项目的行不会被选中。

### 验收 2：超限、伪造长度、伪造哈希、越权登记、路径穿越

| 用例 | 实际断言 |
| --- | --- |
| `test_byte_limit_stops_before_a_complete_attempt` | 超限的 `write_attempt_bytes` 抛 `AttemptTooLarge`，不建 attempt 目录。`expected_size` 超过 `byte_limit` 时票据抛 `AssetRejected("byte_limit")`。已写好的 attempt 再按更小上限登记，结果是 `registered is False`、`reason == "byte_limit"`，票据 `rejected`，作品版本数 0，对象根下没有 `original`。 |
| `test_declared_length_mismatch_leaves_no_referable_original` | 声明长度比字节数少 1。登记 `expected_size_mismatch`，attempt 仍在，版本数 0，票据 `rejected` 且 `generation_id` 为空。 |
| `test_wrong_declared_hash_registers_nothing` | 声明哈希与字节不符。第一次 `declared_hash_mismatch`，第二次同一票据 `ticket_closed`。版本数 0，Outbox 的 `artifact.version_registered` 为 0，项目 `revision` 仍为 1。 |
| `test_subject_without_artifact_upload_leaves_no_referable_original` | 只有 `preview.read` 的成员登记时得到 `ActionForbidden`，动作为 `artifact.upload`。只有 `project.read` 的主体得到 `NotFoundOrHidden`。票据保持 `open`。补上 `artifact.upload` 与 `artifact.original.read` 后，同一票据可以登记并下载，哈希与字节一致。 |
| `test_path_traversal_writes_nothing`、`test_absolute_attempt_path_writes_nothing`、`test_symlink_escape_writes_nothing` | `../`、嵌套 `../`、反斜杠 `..`、绝对路径、符号链接/目录联接都抛 `PathEscape`，根外没有文件。符号链接这条本次没有 skip。 |

路径检查在 `resolve_contained`：拒绝空路径、NUL、`..`、绝对路径和盘符，`resolve` 之后还必须落在配置根内。`register_original` 和 `write_attempt_bytes` 都走这个函数。穿越用例直接打在 `write_attempt_bytes` 上。

成功登记的那条 `test_same_bytes_round_trip_and_do_not_invent_a_preview` 还锁定：服务端哈希等于字节 sha256，对象键为 `objects/<project>/sha256/<hash>/<generation>/original`，键里没有 `..`、反斜杠和前导 `/`，attempt 被移走，Outbox 载荷只有 `project_id`、`artifact_id`、`artifact_version_id`、`content_hash`、`generation_id`、`byte_size`，项目 `revision` 从 1 变成 2。`converter_installed=False` 时 `preview_generated` 为假，`preview_state` 为 `unavailable`。

### 验收 3：提交失败后的孤儿可清，已登记原件保留

`test_rollback_after_install_cleans_only_the_unregistered_orphan`

先成功登记一份字节。下一份在 `register_original` 返回后、`write_uow` 提交前抛 `RuntimeError("database failed")`。回滚后作品版本仍只有已提交的那一行。`discard_unregistered_original` 对已提交 generation 返回 `referenced` 且不删文件；对回滚那份返回 `deleted`，对应 `original` 消失。已提交字节仍可按授权下载，哈希一致。

已提交那次 generation 在 `stored_objects` 里没有行。孤儿是调用方把 generation id 传进去之后，才由 `reserve_generation` 补一行再 claim、unlink。

### 验收 4：注入的转换失败不挡住原件

`test_injected_converter_failures_leave_the_original_downloadable`

四次注入分别得到：

- `failed` / `converter_crashed`
- `timed_out` / `converter_timeout`
- `unsupported` / `format_unsupported`
- `unavailable` / `converter_not_installed`

四组 state/reason 彼此不同。disposition 的文本里没有崩溃详情，也没有占位字节。派生目录里没有文件。原件字节不变，授权下载的 body 和 sha256 与上传一致。

`test_missing_converter_does_not_fabricate_a_preview` 另锁定：工具未安装是 `unavailable`；调用方声明已安装时是 `not_run` / `converter_not_invoked`，`generated` 仍为假，不写预览文件。探测结果允许 `unavailable` 或 `not_run`，本次没有单独打印机器上有没有 Pillow、pypdfium2 或 Blender。

### 验收 6：未授权读不到原件和预览元数据；旧授权看不到新版本

`test_original_download_requires_grant_and_omits_disk_path`

`download_original` 先做 `artifact.original.read`，再解析对象键。只有项目读和 `preview.read` 的成员得到 `ActionForbidden`。只有项目读的主体得到 `NotFoundOrHidden`。两个异常的文本里没有原件字节，也没有对象根、缺失根或临时目录的路径。持有该 `artifact_version_id` 的 `artifact.original.read` 得到 body、服务端哈希、长度和 `media_type`。结果字段只有这五项。下载函数所在模块没有预览写入。

`test_preview_metadata_stops_at_the_new_version_grant`

新版本预览注入为崩溃。旧版本和新版本各写一条元数据。只有新版本 `preview.read` 的主体拿到新版本文件名、`object_key`、`failed` / `converter_crashed`。只有新版本 `artifact.original.read` 的主体得到 `PreviewRefusal(decision="forbidden")`。只有旧版本预览和原件授权的主体，读新版本时得到 `decision="hidden"`。拒绝结果的字段只有 `decision`，文本里没有新旧文件名、对象键、预览原因和崩溃详情，也没有磁盘路径。派生目录没有预览文件。

授权成功时返回的 `object_key` 是逻辑键。测试要求它等于新版本的键，并检查其中没有绝对磁盘路径。

### 表头

`test_head_includes_stored_object_tables`

库版本是 `0005_stored_objects`。表含 `upload_tickets`、`stored_objects`、`object_pins`。`upload_tickets` 的列与 `UploadTicketRow` 一致。草案把 `object_id`、`generation_id`、`preview_state` 列在 `artifact_versions` 的缺失列里。`ArtifactVersion` 模型当前有 `object_key`、`content_hash`、`license`，没有这三列。

### generation、pin、GC

`write_uow` 在交出 session 之前执行 `BEGIN IMMEDIATE`，提交和回滚由这个单元负责。下面四个用例都是两个线程、各自的 `write_uow`。屏障让先手的事务结束之后，后手才开始。本次没有一条测试在同一条写锁里面交错两条语句。

`test_upload_pin_before_gc_keeps_the_generation`

先 `reserve_generation`（`created_at` 比保留期更早），再让 pin 先提交。随后的 `claim_generation` 为 `claimed is False`、`reason == "pinned"`。对象保持 `reserved`，`fencing_token` 为 0。pin 的 `purpose` 为 `upload`，未消费。之后 `install_reserved_original` 成功，状态变为 `ready`，token 变为 1，pin 的 `consumed_at` 等于安装时刻。字节经 `os.link` 进入该 generation，哈希与 payload 一致，没有预览文件。

`test_gc_claim_before_pin_blocks_install`

claim 先提交，`fencing_token` 为 1，状态 `gc_claimed`。后手 pin 为 `generation_unavailable`，安装失败，attempt 仍在，对象根下没有 `original`。token 0 的删除返回 `stale_token`。token 1 的删除把状态写成 `deleted`。删除之后再次 pin 和安装仍然失败，attempt 还在，没有 `original`。该对象的 pin 行数为 0；这两次 pin 在插入前就返回了。

`test_stale_gc_unlink_leaves_the_new_generation_download`

`register_original` 先落下活 generation。再为同一个 generation id 补一行 `stored_objects`，并另建一个旧 generation，写入不同字节。对活对象的 claim 返回 `referenced`，token 0 的删除返回 `not_claimed`，活文件不变。旧对象可以被 claim。token 0 不删旧文件。token 1 只删除旧 generation 的文件，重复删除仍返回 `deleted`。活文件和授权下载的哈希仍是最初那份 payload。

`test_gc_claimed_generation_reuploads_on_a_new_generation`

旧 generation 被 claim 之后，用另一份字节走 `register_original`。新 `generation_id` 与被 claim 的不同，新文件和旧文件不是同一个文件。旧行保持 `gc_claimed`，旧字节还在。这条测试没有调用删除。授权下载得到的是新字节和新哈希。

claim 的条件写在同一条 `UPDATE` 里：状态是 `reserved` 或 `ready`，fence 匹配，`created_at` 早于保留截止，没有未消费且未过期的 pin，也没有相同 `object_key` 的 `ArtifactVersion`。`delete_claimed_generation` 在 fence 或状态不符时先返回，不执行 unlink。

## blocking

**0**

上面列出的断言和本次 pytest 一致。下面的缺口没有一条把这次 22 个测试打失败，也不把已经锁定的原件哈希、越权拒绝或 generation 隔离说成已完成的整卡验收。

## 不阻断的缺口

1. 验收 5 仍开放：真实栅格图、PDF、GLB 预览缺失。`tests/` 下没有 png、pdf、glb。`preview.py` 只用 `find_spec("PIL")`、`find_spec("pypdfium2")` 和 `shutil.which("blender")` 做探测，不渲染。`run_preview_converter` 对正常返回记 `not_run`，并删除尝试目录里的新文件。`PreviewDisposition.output_path` 固定是 `None`。`record_preview_metadata` 在 `generated` 为真或 `output_path` 有值时拒绝写入。许可写在 `ArtifactVersion.license`，来自票据；预览 JSON 只有文件名、state、reason 和 `generated: false`，没有许可，也没有生成器名称或版本。06 要求的派生目录 `derived/<project>/<artifact_version>/<generator_version>/<hash>` 这次没有使用。

2. 验收 6 的封面字节没有读取用例，也没有封面文件。旧版本持有者能否仍读旧版本元数据，这条测试没有再查。

3. `register_original` 不建立 reservation，不插 `stored_objects`，不放 pin，也不把 pin 消费掉。它在调用方的 `write_uow` 里先流式哈希，写入作品版本和 Outbox，再 `os.link`。成功路径的 generation 可以没有对象行；回滚用例明确断言已提交 generation 的 `StoredObjectRow` 为 `None`。06 要求的两阶段是：事务外完成哈希和格式校验，短事务重查 pin、state、fence、票据归属和当前权限，再做同卷安装并登记版本。票据归属没有和 `principal` 比对。不传 `authorizer` 时登记不检查 `artifact.upload`。`request_upload_ticket` 没有授权参数。

4. 同项目复用已有字节时没有 pin。`test_same_name_new_bytes_are_two_versions_and_reuse_stays_in_project` 锁定的是共用 `object_key`，没有断言 `object_pins`。

5. 下载停在 `download_original` 返回的字节和哈希。结果里没有安全文件名。仓库里这个函数没有 HTTP 路由，因此没有 `nosniff`、附件处置或 `Cache-Control: no-store`。`artifact_versions` 没有 `original_filename`。无业务引用对象的 download/backup pin 没有对应函数；`place_upload_pin` 只写 `purpose="upload"`。

6. 格式声明和字节内容没有比对。测试用 `model/gltf-binary` 登记普通文本字节，并要求登记成功。迁移的状态检查允许 `quarantined`，转换触发器也允许进入该状态。`gc.py` 没有写入 `quarantined` 的路径。登记只写 Outbox，不写 `audit_events`。票据没有过期列。项目容量和并行上传数没有检查。登记阶段的 `byte_limit` 在整文件哈希之后才判断。

7. GC 先赢之后的“安全重新上传”分成了两段：`test_gc_claim_before_pin_blocks_install` 证明原 generation 不能再安装；`test_gc_claimed_generation_reuploads_on_a_new_generation` 上传的是另一份哈希，而且旧文件还留在 `gc_claimed`。同一内容在 generation 被删除之后重建出新 generation，这次没有用例。`install_reserved_original` 不核对源文件哈希是否等于该行的 `sha256`。文件占用导致 unlink 失败时，代码返回 `unlink_failed` 且不把 `gc_claimed` 写成 `deleted`；没有用例注入这个失败。

8. 对象键比 06 建议的目录少一层 `sha256/<prefix>/`。草案、`original_key` 和成功登记测试都锁定六段键：`objects/<project>/sha256/<hash>/<generation>/original`。`artifact_versions` 不引用 `stored_objects.object_id`，generation 只出现在键和 Outbox 载荷里。

9. `migrations/versions/0005_stored_objects.py` 开头仍写“本切片不插入 `stored_objects` 和 `object_pins`”。GC 用例会插入这两张表。测试文件开头仍写第一切片且不声称其余验收，正文已经包含下载、pin/GC 和预览元数据。

原件、下载授权和 generation 隔离以这次 22 个测试为准。验收 5 和上面的接线缺口还在。T05 保持 implementing。
