"""Project creation and same-stage parallel pages. Titles stay on the synthetic- prefix."""

from threading import Barrier, Thread
from typing import Any

import pytest
from sqlalchemy import func, select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from sinuo.application.auth.acl import insert_grant
from sinuo.application.auth.authorizer import SessionAuthorizer
from sinuo.application.auth.receipts import execute_authorized_command
from sinuo.application.idempotency import CommandResponse, CommandScope, execute_idempotent
from sinuo.application.versions.pages import ProjectRevisionConflict, create_parallel_page
from sinuo.application.versions.projects import CreatedProject, create_project
from sinuo.domain.auth.principal import Principal
from sinuo.domain.auth.resource import ResourceRef, account_resource
from sinuo.domain.auth.roles import GrantTemplate
from sinuo.domain.errors import IdempotencyConflict
from sinuo.domain.public_contract import Action
from sinuo.domain.schema_types import canonical_document, document_hash
from sinuo.infrastructure.database import write_uow
from sinuo.infrastructure.domain_models import (
    Account,
    AuditEvent,
    Membership,
    ObjectGrant,
    Page,
    PageDraft,
    PageEdge,
    PageVersion,
    Project,
    Stage,
    StageConfigVersion,
)
from sinuo.infrastructure.models import IdempotencyRecord, OutboxEvent

ACCOUNT_ID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
SESSION_ID = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
NOW = 1_700_000_000.0
EMPTY_PAGE = {"schema_version": 1, "nodes": [], "edges": []}
PROJECT_KEY = "synthetic-project-key"
PAGE_KEY = "synthetic-page-key"


@pytest.fixture
def ready(engine):
    with write_uow(engine) as session:
        session.add(
            Account(
                id=ACCOUNT_ID,
                login_name="synthetic-creator",
                display_name="synthetic-creator",
                password_hash="$argon2id$synthetic-hash",
                status="active",
                auth_epoch=1,
                can_create_project=1,
                created_at=NOW,
                disabled_at=None,
            )
        )
    return engine


def _principal() -> Principal:
    return Principal(account_id=ACCOUNT_ID, session_id=SESSION_ID, auth_epoch=1)


def _project_request(title: str = "synthetic-project") -> dict[str, Any]:
    return {
        "brief": "synthetic-brief",
        "page_title": "synthetic-blank",
        "template_key": "synthetic-template",
        "title": title,
    }


def _project_scope(request_key: str) -> CommandScope:
    return CommandScope(ACCOUNT_ID, ACCOUNT_ID, Action.PROJECT_CREATE.value, request_key)


def _page_request(stage_id: str, title: str, expected_revision: int) -> dict[str, Any]:
    return {
        "expected_revision": expected_revision,
        "stage_id": stage_id,
        "title": title,
    }


def _page_scope(project_id: str, request_key: str) -> CommandScope:
    return CommandScope(project_id, ACCOUNT_ID, Action.PAGE_CREATE.value, request_key)


def _project_response(created: CreatedProject) -> CommandResponse:
    return CommandResponse(
        201,
        {
            "access_epoch": created.access_epoch,
            "audit_event_id": created.audit_event_id,
            "content_hash": created.content_hash,
            "grant_ids": [created.grant_id],
            "id": created.project_id,
            "membership_id": created.membership_id,
            "page_id": created.page_id,
            "page_version_id": created.page_version_id,
            "project_id": created.project_id,
            "project_revision": created.project_revision,
            "stage_config_version_id": created.stage_config_version_id,
            "stage_id": created.stage_id,
        },
    )


def _authorize_project(session: Session) -> None:
    SessionAuthorizer(session).authorize(
        Action.PROJECT_CREATE,
        account_resource(),
        _principal(),
    )


def _run_project(
    session: Session,
    body: dict[str, Any],
    *,
    before_audit=None,
) -> CommandResponse:
    created = create_project(
        session,
        account_id=ACCOUNT_ID,
        title=body["title"],
        brief=body["brief"],
        template_key=body["template_key"],
        page_title=body["page_title"],
        now=NOW,
        before_audit=before_audit,
    )
    return _project_response(created)


def _open_project(engine) -> dict[str, Any]:
    body = _project_request()
    with write_uow(engine) as session:
        response, replayed = execute_authorized_command(
            session,
            _project_scope(PROJECT_KEY),
            body,
            SessionAuthorizer(session),
            Action.PROJECT_CREATE,
            account_resource(),
            _principal(),
            lambda open_session: _run_project(open_session, body),
        )
    assert replayed is False
    return response.body


def _allow_parallel(engine, project_id: str) -> None:
    with write_uow(engine) as session:
        insert_grant(
            session,
            project_id=project_id,
            account_id=ACCOUNT_ID,
            spec=GrantTemplate(Action.PAGE_CREATE, "project"),
            created_by_account_id=ACCOUNT_ID,
            now=NOW,
        )


def _run_parallel(
    session: Session,
    project_id: str,
    body: dict[str, Any],
    *,
    before_audit=None,
) -> CommandResponse:
    created = create_parallel_page(
        session,
        project_id=project_id,
        stage_id=body["stage_id"],
        account_id=ACCOUNT_ID,
        title=body["title"],
        expected_revision=body["expected_revision"],
        now=NOW,
        before_audit=before_audit,
    )
    return CommandResponse(
        201,
        {
            "audit_event_id": created.audit_event_id,
            "content_hash": created.content_hash,
            "id": created.page_id,
            "page_id": created.page_id,
            "page_version_id": created.page_version_id,
            "project_id": created.project_id,
            "project_revision": created.project_revision,
            "stage_id": created.stage_id,
        },
    )


def _authorize_page(session: Session, project_id: str) -> None:
    SessionAuthorizer(session).authorize(
        Action.PAGE_CREATE,
        ResourceRef(project_id=project_id, scope_type="project"),
        _principal(),
    )


def _count(session: Session, model) -> int:
    counted = session.scalar(select(func.count()).select_from(model))
    assert isinstance(counted, int)
    return counted


def test_repeated_project_key_creates_one_blank_page(ready):
    body = _project_request()
    first: CommandResponse | None = None
    for _ in range(20):
        with write_uow(ready) as session:
            response, replayed = execute_authorized_command(
                session,
                _project_scope(PROJECT_KEY),
                body,
                SessionAuthorizer(session),
                Action.PROJECT_CREATE,
                account_resource(),
                _principal(),
                lambda open_session: _run_project(open_session, body),
            )
        if first is None:
            first = response
            assert replayed is False
        else:
            assert replayed is True
            assert response.body["page_id"] == first.body["page_id"]
            assert response.body["project_revision"] == 1
            assert response.body["access_epoch"] == 1
    assert first is not None
    empty_json = canonical_document(EMPTY_PAGE)
    empty_hash = document_hash(EMPTY_PAGE)
    with Session(ready) as session:
        assert _count(session, Project) == 1
        assert _count(session, Page) == 1
        assert _count(session, PageVersion) == 1
        assert _count(session, PageDraft) == 1
        assert _count(session, AuditEvent) == 1
        assert _count(session, OutboxEvent) == 1
        assert _count(session, IdempotencyRecord) == 1
        assert _count(session, PageEdge) == 0
        project = session.get(Project, first.body["project_id"])
        assert project is not None
        assert project.title == "synthetic-project"
        assert project.brief == "synthetic-brief"
        assert project.template_key == "synthetic-template"
        assert project.revision == 1
        assert project.access_epoch == 1
        assert project.id != ACCOUNT_ID
        stage = session.get(Stage, first.body["stage_id"])
        assert stage is not None
        assert stage.head_config_version_id == first.body["stage_config_version_id"]
        config = session.get(StageConfigVersion, first.body["stage_config_version_id"])
        assert config is not None
        assert config.version_no == 1
        assert config.position == 0
        assert config.effective_project_revision == 1
        assert config.schema_version == 1
        assert '"goal":"synthetic-project"' in config.goal_json
        page = session.get(Page, first.body["page_id"])
        assert page is not None
        assert page.title == "synthetic-blank"
        assert page.status == "active"
        assert page.head_version_id == first.body["page_version_id"]
        version = session.get(PageVersion, first.body["page_version_id"])
        assert version is not None
        assert version.version_no == 1
        assert version.parent_version_id is None
        assert version.content_json == empty_json
        assert version.content_hash == empty_hash
        assert version.schema_version == 1
        assert version.project_revision == 1
        assert version.created_by_account_id == ACCOUNT_ID
        draft = session.get(PageDraft, page.id)
        assert draft is not None
        assert draft.content_revision == 1
        assert draft.base_version_id == version.id
        assert draft.content_json == empty_json
        assert draft.content_hash == empty_hash
        membership = session.get(Membership, first.body["membership_id"])
        assert membership is not None
        assert membership.status == "active"
        assert membership.account_id == ACCOUNT_ID
        grant = session.get(ObjectGrant, first.body["grant_ids"][0])
        assert grant is not None
        assert grant.action == Action.ACL_MANAGE.value
        assert grant.scope_type == "project"
        assert grant.status == "active"
        assert grant.page_id is None
        audit = session.get(AuditEvent, first.body["audit_event_id"])
        assert audit is not None
        assert audit.command == Action.PROJECT_CREATE.value
        assert audit.actor_id == ACCOUNT_ID
        assert audit.project_revision == 1
        outbox = session.scalars(select(OutboxEvent)).one()
        assert outbox.event_type == "page.version_created"
        assert outbox.schema_version == 1
        assert outbox.payload["page_id"] == page.id
        assert outbox.payload["project_revision"] == 1
        assert "title" not in outbox.payload


def test_project_key_with_a_different_body_conflicts(ready):
    body = _project_request()
    with write_uow(ready) as session:
        execute_authorized_command(
            session,
            _project_scope(PROJECT_KEY),
            body,
            SessionAuthorizer(session),
            Action.PROJECT_CREATE,
            account_resource(),
            _principal(),
            lambda open_session: _run_project(open_session, body),
        )
    other = _project_request("synthetic-project-other")
    with pytest.raises(IdempotencyConflict):
        with write_uow(ready) as session:
            execute_authorized_command(
                session,
                _project_scope(PROJECT_KEY),
                other,
                SessionAuthorizer(session),
                Action.PROJECT_CREATE,
                account_resource(),
                _principal(),
                lambda open_session: _run_project(open_session, other),
            )
    with Session(ready) as session:
        assert _count(session, Project) == 1
        assert _count(session, Page) == 1
        stored = session.scalars(select(Project)).one()
        assert stored.title == "synthetic-project"


@pytest.mark.concurrency
def test_concurrent_project_key_creates_one_page(ready):
    barrier = Barrier(2)
    body = _project_request()
    page_ids: list[str] = []
    errors: list[BaseException] = []

    def attempt() -> None:
        barrier.wait(timeout=5)
        try:
            with write_uow(ready) as session:
                response, _replayed = execute_authorized_command(
                    session,
                    _project_scope(PROJECT_KEY),
                    body,
                    SessionAuthorizer(session),
                    Action.PROJECT_CREATE,
                    account_resource(),
                    _principal(),
                    lambda open_session: _run_project(open_session, body),
                )
            page_ids.append(response.body["page_id"])
        except BaseException as error:
            errors.append(error)

    first = Thread(target=attempt)
    second = Thread(target=attempt)
    first.start()
    second.start()
    first.join(timeout=10)
    second.join(timeout=10)
    assert not first.is_alive()
    assert not second.is_alive()
    assert errors == []
    assert len(page_ids) == 2
    assert page_ids[0] == page_ids[1]
    with Session(ready) as session:
        assert _count(session, Page) == 1
        assert _count(session, IdempotencyRecord) == 1


@pytest.mark.recovery
def test_project_failure_before_audit_drops_page_and_receipt(ready):
    body = _project_request()

    def handler(session: Session) -> CommandResponse:
        def before_audit() -> None:
            page = session.scalars(select(Page)).one()
            assert page.head_version_id is not None
            assert session.get(PageVersion, page.head_version_id) is not None
            assert _count(session, AuditEvent) == 0
            assert _count(session, OutboxEvent) == 0
            raise RuntimeError("injected")

        _run_project(session, body, before_audit=before_audit)
        raise AssertionError("audit must stay unwritten")

    with pytest.raises(RuntimeError, match="injected"):
        with write_uow(ready) as session:
            execute_idempotent(
                session,
                _project_scope(PROJECT_KEY),
                body,
                _authorize_project,
                handler,
            )
    with Session(ready) as session:
        assert _count(session, Page) == 0
        assert _count(session, Project) == 0
        assert _count(session, IdempotencyRecord) == 0
        assert _count(session, AuditEvent) == 0


def test_repeated_parallel_key_creates_one_page_without_next_edge(ready):
    opened = _open_project(ready)
    project_id = opened["project_id"]
    stage_id = opened["stage_id"]
    original_version_id = opened["page_version_id"]
    _allow_parallel(ready, project_id)
    body = _page_request(stage_id, "synthetic-parallel", 1)
    first: CommandResponse | None = None
    for _ in range(20):
        with write_uow(ready) as session:
            response = execute_idempotent(
                session,
                _page_scope(project_id, PAGE_KEY),
                body,
                lambda open_session: _authorize_page(open_session, project_id),
                lambda open_session: _run_parallel(open_session, project_id, body),
            )
        if first is None:
            first = response
        else:
            assert response.body["page_id"] == first.body["page_id"]
            assert response.body["project_revision"] == 2
    assert first is not None
    empty_json = canonical_document(EMPTY_PAGE)
    with Session(ready) as session:
        assert _count(session, Page) == 2
        assert _count(session, PageVersion) == 2
        assert _count(session, PageEdge) == 0
        next_edges = select(func.count()).select_from(PageEdge).where(PageEdge.kind == "next")
        assert session.scalar(next_edges) == 0
        project = session.get(Project, project_id)
        assert project is not None
        assert project.revision == 2
        assert project.access_epoch == 1
        original = session.get(PageVersion, original_version_id)
        assert original is not None
        assert original.project_revision == 1
        assert original.content_json == empty_json
        created = session.get(PageVersion, first.body["page_version_id"])
        assert created is not None
        assert created.version_no == 1
        assert created.parent_version_id is None
        assert created.project_revision == 2
        assert created.content_json == empty_json
        assert created.content_hash == document_hash(EMPTY_PAGE)
        draft = session.get(PageDraft, first.body["page_id"])
        assert draft is not None
        assert draft.content_revision == 1
        assert draft.base_version_id == created.id
        page = session.get(Page, first.body["page_id"])
        assert page is not None
        assert page.stage_id == stage_id
        assert page.title == "synthetic-parallel"
        assert page.head_version_id == created.id
        assert _count(session, Stage) == 1
        audits = session.scalars(
            select(AuditEvent).where(AuditEvent.command == Action.PAGE_CREATE.value)
        ).all()
        assert len(audits) == 1
        assert audits[0].project_revision == 2


def test_parallel_key_with_a_different_body_conflicts(ready):
    opened = _open_project(ready)
    project_id = opened["project_id"]
    _allow_parallel(ready, project_id)
    body = _page_request(opened["stage_id"], "synthetic-parallel", 1)
    with write_uow(ready) as session:
        execute_idempotent(
            session,
            _page_scope(project_id, PAGE_KEY),
            body,
            lambda open_session: _authorize_page(open_session, project_id),
            lambda open_session: _run_parallel(open_session, project_id, body),
        )
    other = _page_request(opened["stage_id"], "synthetic-parallel-other", 1)
    with pytest.raises(IdempotencyConflict):
        with write_uow(ready) as session:
            execute_idempotent(
                session,
                _page_scope(project_id, PAGE_KEY),
                other,
                lambda open_session: _authorize_page(open_session, project_id),
                lambda open_session: _run_parallel(open_session, project_id, other),
            )
    with Session(ready) as session:
        assert _count(session, Page) == 2
        titles = set(session.scalars(select(Page.title)).all())
        assert "synthetic-parallel-other" not in titles
        project = session.get(Project, project_id)
        assert project is not None
        assert project.revision == 2


@pytest.mark.concurrency
def test_concurrent_parallel_key_creates_one_page(ready):
    opened = _open_project(ready)
    project_id = opened["project_id"]
    _allow_parallel(ready, project_id)
    body = _page_request(opened["stage_id"], "synthetic-parallel", 1)
    barrier = Barrier(2)
    page_ids: list[str] = []
    errors: list[BaseException] = []

    def attempt() -> None:
        barrier.wait(timeout=5)
        try:
            with write_uow(ready) as session:
                response = execute_idempotent(
                    session,
                    _page_scope(project_id, PAGE_KEY),
                    body,
                    lambda open_session: _authorize_page(open_session, project_id),
                    lambda open_session: _run_parallel(open_session, project_id, body),
                )
            page_ids.append(response.body["page_id"])
        except BaseException as error:
            errors.append(error)

    first = Thread(target=attempt)
    second = Thread(target=attempt)
    first.start()
    second.start()
    first.join(timeout=10)
    second.join(timeout=10)
    assert not first.is_alive()
    assert not second.is_alive()
    assert errors == []
    assert page_ids[0] == page_ids[1]
    with Session(ready) as session:
        assert _count(session, Page) == 2
        project = session.get(Project, project_id)
        assert project is not None
        assert project.revision == 2
        assert project.access_epoch == 1


@pytest.mark.recovery
def test_parallel_failure_before_audit_drops_page_and_receipt(ready):
    opened = _open_project(ready)
    project_id = opened["project_id"]
    original_page_id = opened["page_id"]
    _allow_parallel(ready, project_id)
    body = _page_request(opened["stage_id"], "synthetic-parallel", 1)

    def handler(session: Session) -> CommandResponse:
        def before_audit() -> None:
            pages = session.scalars(select(Page)).all()
            assert len(pages) == 2
            created = next(page for page in pages if page.id != original_page_id)
            assert created.head_version_id is not None
            assert session.get(PageVersion, created.head_version_id) is not None
            revision = session.execute(
                text("SELECT revision, access_epoch FROM projects WHERE id = :id"),
                {"id": project_id},
            ).one()
            assert revision.revision == 2
            assert revision.access_epoch == 1
            page_audits = session.scalar(
                select(func.count())
                .select_from(AuditEvent)
                .where(AuditEvent.command == Action.PAGE_CREATE.value)
            )
            assert page_audits == 0
            assert _count(session, PageEdge) == 0
            raise RuntimeError("injected")

        _run_parallel(session, project_id, body, before_audit=before_audit)
        raise AssertionError("audit must stay unwritten")

    with pytest.raises(RuntimeError, match="injected"):
        with write_uow(ready) as session:
            execute_idempotent(
                session,
                _page_scope(project_id, PAGE_KEY),
                body,
                lambda open_session: _authorize_page(open_session, project_id),
                handler,
            )
    with Session(ready) as session:
        assert _count(session, Page) == 1
        assert session.get(Page, original_page_id) is not None
        project = session.get(Project, project_id)
        assert project is not None
        assert project.revision == 1
        assert project.access_epoch == 1
        assert (
            session.scalar(
                select(func.count())
                .select_from(IdempotencyRecord)
                .where(IdempotencyRecord.command == Action.PAGE_CREATE.value)
            )
            == 0
        )
        assert (
            session.scalar(
                select(func.count())
                .select_from(AuditEvent)
                .where(AuditEvent.command == Action.PAGE_CREATE.value)
            )
            == 0
        )


def test_stale_parallel_revision_rolls_back(ready):
    opened = _open_project(ready)
    project_id = opened["project_id"]
    _allow_parallel(ready, project_id)
    body = _page_request(opened["stage_id"], "synthetic-parallel", 4)
    with pytest.raises(ProjectRevisionConflict):
        with write_uow(ready) as session:
            execute_idempotent(
                session,
                _page_scope(project_id, PAGE_KEY),
                body,
                lambda open_session: _authorize_page(open_session, project_id),
                lambda open_session: _run_parallel(open_session, project_id, body),
            )
    with Session(ready) as session:
        assert _count(session, Page) == 1
        project = session.get(Project, project_id)
        assert project is not None
        assert project.revision == 1
        assert _count(session, IdempotencyRecord) == 1


def test_confirmed_page_version_content_cannot_be_updated(ready):
    opened = _open_project(ready)
    version_id = opened["page_version_id"]
    with pytest.raises(IntegrityError):
        with write_uow(ready) as session:
            session.execute(
                update(PageVersion)
                .where(PageVersion.id == version_id)
                .values(content_json='{"edges":[],"nodes":[],"schema_version":9}')
            )
            session.flush()
    with Session(ready) as session:
        version = session.get(PageVersion, version_id)
        assert version is not None
        assert version.content_json == canonical_document(EMPTY_PAGE)
        assert version.content_hash == document_hash(EMPTY_PAGE)
