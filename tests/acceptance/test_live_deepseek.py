"""Real DeepSeek smoke. The module marker keeps it out of the default pytest run."""

import asyncio
import json

import httpx
import pytest
from pydantic import SecretStr, ValidationError
from pydantic_settings import BaseSettings, SettingsConfigDict

from sinuo.agents.contracts import InputSnapshot, SpecialistTask
from sinuo.agents.deepseek import DeepSeekAdapter, DeepSeekCallError
from sinuo.config import Settings

pytestmark = pytest.mark.live_model

_TOKEN_BUDGET = 256
_MAX_TOKENS = 32
_OUTPUT_CHAR_BUDGET = 512
_TASKS = (
    SpecialistTask("synthetic-live-flash", "evidence"),
    SpecialistTask("synthetic-live-pro", "critic"),
)
_SNAPSHOT = InputSnapshot(
    "synthetic-live-smoke",
    "synthetic-project",
    "c" * 64,
    '{"synthetic":true,"expect":"one-json-object"}',
)
_MODEL_CONFIG_ERROR = "Enabled model requires server-side model name and API key"


class _LiveEnv(BaseSettings):
    """Same SINUO_ sources as Settings, without the enabled-model pair check."""

    model_config = SettingsConfigDict(
        env_prefix="SINUO_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        hide_input_in_errors=True,
    )

    model_enabled: bool = False
    model_name: str = ""
    deepseek_api_key: SecretStr | None = None


def _key_configured(key: object) -> bool:
    get_secret = getattr(key, "get_secret_value", None)
    if not callable(get_secret):
        return False
    raw = get_secret()
    configured = isinstance(raw, str) and raw.strip() != ""
    del raw
    return configured


def _gaps(source: Settings | _LiveEnv) -> list[str]:
    gaps: list[str] = []
    if source.model_enabled is not True:
        gaps.append("SINUO_MODEL_ENABLED 不是 true")
    if not source.model_name.strip():
        gaps.append("SINUO_MODEL_NAME 为空")
    if not _key_configured(source.deepseek_api_key):
        gaps.append("SINUO_DEEPSEEK_API_KEY 未配置")
    return gaps


def _skip_reason(gaps: list[str]) -> str:
    parts = list(gaps)
    if not any(part.startswith("SINUO_DEEPSEEK_API_KEY") for part in parts):
        parts.append("SINUO_DEEPSEEK_API_KEY 已配置")
    return "烟测未运行：" + "；".join(parts)


def _ready_settings() -> Settings:
    try:
        settings = Settings()
    except ValidationError as exc:
        if _MODEL_CONFIG_ERROR not in str(exc):
            raise
        pytest.skip(_skip_reason(_gaps(_LiveEnv())))
    gaps = _gaps(settings)
    if gaps:
        pytest.skip(_skip_reason(gaps))
    return settings


def _usage_record(usage: dict[str, object] | None) -> str:
    if usage is None:
        return "usage 未返回"
    total = usage.get("total_tokens")
    if isinstance(total, bool) or not isinstance(total, int):
        raise AssertionError("usage 已返回但没有整数 total_tokens")
    if total > _TOKEN_BUDGET:
        raise AssertionError(f"total_tokens {total} exceeds local budget {_TOKEN_BUDGET}")
    return "用量落在本地预算内"


def test_live_deepseek_one_synthetic_call():
    settings = _ready_settings()

    async def once() -> None:
        adapter = DeepSeekAdapter.from_settings(
            settings,
            max_calls=len(_TASKS),
            max_tokens=_MAX_TOKENS,
        )
        try:
            records = []
            for task in _TASKS:
                result = await adapter.execute(task, _SNAPSHOT, ())
                parsed = json.loads(result.output_json)
                assert isinstance(parsed, dict)
                assert result.role == task.role
                assert len(result.output_json) <= _OUTPUT_CHAR_BUDGET
                records.append(
                    {
                        "role": task.role,
                        "model": adapter.model_for_role(task.role),
                        "ok": True,
                        "usage": _usage_record(adapter.last_usage),
                    }
                )
            assert adapter.calls == len(_TASKS)
            print(json.dumps({"calls": records}, ensure_ascii=False), flush=True)
        finally:
            await adapter.client.aclose()

    asyncio.run(once())


def test_from_settings_does_not_send_when_unconfigured_and_sets_deepseek_host(monkeypatch):
    opened: list[str] = []
    seen: list[str] = []

    def forbid(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.host)
        return httpx.Response(500, json={"error": {"message": "synthetic"}})

    real_client = httpx.AsyncClient

    def guarded_client(*args, **kwargs):
        if "transport" not in kwargs:
            opened.append(str(kwargs.get("base_url", "")))
            kwargs["transport"] = httpx.MockTransport(forbid)
        return real_client(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", guarded_client)

    disabled = Settings(
        _env_file=None,
        model_enabled=False,
        model_name="",
        deepseek_api_key=None,
    )
    with pytest.raises(ValueError, match="SINUO_MODEL_ENABLED"):
        DeepSeekAdapter.from_settings(disabled)
    with pytest.raises(ValidationError):
        Settings(
            _env_file=None,
            model_enabled=True,
            model_name="",
            deepseek_api_key=None,
        )
    blank_name = Settings.model_construct(
        model_enabled=True,
        model_name=" ",
        deepseek_api_key=SecretStr("synthetic-guard-key"),
    )
    with pytest.raises(ValueError, match="SINUO_MODEL_NAME") as blank_name_error:
        DeepSeekAdapter.from_settings(blank_name)
    assert "synthetic-guard-key" not in str(blank_name_error.value)
    missing_key = Settings.model_construct(
        model_enabled=True,
        model_name="synthetic-model",
        deepseek_api_key=None,
    )
    with pytest.raises(ValueError, match="SINUO_DEEPSEEK_API_KEY"):
        DeepSeekAdapter.from_settings(missing_key)
    empty_key = Settings.model_construct(
        model_enabled=True,
        model_name="synthetic-model",
        deepseek_api_key=SecretStr("  "),
    )
    with pytest.raises(ValueError, match="SINUO_DEEPSEEK_API_KEY"):
        DeepSeekAdapter.from_settings(empty_key)
    assert opened == []
    assert seen == []

    complete = Settings(
        _env_file=None,
        model_enabled=True,
        model_name="synthetic-model",
        deepseek_api_key=SecretStr("synthetic-guard-key"),
    )
    adapter = DeepSeekAdapter.from_settings(complete, max_calls=1, max_tokens=_MAX_TOKENS)
    try:
        assert opened == ["https://api.deepseek.com"]
        assert adapter.client.base_url.host == "api.deepseek.com"
        assert adapter.client.base_url.scheme == "https"
        assert "synthetic-guard-key" not in repr(adapter)
        assert "Authorization" not in repr(adapter)
        assert "Bearer" not in repr(adapter)
        adapter.calls = 1
        with pytest.raises(DeepSeekCallError) as caught:
            asyncio.run(
                adapter.execute(SpecialistTask("synthetic-live-smoke", "method"), _SNAPSHOT, ())
            )
        assert caught.value.category == "local_call_limit"
        assert caught.value.status_code == 0
        assert adapter.calls == 1
        assert seen == []
    finally:
        asyncio.run(adapter.client.aclose())

    async def bodies() -> None:
        async with real_client(
            transport=httpx.MockTransport(forbid),
            base_url="http://controlled.test",
        ) as client:
            plain = DeepSeekAdapter(
                client=client,
                model="synthetic-model",
                api_key="synthetic-guard-key",
                timeout=2,
            )
            limited = DeepSeekAdapter(
                client=client,
                model="synthetic-model",
                api_key="synthetic-guard-key",
                timeout=2,
                max_calls=1,
                max_tokens=_MAX_TOKENS,
            )
            task = SpecialistTask("synthetic-live-smoke", "method")
            plain_body = plain._request_json(task, _SNAPSHOT)
            limited_body = limited._request_json(task, _SNAPSHOT)
            assert plain._max_calls is None
            assert "max_tokens" not in plain_body
            assert set(plain_body) == {"model", "messages", "response_format", "thinking"}
            assert limited._max_calls == 1
            assert limited_body["max_tokens"] == _MAX_TOKENS
            assert set(limited_body) == {
                "model",
                "messages",
                "max_tokens",
                "response_format",
                "thinking",
            }
            assert plain_body["messages"] == limited_body["messages"]
            assert "synthetic-guard-key" not in repr(plain)

    asyncio.run(bodies())
    assert seen == []
