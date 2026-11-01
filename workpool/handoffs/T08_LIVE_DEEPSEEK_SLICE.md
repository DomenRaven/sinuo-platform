# T08 真实 DeepSeek 烟测入口

- 上游任务 / owner：T08 / 思诺实现代理
- 集成基线：本窗没有 Git commit。HEAD 是 `d7c8a22`。没有新迁移。
- 消费者：主控。本代理不发真实请求。T09 不接收新的确认接口。
- 契约 / schema 版本：没有新的 schema。`DeepSeekAdapter` 构造函数仍是 `client`、`model`、`api_key`、`timeout`。可选参数 `max_calls` 与 `max_tokens` 默认都是 `None`。`from_settings` 是新增类方法。
- 实现位置：`src/sinuo/agents/deepseek.py`、`tests/acceptance/test_live_deepseek.py`
- 权限动作：没有新动作。
- 事务与幂等边界：适配器不写数据库。`from_settings` 打开的客户端由调用方 `await adapter.client.aclose()`。本地上限用尽时在 `post` 之前拒绝，不发 HTTP。
- 成功响应 / 失败响应：受控测试仍传入 `httpx.AsyncClient`，主机仍由该测试断言为 `controlled.test`。上限用尽时 `DeepSeekCallError` 的 `category` 是 `local_call_limit`，`status_code` 是 `0`。烟测未运行。
- 最小调用示例或夹具：`DeepSeekAdapter.from_settings(settings, max_calls=1, max_tokens=32)`。烟测输入是合成 JSON，不是用户原文。密钥只放在 `Authorization` 头。
- 真实验证命令与结果：见下。
- 未验证项 / 已知限制：真实 DeepSeek 没有调用。`SINUO_MODEL_ENABLED` 不是 true，`SINUO_MODEL_NAME` 为空，`SINUO_DEEPSEEK_API_KEY` 未配置。模型名：未发请求，不记录实模型响应。T08 不标 done。T09、T10、T11 没做。
- 下游第一次应运行的命令：环境补齐前不要发请求。补齐后只由主控发一次：`.venv/Scripts/python.exe -m pytest -q tests/acceptance/test_live_deepseek.py -m live_model`。

下游确认：已在基线上执行受控 HTTP 与清环境烟测文件，结果为受控测试退出码 0、烟测文件退出码 0（1 passed，1 skipped）；接手范围为真实模型的那一次调用。本代理未向 `api.deepseek.com` 发请求。

## 适配器

`from_settings(settings, *, max_calls=None, max_tokens=None, timeout=None)` 在 `SINUO_MODEL_ENABLED` 为 true、模型名非空、密钥有值时，用 `SecretStr.get_secret_value()` 创建 `base_url=https://api.deepseek.com` 的客户端。请求路径仍是 `/chat/completions`。密钥不进入异常文本、日志或 `repr`。

默认构造函数不加上限，也不自己打开客户端，不读密钥文件。`max_calls=1` 时第一次 `execute` 计入调用，下一次在发 HTTP 之前拒绝。`max_tokens` 只在显式传入时写入 JSON；不传则请求体与原来相同。

## 测试

`tests/acceptance/test_live_deepseek.py` 整模块 `pytestmark = pytest.mark.live_model`。默认 addopts `-m not live_model` 不会跑它。

烟测用 `Settings()` 看环境。三项不齐就 `pytest.skip`，原因以「烟测未运行」开头，并点名缺项。密钥只写「已配置」或「未配置」。三项齐全才用合成内容发一次，`max_calls=1`，`max_tokens=32`，并断言调用次数是 1。响应有 `usage` 时，`total_tokens` 不得超过测试里写死的 256；没有 `usage` 则记录「usage 未返回」，不编造 token 数。`DeepSeekCallError` 直接失败并保留 `category`。

另一条测试用 `MockTransport`：环境不齐时不向 `api.deepseek.com` 发请求；配置齐全时 `from_settings` 的客户端主机是 `api.deepseek.com`，这条测试本身不发包。

## 命令

工作目录是仓库根。解释器 `.venv/Scripts/python.exe`。没有跑全库。没有跑会访问 `api.deepseek.com` 的命令。

```text
.venv/Scripts/python.exe -m pytest -q tests/acceptance/test_agent_runs.py::test_deepseek_adapter_controlled_http -o cache_dir=_施工文件/2026-11-01_t08trigger/99_临时文件/pytest-cache-b --basetemp=_施工文件/2026-11-01_t08trigger/99_临时文件/pytest-b
```

退出码 0。`1 passed, 1 warning in 0.42s`。警告是既有的 httpx 弃用，不是这次适配器引入的失败。

烟测文件在子进程里把 `SINUO_MODEL_ENABLED` 设为 `false`，把 `SINUO_MODEL_NAME` 和 `SINUO_DEEPSEEK_API_KEY` 设为空字符串，避免 `.env` 把空环境补成真实调用。然后：

```text
.venv/Scripts/python.exe -m pytest -q tests/acceptance/test_live_deepseek.py -m live_model -o cache_dir=_施工文件/2026-11-01_t08trigger/99_临时文件/pytest-cache-b-skip --basetemp=_施工文件/2026-11-01_t08trigger/99_临时文件/pytest-b-skip
```

退出码 0。`1 passed, 1 skipped`。跳过原因：`烟测未运行：SINUO_MODEL_ENABLED 不是 true；SINUO_MODEL_NAME 为空；SINUO_DEEPSEEK_API_KEY 未配置`。

默认收集（不加 `-m live_model`）同一文件：`no tests collected (2 deselected)`，退出码 5。这是 pytest 在全部被标记排除时的退出码，不是用例失败。

`ruff check src/sinuo/agents/deepseek.py tests/acceptance/test_live_deepseek.py` 通过。

## 密钥与烟测

用 `Settings()` 只判断三项是否有值，不打印密钥、`Authorization` 或 `SecretStr`。

- `SINUO_MODEL_ENABLED`：不是 true
- `SINUO_MODEL_NAME`：为空
- `SINUO_DEEPSEEK_API_KEY`：未配置

烟测未运行。模型名：未发请求，不记录实模型响应。T08 不标 done。
