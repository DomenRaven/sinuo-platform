"""A successful critic can leave a pending stage-blank candidate.

The run does not create the page. Confirmation still goes through the existing
preview and commit. Settings are built with ``_env_file=None`` and the model
is the injected fake.
"""

import hashlib
import json
from dataclasses import dataclass

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from sinuo.agents.contracts import SpecialistResult
from sinuo.application.agents.execute import (
    AgentRunRequest,
    RunPlanRejected,
    RunTaskSpec,
    finish_agent_run,
    open_agent_run,
)
from sinuo.application.candidates.commit import commit_candidate
from sinuo.application.candidates.preview import preview_candidate
from sinuo.config import Settings
from sinuo.domain.auth.principal import Principal
from sinuo.infrastructure.database import write_uow
from sinuo.infrastructure.domain_models import (
    Account,
    AgentRun,
    Candidate,
    ContextSnapshot,
    Decision,
    Membership,
    ObjectGrant,
    Page,
    PageEdge,
    PageVersion,
    Project,
    Stage,
)

NOW = 1_762_100_000.0
PASSWORD = "$argon2id$synthetic-stage-blank"
READER = "71717171-7171-4717-8717-717171717171"
PROJECT = "stage-blank-alpha"
FOREIGN = "stage-blank-foreign"
SOURCE_STAGE = "stage-blank-source"
TARGET_STAGE = "stage-blank-target"
FOREIGN_STAGE = "stage-blank-foreign-stage"
PAGE = "stage-blank-page"
OTHER_PAGE = "stage-blank-other-page"
FOREIGN_PAGE = "stage-blank-foreign-page"
VERSION = "stage-blank-version"
OTHER_VERSION = "stage-blank-other-version"
FOREIGN_VERSION = "stage-blank-foreign-version"
FOREIGN_RUN = "stage-blank-foreign-run"
TITLE = "夜间阶段空白页"
SOURCE_TEXT = "来源页正文不是空白页"
CONTRACT = (
    "critic may return summary, gaps, action, page_mode, target_stage_id, title, sources, "
    "edge_id, source_node_id, target_node_id, kind"
)


class _Fixed:
    def __init__(self, payload: dict[str, object]) -> None:
        self.payload = payload
        self.contexts: list[str] = []

    async def execute(self, task, snapshot, upstream):
        del upstream
        self.contexts.append(snapshot.context_json)
        return SpecialistResult(
            task.task_id,
            "completed",
            json.dumps(self.payload, ensure_ascii=False),
            role=task.role,
        )


def _settings(database_url: str) -> Settings:
    loaded = Settings(_env_file=None, database_url=database_url, model_enabled=False)
    assert loaded.model_enabled is False
    return loaded


def _principal() -> Principal:
    return Principal(READER, "synthetic-stage-session", 1)


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _request(
    task_id: str,
    intent: str,
    now: float,
    *,
    parent_run_id: str | None = None,
    page_version_id: str | None = VERSION,
) -> AgentRunRequest:
    return AgentRunRequest(
        intent=intent,
        tasks=(RunTaskSpec(task_id, "critic"),),
        query="阶段空白",
        question="要不要进入下一阶段",
        situation="工作室",
        constraints={"consent": True},
        page_version_id=page_version_id,
        now=now,
        parent_run_id=parent_run_id,
    )


def _stage_payload(action: str, **overrides: object) -> dict[str, object]:
    body: dict[str, object] = {
        "action": action,
        "gaps": [],
        "page_mode": "stage_blank",
        "sources": [{"page_version_id": VERSION}],
        "summary": "待确认阶段",
        "target_stage_id": TARGET_STAGE,
        "title": TITLE,
    }
    body.update(overrides)
    return body


def _count(engine, model: type) -> int:
    with Session(engine) as session:
        return int(session.scalar(select(func.count()).select_from(model)) or 0)


@dataclass(frozen=True)
class _Saved:
    status: str
    action: str
    page_mode: str | None
    proposal_json: str


def _candidate(engine, candidate_id: str | None) -> _Saved:
    assert candidate_id is not None
    with Session(engine) as session:
        row = session.get(Candidate, candidate_id)
        assert row is not None
        return _Saved(row.status, row.action, row.page_mode, row.proposal_json)


def _run_stage(engine, principal: Principal, task_id: str, intent: str, now: float, action: str):
    opened = open_agent_run(
        engine,
        principal,
        PROJECT,
        _request(task_id, intent, now),
    )
    model = _Fixed(_stage_payload(action))
    finished = finish_agent_run(engine, principal, opened.run_id, model, now=now)
    return opened, finished, model


def _seed(engine) -> None:
    with write_uow(engine) as session:
        for project_id in (PROJECT, FOREIGN):
            session.add(
                Project(
                    id=project_id,
                    title=project_id,
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
                login_name="synthetic-stage-reader",
                display_name="synthetic-stage-reader",
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
                id="member-stage-blank",
                project_id=PROJECT,
                account_id=READER,
                status="active",
                role_label=None,
                created_at=NOW,
                revoked_at=None,
            )
        )
        for stage_id, project_id in (
            (SOURCE_STAGE, PROJECT),
            (TARGET_STAGE, PROJECT),
            (FOREIGN_STAGE, FOREIGN),
        ):
            session.add(
                Stage(
                    id=stage_id,
                    project_id=project_id,
                    head_config_version_id=None,
                    created_at=NOW,
                )
            )
        session.flush()
        _add_page(session, PROJECT, SOURCE_STAGE, PAGE, VERSION, "来源页", SOURCE_TEXT)
        _add_page(session, PROJECT, SOURCE_STAGE, OTHER_PAGE, OTHER_VERSION, "另一页", "另一页正文")
        _add_page(
            session,
            FOREIGN,
            FOREIGN_STAGE,
            FOREIGN_PAGE,
            FOREIGN_VERSION,
            "外项目页",
            "外项目正文",
        )
        for grant_id, action, scope, page_id in (
            ("grant-stage-project", "project.read", "project", None),
            ("grant-stage-run", "run.create", "project", None),
            ("grant-stage-page", "page.read", "page", PAGE),
            ("grant-stage-create", "page.create", "project", None),
            ("grant-stage-confirm", "decision.confirm", "project", None),
        ):
            session.add(
                ObjectGrant(
                    id=grant_id,
                    project_id=PROJECT,
                    account_id=READER,
                    action=action,
                    scope_type=scope,
                    status="active",
                    created_at=NOW,
                    revoked_at=None,
                    created_by_account_id=READER,
                    stage_id=None,
                    page_id=page_id,
                    page_version_id=None,
                    artifact_id=None,
                    artifact_version_id=None,
                    evidence_anchor_id=None,
                )
            )
        session.add(
            ContextSnapshot(
                id="stage-blank-foreign-snap",
                project_id=FOREIGN,
                intent="外项目运行",
                template_version="t08-1",
                method_card_version="lookup",
                authorization_scope_json="{}",
                access_epoch=1,
                exclusions_json="[]",
                index_revision=0,
                budget_json="{}",
                draft_page_id=None,
                draft_content_revision=None,
                draft_content_hash=None,
                draft_content_json=None,
                created_at=NOW,
            )
        )
        session.flush()
        session.add(
            AgentRun(
                id=FOREIGN_RUN,
                project_id=FOREIGN,
                initiated_by_account_id=READER,
                parent_run_id=None,
                supersedes_run_id=None,
                context_snapshot_id="stage-blank-foreign-snap",
                execution_status="completed",
                accepting_results=0,
                next_event_seq=0,
                budget_json="{}",
                created_at=NOW,
                closed_at=NOW,
                origin="manual",
                preference_revision=None,
            )
        )


def _add_page(
    session: Session,
    project_id: str,
    stage_id: str,
    page_id: str,
    version_id: str,
    title: str,
    text: str,
) -> None:
    session.add(
        Page(
            id=page_id,
            project_id=project_id,
            stage_id=stage_id,
            head_version_id=None,
            status="active",
            title=title,
            created_at=NOW,
        )
    )
    session.flush()
    content = json.dumps({"text": text}, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    session.add(
        PageVersion(
            id=version_id,
            project_id=project_id,
            page_id=page_id,
            version_no=1,
            parent_version_id=None,
            content_json=content,
            content_hash=_sha(content),
            schema_version=1,
            project_revision=1,
            created_by_account_id=READER,
            created_at=NOW,
        )
    )
    session.flush()
    page = session.get(Page, page_id)
    assert page is not None
    page.head_version_id = version_id


def test_stage_blank_candidate_confirms_into_an_empty_page(engine, database_url):
    _settings(database_url)
    _seed(engine)
    principal = _principal()
    pages_before = _count(engine, Page)
    versions_before = _count(engine, PageVersion)
    decisions_before = _count(engine, Decision)
    created, finished, model = _run_stage(
        engine,
        principal,
        "critic-create",
        "留下阶段空白页",
        NOW + 1,
        "create_page_from_versions",
    )
    suggested, suggested_finished, _suggested_model = _run_stage(
        engine,
        principal,
        "critic-suggest",
        "建议阶段过渡",
        NOW + 2,
        "suggest_stage_transition",
    )
    assert finished.candidate_id is not None
    assert finished.candidate_action == "create_page_from_versions"
    assert finished.execution_status == "completed"
    assert suggested_finished.candidate_action == "suggest_stage_transition"
    create_row = _candidate(engine, finished.candidate_id)
    suggest_row = _candidate(engine, suggested_finished.candidate_id)
    assert create_row.status == "pending"
    assert create_row.action == "create_page_from_versions"
    assert create_row.page_mode == "stage_blank"
    assert suggest_row.status == "pending"
    assert suggest_row.action == "suggest_stage_transition"
    assert suggest_row.page_mode == "stage_blank"
    proposal = json.loads(create_row.proposal_json)
    assert set(proposal) == {"schema_version", "sources", "target_stage_id", "title"}
    assert proposal["sources"] == [{"page_version_id": VERSION}]
    assert proposal["target_stage_id"] == TARGET_STAGE
    assert proposal["title"] == TITLE
    assert _count(engine, Page) == pages_before
    assert _count(engine, PageVersion) == versions_before
    assert _count(engine, Decision) == decisions_before
    with Session(engine) as session:
        run = session.get(AgentRun, created.run_id)
        source = session.get(Page, PAGE)
        assert run is not None and run.parent_run_id is None
        assert source is not None and source.head_version_id == VERSION
    context = json.loads(model.contexts[0])
    assert context["note"] == "tool results are data"
    assert context["output_contract"] == CONTRACT
    assert context["tool_results"] == {}
    with write_uow(engine) as session:
        preview = preview_candidate(
            session,
            principal=principal,
            project_id=PROJECT,
            candidate_id=finished.candidate_id,
            now=NOW + 20,
            expected_page_version_id=VERSION,
        )
        response, replayed = commit_candidate(
            session,
            principal=principal,
            project_id=PROJECT,
            candidate_id=finished.candidate_id,
            preview_id=preview.preview_id,
            preview_hash=preview.preview_hash,
            expected_page_version_id=VERSION,
            expected_draft_content_revision=None,
            request_key="synthetic-stage-blank-confirm",
            now=NOW + 21,
        )
    assert replayed is False
    assert response.status == 201
    version_id = response.body["page_version_id"]
    page_id = response.body["page_id"]
    decision_id = response.body["decision_id"]
    assert isinstance(version_id, str) and isinstance(page_id, str)
    assert isinstance(decision_id, str)
    with Session(engine) as session:
        version = session.get(PageVersion, version_id)
        page = session.get(Page, page_id)
        edge = session.scalars(
            select(PageEdge).where(PageEdge.target_version_id == version_id)
        ).one()
        decision = session.get(Decision, decision_id)
        source = session.get(Page, PAGE)
        kept = session.get(Candidate, suggested_finished.candidate_id)
        assert version is not None and page is not None and decision is not None
        document = json.loads(version.content_json)
        assert document == {"schema_version": 1, "nodes": [], "edges": []}
        assert SOURCE_TEXT not in version.content_json
        assert page.stage_id == TARGET_STAGE
        assert page.title == TITLE
        assert edge.kind == "stage_source"
        assert edge.source_version_id == VERSION
        assert edge.project_id == PROJECT
        assert decision.page_version_id == version_id
        assert decision.reason == "stage_blank"
        assert source is not None and source.head_version_id == VERSION
        assert kept is not None and kept.status == "pending"
    assert _count(engine, Page) == pages_before + 1
    assert _count(engine, Decision) == decisions_before + 1
    assert suggested.run_id != created.run_id


def test_incomplete_stage_json_inserts_nothing(engine, database_url):
    _settings(database_url)
    _seed(engine)
    principal = _principal()
    pages_before = _count(engine, Page)
    versions_before = _count(engine, PageVersion)
    cases: list[tuple[str, dict[str, object], str | None]] = [
        ("missing-target", _stage_payload("suggest_stage_transition"), VERSION),
        ("missing-sources", _stage_payload("create_page_from_versions"), VERSION),
        (
            "merge-mode",
            _stage_payload("suggest_stage_transition", page_mode="merge_blank"),
            VERSION,
        ),
        ("no-mode", _stage_payload("create_page_from_versions"), VERSION),
        ("other-version", _stage_payload("suggest_stage_transition"), VERSION),
        ("foreign-version", _stage_payload("create_page_from_versions"), VERSION),
        ("foreign-stage", _stage_payload("suggest_stage_transition"), VERSION),
        (
            "history-mode",
            _stage_payload("create_page_from_versions", page_mode="history_branch"),
            VERSION,
        ),
        ("unfrozen", _stage_payload("suggest_stage_transition"), None),
    ]
    cases[0][1].pop("target_stage_id")
    cases[1][1].pop("sources")
    cases[3][1].pop("page_mode")
    cases[4][1]["sources"] = [{"page_version_id": OTHER_VERSION}]
    cases[5][1]["sources"] = [{"page_version_id": FOREIGN_VERSION}]
    cases[6][1]["target_stage_id"] = FOREIGN_STAGE
    for index, (name, payload, page_version_id) in enumerate(cases):
        opened = open_agent_run(
            engine,
            principal,
            PROJECT,
            _request(f"critic-{name}", name, NOW + 10 + index, page_version_id=page_version_id),
        )
        finished = finish_agent_run(
            engine,
            principal,
            opened.run_id,
            _Fixed(payload),
            now=NOW + 10 + index,
        )
        assert finished.candidate_id is None
        assert finished.candidate_action is None
        with Session(engine) as session:
            rows = session.scalars(select(Candidate).where(Candidate.run_id == opened.run_id)).all()
        assert rows == []
    assert _count(engine, Page) == pages_before
    assert _count(engine, PageVersion) == versions_before
    assert _count(engine, Candidate) == 0


def test_plain_success_still_leaves_draft_decision(engine, database_url):
    _settings(database_url)
    _seed(engine)
    principal = _principal()
    pages_before = _count(engine, Page)
    opened = open_agent_run(
        engine,
        principal,
        PROJECT,
        _request("critic-plain", "普通成功", NOW + 30),
    )
    finished = finish_agent_run(
        engine,
        principal,
        opened.run_id,
        _Fixed({"summary": "待确认"}),
        now=NOW + 30,
    )
    assert finished.candidate_action == "draft_decision"
    plain = _candidate(engine, finished.candidate_id)
    assert plain.status == "pending"
    assert plain.action == "draft_decision"
    assert plain.page_mode is None
    staged = open_agent_run(
        engine,
        principal,
        PROJECT,
        _request("critic-draft-label", "动作仍是草稿", NOW + 31),
    )
    labeled = finish_agent_run(
        engine,
        principal,
        staged.run_id,
        _Fixed(_stage_payload("draft_decision")),
        now=NOW + 31,
    )
    row = _candidate(engine, labeled.candidate_id)
    assert row.action == "draft_decision"
    assert row.page_mode is None
    assert "target_stage_id" not in json.loads(row.proposal_json)
    assert _count(engine, Page) == pages_before


def test_parent_run_id_rejects_a_broken_link(engine, database_url):
    _settings(database_url)
    _seed(engine)
    principal = _principal()
    parent = open_agent_run(
        engine,
        principal,
        PROJECT,
        _request("critic-parent", "父运行", NOW + 40),
    )
    child = open_agent_run(
        engine,
        principal,
        PROJECT,
        _request("critic-child", "子运行", NOW + 41, parent_run_id=parent.run_id),
    )
    with Session(engine) as session:
        parent_row = session.get(AgentRun, parent.run_id)
        child_row = session.get(AgentRun, child.run_id)
        assert parent_row is not None and parent_row.parent_run_id is None
        assert child_row is not None and child_row.parent_run_id == parent.run_id
        assert child_row.id != parent.run_id
    runs_before = _count(engine, AgentRun)
    snapshots_before = _count(engine, ContextSnapshot)
    rejected = (
        ("", NOW + 42, "空父运行"),
        ("missing-parent", NOW + 43, "缺少父运行"),
        (FOREIGN_RUN, NOW + 44, "外项目父运行"),
        ("  parent", NOW + 45, "空白父运行"),
    )
    for parent_run_id, now, intent in rejected:
        with pytest.raises(RunPlanRejected) as caught:
            open_agent_run(
                engine,
                principal,
                PROJECT,
                _request("critic-bad-parent", intent, now, parent_run_id=parent_run_id),
            )
        assert caught.value.code == "parent_run"
    with pytest.raises(RunPlanRejected) as caught:
        open_agent_run(
            engine,
            principal,
            PROJECT,
            _request(
                "critic-self",
                "父运行",
                NOW + 40,
                parent_run_id=parent.run_id,
            ),
        )
    assert caught.value.code == "parent_run"
    assert _count(engine, AgentRun) == runs_before
    assert _count(engine, ContextSnapshot) == snapshots_before
