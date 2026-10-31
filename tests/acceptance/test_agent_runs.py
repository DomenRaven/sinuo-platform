"""Fake specialist chain. No network and no live model."""

import hashlib
import json
import time

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from sinuo.agents.provider import DisabledModelProvider
from sinuo.api.app import create_app
from sinuo.application.agents.fake_chain import METHOD_QUESTION, FakeChainRejected, run_fake_chain
from sinuo.application.agents.read_chain import read_fake_chain
from sinuo.application.auth.authorizer import SessionAuthorizer
from sinuo.application.auth.sessions import issue_session
from sinuo.domain.auth.errors import AuthRequired
from sinuo.domain.auth.principal import Principal
from sinuo.domain.public_contract import session_cookie_name
from sinuo.infrastructure.database import write_uow
from sinuo.infrastructure.domain_models import (
    Account,
    AgentRun,
    Artifact,
    ArtifactVersion,
    Candidate,
    EvidenceAnchor,
    Membership,
    ObjectGrant,
    Page,
    PageVersion,
    Project,
    RunReadRecord,
    RunTask,
    Stage,
)
from tests.acceptance.identity_fixtures import identity_settings

NOW = 1_762_000_000.0
PASSWORD = "$argon2id$synthetic-fake-chain"
READER = "77777777-7777-4777-8777-777777777777"
PROJECT = "synthetic-fake-alpha"
QUERY = "合成竹材弯曲"
INTENT = "确认合成竹材能不能弯"
VISIBLE_EXCERPT = "合成竹材弯曲"
HIDDEN_EXCERPT = "合成竹材弯曲-无权"
VISIBLE_TITLE = "合成可见页"
VISIBLE_BODY = "合成可见正文"
HIDDEN_TITLE = "合成无权标题"
HIDDEN_BODY = "合成无权正文"
HIT_VERSION = "synthetic-fake-hit-v1"
VISIBLE_PAGE = "synthetic-fake-page-v1"
HIDDEN_PAGE = "synthetic-fake-page-hidden"
MISSING = "88888888-8888-4888-8888-888888888888"


def test_fake_chain_walks_four_roles_and_leaves_a_pending_candidate(engine):
    _seed(engine)
    before = _counts(engine)
    result = run_fake_chain(
        engine,
        Principal(READER, "synthetic-fake-session", 1),
        PROJECT,
        intent=INTENT,
        query=QUERY,
        page_version_id=VISIBLE_PAGE,
        now=NOW,
    )
    hidden = run_fake_chain(
        engine,
        Principal(READER, "synthetic-fake-session", 1),
        PROJECT,
        intent=INTENT,
        query=QUERY,
        page_version_id=HIDDEN_PAGE,
        now=NOW + 1,
    )
    with Session(engine) as session:
        parent = session.get(AgentRun, result.parent_run_id)
        critic = session.get(AgentRun, result.critic_run_id)
        candidate = session.get(Candidate, result.candidate_id)
        roles = session.scalars(
            select(RunTask.role)
            .where(RunTask.run_id == result.parent_run_id)
            .order_by(RunTask.role)
        ).all()
        critic_roles = session.scalars(
            select(RunTask.role).where(RunTask.run_id == result.critic_run_id)
        ).all()
        names = session.scalars(
            text("SELECT name FROM sqlite_master WHERE type = 'table'")
        ).all()
        reads = session.scalars(
            select(RunReadRecord).where(RunReadRecord.run_id == result.parent_run_id)
        ).all()
    assert result.roles == ("clarification", "evidence", "method", "critic")
    assert parent is not None and critic is not None and candidate is not None
    assert critic.parent_run_id == parent.id
    assert parent.accepting_results == 0
    assert critic.accepting_results == 0
    assert parent.execution_status == "completed"
    assert roles == ["clarification", "evidence", "method"]
    assert critic_roles == ["critic"]
    assert candidate.status == "pending"
    assert candidate.revision == 1
    assert candidate.action == "draft_decision"
    visible_proposal = _proposal(engine, result.candidate_id)
    hidden_proposal = _proposal(engine, hidden.candidate_id)
    assert visible_proposal["page_title"] == VISIBLE_TITLE
    assert visible_proposal["page_version_id"] == VISIBLE_PAGE
    assert visible_proposal["artifact_version_ids"] == [HIT_VERSION]
    assert visible_proposal["excerpts"] == [VISIBLE_EXCERPT]
    assert {row.artifact_version_id for row in reads if row.artifact_version_id} == {HIT_VERSION}
    assert {row.page_version_id for row in reads if row.page_version_id} == {VISIBLE_PAGE}
    assert visible_proposal["method_card"] is None
    assert visible_proposal["method_question"] == "没有方法卡可查。上面的证据是否够用？"
    assert hidden_proposal["page_title"] is None
    assert hidden_proposal["page_version_id"] is None
    assert hidden_proposal["excerpts"] == [VISIBLE_EXCERPT]
    assert HIDDEN_EXCERPT not in json.dumps(hidden_proposal, ensure_ascii=False)
    assert HIDDEN_TITLE not in json.dumps(hidden_proposal, ensure_ascii=False)
    assert HIDDEN_BODY not in json.dumps(hidden_proposal, ensure_ascii=False)
    assert HIDDEN_TITLE not in json.dumps(visible_proposal, ensure_ascii=False)
    assert not any("method_card" in name for name in names)
    assert _counts(engine)["candidates"] == before["candidates"] + 2


def test_read_fake_chain_returns_the_pending_candidate_only_when_allowed(engine, database_url):
    _seed(engine)
    result = run_fake_chain(
        engine,
        Principal(READER, "synthetic-fake-session", 1),
        PROJECT,
        intent=INTENT,
        query=QUERY,
        page_version_id=VISIBLE_PAGE,
        now=NOW,
    )
    principal = Principal(READER, "synthetic-fake-session", 1)
    before = _counts(engine)["candidates"]
    with write_uow(engine) as session:
        authorizer = SessionAuthorizer(session)
        denied_sql: list[str] = []
        denied = _traced(
            session,
            lambda: read_fake_chain(
                session, authorizer, principal, PROJECT, result.parent_run_id
            ),
            denied_sql,
        )
    assert _counts(engine)["candidates"] == before
    denied_text = _render_reading(denied)
    assert denied.found is False
    assert denied.candidate_body is None
    assert denied.page_title is None and denied.page_body is None
    assert VISIBLE_TITLE not in denied_text
    assert VISIBLE_BODY not in denied_text
    assert HIDDEN_TITLE not in denied_text
    assert HIDDEN_BODY not in denied_text
    assert HIDDEN_EXCERPT not in denied_text
    assert "待确认" not in denied_text
    assert not any("proposal_json" in statement for statement in denied_sql)
    with write_uow(engine) as session:
        _grant(session, "grant-fake-run", "run.read", "project", None, None)
    with write_uow(engine) as session:
        authorizer = SessionAuthorizer(session)
        allowed = read_fake_chain(
            session, authorizer, principal, PROJECT, result.parent_run_id
        )
        missing = read_fake_chain(
            session, authorizer, principal, PROJECT, "synthetic-missing-run"
        )
        other = read_fake_chain(
            session, authorizer, principal, "synthetic-fake-beta", result.parent_run_id
        )
    assert _counts(engine)["candidates"] == before
    assert allowed.found is True
    assert allowed.parent_status == "completed"
    assert allowed.critic_status == "completed"
    assert [step.role for step in allowed.steps] == [
        "clarification",
        "evidence",
        "method",
        "critic",
    ]
    assert {step.status for step in allowed.steps} == {"succeeded"}
    assert [hit.excerpt for hit in allowed.hits] == [VISIBLE_EXCERPT]
    assert allowed.hits[0].artifact_version_id == HIT_VERSION
    assert allowed.page_title == VISIBLE_TITLE
    assert allowed.page_body == VISIBLE_BODY
    assert allowed.method_question == METHOD_QUESTION
    assert allowed.candidate_status == "pending"
    assert allowed.candidate_body is not None
    assert "待确认" in allowed.candidate_body
    assert VISIBLE_TITLE in allowed.candidate_body
    assert HIDDEN_TITLE not in allowed.candidate_body
    assert HIDDEN_EXCERPT not in allowed.candidate_body
    assert missing.found is False and missing.candidate_body is None
    assert VISIBLE_TITLE not in _render_reading(missing)
    assert other.found is False and other.candidate_body is None
    assert VISIBLE_TITLE not in _render_reading(other)
    with write_uow(engine) as session:
        with pytest.raises(AuthRequired):
            read_fake_chain(
                session,
                SessionAuthorizer(session),
                Principal(MISSING, "synthetic-fake-missing", 1),
                PROJECT,
                result.parent_run_id,
            )
    assert _counts(engine)["candidates"] == before
    _assert_page_shows_the_candidate(engine, database_url, result.parent_run_id)


def _assert_page_shows_the_candidate(engine, database_url: str, parent_run_id: str) -> None:
    settings = identity_settings(database_url)
    with write_uow(engine) as session:
        account = session.get(Account, READER)
        assert account is not None
        token = issue_session(session, account, settings, time.time()).token
    app = create_app(settings)
    view = f"/api/v1/projects/{PROJECT}/agent-runs/{parent_run_id}/fake-chain/view"
    data = f"/api/v1/projects/{PROJECT}/agent-runs/{parent_run_id}/fake-chain"
    before = _counts(engine)["candidates"]
    with TestClient(app) as client:
        anonymous = client.get(view)
        assert anonymous.status_code == 401
        assert VISIBLE_TITLE not in anonymous.text
        assert HIDDEN_TITLE not in anonymous.text
        client.cookies.set(session_cookie_name(settings.public_url), token)
        page = client.get(view)
        body = client.get(data)
    assert page.status_code == 200
    assert "text/html" in page.headers["content-type"]
    assert page.headers["cache-control"] == "no-store"
    assert "页快照和检索来自已测后端" in page.text
    assert "FakeModelProvider" in page.text
    assert "不是真实模型" in page.text
    assert METHOD_QUESTION in page.text
    assert VISIBLE_EXCERPT in page.text
    assert VISIBLE_TITLE in page.text
    assert "待确认" in page.text
    assert "confirm-local" in page.text
    assert "reject-local" in page.text
    assert "consume_candidate" not in page.text
    assert HIDDEN_TITLE not in page.text
    assert HIDDEN_EXCERPT not in page.text
    assert body.status_code == 200
    assert body.json()["data"]["candidate_status"] == "pending"
    assert body.json()["data"]["method_question"] == METHOD_QUESTION
    assert _counts(engine)["candidates"] == before


def _render_reading(reading) -> str:
    payload = {
        "parent_run_id": reading.parent_run_id,
        "page_title": reading.page_title,
        "page_body": reading.page_body,
        "method_question": reading.method_question,
        "candidate_body": reading.candidate_body,
        "hits": [hit.excerpt for hit in reading.hits],
    }
    return json.dumps(payload, ensure_ascii=False)


def _traced(session: Session, callback, seen: list[str]):
    wrapped = session.connection().connection
    raw = getattr(wrapped, "dbapi_connection", None) or wrapped
    raw.set_trace_callback(seen.append)
    try:
        return callback()
    finally:
        raw.set_trace_callback(None)


def test_disabled_provider_stores_nothing(engine):
    _seed(engine)
    before = _counts(engine)
    with pytest.raises(FakeChainRejected):
        run_fake_chain(
            engine,
            Principal(READER, "synthetic-fake-session", 1),
            PROJECT,
            intent=INTENT,
            query=QUERY,
            page_version_id=VISIBLE_PAGE,
            now=NOW,
            provider=DisabledModelProvider(),
        )
    assert _counts(engine) == before


def test_unknown_account_stores_nothing(engine):
    _seed(engine)
    before = _counts(engine)
    with pytest.raises(AuthRequired):
        run_fake_chain(
            engine,
            Principal(MISSING, "synthetic-fake-missing", 1),
            PROJECT,
            intent=INTENT,
            query=QUERY,
            page_version_id=VISIBLE_PAGE,
            now=NOW,
        )
    assert _counts(engine) == before


def test_blank_intent_is_rejected_before_a_run(engine):
    _seed(engine)
    before = _counts(engine)
    with pytest.raises(ValueError, match="intent"):
        run_fake_chain(
            engine,
            Principal(READER, "synthetic-fake-session", 1),
            PROJECT,
            intent="   ",
            query=QUERY,
            page_version_id=None,
            now=NOW,
        )
    assert _counts(engine) == before


def _proposal(engine, candidate_id: str) -> dict:
    with Session(engine) as session:
        raw = session.scalar(
            select(Candidate.proposal_json).where(Candidate.id == candidate_id)
        )
    assert isinstance(raw, str)
    parsed = json.loads(raw)
    assert isinstance(parsed, dict)
    return parsed


def _counts(engine) -> dict[str, int]:
    with Session(engine) as session:
        return {
            "runs": int(session.scalar(select(func.count()).select_from(AgentRun)) or 0),
            "candidates": int(session.scalar(select(func.count()).select_from(Candidate)) or 0),
        }


def _seed(engine) -> None:
    with write_uow(engine) as session:
        session.add(
            Project(
                id=PROJECT,
                title="synthetic-fake-alpha",
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
                login_name="synthetic-fake-reader",
                display_name="synthetic-fake-reader",
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
                id="member-fake-reader",
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
                id="synthetic-fake-stage",
                project_id=PROJECT,
                head_config_version_id=None,
                created_at=NOW,
            )
        )
        session.flush()
        session.add(
            Artifact(
                id="synthetic-fake-art",
                project_id=PROJECT,
                title="synthetic-fake-art",
                created_at=NOW,
            )
        )
        session.flush()
        session.add(
            ArtifactVersion(
                id=HIT_VERSION,
                project_id=PROJECT,
                artifact_id="synthetic-fake-art",
                version_no=1,
                content_hash=hashlib.sha256(HIT_VERSION.encode()).hexdigest(),
                size_bytes=8,
                media_type="text/plain",
                object_key="synthetic/fake-hit",
                license="synthetic",
                sensitivity="project",
                ai_processing_allowed=0,
                project_revision=1,
                created_by_account_id=READER,
                created_at=NOW,
            )
        )
        for page_id, title in (
            ("synthetic-fake-p", VISIBLE_TITLE),
            ("synthetic-fake-h", HIDDEN_TITLE),
        ):
            session.add(
                Page(
                    id=page_id,
                    project_id=PROJECT,
                    stage_id="synthetic-fake-stage",
                    head_version_id=None,
                    status="active",
                    title=title,
                    created_at=NOW,
                )
            )
        session.flush()
        for version_id, page_id, body in (
            (VISIBLE_PAGE, "synthetic-fake-p", VISIBLE_BODY),
            (HIDDEN_PAGE, "synthetic-fake-h", HIDDEN_BODY),
        ):
            session.add(
                PageVersion(
                    id=version_id,
                    project_id=PROJECT,
                    page_id=page_id,
                    version_no=1,
                    parent_version_id=None,
                    content_json=json.dumps({"body": body}, ensure_ascii=False),
                    content_hash=hashlib.sha256(body.encode()).hexdigest(),
                    schema_version=1,
                    project_revision=1,
                    created_by_account_id=READER,
                    created_at=NOW,
                )
            )
        session.flush()
        for page_id, version_id in (
            ("synthetic-fake-p", VISIBLE_PAGE),
            ("synthetic-fake-h", HIDDEN_PAGE),
        ):
            page = session.get(Page, page_id)
            page.head_version_id = version_id
        session.add(
            EvidenceAnchor(
                id="synthetic-fake-hit-anchor",
                project_id=PROJECT,
                artifact_version_id=HIT_VERSION,
                locator_type="text_range",
                locator_json=json.dumps(
                    {"excerpt": VISIBLE_EXCERPT, "page": 1},
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
                excerpt_hash=hashlib.sha256(VISIBLE_EXCERPT.encode()).hexdigest(),
                extractor_version="synthetic-1",
                visibility="project",
                sensitivity="project",
                supersedes_id=None,
                schema_version=1,
                created_by_account_id=READER,
                created_at=NOW,
            )
        )
        session.add(
            EvidenceAnchor(
                id="synthetic-fake-hidden-anchor",
                project_id=PROJECT,
                artifact_version_id=HIT_VERSION,
                locator_type="text_range",
                locator_json=json.dumps(
                    {"excerpt": HIDDEN_EXCERPT},
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
                excerpt_hash=hashlib.sha256(HIDDEN_EXCERPT.encode()).hexdigest(),
                extractor_version="synthetic-1",
                visibility="project",
                sensitivity="project",
                supersedes_id=None,
                schema_version=1,
                created_by_account_id=READER,
                created_at=NOW,
            )
        )
        session.flush()
        _grant(session, "grant-fake-project", "project.read", "project", None, None)
        _grant(session, "grant-fake-page", "page.read", "page", "synthetic-fake-p", None)
        _grant(
            session,
            "grant-fake-evidence",
            "evidence.read",
            "evidence_anchor",
            None,
            "synthetic-fake-hit-anchor",
        )


def _grant(
    session: Session,
    grant_id: str,
    action: str,
    scope_type: str,
    page_id: str | None,
    evidence_anchor_id: str | None,
) -> None:
    session.add(
        ObjectGrant(
            id=grant_id,
            project_id=PROJECT,
            account_id=READER,
            action=action,
            scope_type=scope_type,
            status="active",
            created_at=NOW,
            revoked_at=None,
            created_by_account_id=READER,
            stage_id=None,
            page_id=page_id,
            page_version_id=None,
            artifact_id=None,
            artifact_version_id=None,
            evidence_anchor_id=evidence_anchor_id,
        )
    )
