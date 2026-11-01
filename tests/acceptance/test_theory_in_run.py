"""Persistent evidence and method runs read a caller-supplied course index.

The index is three synthetic chunks and three synthetic pages. No course disk.
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
    AgentResult,
    Membership,
    ObjectGrant,
    Project,
    RunTask,
)

NOW = 1_762_000_000.0
PASSWORD = "$argon2id$synthetic-theory-run"
READER = "79797979-7979-4797-8797-797979797979"
PROJECT = "theory-run-alpha"
PHRASE = "按压按钮需要明确的行程反馈"
BUTTON = "按压按钮需要明确的行程反馈，用户才能确认操作已经生效。"
BUTTON_AGAIN = "按压按钮需要明确的行程反馈，并且确认操作已经生效。"
NIGHT = "夜间阅读界面宜降低对比并避免纯白大面积，以减轻视觉疲劳。"
PAGE_BUTTON = "page-button"
PAGE_FOLLOW = "page-button-follow"
PAGE_FAR = "page-far"
CHUNK_BUTTON = "chunk-button"
CHUNK_FOLLOW = "chunk-button-again"
CHUNK_NIGHT = "chunk-night"
EMPTY_THEORY = {"hits": [], "page_ids": [], "summary": ""}
SOURCE = (
    Path(__file__).resolve().parents[2]
    / "src"
    / "sinuo"
    / "application"
    / "agents"
    / "execute.py"
)


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
    chunks = [
        _chunk(PAGE_BUTTON, CHUNK_BUTTON, BUTTON),
        _chunk(PAGE_FOLLOW, CHUNK_FOLLOW, BUTTON_AGAIN),
        _chunk(PAGE_FAR, CHUNK_NIGHT, NIGHT),
    ]
    graph = {
        "pages": {
            PAGE_BUTTON: {"title": PHRASE, "summary": "合成短摘要只标起点"},
            PAGE_FOLLOW: {"title": PHRASE, "summary": "另一块行程反馈页"},
            PAGE_FAR: {"title": "过远页", "summary": "这一页停在两跳之外"},
        },
        "links": [
            {
                "source_page_id": PAGE_BUTTON,
                "target_page_id": PAGE_FOLLOW,
                "kind": "supports",
            },
            {
                "source_page_id": PAGE_FOLLOW,
                "target_page_id": PAGE_BUTTON,
                "kind": "supports",
            },
        ],
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
                login_name="theory-run-reader",
                display_name="theory-run-reader",
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
                id="member-theory-run",
                project_id=PROJECT,
                account_id=READER,
                status="active",
                role_label=None,
                created_at=NOW,
                revoked_at=None,
            )
        )
        for grant_id, action in (
            ("grant-theory-project", "project.read"),
            ("grant-theory-run", "run.create"),
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


def _request(suffix: str, now: float) -> AgentRunRequest:
    return AgentRunRequest(
        intent=f"理论召回{suffix}",
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
    )


def _run(engine, suffix: str, now: float) -> tuple[object, dict[str, dict[str, object]]]:
    principal = Principal(READER, "theory-run-session", 1)
    opened = open_agent_run(engine, principal, PROJECT, _request(suffix, now))
    capture = _Capture()
    finished = finish_agent_run(engine, principal, opened.run_id, capture, now=now)
    return finished, capture.tools


def _stored_theory(engine, run_id: str, role: str) -> dict[str, object]:
    with Session(engine) as session:
        task_id = session.scalar(
            select(RunTask.id).where(RunTask.run_id == run_id, RunTask.role == role)
        )
        raw = session.scalar(
            select(AgentResult.output_json).where(
                AgentResult.run_id == run_id,
                AgentResult.task_id == task_id,
                AgentResult.disposition == "published",
            )
        )
    assert isinstance(raw, str)
    parsed = json.loads(raw)
    theory = parsed["theory"]
    assert isinstance(theory, dict)
    return theory


def _assert_hit(theory: dict[str, object]) -> None:
    hits = theory["hits"]
    assert isinstance(hits, list)
    by_chunk = {hit["chunk_id"]: hit["page_id"] for hit in hits}
    assert by_chunk == {
        CHUNK_BUTTON: PAGE_BUTTON,
        CHUNK_FOLLOW: PAGE_FOLLOW,
    }
    assert set(theory["page_ids"]) == {PAGE_BUTTON, PAGE_FOLLOW}
    summary = theory["summary"]
    assert isinstance(summary, str)
    assert summary.endswith("。")
    assert PAGE_FOLLOW in summary
    assert PAGE_FAR not in summary
    assert CHUNK_NIGHT not in json.dumps(theory, ensure_ascii=False)


def _on_course_disk(path: Path) -> bool:
    text = str(path)
    return path.drive.upper() == "F:" or "工业设计理论库" in text


def _watch_course_disk(monkeypatch) -> list[str]:
    touched: list[str] = []

    def note(path: Path) -> None:
        if _on_course_disk(path):
            touched.append(str(path))

    def wrap(name: str):
        original = getattr(Path, name)

        def wrapped(self, *args, **kwargs):
            note(self)
            return original(self, *args, **kwargs)

        return wrapped

    for name in ("is_dir", "is_file", "read_text", "read_bytes", "open"):
        monkeypatch.setattr(Path, name, wrap(name))
    return touched


def test_execute_source_does_not_name_the_course_disk():
    source = SOURCE.read_text(encoding="utf-8")
    assert "工业设计理论库" not in source
    assert "F:/" not in source
    assert "F:\\" not in source


def test_evidence_and_method_runs_include_theory_ids(engine, monkeypatch, tmp_path):
    corpus = tmp_path / "theory"
    _write_corpus(corpus)
    touched = _watch_course_disk(monkeypatch)
    monkeypatch.setenv("SINUO_THEORY_ROOT", str(corpus))
    _seed(engine)
    finished, tools = _run(engine, "hit", NOW)
    assert finished.execution_status == "completed"
    assert set(tools) == {"evidence", "method"}
    assert tools["evidence"]["hits"] == []
    assert tools["method"]["lookup"]["status"] == "no_match"
    for role in ("evidence", "method"):
        theory = tools[role]["theory"]
        assert isinstance(theory, dict)
        _assert_hit(theory)
        assert _stored_theory(engine, finished.run_id, role) == theory
    assert touched == []


def test_missing_theory_root_keeps_project_retrieval_and_skips_course_disk(
    engine, monkeypatch, tmp_path
):
    touched = _watch_course_disk(monkeypatch)
    monkeypatch.delenv("SINUO_THEORY_ROOT", raising=False)
    _seed(engine)
    cases = (
        ("unset", None, NOW),
        ("absent", tmp_path / "absent", NOW + 1),
        ("empty", tmp_path / "empty", NOW + 2),
    )
    (tmp_path / "empty").mkdir()
    for suffix, root, now in cases:
        if root is None:
            monkeypatch.delenv("SINUO_THEORY_ROOT", raising=False)
        else:
            monkeypatch.setenv("SINUO_THEORY_ROOT", str(root))
        finished, tools = _run(engine, suffix, now)
        assert finished.execution_status == "completed"
        assert set(tools) == {"evidence", "method"}
        assert tools["evidence"]["hits"] == []
        assert tools["method"]["lookup"]["status"] == "no_match"
        for role in ("evidence", "method"):
            assert tools[role]["theory"] == EMPTY_THEORY
            assert _stored_theory(engine, finished.run_id, role) == EMPTY_THEORY
    assert touched == []
