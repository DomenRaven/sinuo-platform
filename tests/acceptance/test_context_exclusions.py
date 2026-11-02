"""StartRunBody exclusions skip theory and method lookups for one run.

The exclusion set is frozen on the ContextSnapshot. It only affects the run
that carries it; a later run without exclusions reads theory and methods again.
"""

import hashlib
import json
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from sinuo.agents.contracts import SpecialistResult
from sinuo.application.agents.execute import (
    AgentRunRequest,
    RunTaskSpec,
    finish_agent_run,
    open_agent_run,
)
from sinuo.domain.auth.principal import Principal
from sinuo.infrastructure.database import write_uow
from sinuo.infrastructure.domain_models import (
    Account,
    ContextSnapshot,
    Membership,
    ObjectGrant,
    Project,
)

NOW = 1_762_000_000.0
PASSWORD = "$argon2id$synthetic-context-exclusions"
READER = "88888888-8888-4888-8888-888888888888"
PROJECT = "context-exclusions"
PHRASE = "按压按钮需要明确的行程反馈"
BUTTON = "按压按钮需要明确的行程反馈，用户才能确认操作已经生效。"
PAGE_BUTTON = "page-button"
CHUNK_BUTTON = "chunk-button"
EMPTY_THEORY = {"hits": [], "page_ids": [], "summary": ""}


class _Capture:
    def __init__(self) -> None:
        self.tools: dict[str, dict[str, object]] = {}

    async def execute(self, task, snapshot, upstream):
        del upstream
        body = json.loads(snapshot.context_json)
        tools = body["tool_results"]
        assert isinstance(tools, dict)
        self.tools[task.role] = tools
        return SpecialistResult(
            task.task_id,
            "completed",
            json.dumps({"summary": "待确认"}, ensure_ascii=False),
            role=task.role,
        )


def _chunk(page_id: str, chunk_id: str, text: str) -> dict[str, str]:
    return {
        "library_key": "course",
        "course": "design-psychology",
        "page_id": page_id,
        "chunk_id": chunk_id,
        "text": text,
        "text_hash": hashlib.sha256(text.encode("utf-8")).hexdigest(),
        "source_path": f"mem://synthetic/{chunk_id}.md",
    }


def _write_corpus(directory: Path) -> None:
    directory.mkdir()
    chunks = [_chunk(PAGE_BUTTON, CHUNK_BUTTON, BUTTON)]
    graph = {
        "pages": {PAGE_BUTTON: {"title": PHRASE, "summary": "合成短摘要只标起点"}},
        "links": [],
    }
    (directory / "chunks.json").write_text(
        json.dumps(chunks, ensure_ascii=False),
        encoding="utf-8",
    )
    (directory / "graph.json").write_text(
        json.dumps(graph, ensure_ascii=False),
        encoding="utf-8",
    )


def _seed(engine) -> None:
    with write_uow(engine) as session:
        session.add(
            Project(
                id=PROJECT,
                title=PROJECT,
                brief="synthetic",
                template_key=None,
                revision=1,
                access_epoch=1,
                created_at=NOW,
            )
        )
        session.add(
            Account(
                id=READER,
                login_name="context-exclusions-reader",
                display_name="context-exclusions-reader",
                password_hash=PASSWORD,
                status="active",
                auth_epoch=1,
                can_create_project=0,
                created_at=NOW,
                disabled_at=None,
            )
        )
        session.flush()
        session.add(
            Membership(
                id="member-context-exclusions",
                project_id=PROJECT,
                account_id=READER,
                status="active",
                role_label=None,
                created_at=NOW,
                revoked_at=None,
            )
        )
        for grant_id, action in (
            ("grant-context-project", "project.read"),
            ("grant-context-run", "run.create"),
        ):
            session.add(
                ObjectGrant(
                    id=grant_id,
                    project_id=PROJECT,
                    account_id=READER,
                    action=action,
                    scope_type="project",
                    status="active",
                    created_at=NOW,
                    revoked_at=None,
                    created_by_account_id=READER,
                    stage_id=None,
                    page_id=None,
                    page_version_id=None,
                    artifact_id=None,
                    artifact_version_id=None,
                    evidence_anchor_id=None,
                )
            )


def _request(suffix: str, now: float, exclusions: tuple[str, ...]) -> AgentRunRequest:
    return AgentRunRequest(
        intent=f"读集排除{suffix}",
        tasks=(
            RunTaskSpec(f"ev-{suffix}", "evidence"),
            RunTaskSpec(f"me-{suffix}", "method"),
        ),
        query=PHRASE,
        question=PHRASE,
        situation="合成情境",
        constraints={"consent": True},
        page_version_id=None,
        now=now,
        exclusions=exclusions,
    )


def _run(engine, suffix: str, now: float, exclusions: tuple[str, ...]):
    principal = Principal(READER, "context-exclusions-session", 1)
    opened = open_agent_run(engine, principal, PROJECT, _request(suffix, now, exclusions))
    capture = _Capture()
    finished = finish_agent_run(engine, principal, opened.run_id, capture, now=now)
    return finished, capture.tools, opened.snapshot_id


def _stored_exclusions(engine, snapshot_id: str) -> list[str]:
    with Session(engine) as session:
        raw = session.scalar(
            select(ContextSnapshot.exclusions_json).where(ContextSnapshot.id == snapshot_id)
        )
    assert isinstance(raw, str)
    parsed = json.loads(raw)
    assert isinstance(parsed, list)
    return parsed


def test_theory_exclusion_skips_theory_but_not_methods(engine, monkeypatch, tmp_path):
    corpus = tmp_path / "theory"
    _write_corpus(corpus)
    monkeypatch.setenv("SINUO_THEORY_ROOT", str(corpus))
    _seed(engine)
    finished, tools, run_id = _run(engine, "theory", NOW, ("theory",))
    assert finished.execution_status == "completed"
    assert set(tools) == {"evidence", "method"}
    # 排除 theory：两个职责的 theory 都应是空，且不命中
    for role in ("evidence", "method"):
        assert tools[role]["theory"] == EMPTY_THEORY
    # 排除 theory 不影响 method 角色查方法卡（no_match，因为方法库为空）
    assert tools["method"]["lookup"]["status"] == "no_match"
    assert _stored_exclusions(engine, run_id) == ["theory"]


def test_methods_exclusion_skips_methods_but_not_theory(engine, monkeypatch, tmp_path):
    corpus = tmp_path / "theory"
    _write_corpus(corpus)
    monkeypatch.setenv("SINUO_THEORY_ROOT", str(corpus))
    _seed(engine)
    finished, tools, run_id = _run(engine, "methods", NOW + 1, ("methods",))
    assert finished.execution_status == "completed"
    assert set(tools) == {"evidence", "method"}
    # 排除 methods：method 角色跳过方法库
    assert tools["method"]["lookup"]["status"] == "skipped"
    assert tools["method"]["lookup"]["cards"] == []
    # theory 仍命中（未被排除）
    assert tools["evidence"]["theory"]["hits"] != []
    assert _stored_exclusions(engine, run_id) == ["methods"]


def test_exclusions_are_per_run_not_persistent(engine, monkeypatch, tmp_path):
    corpus = tmp_path / "theory"
    _write_corpus(corpus)
    monkeypatch.setenv("SINUO_THEORY_ROOT", str(corpus))
    _seed(engine)
    # 第一次带排除，第二次不带：第二次恢复读 theory/methods
    first, _, first_run = _run(engine, "on", NOW + 2, ("theory", "methods"))
    second, tools2, second_run = _run(engine, "off", NOW + 3, ())
    assert first.execution_status == "completed"
    assert second.execution_status == "completed"
    assert _stored_exclusions(engine, first_run) == ["methods", "theory"]
    assert _stored_exclusions(engine, second_run) == []
    # 第二次不带排除，theory 恢复命中
    assert tools2["evidence"]["theory"]["hits"] != []


def test_unknown_exclusions_are_ignored(engine, monkeypatch, tmp_path):
    corpus = tmp_path / "theory"
    _write_corpus(corpus)
    monkeypatch.setenv("SINUO_THEORY_ROOT", str(corpus))
    _seed(engine)
    finished, tools, run_id = _run(engine, "unknown", NOW + 4, ("bogus", "theory"))
    assert finished.execution_status == "completed"
    # 未知键被忽略，只保留 theory
    assert _stored_exclusions(engine, run_id) == ["theory"]
    assert tools["evidence"]["theory"] == EMPTY_THEORY


def _http_settings(database_url):
    from sinuo.config import Settings

    return Settings(
        _env_file=None,
        database_url=database_url,
        argon2_memory_kib=19456,
        argon2_parallelism=1,
        argon2_time_cost=3,
    )


def _http_world(engine, settings):
    from tests.acceptance.identity_fixtures import (
        LEAD_LOGIN,
        LEAD_PASSWORD,
        seed_identity_world,
    )

    seed_identity_world(engine, settings)
    return LEAD_LOGIN, LEAD_PASSWORD


def test_http_exclusions_are_validated_and_stored(engine, database_url):
    from fastapi.testclient import TestClient

    from sinuo.agents.fake import FakeModelProvider
    from sinuo.api.app import create_app
    from sinuo.domain.public_contract import CSRF_HEADER
    from tests.acceptance.identity_fixtures import (
        PAGE_A,
        PROJECT_A,
        identity_settings,
        seed_identity_world,
    )

    settings = identity_settings(database_url)
    seed_identity_world(engine, settings)
    app = create_app(settings)
    app.state.agent_provider = FakeModelProvider()
    path = f"/api/v1/projects/{PROJECT_A}/agent-runs"
    origin = "http://127.0.0.1:8000"
    with TestClient(app) as client:
        login = client.post(
            "/api/v1/session",
            headers={"Origin": origin},
            json={"login_name": "Synthetic-Lead", "password": "synthetic-pass-lead"},
        )
        assert login.status_code == 204
        csrf = client.get("/api/v1/session").json()["data"]["csrf_token"]
        from sqlalchemy.orm import Session

        from sinuo.infrastructure.domain_models import Page

        with Session(engine) as session:
            page = session.get(Page, PAGE_A)
        assert page is not None and isinstance(page.head_version_id, str)
        page_version_id = page.head_version_id
        # 非法排除值 → 422
        bad = client.post(
            path,
            headers={"Origin": origin, CSRF_HEADER: csrf, "Idempotency-Key": "ctx-bad"},
            json={
                "intent": "问资料",
                "input_mode": "confirmed",
                "page_version_id": page_version_id,
                "exclusions": ["bogus"],
            },
        )
        assert bad.status_code == 422
        # 合法排除值 → 202，且落库
        ok = client.post(
            path,
            headers={"Origin": origin, CSRF_HEADER: csrf, "Idempotency-Key": "ctx-ok"},
            json={
                "intent": "问资料",
                "input_mode": "confirmed",
                "page_version_id": page_version_id,
                "exclusions": ["theory"],
            },
        )
        assert ok.status_code == 202, ok.text
        snapshot_id = ok.json()["data"]["snapshot_id"]
        from sqlalchemy import select

        from sinuo.infrastructure.domain_models import ContextSnapshot

        with Session(engine) as session:
            raw = session.scalar(
                select(ContextSnapshot.exclusions_json).where(
                    ContextSnapshot.id == snapshot_id
                )
            )
        assert json.loads(raw) == ["theory"]
        # 缺省（不带 exclusions）→ 202，落库为 []
        default = client.post(
            path,
            headers={"Origin": origin, CSRF_HEADER: csrf, "Idempotency-Key": "ctx-default"},
            json={
                "intent": "问资料",
                "input_mode": "confirmed",
                "page_version_id": page_version_id,
            },
        )
        assert default.status_code == 202
        snapshot_id = default.json()["data"]["snapshot_id"]
        with Session(engine) as session:
            raw = session.scalar(
                select(ContextSnapshot.exclusions_json).where(
                    ContextSnapshot.id == snapshot_id
                )
            )
        assert json.loads(raw) == []
