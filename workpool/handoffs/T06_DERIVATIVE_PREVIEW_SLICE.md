# T06 派生预览

日期：2026-10-31。T06 状态保持 implementing。本切片不把任务标成 done。

## 对照

冻结合同 `workpool/handoffs/T06_DERIVATIVE_PREVIEW_CONTRACT.md`，版本 `T06-DERIVATIVE-PREVIEW-1`。

施工文档 `docs/construction/05_WORKFLOW_AND_CONCURRENCY.md` 的「任务认领、租约与 fencing token」：认领在短事务里提交，外部转换留在事务外，只有当前 fencing token 能 finalize。文件先落在这次 attempt 的路径上，旧 token 不能改掉新指针。同文「Outbox 与至少一次消费」：文件系统进不了同一个数据库事务；派生文件写完、登记前回崩溃，可以再登记一次，最终只有一个有效指针。

`docs/construction/06_SECURITY_STORAGE_RECOVERY.md` 的「预览、抽取与专业文件」：栅格用 Pillow，像素超过上限就拒绝；GLB 在 Blender 未安装时标成不可用，不用占位图代替封面。预览失败不影响原件。

本仓库没有单独的文献库。上面这份合同和这两节施工文档就是本切片的依据。

## 选型

渲染复用 `render_registered_preview`。它读取已经落盘的原件，再调用 `render_original_preview`。相对路径复用 `derivative_files._attempt_relative`，成功时 PNG 写在 `jobs/<job_id>/<fencing_token>/result`。渲染函数不接收会话，也不打开 `write_uow`。

发布仍由调用方在下一次 `write_uow` 里调用现有的 `finalize_job`。`output_ref` 就是渲染返回的这条相对路径。`finalize_job` 的 WHERE 没有改。

像素超限和 SVG 不写文件。结束类别用现有不可重试的 `invalid_schema`，调用方把它交给 `finish_job_attempt`。没有 Blender 的 GLB 不写文件，类别是现有的 `unavailable`。测试把 `max_attempts` 设成当前 `attempt_no`。次数上限先于重新排队，所以这次写成 `failed`，`last_error` 为 `attempts_exhausted`，不会无限回到 queued。

放弃在渲染函数里开事务，也放弃改认领条件或迁移。

## 行为

认领在 `write_uow` 里提交。事务结束后才读原件并渲染，这段不构造数据库会话。小 PNG 写成该 token 的 `result`，此时 `output_ref` 仍为空。随后另一次 `write_uow` 发布 `jobs/<job_id>/<token>/result`。同一 token 再发布一次被 `StaleLease` 拒绝。

租约过期后，新 token 再渲染并发布。旧 token 的 `finalize_job` 抛出 `StaleLease`，`output_ref` 仍是新路径。旧 PNG 可以留在旧 token 目录里，但不是这条指针。

GLB 在本机没有 Blender 时，渲染结果是 `unavailable` / `converter_not_installed`。派生目录没有文件。`finish_job_attempt` 结束后 `output_ref` 仍为空，状态是 `failed`。

像素超限和 SVG 同样不写文件，并以 `invalid_schema` 失败结束。原件字节只读。

## 证据

先创建目录 `_施工文件/2026-10-31_grok-dev-b6/99_临时文件`，再从仓库根执行：

```text
.venv/Scripts/python.exe -m pytest -q tests/acceptance/test_derivative_preview.py -o cache_dir=_施工文件/2026-10-31_grok-dev-b6/99_临时文件/pytest-cache --basetemp=_施工文件/2026-10-31_grok-dev-b6/99_临时文件/pytest
```

退出码 0。5 passed。

## 还没做

没有调用模型，没有新迁移，没有常驻 consumer，没有改预览 HTTP。Blender 在场时的 GLB 封面仍未做。T06 不标 done。
