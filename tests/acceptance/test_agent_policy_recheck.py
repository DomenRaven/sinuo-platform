"""Policy-run recheck. Synthetic rows only. No network and no live model."""

import hashlib
import json

import pytest
from sqlalchemy import event, func, select
from sqlalchemy.orm import Session

from sinuo.agents.contracts import SpecialistResult
from sinuo.application.agents import execute
from sinuo.application.agents.execute import (
    AgentRunRequest,
    PolicyRunRequest,
    RunPlanRejected,
    RunTaskSpec,
    finish_agent_run,
    model_call_is_outside_write,
    open_agent_run,
    open_policy_run,
)
from sinuo.application.retrieval.search import rebuild_evidence_index
from sinuo.domain.auth.principal import Principal
from sinuo.infrastructure.database import write_uow
from sinuo.infrastructure.domain_models import (
    Account,
    AgentRun,
    Artifact,
    ArtifactVersion,
    Candidate,
    ContextSnapshot,
    ContextSnapshotRef,
    Decision,
    EvidenceAnchor,
    Job,
    Membership,
    ObjectGrant,
    Page,
    PageVersion,
    Project,
    Stage,
    ViewState,
)
from sinuo.workers.suggestions import (
    consider_suggestion_event,
    poll_suggestion_digest,
    read_suggestion_preference,
    save_suggestion_preference,
)

NOW = 1_762_100_000.0
PROJECT = "synthetic-policy-recheck"
ACTOR = "11111111-1111-4111-8111-111111111111"
ADMIN = "22222222-2222-4222-8222-222222222222"
PASSWORD = "$argon2id$synthetic-policy"
PAGE = "synthetic-policy-page"
OLD = "synthetic-policy-page-v1"
NEW = "synthetic-policy-page-v2"
QUERY = "合成竹材范围"
QUESTION = "合成方法问题"
INTENT = "合成自动运行"
DECISION = "synthetic-policy-decision"
ALLOWED_VERSION = "synthetic-policy-allowed-v1"
DENIED_VERSION = "synthetic-policy-denied-v1"
ALLOWED_EXCERPT = "合成外发允许摘录"
DENIED_EXCERPT = "合成外发禁止摘录"
OUTBOUND_QUERY = "合成外发"
GRANT_RUN = "grant-policy-run"
GRANT_ADMIN_RUN = "grant-policy-admin-run"
SCOPE_OLD = {"page_version_id": OLD, "query": QUERY}
SCOPE_NEW = {"page_version_id": NEW, "query": QUERY}
SCOPE_QUERY = {"query": QUERY}


def test_policy_open_freezes_the_scope_version_not_the_head(engine):
    _seed(engine)
    revision = _save(engine, mode="event", scope=SCOPE_OLD, now=NOW)
    job_id = _queue(engine, "synthetic-policy-event-freeze", NOW + 1)
    provider = RecordingProvider()
    outcome, statements = _traced(
        engine,
        lambda: open_policy_run(
            engine,
            _actor(),
            _policy(job_id, (RunTaskSpec("critic", "critic"),), now=NOW + 2),
        ),
    )
    rendered = "\n".join(statements).lower()
    assert "head_version_id" not in rendered
    assert "view_states" not in rendered
    assert outcome.run is not None
    assert outcome.reason == "started"
    _assert_idle_job(engine, job_id, "succeeded")
    with Session(engine) as session:
        run = session.get(AgentRun, outcome.run.run_id)
        page = session.get(Page, PAGE)
        view = session.get(ViewState, "synthetic-policy-view")
        refs = session.scalars(
            select(ContextSnapshotRef.page_version_id).where(
                ContextSnapshotRef.snapshot_id == outcome.run.snapshot_id
            )
        ).all()
        stored = session.get(ContextSnapshot, outcome.run.snapshot_id)
        assert run is not None and page is not None and view is not None and stored is not None
        assert page.head_version_id == NEW
        assert NEW in view.layout_json
        assert refs == [OLD]
        assert json.loads(stored.authorization_scope_json)["page_version_id"] == OLD
        assert run.origin == "policy"
        assert run.preference_revision == revision
        assert run.execution_status == "running"
        assert run.accepting_results == 1
    finished = finish_agent_run(
        engine, _actor(), outcome.run.run_id, provider, now=NOW + 3
    )
    assert provider.calls == 1
    assert provider.outside == [True]
    assert finished.candidate_id is not None
    assert finished.execution_status == "completed"
    _assert_pending(engine, finished.candidate_id)
    _assert_budget_spent(engine, outcome.run.run_id, 1)
    manual = open_agent_run(
        engine,
        _actor(),
        PROJECT,
        _manual((RunTaskSpec("manual-critic", "critic"),), now=NOW + 4, page_version_id=NEW),
    )
    with Session(engine) as session:
        manual_run = session.get(AgentRun, manual.run_id)
        manual_refs = session.scalars(
            select(ContextSnapshotRef.page_version_id).where(
                ContextSnapshotRef.snapshot_id == manual.snapshot_id
            )
        ).all()
        assert manual_run is not None
        assert manual_run.origin == "manual"
        assert manual_run.preference_revision is None
        assert manual_refs == [NEW]
        assert session.get(Page, PAGE).head_version_id == NEW
        assert session.scalar(select(Project.revision).where(Project.id == PROJECT)) == 4
    again = open_policy_run(
        engine,
        _actor(),
        _policy(job_id, (RunTaskSpec("critic-again", "critic"),), now=NOW + 5),
    )
    assert again.run is None
    assert again.reason == "job"
    assert again.job_status == "succeeded"
    assert _run_count(engine) == 2


def test_missing_scope_page_freezes_none_while_head_exists(engine):
    _seed(engine)
    _save(engine, mode="event", scope=SCOPE_QUERY, now=NOW)
    job_id = _queue(engine, "synthetic-policy-event-none", NOW + 1)
    outcome = open_policy_run(
        engine,
        _actor(),
        _policy(job_id, (RunTaskSpec("critic-none", "critic"),), now=NOW + 2),
    )
    assert outcome.run is not None
    with Session(engine) as session:
        page = session.get(Page, PAGE)
        stored = session.get(ContextSnapshot, outcome.run.snapshot_id)
        refs = session.scalars(select(ContextSnapshotRef)).all()
        assert page is not None and stored is not None
        assert page.head_version_id == NEW
        assert json.loads(stored.authorization_scope_json)["page_version_id"] is None
        assert refs == []


def test_digest_mode_starts_a_policy_run(engine):
    _seed(engine)
    revision = _save(engine, mode="digest", scope=SCOPE_OLD, now=NOW)
    with write_uow(engine) as session:
        queued = poll_suggestion_digest(
            session, project_id=PROJECT, actor_id=ACTOR, now=NOW + 1
        )
    assert queued.created_job is True and queued.job_id is not None
    provider = RecordingProvider()
    outcome = open_policy_run(
        engine,
        _actor(),
        _policy(queued.job_id, (RunTaskSpec("digest-critic", "critic"),), now=NOW + 2),
    )
    assert outcome.run is not None
    finished = finish_agent_run(
        engine, _actor(), outcome.run.run_id, provider, now=NOW + 3
    )
    assert provider.calls == 1
    assert finished.candidate_id is not None
    with Session(engine) as session:
        run = session.get(AgentRun, outcome.run.run_id)
        assert run is not None
        assert run.origin == "policy"
        assert run.preference_revision == revision


def test_stale_policy_does_not_start_or_call_a_model(engine):
    _seed(engine)
    _save(engine, mode="event", scope=SCOPE_OLD, now=NOW)
    off_job = _queue(engine, "synthetic-policy-event-off", NOW + 1)
    _save(engine, mode="off", scope=SCOPE_OLD, now=NOW + 2)
    _assert_refused(engine, off_job, "superseded", "mode", NOW + 3)

    _save(engine, mode="event", scope=SCOPE_OLD, now=NOW + 4)
    manual_job = _queue(engine, "synthetic-policy-event-manual", NOW + 5)
    _save(engine, mode="manual", scope=SCOPE_OLD, now=NOW + 6)
    _assert_refused(engine, manual_job, "superseded", "mode", NOW + 7)

    _save(engine, mode="event", scope=SCOPE_OLD, now=NOW + 8)
    moved_job = _queue(engine, "synthetic-policy-event-revision", NOW + 9)
    _save(engine, mode="event", scope=SCOPE_OLD, now=NOW + 10)
    _assert_refused(engine, moved_job, "superseded", "revision", NOW + 11)

    _save(engine, mode="event", scope={"page_version_id": OLD}, now=NOW + 12)
    bare_job = _queue(engine, "synthetic-policy-event-bare", NOW + 13)
    _assert_refused(
        engine,
        bare_job,
        "superseded",
        "scope",
        NOW + 14,
        tasks=(RunTaskSpec("needs-query", "evidence"),),
    )
    assert _run_count(engine) == 0


def test_queued_job_after_revoke_or_disable_does_not_call_a_model(engine):
    _seed(engine)
    _save(engine, mode="event", scope=SCOPE_OLD, now=NOW)
    job_id = _queue(engine, "synthetic-policy-event-admin", NOW + 1)
    provider = RecordingProvider()
    admin = open_policy_run(
        engine,
        _admin(),
        _policy(job_id, (RunTaskSpec("admin-critic", "critic"),), now=NOW + 2),
    )
    _finish_if_opened(engine, _admin(), admin, provider, NOW + 2)
    assert admin.run is None
    assert admin.reason == "actor"
    assert provider.calls == 0
    _assert_idle_job(engine, job_id, "queued")

    with write_uow(engine) as session:
        grant = session.get(ObjectGrant, GRANT_RUN)
        assert grant is not None
        grant.status = "revoked"
        grant.revoked_at = NOW + 3
    revoked = open_policy_run(
        engine,
        _actor(),
        _policy(job_id, (RunTaskSpec("revoked-critic", "critic"),), now=NOW + 4),
    )
    _finish_if_opened(engine, _actor(), revoked, provider, NOW + 4)
    assert revoked.run is None
    assert revoked.reason == "grant"
    assert provider.calls == 0
    _assert_idle_job(engine, job_id, "cancelled")

    _restore_run_grant(engine)
    _save(engine, mode="event", scope=SCOPE_OLD, now=NOW + 5)
    disabled_job = _queue(engine, "synthetic-policy-event-disabled", NOW + 6)
    with write_uow(engine) as session:
        account = session.get(Account, ACTOR)
        assert account is not None
        account.status = "disabled"
        account.disabled_at = NOW + 7
    disabled = open_policy_run(
        engine,
        _actor(),
        _policy(disabled_job, (RunTaskSpec("disabled-critic", "critic"),), now=NOW + 8),
    )
    _finish_if_opened(engine, _actor(), disabled, provider, NOW + 8)
    assert disabled.run is None
    assert disabled.reason == "account"
    assert provider.calls == 0
    _assert_idle_job(engine, disabled_job, "cancelled")
    assert _run_count(engine) == 0


def test_forbidden_outbound_list_does_not_start(engine):
    _seed(engine)
    scope = {"query": OUTBOUND_QUERY, "artifact_version_ids": [DENIED_VERSION]}
    _save(engine, mode="event", scope=scope, now=NOW)
    job_id = _queue(engine, "synthetic-policy-event-outbound", NOW + 1)
    _assert_refused(engine, job_id, "superseded", "outbound", NOW + 2)
    assert _run_count(engine) == 0


def test_closing_policy_after_the_first_round_blocks_the_second(engine, monkeypatch):
    _seed(engine)
    before = _decision_facts(engine)
    revision = _save(engine, mode="event", scope=SCOPE_OLD, now=NOW)
    job_id = _queue(engine, "synthetic-policy-event-close", NOW + 1)
    outcome = open_policy_run(
        engine,
        _actor(),
        _policy(job_id, _clarify_then("evidence"), now=NOW + 2),
    )
    assert outcome.run is not None
    calls = _spy(monkeypatch)
    provider = MutatingProvider(
        engine, mode="off", scope=SCOPE_OLD, expected_revision=revision, now=NOW + 3
    )
    finished = finish_agent_run(
        engine, _actor(), outcome.run.run_id, provider, now=NOW + 4
    )
    assert provider.calls == 1
    assert provider.outside == [True]
    assert calls == {"search_evidence": 0, "lookup_method": 0, "get_page_snapshot": 0}
    assert finished.candidate_id is None
    assert finished.execution_status == "cancelled"
    assert _candidate_count(engine) == 0
    assert _decision_facts(engine) == before
    with Session(engine) as session:
        run = session.get(AgentRun, outcome.run.run_id)
        assert run is not None
        assert run.origin == "policy"
        assert run.preference_revision == revision
        assert run.accepting_results == 0
        assert run.execution_status == "cancelled"


def test_narrowing_scope_after_the_first_round_blocks_the_second(engine, monkeypatch):
    _seed(engine)
    before = _decision_facts(engine)
    revision = _save(engine, mode="event", scope=SCOPE_OLD, now=NOW)
    job_id = _queue(engine, "synthetic-policy-event-narrow", NOW + 1)
    outcome = open_policy_run(
        engine,
        _actor(),
        _policy(job_id, _clarify_then("method"), now=NOW + 2),
    )
    assert outcome.run is not None
    calls = _spy(monkeypatch)
    provider = MutatingProvider(
        engine, mode="event", scope=SCOPE_NEW, expected_revision=revision, now=NOW + 3
    )
    finished = finish_agent_run(
        engine, _actor(), outcome.run.run_id, provider, now=NOW + 4
    )
    assert provider.calls == 1
    assert calls == {"search_evidence": 0, "lookup_method": 0, "get_page_snapshot": 0}
    assert finished.candidate_id is None
    assert finished.execution_status == "cancelled"
    assert _candidate_count(engine) == 0
    assert _decision_facts(engine) == before
    with Session(engine) as session:
        view = read_suggestion_preference(session, project_id=PROJECT, actor_id=ACTOR)
        assert view.mode == "event"
        assert view.revision == revision + 1
        assert json.loads(view.scope_json)["page_version_id"] == NEW


def test_manual_run_stays_open_when_suggestions_turn_off(engine):
    _seed(engine)
    opened = open_agent_run(
        engine,
        _actor(),
        PROJECT,
        _manual((RunTaskSpec("manual-open", "critic"),), now=NOW, page_version_id=None),
    )
    _save(engine, mode="off", scope={}, now=NOW + 1)
    with Session(engine) as session:
        run = session.get(AgentRun, opened.run_id)
        assert run is not None
        assert run.origin == "manual"
        assert run.preference_revision is None
        assert run.execution_status == "running"
        assert run.accepting_results == 1
        assert session.scalar(select(Project.revision).where(Project.id == PROJECT)) == 4
    provider = RecordingProvider()
    finished = finish_agent_run(engine, _actor(), opened.run_id, provider, now=NOW + 2)
    assert provider.calls == 1
    assert provider.outside == [True]
    assert finished.execution_status == "completed"
    assert finished.candidate_id is not None
    _assert_pending(engine, finished.candidate_id)
    with Session(engine) as session:
        run = session.get(AgentRun, opened.run_id)
        assert run is not None
        assert run.origin == "manual"
        assert run.preference_revision is None
        assert run.accepting_results == 0


def test_manual_run_stops_the_model_when_its_grant_is_revoked(engine, monkeypatch):
    _seed(engine)
    opened = open_agent_run(
        engine,
        _actor(),
        PROJECT,
        _manual(_clarify_then("evidence"), now=NOW, page_version_id=OLD),
    )
    calls = _spy(monkeypatch)
    provider = RevokeGrantProvider(engine)
    finished = finish_agent_run(engine, _actor(), opened.run_id, provider, now=NOW + 1)
    assert provider.calls == 1
    assert calls == {"search_evidence": 0, "lookup_method": 0, "get_page_snapshot": 0}
    assert finished.candidate_id is None
    assert _candidate_count(engine) == 0
    with Session(engine) as session:
        run = session.get(AgentRun, opened.run_id)
        grant = session.get(ObjectGrant, GRANT_RUN)
        assert run is not None and grant is not None
        assert run.origin == "manual"
        assert grant.status == "revoked"
        assert grant.revoked_at is not None


def test_policy_context_omits_disallowed_excerpts(engine):
    _seed(engine)
    _save(engine, mode="event", scope={"query": OUTBOUND_QUERY}, now=NOW)
    job_id = _queue(engine, "synthetic-policy-event-excerpt", NOW + 1)
    policy_provider = RecordingProvider()
    outcome = open_policy_run(
        engine,
        _actor(),
        _policy(job_id, (RunTaskSpec("policy-evidence", "evidence"),), now=NOW + 2),
    )
    assert outcome.run is not None
    finish_agent_run(engine, _actor(), outcome.run.run_id, policy_provider, now=NOW + 3)
    assert policy_provider.calls == 1
    policy_context = "\n".join(policy_provider.contexts)
    assert ALLOWED_EXCERPT in policy_context
    assert DENIED_EXCERPT not in policy_context
    assert ALLOWED_VERSION in policy_context
    assert DENIED_VERSION not in policy_context

    manual_provider = RecordingProvider()
    manual = open_agent_run(
        engine,
        _actor(),
        PROJECT,
        _manual(
            (RunTaskSpec("manual-evidence", "evidence"),),
            now=NOW + 4,
            page_version_id=None,
            query=OUTBOUND_QUERY,
        ),
    )
    finish_agent_run(engine, _actor(), manual.run_id, manual_provider, now=NOW + 5)
    assert manual_provider.calls == 1
    manual_context = "\n".join(manual_provider.contexts)
    assert DENIED_EXCERPT in manual_context
    assert ALLOWED_EXCERPT in manual_context
    with Session(engine) as session:
        policy_run = session.get(AgentRun, outcome.run.run_id)
        manual_run = session.get(AgentRun, manual.run_id)
        assert policy_run is not None and manual_run is not None
        assert policy_run.origin == "policy"
        assert manual_run.origin == "manual"
        assert manual_run.preference_revision is None


def test_four_roles_and_a_cycle_are_rejected_before_a_run(engine):
    before = _run_count(engine)
    four = (
        RunTaskSpec("role-c", "clarification"),
        RunTaskSpec("role-e", "evidence"),
        RunTaskSpec("role-m", "method"),
        RunTaskSpec("role-k", "critic"),
    )
    cycle = (
        RunTaskSpec("cycle-e", "evidence", ("cycle-m",)),
        RunTaskSpec("cycle-m", "method", ("cycle-e",)),
    )
    with pytest.raises(RunPlanRejected) as roles:
        open_policy_run(engine, _actor(), _policy("missing-job", four, now=NOW))
    with pytest.raises(RunPlanRejected) as loop:
        open_policy_run(engine, _actor(), _policy("missing-job", cycle, now=NOW + 1))
    assert roles.value.code == "roles"
    assert loop.value.code == "cycle"
    assert _run_count(engine) == before


class RecordingProvider:
    def __init__(self):
        self.calls = 0
        self.contexts: list[str] = []
        self.outside: list[bool] = []

    async def execute(self, task, snapshot, upstream):
        del upstream
        self.calls += 1
        self.contexts.append(snapshot.context_json)
        self.outside.append(model_call_is_outside_write())
        body = json.dumps({"summary": "待确认"}, ensure_ascii=False)
        return SpecialistResult(task.task_id, "completed", body, role=task.role)


class MutatingProvider(RecordingProvider):
    def __init__(self, engine, *, mode: str, scope: dict, expected_revision: int, now: float):
        super().__init__()
        self.engine = engine
        self.mode = mode
        self.scope = scope
        self.expected_revision = expected_revision
        self.moment = now

    async def execute(self, task, snapshot, upstream):
        result = await super().execute(task, snapshot, upstream)
        if self.calls == 1:
            with write_uow(self.engine) as session:
                save_suggestion_preference(
                    session,
                    project_id=PROJECT,
                    actor_id=ACTOR,
                    expected_revision=self.expected_revision,
                    mode=self.mode,
                    scope=self.scope,
                    cooldown_seconds=0,
                    digest_interval_seconds=86400,
                    now=self.moment,
                )
        return result


class RevokeGrantProvider(RecordingProvider):
    def __init__(self, engine):
        super().__init__()
        self.engine = engine

    async def execute(self, task, snapshot, upstream):
        result = await super().execute(task, snapshot, upstream)
        if self.calls == 1:
            with write_uow(self.engine) as session:
                grant = session.get(ObjectGrant, GRANT_RUN)
                assert grant is not None
                grant.status = "revoked"
                grant.revoked_at = NOW + 30
        return result


def _actor() -> Principal:
    return Principal(ACTOR, "synthetic-policy-session", 1)


def _admin() -> Principal:
    return Principal(ADMIN, "synthetic-policy-admin", 1)


def _policy(job_id: str, tasks: tuple[RunTaskSpec, ...], *, now: float) -> PolicyRunRequest:
    return PolicyRunRequest(
        job_id=job_id,
        intent=INTENT,
        tasks=tasks,
        question=QUESTION,
        situation="家庭厨房",
        constraints={"consent": True},
        now=now,
    )


def _manual(
    tasks: tuple[RunTaskSpec, ...],
    *,
    now: float,
    page_version_id: str | None,
    query: str = QUERY,
) -> AgentRunRequest:
    return AgentRunRequest(
        intent=INTENT,
        tasks=tasks,
        query=query,
        question=QUESTION,
        situation="家庭厨房",
        constraints={"consent": True},
        page_version_id=page_version_id,
        now=now,
    )


def _clarify_then(role: str) -> tuple[RunTaskSpec, ...]:
    return (
        RunTaskSpec("clarify", "clarification"),
        RunTaskSpec(role, role, ("clarify",)),
    )


def _save(engine, *, mode: str, scope: dict, now: float) -> int:
    with write_uow(engine) as session:
        current = read_suggestion_preference(session, project_id=PROJECT, actor_id=ACTOR)
        saved = save_suggestion_preference(
            session,
            project_id=PROJECT,
            actor_id=ACTOR,
            expected_revision=current.revision,
            mode=mode,
            scope=scope,
            cooldown_seconds=0,
            digest_interval_seconds=86400,
            now=now,
        )
    return saved.revision


def _queue(engine, event_id: str, now: float) -> str:
    with write_uow(engine) as session:
        result = consider_suggestion_event(
            session,
            project_id=PROJECT,
            actor_id=ACTOR,
            event_id=event_id,
            event_type="page.version_created",
            now=now,
        )
    assert result.created_job is True
    assert result.job_id is not None
    return result.job_id


def _finish_if_opened(engine, principal, outcome, provider, now: float):
    if outcome.run is None:
        return None
    return finish_agent_run(engine, principal, outcome.run.run_id, provider, now=now)


def _assert_refused(engine, job_id: str, status: str, reason: str, now: float, tasks=None):
    chosen = (RunTaskSpec("refused-critic", "critic"),) if tasks is None else tasks
    provider = RecordingProvider()
    outcome = open_policy_run(engine, _actor(), _policy(job_id, chosen, now=now))
    _finish_if_opened(engine, _actor(), outcome, provider, now)
    assert outcome.run is None
    assert outcome.job_status == status
    assert outcome.reason == reason
    assert provider.calls == 0
    _assert_idle_job(engine, job_id, status)


def _assert_idle_job(engine, job_id: str, status: str) -> None:
    with Session(engine) as session:
        job = session.get(Job, job_id)
        assert job is not None
        assert job.status == status
        assert job.status != "running"
        assert job.lease_owner is None
        assert job.lease_until is None
        assert job.attempt_no == 0
        assert job.fencing_token == 0


def _assert_pending(engine, candidate_id: str) -> None:
    with Session(engine) as session:
        candidate = session.get(Candidate, candidate_id)
        assert candidate is not None
        assert candidate.status == "pending"
        assert candidate.revision == 1


def _assert_budget_spent(engine, run_id: str, spent: int) -> None:
    with Session(engine) as session:
        raw = session.scalar(select(AgentRun.budget_json).where(AgentRun.id == run_id))
    parsed = json.loads(raw)
    assert parsed["spent_model_calls"] == spent
    assert parsed["reserved_model_calls"] == 0


def _run_count(engine) -> int:
    with Session(engine) as session:
        return int(session.scalar(select(func.count()).select_from(AgentRun)) or 0)


def _candidate_count(engine) -> int:
    with Session(engine) as session:
        return int(session.scalar(select(func.count()).select_from(Candidate)) or 0)


def _decision_facts(engine):
    with Session(engine) as session:
        row = session.get(Decision, DECISION)
        revision = session.scalar(select(Project.revision).where(Project.id == PROJECT))
        assert row is not None
        return (
            row.id,
            row.project_id,
            row.account_id,
            row.body,
            row.reason,
            row.page_version_id,
            row.artifact_version_id,
            row.stage_id,
            row.supersedes_id,
            row.project_revision,
            row.created_at,
            revision,
        )


def _spy(monkeypatch) -> dict[str, int]:
    calls = {"search_evidence": 0, "lookup_method": 0, "get_page_snapshot": 0}

    def wrap(name: str, original):
        def inner(*args, **kwargs):
            calls[name] += 1
            return original(*args, **kwargs)

        return inner

    for name in calls:
        monkeypatch.setattr(execute, name, wrap(name, getattr(execute, name)))
    return calls


def _traced(engine, callback):
    seen: list[str] = []

    def record(_conn, _cursor, statement, _parameters, _context, _executemany) -> None:
        seen.append(statement)

    event.listen(engine, "before_cursor_execute", record)
    try:
        return callback(), seen
    finally:
        event.remove(engine, "before_cursor_execute", record)


def _restore_run_grant(engine) -> None:
    with write_uow(engine) as session:
        session.add(
            ObjectGrant(
                id="grant-policy-run-restored",
                project_id=PROJECT,
                account_id=ACTOR,
                action="run.create",
                scope_type="project",
                status="active",
                created_at=NOW + 4,
                revoked_at=None,
                created_by_account_id=ACTOR,
                stage_id=None,
                page_id=None,
                page_version_id=None,
                artifact_id=None,
                artifact_version_id=None,
                evidence_anchor_id=None,
            )
        )


def _seed(engine) -> None:
    with write_uow(engine) as session:
        session.add(
            Project(
                id=PROJECT,
                title="synthetic-policy",
                brief="synthetic",
                template_key=None,
                revision=4,
                access_epoch=1,
                created_at=NOW,
            )
        )
        for account_id, login_name, can_create in (
            (ACTOR, "synthetic-policy-actor", 0),
            (ADMIN, "synthetic-policy-admin", 1),
        ):
            session.add(
                Account(
                    id=account_id,
                    login_name=login_name,
                    display_name=login_name,
                    password_hash=PASSWORD,
                    status="active",
                    auth_epoch=1,
                    can_create_project=can_create,
                    created_at=NOW,
                    disabled_at=None,
                )
            )
        session.flush()
        for membership_id, account_id in (
            ("member-policy-actor", ACTOR),
            ("member-policy-admin", ADMIN),
        ):
            session.add(
                Membership(
                    id=membership_id,
                    project_id=PROJECT,
                    account_id=account_id,
                    status="active",
                    role_label=None,
                    created_at=NOW,
                    revoked_at=None,
                )
            )
        session.add(
            Stage(
                id="synthetic-policy-stage",
                project_id=PROJECT,
                head_config_version_id=None,
                created_at=NOW,
            )
        )
        session.flush()
        session.add(
            Page(
                id=PAGE,
                project_id=PROJECT,
                stage_id="synthetic-policy-stage",
                head_version_id=None,
                status="active",
                title="合成策略页",
                created_at=NOW,
            )
        )
        session.flush()
        _page_version(session, OLD, 1, None, "合成旧正文")
        session.flush()
        _page_version(session, NEW, 2, OLD, "合成新正文")
        session.flush()
        page = session.get(Page, PAGE)
        assert page is not None
        page.head_version_id = NEW
        layout = json.dumps({"page_version_id": NEW}, sort_keys=True, separators=(",", ":"))
        session.add(
            ViewState(
                id="synthetic-policy-view",
                account_id=ACTOR,
                project_id=PROJECT,
                page_id=PAGE,
                view_name="canvas",
                layout_revision=1,
                layout_hash=_sha(layout),
                layout_json=layout,
                updated_at=NOW,
            )
        )
        session.add(
            Artifact(
                id="synthetic-policy-art",
                project_id=PROJECT,
                title="synthetic-policy-art",
                created_at=NOW,
            )
        )
        session.flush()
        _artifact_version(session, ALLOWED_VERSION, 1, 1)
        _artifact_version(session, DENIED_VERSION, 2, 0)
        session.flush()
        _anchor(session, "synthetic-policy-allowed-anchor", ALLOWED_VERSION, ALLOWED_EXCERPT)
        _anchor(session, "synthetic-policy-denied-anchor", DENIED_VERSION, DENIED_EXCERPT)
        session.flush()
        _grant(session, "grant-policy-project", ACTOR, "project.read", "project")
        _grant(session, GRANT_RUN, ACTOR, "run.create", "project")
        _grant(session, GRANT_ADMIN_RUN, ADMIN, "run.create", "project")
        _grant(session, "grant-policy-page", ACTOR, "page.read", "page", page_id=PAGE)
        _grant(
            session,
            "grant-policy-allowed",
            ACTOR,
            "evidence.read",
            "evidence_anchor",
            evidence_anchor_id="synthetic-policy-allowed-anchor",
        )
        _grant(
            session,
            "grant-policy-denied",
            ACTOR,
            "evidence.read",
            "evidence_anchor",
            evidence_anchor_id="synthetic-policy-denied-anchor",
        )
        session.add(
            Decision(
                id=DECISION,
                project_id=PROJECT,
                account_id=ACTOR,
                body="合成已提交决定",
                reason="合成理由",
                page_version_id=OLD,
                artifact_version_id=None,
                stage_id=None,
                supersedes_id=None,
                project_revision=4,
                created_at=NOW,
            )
        )
        session.flush()
        rebuild_evidence_index(session)


def _page_version(session: Session, version_id: str, number: int, parent: str | None, body: str):
    content = json.dumps({"body": body}, ensure_ascii=False)
    session.add(
        PageVersion(
            id=version_id,
            project_id=PROJECT,
            page_id=PAGE,
            version_no=number,
            parent_version_id=parent,
            content_json=content,
            content_hash=_sha(content),
            schema_version=1,
            project_revision=4,
            created_by_account_id=ACTOR,
            created_at=NOW,
        )
    )


def _artifact_version(session: Session, version_id: str, number: int, allowed: int) -> None:
    session.add(
        ArtifactVersion(
            id=version_id,
            project_id=PROJECT,
            artifact_id="synthetic-policy-art",
            version_no=number,
            content_hash=_sha(version_id),
            size_bytes=8,
            media_type="text/plain",
            object_key=f"synthetic/{version_id}",
            license="synthetic",
            sensitivity="project",
            ai_processing_allowed=allowed,
            project_revision=4,
            created_by_account_id=ACTOR,
            created_at=NOW,
        )
    )


def _anchor(session: Session, anchor_id: str, version_id: str, excerpt: str) -> None:
    session.add(
        EvidenceAnchor(
            id=anchor_id,
            project_id=PROJECT,
            artifact_version_id=version_id,
            locator_type="text_range",
            locator_json=json.dumps(
                {"excerpt": excerpt}, ensure_ascii=False, separators=(",", ":")
            ),
            excerpt_hash=_sha(excerpt),
            extractor_version="synthetic-1",
            visibility="project",
            sensitivity="project",
            supersedes_id=None,
            schema_version=1,
            created_by_account_id=ACTOR,
            created_at=NOW,
        )
    )


def _grant(
    session: Session,
    grant_id: str,
    account_id: str,
    action: str,
    scope_type: str,
    *,
    page_id: str | None = None,
    evidence_anchor_id: str | None = None,
) -> None:
    session.add(
        ObjectGrant(
            id=grant_id,
            project_id=PROJECT,
            account_id=account_id,
            action=action,
            scope_type=scope_type,
            status="active",
            created_at=NOW,
            revoked_at=None,
            created_by_account_id=ACTOR,
            stage_id=None,
            page_id=page_id,
            page_version_id=None,
            artifact_id=None,
            artifact_version_id=None,
            evidence_anchor_id=evidence_anchor_id,
        )
    )


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()
