"""Runtime duties, citations, method cards, and the run ledger. Fake model only."""

import hashlib
import json

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from sinuo.agents.contracts import SpecialistResult
from sinuo.agents.deepseek import DeepSeekCallError
from sinuo.application.agents.execute import (
    AgentRunRequest,
    finish_agent_run,
    open_agent_run,
)
from sinuo.application.agents.intent import plan_duties
from sinuo.application.retrieval.search import rebuild_evidence_index
from sinuo.domain.auth.principal import Principal
from sinuo.infrastructure.database import write_uow
from sinuo.infrastructure.domain_models import (
    Account,
    AgentResult,
    Artifact,
    ArtifactVersion,
    Candidate,
    ContextSnapshot,
    Decision,
    EvidenceAnchor,
    Membership,
    ObjectGrant,
    Page,
    PageVersion,
    Project,
    RunEvent,
    RunTask,
    Stage,
)
from sinuo.infrastructure.methods import MethodCard

NOW = 1_763_200_000.0
PASSWORD = "$argon2id$synthetic-runtime-contract"
READER = "86868686-8686-4868-8686-868686868686"
PROJECT = "runtime-contract"
PAGE = "runtime-page"
VERSION = "runtime-page-v1"
STAGE = "runtime-stage"
ART = "runtime-art"
OLD_VERSION = "runtime-art-old"
NEW_VERSION = "runtime-art-new"
OLD_ANCHOR = "runtime-anchor-old"
NEW_ANCHOR = "runtime-anchor-new"
OLD_DECISION = "runtime-decision-old"
NEW_DECISION = "runtime-decision-new"
CARD = "runtime-method-v1"
OLD_EXCERPT = "过时锚点夜间取物留在历史"
NEW_EXCERPT = "现行锚点夜间取物可以引用"
OLD_CONCLUSION = "过时结论不要进入默认检索"
NEW_CONCLUSION = "现行结论仍可检索"
QUERY = "夜间取物"
QUESTION = "夜间观察怎么做"


class CountingFake:
    def __init__(self) -> None:
        self.roles: list[str] = []
        self.contexts: list[str] = []

    async def execute(self, task, snapshot, upstream):
        del upstream
        self.roles.append(task.role)
        self.contexts.append(snapshot.context_json)
        body = json.dumps({"gaps": [], "summary": "待确认"}, ensure_ascii=False)
        return SpecialistResult(task.task_id, "completed", body, role=task.role)


class UnauthorizedFake:
    async def execute(self, task, snapshot, upstream):
        del task, snapshot, upstream
        raise DeepSeekCallError("unauthorized", 401)


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _principal() -> Principal:
    return Principal(READER, "runtime-session", 1)


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
                login_name="synthetic-runtime-reader",
                display_name="synthetic-runtime-reader",
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
                id="member-runtime",
                project_id=PROJECT,
                account_id=READER,
                status="active",
                role_label=None,
                created_at=NOW,
                revoked_at=None,
            )
        )
        session.add(
            Stage(
                id=STAGE,
                project_id=PROJECT,
                head_config_version_id=None,
                created_at=NOW,
            )
        )
        session.flush()
        session.add(Artifact(id=ART, project_id=PROJECT, title=ART, created_at=NOW))
        session.flush()
        for version_id, number in ((OLD_VERSION, 1), (NEW_VERSION, 2)):
            session.add(
                ArtifactVersion(
                    id=version_id,
                    project_id=PROJECT,
                    artifact_id=ART,
                    version_no=number,
                    content_hash=_sha(version_id),
                    size_bytes=8,
                    media_type="text/plain",
                    object_key=f"synthetic/{version_id}",
                    license="synthetic",
                    sensitivity="project",
                    ai_processing_allowed=0,
                    project_revision=1,
                    created_by_account_id=READER,
                    created_at=NOW,
                )
            )
        session.flush()
        session.add(
            Page(
                id=PAGE,
                project_id=PROJECT,
                stage_id=STAGE,
                head_version_id=None,
                status="active",
                title="合成运行页",
                created_at=NOW,
            )
        )
        session.flush()
        content = json.dumps({"body": "合成运行正文"}, ensure_ascii=False)
        session.add(
            PageVersion(
                id=VERSION,
                project_id=PROJECT,
                page_id=PAGE,
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
        page = session.get(Page, PAGE)
        assert page is not None
        page.head_version_id = VERSION
        _anchor(session, OLD_ANCHOR, OLD_VERSION, OLD_EXCERPT, 1, None)
        session.flush()
        _anchor(session, NEW_ANCHOR, NEW_VERSION, NEW_EXCERPT, 4, OLD_ANCHOR)
        session.add(
            Decision(
                id=OLD_DECISION,
                project_id=PROJECT,
                account_id=READER,
                body=OLD_CONCLUSION,
                reason="旧依据",
                page_version_id=VERSION,
                artifact_version_id=None,
                stage_id=None,
                supersedes_id=None,
                project_revision=1,
                created_at=NOW,
            )
        )
        session.flush()
        session.add(
            Decision(
                id=NEW_DECISION,
                project_id=PROJECT,
                account_id=READER,
                body=NEW_CONCLUSION,
                reason="新依据",
                page_version_id=VERSION,
                artifact_version_id=None,
                stage_id=None,
                supersedes_id=OLD_DECISION,
                project_revision=1,
                created_at=NOW + 1,
            )
        )
        session.add(_card())
        for grant_id, action, scope, page_id, anchor_id in (
            ("grant-runtime-project", "project.read", "project", None, None),
            ("grant-runtime-run", "run.create", "project", None, None),
            ("grant-runtime-page", "page.read", "page", PAGE, None),
            ("grant-runtime-old", "evidence.read", "evidence_anchor", None, OLD_ANCHOR),
            ("grant-runtime-new", "evidence.read", "evidence_anchor", None, NEW_ANCHOR),
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
                    evidence_anchor_id=anchor_id,
                )
            )
        session.flush()
        rebuild_evidence_index(session)


def _anchor(
    session: Session,
    anchor_id: str,
    version_id: str,
    excerpt: str,
    page: int,
    supersedes_id: str | None,
) -> None:
    session.add(
        EvidenceAnchor(
            id=anchor_id,
            project_id=PROJECT,
            artifact_version_id=version_id,
            locator_type="text_range",
            locator_json=json.dumps(
                {"excerpt": excerpt, "page": page},
                ensure_ascii=False,
                separators=(",", ":"),
            ),
            excerpt_hash=_sha(excerpt),
            extractor_version="synthetic-1",
            visibility="project",
            sensitivity="project",
            supersedes_id=supersedes_id,
            schema_version=1,
            created_by_account_id=READER,
            created_at=NOW,
        )
    )


def _card() -> MethodCard:
    applicable = {
        "question_terms": ["夜间观察"],
        "requires": ["consent"],
        "situations": ["家庭"],
        "stage_names": ["discover"],
    }
    return MethodCard(
        id=CARD,
        library_key=PROJECT,
        project_id=PROJECT,
        card_key="runtime-observe",
        version_no=1,
        name="夜间观察",
        source_json=json.dumps(
            {"license": "synthetic", "locator": "合成讲义第4页", "title": "课程观察卡"},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ),
        applicable_json=json.dumps(
            applicable, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ),
        inputs_json=json.dumps(["同意记录"], ensure_ascii=False),
        steps_json=json.dumps(["先记下观察"], ensure_ascii=False),
        outputs_json=json.dumps(["观察记录"], ensure_ascii=False),
        limits_json=json.dumps(["不能代替访谈"], ensure_ascii=False),
        created_at=NOW,
    )


def _request(
    tasks,
    *,
    intent: str,
    now: float,
    query: str = "",
    question: str = "",
    gaps: tuple[str, ...] = (),
) -> AgentRunRequest:
    return AgentRunRequest(
        intent=intent,
        tasks=tasks,
        query=query,
        question=question,
        situation="家庭厨房",
        constraints={"consent": True},
        page_version_id=VERSION,
        now=now,
        opening_gaps=gaps,
    )


def _counts(engine) -> tuple[int, int, str | None]:
    with Session(engine) as session:
        pages = int(session.scalar(select(func.count()).select_from(Page)) or 0)
        versions = int(session.scalar(select(func.count()).select_from(PageVersion)) or 0)
        head = session.scalar(select(Page.head_version_id).where(Page.id == PAGE))
    return pages, versions, head


def _output(engine, run_id: str, role: str) -> dict:
    with Session(engine) as session:
        task_id = session.scalar(
            select(RunTask.id).where(RunTask.run_id == run_id, RunTask.role == role)
        )
        body = session.scalar(
            select(AgentResult.output_json).where(
                AgentResult.run_id == run_id,
                AgentResult.task_id == task_id,
                AgentResult.disposition == "published",
            )
        )
    assert isinstance(body, str)
    parsed = json.loads(body)
    assert isinstance(parsed, dict)
    return parsed


def _events(engine, run_id: str) -> list[dict]:
    with Session(engine) as session:
        rows = session.scalars(
            select(RunEvent).where(RunEvent.run_id == run_id).order_by(RunEvent.seq)
        ).all()
    found = []
    for row in rows:
        payload = json.loads(row.payload)
        found.append({"type": row.event_type, "seq": row.seq, "payload": payload})
    return found


def test_selected_duties_are_the_only_calls_and_do_not_advance_stage(engine):
    _seed(engine)
    before = _counts(engine)
    provider = CountingFake()
    cases = (
        ("问资料", ("evidence",), ()),
        ("问方法", ("method",), ()),
        ("问下一步", ("evidence", "critic"), ("阶段判断等待证据",)),
        ("简报变化", ("clarification",), ()),
    )
    for index, (intent, roles, gaps) in enumerate(cases):
        plan = plan_duties(intent, project_id=PROJECT, now=NOW + index)
        assert plan.roles == roles
        assert plan.gaps == gaps
        seen = len(provider.roles)
        opened = open_agent_run(
            engine,
            _principal(),
            PROJECT,
            _request(
                plan.tasks,
                intent=intent,
                now=NOW + index,
                query=intent if "evidence" in roles else "",
                question=intent if "method" in roles else "",
                gaps=plan.gaps,
            ),
        )
        finish_agent_run(engine, _principal(), opened.run_id, provider, now=NOW + index)
        assert tuple(provider.roles[seen:]) == roles
        with Session(engine) as session:
            stored = session.get(ContextSnapshot, opened.snapshot_id)
            assert stored is not None
            scope = json.loads(stored.authorization_scope_json)
        assert scope["route"] == list(roles)
        assert scope["opening_gaps"] == list(gaps)
        assert scope["page_version_id"] == VERSION
        planned = _events(engine, opened.run_id)[0]
        assert planned["seq"] == 1
        assert planned["type"] == "run.planned"
        assert planned["payload"]["route"] == list(roles)
        assert planned["payload"]["input_page_version_id"] == VERSION
        assert planned["payload"]["opening_gaps"] == list(gaps)
    assert provider.roles.count("clarification") == 1
    assert provider.roles.count("method") == 1
    assert _counts(engine) == before
    with Session(engine) as session:
        stage_id = session.scalar(select(Page.stage_id).where(Page.id == PAGE))
    assert stage_id == STAGE


def test_citations_method_cards_and_retired_conclusions(engine):
    _seed(engine)
    before = _counts(engine)
    provider = CountingFake()
    plan = plan_duties("问资料", project_id=PROJECT, now=NOW + 20)
    opened = open_agent_run(
        engine,
        _principal(),
        PROJECT,
        _request(
            (
                plan.tasks[0],
                plan_duties("问方法", project_id=PROJECT, now=NOW + 21).tasks[0],
            ),
            intent="问资料",
            now=NOW + 22,
            query=QUERY,
            question=QUESTION,
        ),
    )
    finished = finish_agent_run(engine, _principal(), opened.run_id, provider, now=NOW + 22)
    assert finished.execution_status == "completed"
    assert set(provider.roles) == {"evidence", "method"}
    assert len(provider.roles) == 2
    evidence = _output(engine, opened.run_id, "evidence")
    assert evidence["citations"] == [
        {"artifact_version_id": NEW_VERSION, "place": {"page": 4}}
    ]
    assert "evidence_gap" not in evidence
    rendered = json.dumps(evidence, ensure_ascii=False)
    assert OLD_EXCERPT not in rendered
    assert NEW_EXCERPT in rendered
    memory = evidence["memory"]
    assert memory["session"]["snapshot_id"] == opened.snapshot_id
    assert memory["task"]["role"] == "evidence"
    assert [item["conclusion"] for item in memory["confirmed"]] == [NEW_CONCLUSION]
    assert memory["rebuildable"]["retired_conclusion_ids"] == [OLD_DECISION]
    assert OLD_CONCLUSION not in json.dumps(provider.contexts, ensure_ascii=False)
    advice = _output(engine, opened.run_id, "method")["method_advice"]
    assert advice == [
        {
            "inputs": ["同意记录"],
            "limits": ["不能代替访谈"],
            "problem": ["夜间观察"],
            "source": {"license": "synthetic", "locator": "合成讲义第4页", "title": "课程观察卡"},
            "steps": ["先记下观察"],
        }
    ]
    names = [call["name"] for call in evidence["calls"]]
    assert "search_evidence" in names
    assert "lookup_method" not in names
    ledger = json.dumps(_events(engine, opened.run_id), ensure_ascii=False)
    assert OLD_EXCERPT not in ledger
    assert OLD_CONCLUSION not in ledger
    assert '"name": "search_evidence"' in ledger
    assert '"name": "lookup_method"' in ledger
    with Session(engine) as session:
        assert session.get(EvidenceAnchor, OLD_ANCHOR) is not None
        assert session.get(Decision, OLD_DECISION) is not None
        assert session.scalar(select(func.count()).select_from(Candidate)) == 0
    assert _counts(engine) == before


def test_intent_word_query_falls_back_to_page_title(engine):
    """意图词（如「下一步」）不是检索词，证据检索回退到页面标题。"""
    _seed(engine)
    provider = CountingFake()
    plan = plan_duties("下一步", project_id=PROJECT, now=NOW + 40)
    opened = open_agent_run(
        engine,
        _principal(),
        PROJECT,
        _request(
            plan.tasks,
            intent="下一步",
            now=NOW + 40,
            query="下一步",
            gaps=plan.gaps,
        ),
    )
    finish_agent_run(engine, _principal(), opened.run_id, provider, now=NOW + 40)
    evidence = _output(engine, opened.run_id, "evidence")
    scope = evidence.get("evidence_scope")
    assert isinstance(scope, dict)
    # 意图词「下一步」不是主题词，应回退为页面标题「合成运行页」
    assert scope["query"] == "合成运行页"


def test_run_ledger_records_tool_calls_and_error_category(engine):
    _seed(engine)
    before = _counts(engine)
    opened = open_agent_run(
        engine,
        _principal(),
        PROJECT,
        _request(
            plan_duties("简报变化", project_id=PROJECT, now=NOW + 30).tasks,
            intent="简报变化",
            now=NOW + 30,
        ),
    )
    finished = finish_agent_run(
        engine, _principal(), opened.run_id, UnauthorizedFake(), now=NOW + 30
    )
    assert finished.execution_status == "partial"
    assert finished.candidate_id is None
    with Session(engine) as session:
        task = session.scalars(select(RunTask).where(RunTask.run_id == opened.run_id)).one()
        assert task.role == "clarification"
        assert task.last_error == "unauthorized"
        assert session.scalar(select(func.count()).select_from(Page)) == before[0]
    events = _events(engine, opened.run_id)
    assert events[0]["payload"]["input_page_version_id"] == VERSION
    assert events[0]["payload"]["route"] == ["clarification"]
    failed = [item for item in events if item["type"] == "task.failed"]
    assert failed
    assert failed[0]["payload"]["error"] == "unauthorized"
    blob = json.dumps(events, ensure_ascii=False)
    assert "Bearer" not in blob
    assert "api.deepseek.com" not in blob
    assert _counts(engine) == before
