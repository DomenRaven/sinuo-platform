# T06 派生预览复审

日期：2026-10-31。本次阅读范围是 `workpool/handoffs/T06_DERIVATIVE_PREVIEW_CONTRACT.md`、`workpool/handoffs/T06_DERIVATIVE_PREVIEW_SLICE.md`、`src/sinuo/workers/derivative_preview.py`、`tests/acceptance/test_derivative_preview.py`。下面只记录这次阅读和这次运行。

## 命令与退出码

先创建 `_施工文件/2026-10-31_grok-review-dpreview/99_临时文件`，再从仓库根执行：

```text
.venv/Scripts/python.exe -m pytest -q tests/acceptance/test_derivative_preview.py -o cache_dir=_施工文件/2026-10-31_grok-review-dpreview/99_临时文件/pytest-cache --basetemp=_施工文件/2026-10-31_grok-review-dpreview/99_临时文件/pytest
```

标准输出：`5 passed in 0.53s`。退出码 0。输出里没有 skipped。`basetemp` 下有五个用例目录，对应小 PNG、旧 token、无 Blender 的 GLB，以及像素超限和 SVG 两组拒绝。

## 这次读到的渲染函数

`render_derivative_preview` 不接收会话，函数体内也不调用 `write_uow`。它用 `_attempt_relative(job_id, fencing_token)` 得到相对路径，再调用 `render_registered_preview`。`generated` 为真时返回该相对路径，`finish_category` 为 `None`。为假时 `relative_path` 为 `None`。`_finish_category` 在 `reason` 属于 `pixel_limit` 或 `svg_rejected`，或者 `state == "unsupported"` 时返回 `invalid_schema`，其余返回 `unavailable`。

## 测试锁住的行为

渲染不在 `write_uow` 里。`claim()` 的 `with write_uow` 在函数返回时结束。`test_small_png_publishes_attempt_png` 和 `test_old_token_cannot_replace_published_preview` 把阶段列表断言为 `claim`、`claim-committed`、`render`、`render-closed`、`publish`、`publish-committed`。`claim-committed` 记在 `claim()` 返回之后，下一次 `write_uow` 记在 `render-closed` 之后。`test_glb_without_blender_does_not_publish` 和 `test_non_retryable_preview_refusal_writes_nothing` 同样在 `claim()` 返回之后调用 `render_outside`，到 `finish_job_attempt` 时才再开 `write_uow`。

`render_outside` 在进入会话计数器之前、`render_derivative_preview` 返回之后、计数器退出之后，三处断言 `engine.pool.checkedout() == 0`。`render_without_database_session` 断言这段期间 `Session.__init__` 的调用次数为 0，否则抛出 `AssertionError("database session opened during render")`。这次 5 条都通过，所以这次运行里的每一次 `render_outside` 都满足这两条断言。

`test_small_png_publishes_attempt_png` 把成功 PNG 钉在当前 token 上。

- 认领提交后 `fetch_job` 断言 `status == "running"`、`fencing_token == attempt.fencing_token`、`output_ref is None`、`lease_owner == "worker-1"`，并断言 `engine.pool.checkedout() == 0`。
- 渲染返回值断言 `generated is True`、`finish_category is None`、`state == "ready"`、`reason == "rendered"`，并且 `relative_path == f"jobs/{job_id}/{attempt.fencing_token}/result"`。
- `assert_png` 断言该文件以 `PNG_MAGIC` 开头，且 `Image.open` 的 `format == "PNG"`。`files_under(derived) == [stored]`。
- 发布前 `fetch_job` 断言 `status == "running"`、`output_ref is None`。`read_original(...) == payload`。
- 随后一次 `write_uow` 里用当前 `fencing_token` 和这条 `relative_path` 调用 `finalize_job`。返回值断言 `output_ref == relative`。`fetch_job` 断言 `status == "succeeded"`、`output_ref == relative`、`output_ref == f"jobs/{job_id}/{attempt.fencing_token}/result"`、`lease_owner is None`。文件再次通过 `assert_png`。
- 同一 owner、同一 token 再 `finalize_job` 一次，断言抛出 `StaleLease`。之后断言 `output_ref == relative`、`fencing_token == attempt.fencing_token`，文件仍通过 `assert_png`。

`test_old_token_cannot_replace_published_preview` 把旧 token 不能换掉指针钉在断言上。

- 旧 token 渲染后断言 `relative_path` 等于 `jobs/{job_id}/{first.fencing_token}/result`，旧文件通过 `assert_png`，`output_ref is None`。
- 租约写成 `lease_until=1.0` 后，另一 owner 认领。断言 `second.fencing_token != first.fencing_token`，`lease_owner == "worker-2"`。
- 新 token 渲染断言 `generated is True`，`relative_path` 等于 `jobs/{job_id}/{second.fencing_token}/result`，新文件通过 `assert_png`，发布前 `output_ref is None`。
- 新 token 的 `finalize_job` 返回值断言 `output_ref == new_relative`。
- 旧 owner、旧 token、旧路径的 `finalize_job` 断言抛出 `StaleLease`。
- 随后 `fetch_job` 断言 `status == "succeeded"`、`output_ref == new_relative`、`output_ref == f"jobs/{job_id}/{second.fencing_token}/result"`、`fencing_token == second.fencing_token`。旧文件和新文件都通过 `assert_png`。`read_original(...) == payload`。

`test_glb_without_blender_does_not_publish` 把没有 Blender 的 GLB 钉在断言上。这次运行没有 skipped，所以 `shutil.which("blender") is None` 已执行并通过。

- 渲染返回值断言 `generated is False`、`state == "unavailable"`、`reason == "converter_not_installed"`、`relative_path is None`、`finish_category == "unavailable"`。
- `files_under(derived) == []`，当前 token 的 `result` 路径断言不存在，`output_ref is None`。`read_original(...) == payload`。
- `finish_job_attempt` 使用 `category=rendered.finish_category` 且 `max_attempts=attempt.attempt_no`。返回值断言 `status == "failed"`、`last_error == "attempts_exhausted"`、`already_finished is False`。
- `fetch_job` 断言 `status == "failed"`、`output_ref is None`、`last_error == "attempts_exhausted"`、`lease_owner is None`。`files_under(derived)` 仍断言为 `[]`。原件字节仍断言等于 `payload`。

`test_non_retryable_preview_refusal_writes_nothing` 的两组参数是 `("image/png", None, 35, "pixel_limit")` 和 `("image/svg+xml", svg 字节, 64, "svg_rejected")`。`payload is None` 时原件是 6×6 PNG。每一组都断言：

- `generated is False`，`rendered.reason` 等于该组的 `pixel_limit` 或 `svg_rejected`，`relative_path is None`，`finish_category == "invalid_schema"`。
- 渲染后 `files_under(derived) == []`。
- `finish_job_attempt` 的 `max_attempts=8`。返回值断言 `status == "failed"`、`last_error == "invalid_schema"`。
- `fetch_job` 断言 `status == "failed"`、`output_ref is None`、`last_error == "invalid_schema"`。`files_under(derived)` 仍为 `[]`。`read_original(...) == body`。

## 阻塞计数

阻塞 0。

这次阅读里，`render_derivative_preview` 不打开 `write_uow`。这次运行里，渲染期间 `Session.__init__` 计数为 0，连接池借出数为 0。成功 PNG 发布后，`output_ref` 断言等于当前 fencing token 的 `jobs/<job_id>/<token>/result`。旧 token 的 `finalize_job` 抛出 `StaleLease` 之后，`output_ref` 仍断言等于新 token 路径。没有 Blender 的 GLB 断言 `generated is False`、`output_ref is None`、状态为 `failed`，派生目录文件列表为 `[]`，当前 token 的 result 路径不存在。按本次给定的阻塞含义，这四项在这次阅读和这次运行里没有出现。

真实模型调用和恰好一次投递不在这次断言里，不记成已关闭。

## 任务状态

T06 仍是 implementing，本文件不是完成声明。
