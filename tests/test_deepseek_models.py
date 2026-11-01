"""Role routing stays on the adapter. No network."""

import asyncio
import json

import httpx
import pytest
from pydantic import SecretStr, ValidationError

from sinuo.agents.contracts import InputSnapshot, SpecialistTask
from sinuo.agents.deepseek import DeepSeekAdapter
from sinuo.config import Settings

_SNAPSHOT = InputSnapshot("synthetic-snapshot", "synthetic-project", "a" * 64, '{"synthetic":true}')


def test_from_settings_sends_flash_or_pro_by_role(monkeypatch):
    bodies: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content.decode("utf-8")))
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": '{"summary":"synthetic"}'}}]},
        )

    real_client = httpx.AsyncClient

    def guarded(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return real_client(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", guarded)
    settings = Settings(
        _env_file=None,
        model_enabled=True,
        model_name="deepseek-flash",
        model_name_reasoning="deepseek-v4-pro",
        deepseek_api_key=SecretStr("synthetic-routing-key"),
    )
    adapter = DeepSeekAdapter.from_settings(settings, max_calls=4, max_tokens=16)

    async def once() -> None:
        tasks = (
            SpecialistTask("synthetic-clarification", "clarification"),
            SpecialistTask("synthetic-evidence", "evidence"),
            SpecialistTask("synthetic-method", "method"),
            SpecialistTask("synthetic-critic", "critic"),
        )
        expected = {
            "clarification": "deepseek-flash",
            "evidence": "deepseek-flash",
            "method": "deepseek-v4-pro",
            "critic": "deepseek-v4-pro",
        }
        for task in tasks:
            await adapter.execute(task, _SNAPSHOT, ())
            assert adapter.model_for_role(task.role) == expected[task.role]
            assert bodies[-1]["model"] == expected[task.role]
            assert bodies[-1]["response_format"] == {"type": "json_object"}
            assert bodies[-1]["thinking"] == {"type": "disabled"}

    try:
        assert "synthetic-routing-key" not in repr(adapter)
        asyncio.run(once())
    finally:
        asyncio.run(adapter.client.aclose())


def test_direct_client_keeps_the_single_model():
    async def once() -> None:
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(
                    200,
                    json={"choices": [{"message": {"content": '{"summary":"synthetic"}'}}]},
                )
            ),
            base_url="http://controlled.test",
        ) as client:
            adapter = DeepSeekAdapter(
                client=client,
                model="synthetic-model",
                api_key="synthetic-routing-key",
                timeout=2,
            )
            await adapter.execute(SpecialistTask("synthetic-evidence", "evidence"), _SNAPSHOT, ())
            assert adapter.model_for_role("evidence") == "synthetic-model"
            assert adapter.model_for_role("critic") == "synthetic-model"

    asyncio.run(once())


def test_settings_reject_an_enabled_model_without_a_key():
    with pytest.raises(ValidationError, match="Enabled model requires"):
        Settings(
            _env_file=None,
            model_enabled=True,
            model_name="deepseek-flash",
            deepseek_api_key=None,
        )
