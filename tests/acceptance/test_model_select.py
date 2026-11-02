"""Which provider a started run uses. No network and no live model."""

import json

from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from sinuo.agents.fake import FakeModelProvider
from sinuo.api.app import create_app
from sinuo.domain.public_contract import CSRF_HEADER
from sinuo.infrastructure.domain_models import AgentResult, AgentRun, Candidate, Page, RunTask
from tests.acceptance.identity_fixtures import (
    LEAD_LOGIN,
    LEAD_PASSWORD,
    PAGE_A,
    PROJECT_A,
    identity_settings,
    seed_identity_world,
)

ORIGIN = "http://127.0.0.1:8000"
PATH = f"/api/v1/projects/{PROJECT_A}/agent-runs"
INTENT = "问资料"
SYNTHETIC_KEY = "sk-synthetic-not-sent"
DISABLED_GAP = "必需依赖失败，结果只说明缺口，不发布阶段推进"


class _Closer:
    def __init__(self) -> None:
        self.closed = False

    async def aclose(self) -> None:
        self.closed = True


class _NetworkGuard:
    def __init__(self) -> None:
        self.calls = 0

    def AsyncClient(self, *args, **kwargs):
        del args, kwargs
        self.calls += 1
        raise AssertionError("live model")

    def __getattr__(self, name: str):
        del name
        raise AssertionError("live model")


def _refuse(*_args, **_kwargs):
    raise AssertionError("live model")


def _install(monkeypatch) -> tuple[list[tuple], list[FakeModelProvider], _NetworkGuard]:
    seen: list[tuple] = []
    created: list[FakeModelProvider] = []
    guard = _NetworkGuard()
    monkeypatch.setattr("sinuo.agents.deepseek.httpx", guard)
    monkeypatch.setattr("sinuo.agents.deepseek.DeepSeekAdapter.__init__", _refuse)
    monkeypatch.setattr("sinuo.agents.deepseek.DeepSeekAdapter.execute", _refuse)

    def from_settings(*args, **kwargs):
        seen.append((args, kwargs))
        provider = FakeModelProvider()
        provider.client = _Closer()
        created.append(provider)
        return provider

    monkeypatch.setattr("sinuo.agents.deepseek.DeepSeekAdapter.from_settings", from_settings)
    return seen, created, guard


def _enabled(database_url: str):
    return identity_settings(
        database_url,
        model_enabled=True,
        model_name="deepseek-flash",
        model_name_reasoning="deepseek-v4-pro",
        deepseek_api_key=SYNTHETIC_KEY,
    )


def _prepare(engine, settings, provider: FakeModelProvider | None = None):
    seed_identity_world(engine, settings)
    with Session(engine) as session:
        page = session.get(Page, PAGE_A)
    assert page is not None and isinstance(page.head_version_id, str)
    app = create_app(settings)
    if provider is not None:
        app.state.agent_provider = provider
    return app, page.head_version_id


def _ask(app, page_version_id: str, key: str):
    with TestClient(app) as client:
        signed = client.post(
            "/api/v1/session",
            headers={"Origin": ORIGIN},
            json={"login_name": LEAD_LOGIN, "password": LEAD_PASSWORD},
        )
        assert signed.status_code == 204
        token = client.get("/api/v1/session").json()["data"]["csrf_token"]
        assert isinstance(token, str) and token
        response = client.post(
            PATH,
            headers={"Origin": ORIGIN, CSRF_HEADER: token, "Idempotency-Key": key},
            json={
                "intent": INTENT,
                "input_mode": "confirmed",
                "page_version_id": page_version_id,
            },
        )
    redacted = response.text.replace(SYNTHETIC_KEY, "[redacted]")
    assert response.status_code == 202, redacted
    assert "sk-synthetic" not in response.text
    payload = response.json()
    assert payload["meta"]["idempotency_replayed"] is False
    assert set(payload["data"]) == {"run_id", "snapshot_id", "events_url"}
    return payload["data"]["run_id"]


def _stored(engine, run_id: str) -> dict:
    with Session(engine) as session:
        run = session.get(AgentRun, run_id)
        assert run is not None
        tasks = session.scalars(select(RunTask).where(RunTask.run_id == run_id)).all()
        results = session.scalars(select(AgentResult).where(AgentResult.run_id == run_id)).all()
        candidates = session.scalars(select(Candidate).where(Candidate.run_id == run_id)).all()
        budget = json.loads(run.budget_json)
    outputs = [json.loads(item.output_json) for item in results]
    return {
        "status": run.execution_status,
        "accepting": run.accepting_results,
        "roles": {task.role: task.status for task in tasks},
        "outputs": outputs,
        "candidates": len(candidates),
        "spent": budget["spent_model_calls"],
        "reserved": budget["reserved_model_calls"],
    }


def _assert_fake_evidence(stored: dict) -> None:
    assert stored["status"] == "completed"
    assert stored["accepting"] == 0
    assert stored["roles"] == {"evidence": "succeeded"}
    assert stored["candidates"] == 0
    assert stored["spent"] == 1
    assert stored["reserved"] == 0
    assert len(stored["outputs"]) == 1
    body = stored["outputs"][0]
    assert body["role"] == "evidence"
    assert body["gaps"] == []
    assert body["model"] == {"gaps": [], "summary": "待确认"}


def test_override_skips_from_settings(engine, database_url, monkeypatch):
    seen, created, guard = _install(monkeypatch)
    settings = _enabled(database_url)
    assert settings.model_enabled is True
    provider = FakeModelProvider()
    assert type(provider) is FakeModelProvider
    app, page_version_id = _prepare(engine, settings, provider)
    assert app.state.agent_provider is provider
    run_id = _ask(app, page_version_id, "synthetic-select-override")
    assert seen == []
    assert created == []
    assert guard.calls == 0
    _assert_fake_evidence(_stored(engine, run_id))


def test_enabled_model_calls_from_settings_once(engine, database_url, monkeypatch):
    seen, created, guard = _install(monkeypatch)
    settings = _enabled(database_url)
    app, page_version_id = _prepare(engine, settings)
    assert getattr(app.state, "agent_provider", None) is None
    run_id = _ask(app, page_version_id, "synthetic-select-enabled")
    assert len(seen) == 1
    args, kwargs = seen[0]
    assert kwargs == {}
    assert len(args) == 1
    assert args[0] is app.state.settings
    assert args[0].model_enabled is True
    assert len(created) == 1
    assert type(created[0]) is FakeModelProvider
    assert created[0].client.closed is True
    assert guard.calls == 0
    _assert_fake_evidence(_stored(engine, run_id))


def test_disabled_model_skips_from_settings(engine, database_url, monkeypatch):
    seen, created, guard = _install(monkeypatch)
    settings = identity_settings(database_url, model_enabled=False)
    assert settings.model_enabled is False
    app, page_version_id = _prepare(engine, settings)
    assert getattr(app.state, "agent_provider", None) is None
    run_id = _ask(app, page_version_id, "synthetic-select-disabled")
    assert seen == []
    assert created == []
    assert guard.calls == 0
    stored = _stored(engine, run_id)
    assert stored["status"] == "partial"
    assert stored["accepting"] == 0
    assert stored["roles"] == {"evidence": "failed"}
    assert stored["candidates"] == 0
    assert stored["spent"] == 1
    assert stored["reserved"] == 0
    assert len(stored["outputs"]) == 1
    body = stored["outputs"][0]
    assert body["role"] == "evidence"
    assert body["gaps"] == [DISABLED_GAP]
    assert body["model"] == {}
