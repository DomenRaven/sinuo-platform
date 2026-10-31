# T06 派生尝试文件复审

日期：2026-10-31。本次阅读范围是 `workpool/handoffs/T06_DERIVATIVE_ATTEMPT_CONTRACT.md`、`workpool/handoffs/T06_DERIVATIVE_ATTEMPT_SLICE.md`、`src/sinuo/workers/derivative_files.py`、`src/sinuo/workers/jobs.py` 里 `finalize_job` 的 WHERE 与 `_require_output_ref`，以及 `tests/acceptance/test_derivative_attempt.py`。下面只记录这次阅读和这次运行。

## 命令与退出码

先创建 `_施工文件/2026-10-31_grok-review-derivative/99_临时文件`，再从仓库根执行：

```text
.venv/Scripts/python.exe -m pytest -q tests/acceptance/test_derivative_attempt.py -o cache_dir=_施工文件/2026-10-31_grok-review-derivative/99_临时文件/pytest-cache --basetemp=_施工文件/2026-10-31_grok-review-derivative/99_临时文件/pytest
```

标准输出：`11 passed in 0.43s`。退出码 0。

## 这次读到的写文件与登记

`write_derivative_attempt` 不接收会话，函数体内也不调用 `write_uow`。它先经 `_attempt_relative` 得到 `jobs/<job_id>/<fencing_token>/result`，再把字节交给 `write_attempt_bytes`。`fencing_token` 的类型不是 `int` 或数值小于 1，或者 `job_id`、token 字符串过不了 `is_storage_segment`，就在调用 `write_attempt_bytes` 之前抛 `PathEscape`。`is_storage_segment` 要求整段匹配 `^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$`，并且字符串里不含 `..`。

`publish_derivative_attempt` 把同一条相对路径作为 `output_ref` 传给现有的 `finalize_job`。它使用调用方传入的会话，自己不打开 `write_uow`。

这次读到的 `finalize_job` 更新语句是 `.where(*_live(job_id, owner, token, input_hash, now))`。`_live` 的条件是 `Job.id == job_id`、`Job.status == "running"`、`Job.lease_owner == owner`、`Job.fencing_token == fencing_token`、`Job.input_hash == input_hash`、`Job.lease_until >= now`。`_require_output_ref` 在这条更新之前做格式检查：不是字符串、长度大于 200、或含 `..` 就抛 `ValueError`；整段匹配 `_LABEL` 或 `_ATTEMPT_REF` 才把原字符串返回。`_ATTEMPT_REF` 读到的是 `jobs/[A-Za-z0-9][A-Za-z0-9._-]{0,63}/[1-9][0-9]{0,63}/result`。这处格式检查不在上面的 WHERE 里。

`write_attempt_bytes` 所在模块写明不打开数据库事务。相对路径先交给 `resolve_contained`；字符串里含 `..` 时，在创建目录和写入字节之前抛 `PathEscape`。

## 测试锁住的文件与发布行为

`test_crash_before_finalize_keeps_file_and_same_token_publishes_once` 把下面这些结果钉在断言上。`claim` 返回之后才调用 `write_derivative_attempt`，再之后才 `publish`。

- 返回值断言 `relative == f"jobs/{JOB_ID}/{attempt.fencing_token}/result"`。
- `root/jobs/<JOB_ID>/<token>/result` 的 `read_bytes()` 断言等于 `FIRST`，即 `b"attempt-before-receipt"`。
- 同一次写入之后、发布之前，`fetch_job` 断言 `status == "running"`、`output_ref is None`、`fencing_token == attempt.fencing_token`、`lease_owner == "worker-1"`。
- `publish` 的返回值断言 `output_ref == relative`、`fencing_token == attempt.fencing_token`。随后 `fetch_job` 断言 `status == "succeeded"`、`output_ref == relative`、`lease_owner is None`。同一文件的 `read_bytes()` 仍断言等于 `FIRST`。
- 同一 owner、同一 token 再 `publish` 一次，断言抛出 `StaleLease`。之后断言 `output_ref == relative`、`fencing_token == attempt.fencing_token`，文件字节仍断言等于 `FIRST`。

`test_old_token_cannot_replace_the_new_attempt_ref` 把下面这些结果钉在断言上。

- 旧 token 写入后，旧文件 `read_bytes()` 断言等于 `FIRST`，`fetch_job().output_ref is None`。
- 把 `lease_until` 写成 `1.0` 之后，另一个 owner 认领。断言 `second.fencing_token != first.fencing_token`。新返回值断言等于 `f"jobs/{JOB_ID}/{second.fencing_token}/result"`。新文件 `read_bytes()` 断言等于 `SECOND`，即 `b"attempt-after-takeover"`。
- 新 token 的 `publish` 返回值断言 `output_ref == new_relative`。
- 旧 owner、旧 token 的 `publish` 断言抛出 `StaleLease`。
- 随后 `fetch_job` 断言 `status == "succeeded"`、`output_ref == new_relative`、`output_ref != old_relative`、`fencing_token == second.fencing_token`。旧文件字节仍断言等于 `FIRST`，新文件字节仍断言等于 `SECOND`。

`test_escaping_attempt_path_writes_nothing` 的八组参数是 `("..", 1)`、`("../outside", 1)`、`("job/../../outside", 1)`、`("..\\outside", 1)`、`("a\\..\\b", 1)`、`("job-ok", 0)`、`("job-ok", -1)`、`("job-ok", True)`。每一组都断言 `write_derivative_attempt` 抛出 `PathEscape`，并且 `list(root.iterdir()) == []`，`tmp_path` 下 `is_file()` 的路径列表是 `[]`。

`test_symlink_attempt_directory_writes_nothing` 在 `root/jobs` 指向目录外的 `outside` 时，断言 `write_derivative_attempt(root, "job-ok", 1, b"escape-bytes")` 抛出 `PathEscape`，`(outside / "job-ok").exists()` 为假，并且 `outside` 下没有文件。这次运行的输出是 `11 passed`，没有 skipped，所以这条断言被执行了。

## 阻塞计数

阻塞 0。

这次阅读里，写字节的函数不接收会话，也不打开 `write_uow`。这次运行里，文件在 `output_ref is None` 且 `status == "running"` 时已经能读到 `FIRST`。旧 token 的 `publish` 抛出 `StaleLease` 之后，`output_ref` 仍断言等于新路径，并且断言不等于旧路径。含 `..` 的参数都抛出 `PathEscape`，调用方根目录下没有新建条目，也没有文件。按本次给定的阻塞含义，这三项在这次阅读和这次运行里没有出现。

真实模型调用和恰好一次投递不在这次断言里，不记成已关闭。

## 任务状态

T06 仍是 implementing，本文件不是完成声明。
