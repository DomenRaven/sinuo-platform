"""Start one run over HTTP. The provider is FakeModelProvider, never DeepSeek."""

import json

from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from sinuo.agents.contracts import SpecialistResult
from sinuo.agents.fake import FakeModelProvider
from sinuo.api.app import create_app
from sinuo.domain.public_contract import CSRF_HEADER
from sinuo.infrastructure.domain_models import (
    AgentResult,
    AgentRun,
    Candidate,
    ContextSnapshot,
    Page,
    Project,
    RunTask,
    RunTaskDependency,
)
from tests.acceptance.identity_fixtures import (
    LEAD_LOGIN,
    LEAD_PASSWORD,
    OTHER_LOGIN,
    OTHER_PASSWORD,
    PAGE_A,
    PAGE_A_TITLE,
    PROJECT_A,
    PROJECT_A_TITLE,
    REVIEWER_LOGIN,
    REVIEWER_PASSWORD,
    TEAM_LOGIN,
    TEAM_PASSWORD,
    identity_settings,
    seed_identity_world,
)

ORIGIN = "http://127.0.0.1:8000"
PATH = f"/api/v1/projects/{PROJECT_A}/agent-runs"
ROUTE = "/api/v1/projects/{project_id}/agent-runs"
CASES = (
    ("问资料", ("evidence",)),
    ("问方法", ("method",)),
    ("问下一步", ("evidence", "critic")),
    ("简报变化", ("clarification",)),
    ("问文献", ("evidence",)),
    ("怎么访谈", ("method",)),
    ("下一步做什么", ("evidence", "critic")),
    ("任务书刚变化", ("clarification",)),
    ("检查进度", ("evidence", "critic")),
)


class RecordingFake(FakeModelProvider):
    """Count roles, then let FakeModelProvider echo the injected output."""

    def __init__(self) -> None:
        self.roles: list[str] = []

    async def execute(self, task, snapshot, upstream) -> SpecialistResult:
        self.roles.append(task.role)
        return await super().execute(task, snapshot, upstream)


def _refuse(*_args, **_kwargs):
    raise AssertionError("live model")


def _settings(database_url: str):
    return identity_settings(
        database_url,
        model_enabled=True,
        model_name="deepseek-chat",
        deepseek_api_key="sk-synthetic-not-sent",
    )


def _block_live_model(monkeypatch) -> None:
    monkeypatch.setattr("sinuo.agents.deepseek.DeepSeekAdapter.__init__", _refuse)
    monkeypatch.setattr("sinuo.agents.deepseek.DeepSeekAdapter.from_settings", _refuse)
    monkeypatch.setattr("sinuo.agents.deepseek.DeepSeekAdapter.execute", _refuse)


def _login(client: TestClient, login_name: str, password: str) -> str:
    response = client.post(
        "/api/v1/session",
        headers={"Origin": ORIGIN},
        json={"login_name": login_name, "password": password},
    )
    assert response.status_code == 204
    token = client.get("/api/v1/session").json()["data"]["csrf_token"]
    assert isinstance(token, str) and token
    return token


def _page_version(engine) -> str:
    with Session(engine) as session:
        page = session.get(Page, PAGE_A)
    assert page is not None and isinstance(page.head_version_id, str)
    return page.head_version_id


def _body(intent: str, page_version_id: str, **extra: object) -> dict:
    payload: dict[str, object] = {
        "intent": intent,
        "input_mode": "confirmed",
        "page_version_id": page_version_id,
    }
    payload.update(extra)
    return payload


def _post(client: TestClient, csrf: str, key: str, body: dict):
    return client.post(
        PATH,
        headers={"Origin": ORIGIN, CSRF_HEADER: csrf, "Idempotency-Key": key},
        json=body,
    )


def _run_count(engine) -> int:
    with Session(engine) as session:
        counted = session.scalar(select(func.count()).select_from(AgentRun))
    assert isinstance(counted, int)
    return counted


def _saved(engine, run_id: str) -> dict:
    with Session(engine) as session:
        run = session.get(AgentRun, run_id)
        assert run is not None
        tasks = session.scalars(select(RunTask).where(RunTask.run_id == run_id)).all()
        deps = session.execute(
            select(
                RunTaskDependency.task_id,
                RunTaskDependency.depends_on_task_id,
            ).where(RunTaskDependency.run_id == run_id)
        ).all()
        snapshot = session.get(ContextSnapshot, run.context_snapshot_id)
        assert snapshot is not None
        scope = json.loads(snapshot.authorization_scope_json)
        budget = json.loads(run.budget_json)
        candidates = session.scalars(select(Candidate).where(Candidate.run_id == run_id)).all()
        project = session.get(Project, PROJECT_A)
        assert project is not None
    return {
        "origin": run.origin,
        "preference_revision": run.preference_revision,
        "status": run.execution_status,
        "accepting": run.accepting_results,
        "roles": {task.role: task.id for task in tasks},
        "task_status": {task.role: task.status for task in tasks},
        "deps": [(row.task_id, row.depends_on_task_id) for row in deps],
        "query": scope["query"],
        "question": scope["question"],
        "page_version_id": scope["page_version_id"],
        "spent": budget["spent_model_calls"],
        "reserved": budget["reserved_model_calls"],
        "candidates": [(item.status, item.action) for item in candidates],
        "revision": project.revision,
    }


def test_questions_select_one_to_three_duties(engine, database_url, monkeypatch):
    _block_live_model(monkeypatch)
    settings = _settings(database_url)
    seed_identity_world(engine, settings)
    page_version_id = _page_version(engine)
    provider = RecordingFake()
    assert isinstance(provider, FakeModelProvider)
    app = create_app(settings)
    app.state.agent_provider = provider
    assert "post" in app.openapi()["paths"][ROUTE]
    with TestClient(app) as client:
        csrf = _login(client, LEAD_LOGIN, LEAD_PASSWORD)
        seen: list[str] = []
        first_key = "synthetic-start-0"
        first_body = _body(CASES[0][0], page_version_id)
        first_run = ""
        for index, (intent, roles) in enumerate(CASES):
            before = len(provider.roles)
            before_runs = _run_count(engine)
            body = _body(intent, page_version_id)
            response = _post(client, csrf, f"synthetic-start-{index}", body)
            assert response.status_code == 202, (intent, response.text)
            assert response.headers["cache-control"] == "no-store"
            payload = response.json()
            assert payload["meta"]["idempotency_replayed"] is False
            assert "project_revision" not in payload["meta"]
            assert set(payload["data"]) == {"run_id", "snapshot_id", "events_url"}
            assert "deepseek" not in response.text.lower()
            assert "sk-synthetic" not in response.text
            run_id = payload["data"]["run_id"]
            assert payload["data"]["events_url"] == f"/api/v1/agent-runs/{run_id}/events"
            saved = _saved(engine, run_id)
            assert saved["origin"] == "manual"
            assert saved["preference_revision"] is None
            assert saved["status"] == "completed"
            assert saved["accepting"] == 0
            assert tuple(provider.roles[before:]) == roles
            assert set(saved["roles"]) == set(roles)
            assert saved["task_status"] == {role: "succeeded" for role in roles}
            assert saved["spent"] == len(roles)
            assert saved["reserved"] == 0
            assert saved["page_version_id"] == page_version_id
            assert saved["revision"] == 1
            assert _run_count(engine) == before_runs + 1
            if "evidence" in roles:
                assert saved["query"] == intent
            else:
                assert saved["query"] == ""
            if "method" in roles:
                assert saved["question"] == intent
            else:
                assert saved["question"] == ""
            if "critic" in roles:
                evidence_id = saved["roles"]["evidence"]
                critic_id = saved["roles"]["critic"]
                assert saved["deps"] == [(critic_id, evidence_id)]
                assert saved["candidates"] == [("pending", "draft_decision")]
            else:
                assert saved["deps"] == []
                assert saved["candidates"] == []
            assert len(roles) <= 3
            seen.extend(roles)
            if index == 0:
                first_run = run_id
        assert provider.roles == seen
        assert first_run
        replayed = _post(client, csrf, first_key, first_body)
        assert replayed.status_code == 202
        assert replayed.json()["meta"]["idempotency_replayed"] is True
        assert replayed.json()["data"]["run_id"] == first_run
        assert provider.roles == seen
        assert _run_count(engine) == len(CASES)
        reused = _post(client, csrf, first_key, _body("问方法", page_version_id))
        assert reused.status_code == 409
        assert reused.json()["error"]["code"] == "IDEMPOTENCY_KEY_REUSED"
        mixed = _post(client, csrf, "synthetic-start-mixed", _body("问资料问方法", page_version_id))
        assert mixed.status_code == 422
        assert mixed.json()["error"]["code"] == "INVALID_SCHEMA"
        extra = _body(
            "问资料",
            page_version_id,
            roles=["clarification", "evidence", "method", "critic"],
        )
        rejected = _post(client, csrf, "synthetic-start-roles", extra)
        assert rejected.status_code == 422
        assert rejected.json()["error"]["code"] == "INVALID_SCHEMA"
        blank = _post(client, csrf, "synthetic-start-blank", _body("   ", page_version_id))
        assert blank.status_code == 422
        stale = _body("问资料", page_version_id, draft_content_revision=1)
        conflict = _post(client, csrf, "synthetic-start-draft", stale)
        assert conflict.status_code == 409
        assert conflict.json()["error"]["code"] == "DRAFT_REVISION_CONFLICT"
        assert provider.roles == seen
        assert _run_count(engine) == len(CASES)
        missing_key = client.post(
            PATH,
            headers={"Origin": ORIGIN, CSRF_HEADER: csrf},
            json=_body("问资料", page_version_id),
        )
        assert missing_key.status_code == 422
        assert _run_count(engine) == len(CASES)


def test_run_create_and_exact_fake_provider(engine, database_url, monkeypatch):
    _block_live_model(monkeypatch)
    settings = _settings(database_url)
    seed_identity_world(engine, settings)
    page_version_id = _page_version(engine)
    provider = FakeModelProvider()
    assert type(provider) is FakeModelProvider
    app = create_app(settings)
    app.state.agent_provider = provider
    body = _body("问下一步", page_version_id)
    with TestClient(app) as client:
        anonymous = client.post(
            PATH,
            headers={"Origin": ORIGIN, "Idempotency-Key": "synthetic-start-anon"},
            json=body,
        )
        assert anonymous.status_code == 401
        assert anonymous.headers["cache-control"] == "no-store"
        assert PROJECT_A_TITLE not in anonymous.text
    with TestClient(app) as client:
        csrf = _login(client, REVIEWER_LOGIN, REVIEWER_PASSWORD)
        denied = _post(client, csrf, "synthetic-start-reviewer", body)
        assert denied.status_code == 403
        assert denied.json()["error"]["code"] == "ACTION_FORBIDDEN"
        assert denied.json()["error"]["details"]["action"] == "run.create"
        assert PAGE_A_TITLE not in denied.text
    with TestClient(app) as client:
        csrf = _login(client, TEAM_LOGIN, TEAM_PASSWORD)
        denied = _post(client, csrf, "synthetic-start-team", body)
        assert denied.status_code == 403
        assert denied.json()["error"]["details"]["action"] == "run.create"
    with TestClient(app) as client:
        csrf = _login(client, OTHER_LOGIN, OTHER_PASSWORD)
        hidden = _post(client, csrf, "synthetic-start-other", body)
        assert hidden.status_code == 404
        assert hidden.json()["error"]["code"] == "NOT_FOUND_OR_HIDDEN"
        assert PROJECT_A_TITLE not in hidden.text
        assert PAGE_A_TITLE not in hidden.text
    assert _run_count(engine) == 0
    with TestClient(app) as client:
        csrf = _login(client, LEAD_LOGIN, LEAD_PASSWORD)
        response = _post(client, csrf, "synthetic-start-next", body)
        assert response.status_code == 202, response.text
        run_id = response.json()["data"]["run_id"]
        saved = _saved(engine, run_id)
        assert set(saved["roles"]) == {"evidence", "critic"}
        assert saved["task_status"] == {"evidence": "succeeded", "critic": "succeeded"}
        assert saved["spent"] == 2
        assert saved["deps"] == [(saved["roles"]["critic"], saved["roles"]["evidence"])]
        materials = _post(
            client,
            csrf,
            "synthetic-start-materials",
            _body("问资料", page_version_id),
        )
        assert materials.status_code == 202, materials.text
        material_id = materials.json()["data"]["run_id"]
        material = _saved(engine, material_id)
        assert set(material["roles"]) == {"evidence"}
        assert material["spent"] == 1
        assert material["task_status"] == {"evidence": "succeeded"}
    with Session(engine) as session:
        calls = session.scalars(select(AgentResult).where(AgentResult.run_id == run_id)).all()
    assert len(calls) == 2
    assert _run_count(engine) == 2
