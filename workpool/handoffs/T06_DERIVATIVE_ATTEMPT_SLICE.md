# T06 派生尝试文件

日期：2026-10-31。T06 状态保持 implementing。本切片不把任务标成 done。

## 对照

冻结合同 `workpool/handoffs/T06_DERIVATIVE_ATTEMPT_CONTRACT.md`，版本 `T06-DERIVATIVE-ATTEMPT-1`。施工文档 `docs/construction/05_WORKFLOW_AND_CONCURRENCY.md` 的「任务认领、租约与 fencing token」：文件转换先写 attempt 专属目录，只有当前 token 能登记有效指针；旧 Worker 可以留下不可达文件，不能覆盖新结果路径。同文「Outbox 与至少一次消费」，以及 `docs/construction/07_BACKEND_ACCEPTANCE.md` 的 W07：派生文件完成后、登记前崩溃，可以重试登记，最终只有一个有效指针。

本仓库没有单独的文献库。上面这份合同和施工节就是本切片的依据。

## 选型

写字节复用 `write_attempt_bytes`，路径复用 `is_storage_segment` 和 `resolve_contained`。相对路径固定为 `jobs/<job_id>/<fencing_token>/result`。`job_id` 或 token 不是安全路径段、含 `..`，或者解析后离开调用方给出的根目录，就在创建任何文件之前拒绝。

登记仍调用现有的 `finalize_job`。`output_ref` 就是这条相对路径。调用方把这次调用放在自己的 `write_uow` 里。写文件的函数不接收会话，也不打开 `write_uow`。

现有 `_require_label` 不接受 `/`，这条相对路径过不去。`finalize_job` 的 WHERE 没有改，仍是 `_live`：`running`、owner、fencing token、`input_hash`、未过期租约。只增加 `_require_output_ref`：原来的单段 token 仍接受；另外接受 `jobs/<段>/<正整数>/result`。整段里的 `..` 仍然拒绝。放弃把文件写进事务，也放弃放宽认领条件来让旧 token 成功。

## 行为

认领在短事务里提交。事务结束后把字节写到该 token 的 `result`。此时若还没 finalize，`output_ref` 仍为空，文件留在原地。同一个 token 可以在下一次 `write_uow` 里 finalize 一次；再调用一次被 `StaleLease` 拒绝，`output_ref` 不变。

租约过期后另一个 worker 认领，把自己的字节写到新 token 目录，并 finalize。旧 token 的 finalize 抛出 `StaleLease`，`output_ref` 仍是新路径。旧文件可以留在旧 token 目录里，但不是这条指针。

## 证据

先创建目录 `_施工文件/2026-10-31_grok-dev-b5/99_临时文件`，再从仓库根执行：

```text
.venv/Scripts/python.exe -m pytest -q tests/acceptance/test_derivative_attempt.py -o cache_dir=_施工文件/2026-10-31_grok-dev-b5/99_临时文件/pytest-cache --basetemp=_施工文件/2026-10-31_grok-dev-b5/99_临时文件/pytest
```

退出码 0。11 passed。

## 还没做

没有调用模型，没有新迁移，没有常驻 consumer。T06 不标 done。
