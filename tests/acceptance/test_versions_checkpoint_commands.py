"""Checkpoint idempotency and rollback. Uses the seeded domain database."""

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from sinuo.application.auth.authorizer import SessionAuthorizer
from sinuo.application.idempotency import CommandResponse, CommandScope, execute_idempotent
from sinuo.application.versions.branches import branch_from_version
from sinuo.application.versions.checkpoints import checkpoint_page
from sinuo.domain.auth.principal import Principal
from sinuo.domain.auth.resource import ResourceRef
from sinuo.domain.errors import IdempotencyConflict
from sinuo.domain.public_contract import Action
from sinuo.infrastructure.database import write_uow
from sinuo.infrastructure.domain_models import AuditEvent, PageVersion
from sinuo.infrastructure.models import IdempotencyRecord, OutboxEvent
from tests.acceptance.test_domain_schema import (
    ACCOUNT_A,
    NOW,
    PAGE_A,
    PROJECT_A,
    require_domain_head,
    seed,
)
from tests.acceptance.test_versions_history import _draft, _page, _project, _versions


@pytest.fixture
def ready(engine):
    require_domain_head(engine)
    with engine.begin() as connection:
        seed(connection)
    return engine


def _principal() -> Principal:
    return Principal(account_id=ACCOUNT_A, session_id="synthetic-session", auth_epoch=1)


def _allow_checkpoint(session: Session) -> None:
    SessionAuthorizer(session).authorize(
        Action.PAGE_CHECKPOINT,
        ResourceRef(project_id=PROJECT_A, scope_type="page", page_id=PAGE_A),
        _principal(),
    )


def _request(engine) -> dict:
    draft = _draft(engine)
    return {
        "page_id": PAGE_A,
        "expected_head_version_id": draft.base_version_id,
        "expected_content_revision": int(draft.content_revision),
        "expected_content_hash": draft.content_hash,
    }


def _grant_checkpoint(engine) -> None:
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO object_grants (
                    id, project_id, account_id, action, scope_type, status, created_at,
                    revoked_at, created_by_account_id, stage_id, page_id, page_version_id,
                    artifact_id, artifact_version_id, evidence_anchor_id
                ) VALUES (
                    'synthetic-grant-checkpoint', :project, :account, 'page.checkpoint',
                    'project', 'active', :now, NULL, :account,
                    NULL, NULL, NULL, NULL, NULL, NULL
                )
                """
            ),
            {"project": PROJECT_A, "account": ACCOUNT_A, "now": NOW},
        )


def _run(session: Session, request: dict, *, before_audit=None) -> CommandResponse:
    saved = checkpoint_page(
        session,
        project_id=PROJECT_A,
        page_id=PAGE_A,
        expected_head_version_id=request["expected_head_version_id"],
        expected_content_revision=request["expected_content_revision"],
        expected_content_hash=request["expected_content_hash"],
        actor_id=ACCOUNT_A,
        now=NOW + 9,
        before_audit=before_audit,
    )
    return CommandResponse(
        201,
        {
            "page_version_id": saved.page_version_id,
            "version_no": saved.version_no,
            "audit_event_id": saved.audit_event_id,
            "project_revision": saved.project_revision,
        },
    )


def _created_count(engine) -> int:
    with Session(engine) as session:
        counted = session.scalar(
            select(func.count())
            .select_from(OutboxEvent)
            .where(OutboxEvent.event_type == "page.version_created")
        )
    assert isinstance(counted, int)
    return counted


@pytest.mark.integration
def test_repeated_checkpoint_key_creates_one_version(ready):
    _grant_checkpoint(ready)
    request = _request(ready)
    before = len(_versions(ready, PAGE_A))
    scope = CommandScope(
        PROJECT_A,
        ACCOUNT_A,
        Action.PAGE_CHECKPOINT.value,
        "synthetic-checkpoint-20",
    )
    first = None
    for _ in range(20):
        with write_uow(ready) as session:
            response = execute_idempotent(
                session,
                scope,
                request,
                _allow_checkpoint,
                lambda open_session: _run(open_session, request),
            )
        if first is None:
            first = response
        else:
            assert response.body["page_version_id"] == first.body["page_version_id"]
    assert first is not None
    assert len(_versions(ready, PAGE_A)) == before + 1
    with Session(ready) as session:
        stored = session.get(
            IdempotencyRecord,
            (PROJECT_A, ACCOUNT_A, Action.PAGE_CHECKPOINT.value, "synthetic-checkpoint-20"),
        )
        assert stored is not None
        assert stored.response_json["page_version_id"] == first.body["page_version_id"]


@pytest.mark.integration
def test_checkpoint_key_with_a_different_body_conflicts(ready):
    _grant_checkpoint(ready)
    request = _request(ready)
    scope = CommandScope(
        PROJECT_A,
        ACCOUNT_A,
        Action.PAGE_CHECKPOINT.value,
        "synthetic-checkpoint-body",
    )
    with write_uow(ready) as session:
        execute_idempotent(
            session,
            scope,
            request,
            _allow_checkpoint,
            lambda open_session: _run(open_session, request),
        )
    other = dict(request)
    other["expected_content_hash"] = "f" * 64
    before = len(_versions(ready, PAGE_A))
    with pytest.raises(IdempotencyConflict):
        with write_uow(ready) as session:
            execute_idempotent(
                session,
                scope,
                other,
                _allow_checkpoint,
                lambda open_session: _run(open_session, other),
            )
    assert len(_versions(ready, PAGE_A)) == before


@pytest.mark.recovery
def test_checkpoint_failure_before_audit_rolls_back(ready):
    request = _request(ready)
    before_versions = _versions(ready, PAGE_A)
    before_head = _page(ready, PAGE_A).head_version_id
    before_revision = _project(ready).revision
    before_created = _created_count(ready)

    def handler(session: Session) -> CommandResponse:
        def before_audit() -> None:
            versions = session.scalar(
                select(func.count()).select_from(PageVersion).where(PageVersion.page_id == PAGE_A)
            )
            created = session.scalar(
                select(func.count())
                .select_from(OutboxEvent)
                .where(OutboxEvent.event_type == "page.version_created")
            )
            audits = session.scalar(
                select(func.count())
                .select_from(AuditEvent)
                .where(AuditEvent.command == Action.PAGE_CHECKPOINT.value)
            )
            assert versions == len(before_versions) + 1
            assert created == before_created
            assert audits == 0
            raise RuntimeError("injected")

        _run(session, request, before_audit=before_audit)
        raise AssertionError("audit must stay unwritten")

    with pytest.raises(RuntimeError, match="injected"):
        with write_uow(ready) as session:
            execute_idempotent(
                session,
                CommandScope(
                    PROJECT_A,
                    ACCOUNT_A,
                    Action.PAGE_CHECKPOINT.value,
                    "synthetic-checkpoint-fail",
                ),
                request,
                lambda _session: None,
                handler,
            )
    assert _versions(ready, PAGE_A) == before_versions
    assert _page(ready, PAGE_A).head_version_id == before_head
    assert _project(ready).revision == before_revision
    assert _created_count(ready) == before_created
    with Session(ready) as session:
        assert (
            session.get(
                IdempotencyRecord,
                (
                    PROJECT_A,
                    ACCOUNT_A,
                    Action.PAGE_CHECKPOINT.value,
                    "synthetic-checkpoint-fail",
                ),
            )
            is None
        )


def _allow_branch(session: Session) -> None:
    SessionAuthorizer(session).authorize(
        Action.PAGE_BRANCH,
        ResourceRef(project_id=PROJECT_A, scope_type="page", page_id=PAGE_A),
        _principal(),
    )


def _grant_branch(engine) -> None:
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO object_grants (
                    id, project_id, account_id, action, scope_type, status, created_at,
                    revoked_at, created_by_account_id, stage_id, page_id, page_version_id,
                    artifact_id, artifact_version_id, evidence_anchor_id
                ) VALUES (
                    'synthetic-grant-branch', :project, :account, 'page.branch',
                    'project', 'active', :now, NULL, :account,
                    NULL, NULL, NULL, NULL, NULL, NULL
                )
                """
            ),
            {"project": PROJECT_A, "account": ACCOUNT_A, "now": NOW},
        )


def _branch_request() -> dict:
    return {"source_version_id": "synthetic-pv-a2", "title": "synthetic-branch"}


def _run_branch(session: Session, request: dict, *, before_audit=None) -> CommandResponse:
    opened = branch_from_version(
        session,
        project_id=PROJECT_A,
        source_version_id=request["source_version_id"],
        actor_id=ACCOUNT_A,
        title=request["title"],
        now=NOW + 11,
        before_audit=before_audit,
    )
    return CommandResponse(201, {"page_id": opened.page_id, "edge_id": opened.edge_id})


@pytest.mark.integration
def test_repeated_branch_key_creates_one_page(ready):
    from sinuo.infrastructure.domain_models import Page

    _grant_branch(ready)
    request = _branch_request()
    with Session(ready) as session:
        before = session.scalar(select(func.count()).select_from(Page))
    scope = CommandScope(PROJECT_A, ACCOUNT_A, Action.PAGE_BRANCH.value, "synthetic-branch-20")
    first = None
    for _ in range(20):
        with write_uow(ready) as session:
            response = execute_idempotent(
                session,
                scope,
                request,
                _allow_branch,
                lambda open_session: _run_branch(open_session, request),
            )
        if first is None:
            first = response
        else:
            assert response.body["page_id"] == first.body["page_id"]
    assert first is not None
    with Session(ready) as session:
        after = session.scalar(select(func.count()).select_from(Page))
    assert after == before + 1


@pytest.mark.recovery
def test_branch_failure_before_audit_rolls_back(ready):
    from sinuo.infrastructure.domain_models import Page, PageEdge

    request = _branch_request()
    with Session(ready) as session:
        before_pages = session.scalar(select(func.count()).select_from(Page))
        before_edges = session.scalar(select(func.count()).select_from(PageEdge))

    def handler(session: Session) -> CommandResponse:
        def before_audit() -> None:
            pages = session.scalar(select(func.count()).select_from(Page))
            assert pages == before_pages + 1
            raise RuntimeError("injected")

        _run_branch(session, request, before_audit=before_audit)
        raise AssertionError("audit must stay unwritten")

    with pytest.raises(RuntimeError, match="injected"):
        with write_uow(ready) as session:
            execute_idempotent(
                session,
                CommandScope(
                    PROJECT_A,
                    ACCOUNT_A,
                    Action.PAGE_BRANCH.value,
                    "synthetic-branch-fail",
                ),
                request,
                lambda _session: None,
                handler,
            )
    with Session(ready) as session:
        assert session.scalar(select(func.count()).select_from(Page)) == before_pages
        assert session.scalar(select(func.count()).select_from(PageEdge)) == before_edges
        assert (
            session.get(
                IdempotencyRecord,
                (PROJECT_A, ACCOUNT_A, Action.PAGE_BRANCH.value, "synthetic-branch-fail"),
            )
            is None
        )
