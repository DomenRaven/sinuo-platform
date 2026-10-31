"""Acceptance coverage for local identity and object authorization.

HTTP cases call ``create_app`` directly. Routes are not mounted there yet, so those
cases keep the expected status instead of treating 404 as success.
"""

import json
import os
import subprocess
import sys
from pathlib import Path
from threading import Barrier, Event, Thread

import pytest
from argon2 import PasswordHasher
from argon2.low_level import Type
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import select, update
from sqlalchemy.orm import Session
from starlette.responses import Response

from sinuo.api.app import create_app
from sinuo.api.auth.routes import router as auth_router
from sinuo.api.auth.schemas import parse_access
from sinuo.application.auth.acl import commit_acl
from sinuo.application.auth.authorizer import SessionAuthorizer
from sinuo.application.auth.passwords import hash_password
from sinuo.application.auth.receipts import execute_authorized_command
from sinuo.application.auth.sessions import (
    LogoutStatus,
    RateLimited,
    authenticate_login,
    logout_session,
    resolve_principal,
    rotate_csrf,
)
from sinuo.application.idempotency import CommandScope, canonical_hash
from sinuo.domain.auth.cache import authorization_cache_key, cache_key_is_current
from sinuo.domain.auth.decide import Decision, GrantFact, decide
from sinuo.domain.auth.errors import (
    AccessEpochConflict,
    ActionForbidden,
    AuthRequired,
    InvalidSchema,
    NotFoundOrHidden,
)
from sinuo.domain.auth.principal import Principal
from sinuo.domain.auth.projection import project_receipt, project_visible_fields
from sinuo.domain.auth.resource import ResourceRef, account_resource
from sinuo.domain.auth.roles import creator_acl_grant, grants_for_template
from sinuo.domain.public_contract import Action, RoleTemplate
from sinuo.infrastructure.auth.cookies import attach_session_cookie
from sinuo.infrastructure.database import write_uow
from sinuo.infrastructure.domain_models import Account, Membership, Project
from sinuo.infrastructure.domain_models import Session as AccountSession
from sinuo.infrastructure.models import IdempotencyRecord, OutboxEvent
from tests.acceptance.identity_fixtures import (
    CITATION,
    EXCERPT,
    LEAD_ID,
    LEAD_PASSWORD,
    NOW,
    OTHER_ID,
    PAGE_A,
    PAGE_A_TITLE,
    PAGE_B,
    PAGE_B_TITLE,
    PROJECT_A,
    PROJECT_A_TITLE,
    PROJECT_B,
    PROJECT_B_TITLE,
    RELEASED_OBJECT_KEY,
    RELEASED_TITLE,
    REVIEWER_ID,
    SENSITIVE_OBJECT_KEY,
    SENSITIVE_TITLE,
    TEAM_ID,
    THUMBNAIL,
    IdentityWorld,
    SignedIn,
    identity_settings,
    production_settings,
    seed_identity_world,
)

ROOT = Path(__file__).resolve().parents[2]
LOCAL_ORIGIN = "http://127.0.0.1:8000"
PRODUCTION_ORIGIN = "https://sinuo.example"


def _principal(signed: SignedIn) -> Principal:
    return Principal(signed.account_id, signed.session_id, signed.auth_epoch)


def _page() -> ResourceRef:
    return ResourceRef(project_id=PROJECT_A, scope_type="page", page_id=PAGE_A)


def _world(engine, database_url: str) -> tuple[IdentityWorld, object]:
    settings = identity_settings(database_url)
    return seed_identity_world(engine, settings), settings


def _cookie_header(response: Response) -> str:
    header = response.headers.get("set-cookie")
    assert header is not None
    return header


def _replay_must_not_write(_session: Session):
    raise AssertionError("replay must not write a new body")


def _sensitive_version(world: IdentityWorld) -> ResourceRef:
    return ResourceRef(
        project_id=PROJECT_A,
        scope_type="artifact_version",
        artifact_version_id=world.sensitive_version_id,
    )


@pytest.mark.integration
def test_missing_expired_and_revoked_session_rejected(engine, database_url):
    world, _settings = _world(engine, database_url)
    with Session(engine) as session:
        with pytest.raises(AuthRequired):
            resolve_principal(session, None, now=world.now)
        with pytest.raises(AuthRequired):
            resolve_principal(session, "synthetic-not-a-token", now=world.now)
        row = session.get(AccountSession, world.team.session_id)
        assert row is not None
        alive = resolve_principal(session, world.team.token, now=row.idle_expires_at - 1)
        assert alive.account_id == TEAM_ID
        with pytest.raises(AuthRequired):
            resolve_principal(session, world.team.token, now=row.idle_expires_at)
        with pytest.raises(AuthRequired):
            resolve_principal(session, world.team.token, now=row.absolute_expires_at)
    with write_uow(engine) as session:
        reviewer = session.get(AccountSession, world.reviewer.session_id)
        reviewer.revoked_at = world.now
    with Session(engine) as session:
        with pytest.raises(AuthRequired):
            resolve_principal(session, world.reviewer.token, now=world.now + 1)
    with write_uow(engine) as session:
        result = session.execute(
            update(Account)
            .where(Account.id == LEAD_ID, Account.auth_epoch == 1)
            .values(auth_epoch=Account.auth_epoch + 1),
            execution_options={"synchronize_session": False},
        )
        assert result.rowcount == 1
    with Session(engine) as session:
        with pytest.raises(AuthRequired):
            resolve_principal(session, world.lead.token, now=world.now + 1)


@pytest.mark.integration
def test_login_principal_comes_from_server_session(engine, database_url):
    world, settings = _world(engine, database_url)
    result = authenticate_login(
        engine,
        settings,
        login_name="  SYNTHETIC-LEAD  ",
        password=LEAD_PASSWORD,
        origin=LOCAL_ORIGIN,
        source_host="127.0.0.1",
        now=world.now + 5,
    )
    assert result.principal.account_id == LEAD_ID
    assert result.principal.account_id != OTHER_ID
    with Session(engine) as session:
        resolved = resolve_principal(session, result.token, now=world.now + 5)
        assert resolved == result.principal
        still_team = resolve_principal(session, world.team.token, now=world.now + 5)
        assert still_team.account_id == TEAM_ID
    with pytest.raises(AuthRequired):
        authenticate_login(
            engine,
            settings,
            login_name="synthetic-lead",
            password="synthetic-wrong-pass",
            origin=LOCAL_ORIGIN,
            source_host="127.0.0.1",
            now=world.now + 6,
        )
    with pytest.raises(AuthRequired):
        authenticate_login(
            engine,
            settings,
            login_name="synthetic-nobody",
            password=LEAD_PASSWORD,
            origin=LOCAL_ORIGIN,
            source_host="127.0.0.1",
            now=world.now + 7,
        )


@pytest.mark.integration
def test_page_projection_strips_original_thumbnail_excerpt_citation_and_hidden_counts(
    engine, database_url
):
    world, _settings = _world(engine, database_url)
    reviewer = _principal(world.reviewer)
    lead = _principal(world.lead)
    rich = {
        "page_id": PAGE_A,
        "title": PAGE_A_TITLE,
        "artifacts": [
            {
                "id": world.sensitive_version_id,
                "title": SENSITIVE_TITLE,
                "object_key": SENSITIVE_OBJECT_KEY,
                "original_bytes": "synthetic-original-bytes",
                "thumbnail": THUMBNAIL,
                "sensitivity": "sensitive",
            },
            {
                "id": world.released_version_id,
                "title": RELEASED_TITLE,
                "object_key": RELEASED_OBJECT_KEY,
                "thumbnail": f"{RELEASED_OBJECT_KEY}#thumbnail",
                "preview_ref": "synthetic-preview-released",
                "sensitivity": "released",
            },
        ],
        "evidence": [
            {
                "id": world.evidence_id,
                "title": SENSITIVE_TITLE,
                "excerpt": EXCERPT,
                "locator": {"marker": CITATION},
                "sensitivity": "sensitive",
            }
        ],
        "sensitive_count": 17,
        "hidden_count": 17,
        "total": 17,
    }
    with Session(engine) as session:
        authorizer = SessionAuthorizer(session)
        authorizer.authorize(Action.PAGE_READ, _page(), reviewer)
        with pytest.raises((ActionForbidden, NotFoundOrHidden)):
            authorizer.authorize(Action.ARTIFACT_ORIGINAL_READ, _sensitive_version(world), reviewer)
        projected = project_visible_fields(
            rich,
            can_read_original=False,
            can_read_evidence=False,
            can_preview=True,
        )
        from sinuo.application.auth.views import project_page_projection

        reviewer_view = project_page_projection(
            session, principal=reviewer, url_project_id=PROJECT_A, page_id=PAGE_A
        )
        lead_view = project_page_projection(
            session, principal=lead, url_project_id=PROJECT_A, page_id=PAGE_A
        )
    encoded = json.dumps(projected)
    reviewer_encoded = json.dumps(reviewer_view)
    lead_encoded = json.dumps(lead_view)
    for blob in (encoded, reviewer_encoded):
        assert SENSITIVE_TITLE not in blob
        assert SENSITIVE_OBJECT_KEY not in blob
        assert THUMBNAIL not in blob
        assert EXCERPT not in blob
        assert CITATION not in blob
        assert "synthetic-original-bytes" not in blob
        assert "hidden_count" not in blob
        assert "sensitive_count" not in blob
        assert '"total"' not in blob
    assert projected["title"] == PAGE_A_TITLE
    assert reviewer_view["title"] == PAGE_A_TITLE
    assert RELEASED_TITLE in encoded
    assert "synthetic-preview-released" in reviewer_encoded
    assert RELEASED_OBJECT_KEY not in reviewer_encoded
    assert SENSITIVE_OBJECT_KEY in lead_encoded
    assert CITATION in lead_encoded


@pytest.mark.integration
def test_foreign_project_id_in_authorized_url_hides_title_and_count(engine, database_url):
    world, _settings = _world(engine, database_url)
    lead = _principal(world.lead)
    from sinuo.application.auth.views import read_bound_object

    with Session(engine) as session:
        with pytest.raises(NotFoundOrHidden) as foreign_page:
            read_bound_object(
                session,
                principal=lead,
                url_project_id=PROJECT_A,
                object_id=PAGE_B,
                object_kind="page",
            )
        with pytest.raises(NotFoundOrHidden) as missing_page:
            read_bound_object(
                session,
                principal=lead,
                url_project_id=PROJECT_A,
                object_id="synthetic-missing-page",
                object_kind="page",
            )
        with pytest.raises(NotFoundOrHidden) as foreign_artifact:
            read_bound_object(
                session,
                principal=lead,
                url_project_id=PROJECT_A,
                object_id=world.foreign_artifact_id,
                object_kind="artifact",
            )
        with pytest.raises(NotFoundOrHidden) as foreign_evidence:
            read_bound_object(
                session,
                principal=lead,
                url_project_id=PROJECT_A,
                object_id=world.foreign_evidence_id,
                object_kind="evidence_anchor",
            )
        visible = read_bound_object(
            session,
            principal=lead,
            url_project_id=PROJECT_A,
            object_id=PAGE_A,
            object_kind="page",
        )
    assert foreign_page.value.args == missing_page.value.args == ()
    assert foreign_artifact.value.args == foreign_evidence.value.args == ()
    for caught in (foreign_page, foreign_artifact, foreign_evidence):
        rendered = repr(caught.value)
        assert PAGE_B_TITLE not in rendered
        assert PROJECT_B_TITLE not in rendered
        assert CITATION not in rendered
        assert "count" not in rendered
    assert visible == {"id": PAGE_A, "title": PAGE_A_TITLE}
    assert "count" not in visible
    assert PAGE_B_TITLE not in json.dumps(visible)


@pytest.mark.integration
def test_cookie_write_rejects_missing_or_wrong_csrf_and_bad_origin(engine, database_url):
    world, settings = _world(engine, database_url)
    with write_uow(engine) as session:
        csrf_token = rotate_csrf(session, _principal(world.lead))
    with write_uow(engine) as session:
        with pytest.raises(ActionForbidden) as missing:
            logout_session(
                session,
                token=world.lead.token,
                origin=LOCAL_ORIGIN,
                csrf_token=None,
                allowed_origins=settings.parsed_allowed_origins(),
                now=world.now + 2,
            )
        with pytest.raises(ActionForbidden) as wrong:
            logout_session(
                session,
                token=world.lead.token,
                origin=LOCAL_ORIGIN,
                csrf_token="synthetic-wrong-csrf",
                allowed_origins=settings.parsed_allowed_origins(),
                now=world.now + 2,
            )
        with pytest.raises(ActionForbidden) as origin:
            logout_session(
                session,
                token=world.lead.token,
                origin="https://evil.example",
                csrf_token=csrf_token,
                allowed_origins=settings.parsed_allowed_origins(),
                now=world.now + 2,
            )
        assert resolve_principal(session, world.lead.token, now=world.now + 2).account_id == LEAD_ID
    assert missing.value.category == wrong.value.category == "csrf"
    assert origin.value.category == "origin"
    with Session(engine) as session:
        row = session.get(AccountSession, world.lead.session_id)
        assert row.revoked_at is None


@pytest.mark.integration
def test_logout_revokes_session_and_repeat_is_safe(engine, database_url):
    world, settings = _world(engine, database_url)
    with write_uow(engine) as session:
        csrf_token = rotate_csrf(session, _principal(world.lead))
    with write_uow(engine) as session:
        status = logout_session(
            session,
            token=world.lead.token,
            origin=LOCAL_ORIGIN,
            csrf_token=csrf_token,
            allowed_origins=settings.parsed_allowed_origins(),
            now=world.now + 3,
        )
        assert status is LogoutStatus.REVOKED
    with write_uow(engine) as session:
        repeated = logout_session(
            session,
            token=world.lead.token,
            origin=LOCAL_ORIGIN,
            csrf_token=csrf_token,
            allowed_origins=settings.parsed_allowed_origins(),
            now=world.now + 4,
        )
        assert repeated is LogoutStatus.ABSENT
        absent = logout_session(
            session,
            token=None,
            origin=None,
            csrf_token=None,
            allowed_origins=settings.parsed_allowed_origins(),
            now=world.now + 4,
        )
        assert absent is LogoutStatus.ABSENT
        assert resolve_principal(session, world.team.token, now=world.now + 4).account_id == TEAM_ID
        with pytest.raises(AuthRequired):
            resolve_principal(session, world.lead.token, now=world.now + 4)


@pytest.mark.integration
def test_only_acl_manage_can_grant_and_body_role_is_rejected(engine, database_url):
    world, _settings = _world(engine, database_url)
    for template, pages in (
        (RoleTemplate.LEAD_DESIGNER, ()),
        (RoleTemplate.TEAM_MEMBER, (PAGE_A,)),
        (RoleTemplate.REVIEWER, (PAGE_A,)),
    ):
        actions = {item.action for item in grants_for_template(template, pages)}
        assert Action.PROJECT_CREATE not in actions
        assert Action.ACL_MANAGE not in actions
        assert Action.PRIVACY_DELETE not in actions
    assert creator_acl_grant().action is Action.ACL_MANAGE
    with pytest.raises(InvalidSchema):
        parse_access(
            {
                "expected_access_epoch": 1,
                "subject_account_id": TEAM_ID,
                "role_template": "team_member",
                "page_ids": [PAGE_A],
                "role": "lead_designer",
                "actor_id": LEAD_ID,
                "allowed_tools": ["acl.manage"],
            }
        )
    with Session(engine) as session:
        authorizer = SessionAuthorizer(session)
        project = ResourceRef(project_id=PROJECT_A, scope_type="project")
        with pytest.raises(ActionForbidden):
            authorizer.authorize(Action.ACL_MANAGE, project, _principal(world.team))
        with pytest.raises(NotFoundOrHidden):
            authorizer.authorize(Action.ACL_MANAGE, project, _principal(world.other))
    with write_uow(engine) as session:
        with pytest.raises(ActionForbidden):
            commit_acl(
                session,
                principal=_principal(world.team),
                project_id=PROJECT_A,
                expected_access_epoch=1,
                subject_account_id=TEAM_ID,
                role_template=RoleTemplate.TEAM_MEMBER,
                page_ids=(PAGE_A,),
                membership_status=None,
                revoke_grant_ids=(),
                now=world.now,
            )
    with Session(engine) as session:
        project = session.get(Project, PROJECT_A)
        assert project.access_epoch == 1
        assert project.revision == 1
        assert project.title == PROJECT_A_TITLE


@pytest.mark.integration
def test_revocation_blocks_session_receipt_replay_and_auth_cache(engine, database_url):
    world, _settings = _world(engine, database_url)
    reviewer = _principal(world.reviewer)
    request = {"page_id": PAGE_A}
    old_key = authorization_cache_key(REVIEWER_ID, PROJECT_A, world.sensitive_version_id, 1)
    with write_uow(engine) as session:
        session.add(
            IdempotencyRecord(
                project_id=PROJECT_A,
                actor_id=REVIEWER_ID,
                command=Action.PAGE_READ.value,
                request_key="synthetic-replay",
                request_hash=canonical_hash(request),
                response_status=200,
                response_json={
                    "id": PAGE_A,
                    "title": SENSITIVE_TITLE,
                    "thumbnail": THUMBNAIL,
                    "excerpt": EXCERPT,
                    "count": 17,
                    "access_epoch": 1,
                },
                created_at=world.now,
            )
        )
    with write_uow(engine) as session:
        response, replayed = execute_authorized_command(
            session,
            CommandScope(PROJECT_A, REVIEWER_ID, Action.PAGE_READ.value, "synthetic-replay"),
            request,
            SessionAuthorizer(session),
            Action.PAGE_READ,
            _page(),
            reviewer,
            _replay_must_not_write,
        )
        assert replayed is True
        assert response.body["id"] == PAGE_A
        assert response.body["access_epoch"] == 1
        assert SENSITIVE_TITLE not in json.dumps(response.body)
        assert THUMBNAIL not in json.dumps(response.body)
        assert EXCERPT not in json.dumps(response.body)
        assert "count" not in response.body
    with write_uow(engine) as session:
        with pytest.raises(AccessEpochConflict):
            commit_acl(
                session,
                principal=_principal(world.lead),
                project_id=PROJECT_A,
                expected_access_epoch=99,
                subject_account_id=REVIEWER_ID,
                role_template=None,
                page_ids=(),
                membership_status=None,
                revoke_grant_ids=world.reviewer_page_grant_ids,
                now=world.now,
            )
    with write_uow(engine) as session:
        committed = commit_acl(
            session,
            principal=_principal(world.lead),
            project_id=PROJECT_A,
            expected_access_epoch=1,
            subject_account_id=REVIEWER_ID,
            role_template=None,
            page_ids=(),
            membership_status=None,
            revoke_grant_ids=world.reviewer_page_grant_ids,
            now=world.now,
        )
        assert committed.access_epoch == 2
        assert committed.project_revision == 1
    with Session(engine) as session:
        project = session.get(Project, PROJECT_A)
        assert project.access_epoch == 2
        assert project.revision == 1
        assert project.title == PROJECT_A_TITLE
        resolved = resolve_principal(session, world.reviewer.token, now=world.now + 5)
        assert resolved.account_id == REVIEWER_ID
        with pytest.raises(NotFoundOrHidden):
            execute_authorized_command(
                session,
                CommandScope(PROJECT_A, REVIEWER_ID, Action.PAGE_READ.value, "synthetic-replay"),
                request,
                SessionAuthorizer(session),
                Action.PAGE_READ,
                _page(),
                reviewer,
                _replay_must_not_write,
            )
        stored = session.get(
            IdempotencyRecord,
            (PROJECT_A, REVIEWER_ID, Action.PAGE_READ.value, "synthetic-replay"),
        )
        assert stored.response_json["title"] == SENSITIVE_TITLE
        stripped = project_receipt(stored.response_json)
        assert stripped["id"] == PAGE_A
        assert SENSITIVE_TITLE not in json.dumps(stripped)
        assert cache_key_is_current(
            old_key,
            account_id=REVIEWER_ID,
            project_id=PROJECT_A,
            object_version_id=world.sensitive_version_id,
            access_epoch=project.access_epoch,
        ) is False
        current_key = authorization_cache_key(
            REVIEWER_ID,
            PROJECT_A,
            world.sensitive_version_id,
            project.access_epoch,
        )
        assert cache_key_is_current(
            current_key,
            account_id=REVIEWER_ID,
            project_id=PROJECT_A,
            object_version_id=world.sensitive_version_id,
            access_epoch=project.access_epoch,
        ) is True
        payloads = session.scalars(
            select(OutboxEvent.payload).where(OutboxEvent.event_type == "project.access_changed")
        ).all()
        assert payloads
        encoded = json.dumps(payloads)
        assert PROJECT_A_TITLE not in encoded
        assert SENSITIVE_TITLE not in encoded
        assert "password" not in encoded
        assert "cache" not in vars(SessionAuthorizer(session))


@pytest.mark.integration
def test_login_and_logout_do_not_write_idempotency_records(engine, database_url):
    world, settings = _world(engine, database_url)

    def count() -> int:
        with Session(engine) as session:
            return len(session.scalars(select(IdempotencyRecord)).all())

    assert count() == 0
    result = authenticate_login(
        engine,
        settings,
        login_name="synthetic-lead",
        password=LEAD_PASSWORD,
        origin=LOCAL_ORIGIN,
        source_host="127.0.0.1",
        now=world.now + 8,
    )
    with write_uow(engine) as session:
        csrf_token = rotate_csrf(session, result.principal)
    assert count() == 0
    with write_uow(engine) as session:
        logout_session(
            session,
            token=result.token,
            origin=LOCAL_ORIGIN,
            csrf_token=csrf_token,
            allowed_origins=settings.parsed_allowed_origins(),
            now=world.now + 9,
        )
    assert count() == 0


@pytest.mark.integration
def test_role_label_without_grants_does_not_authorize(engine, database_url):
    world, _settings = _world(engine, database_url)
    with write_uow(engine) as session:
        session.add(
            Membership(
                id="synthetic-label-only",
                project_id=PROJECT_A,
                account_id=OTHER_ID,
                status="active",
                role_label="lead_designer",
                created_at=world.now,
                revoked_at=None,
            )
        )
    with Session(engine) as session:
        authorizer = SessionAuthorizer(session)
        project = ResourceRef(project_id=PROJECT_A, scope_type="project")
        with pytest.raises(ActionForbidden):
            authorizer.authorize(Action.PROJECT_READ, project, _principal(world.other))
        with pytest.raises(NotFoundOrHidden):
            authorizer.authorize(Action.PAGE_READ, _page(), _principal(world.other))
        with pytest.raises(NotFoundOrHidden):
            authorizer.authorize(
                Action.ARTIFACT_ORIGINAL_READ,
                _sensitive_version(world),
                _principal(world.other),
            )


def test_decide_hides_unassigned_sensitive_preview_and_cross_project_url():
    principal = Principal(LEAD_ID, "synthetic-session", 1)
    project = ResourceRef(project_id=PROJECT_A, scope_type="project")
    project_read = (GrantFact(Action.PROJECT_READ.value, "project", "active"),)
    common = {
        "account_status": "active",
        "can_create_project": False,
        "membership_status": "active",
    }
    allowed_project = decide(
        Action.PROJECT_READ, project, principal, grants=project_read, **common
    )
    assert allowed_project is Decision.ALLOW
    hidden_page = decide(Action.PAGE_READ, _page(), principal, grants=project_read, **common)
    assert hidden_page is Decision.HIDDEN
    hidden = decide(
        Action.PAGE_READ,
        _page(),
        principal,
        grants=(),
        membership_status=None,
        account_status="active",
        can_create_project=False,
    )
    assert hidden is Decision.HIDDEN
    preview = (GrantFact(Action.PREVIEW_READ.value, "project", "active"),)
    sensitive = ResourceRef(
        project_id=PROJECT_A,
        scope_type="artifact_version",
        artifact_version_id="synthetic-version",
        sensitivity="sensitive",
    )
    forbidden = decide(Action.PREVIEW_READ, sensitive, principal, grants=preview, **common)
    assert forbidden is Decision.FORBIDDEN
    original = (GrantFact(Action.ARTIFACT_ORIGINAL_READ.value, "project", "active"),)
    allowed = decide(Action.ARTIFACT_ORIGINAL_READ, sensitive, principal, grants=original, **common)
    assert allowed is Decision.ALLOW
    opinion = ResourceRef(
        project_id=PROJECT_A,
        scope_type="page",
        page_id=PAGE_A,
        author_account_id=OTHER_ID,
    )
    own = (GrantFact(Action.OPINION_EDIT_OWN.value, "page", "active", page_id=PAGE_A),)
    edited = decide(Action.OPINION_EDIT_OWN, opinion, principal, grants=own, **common)
    assert edited is Decision.FORBIDDEN
    refused = decide(
        Action.PROJECT_CREATE,
        account_resource(),
        principal,
        account_status="active",
        can_create_project=False,
        membership_status=None,
    )
    assert refused is Decision.FORBIDDEN
    created = decide(
        Action.PROJECT_CREATE,
        account_resource(),
        principal,
        account_status="active",
        can_create_project=True,
        membership_status=None,
    )
    assert created is Decision.ALLOW
    crossed = decide(
        Action.PAGE_READ,
        _page(),
        principal,
        grants=own,
        url_project_id=PROJECT_B,
        **common,
    )
    assert crossed is Decision.HIDDEN


def test_http_cookie_is_host_only_httponly_lax_without_host_prefix(database_url):
    response = Response()
    attach_session_cookie(
        response,
        identity_settings(database_url),
        "synthetic-token",
        1_761_800_100.0,
        1_761_800_000.0,
    )
    header = _cookie_header(response).lower()
    assert header.startswith("sinuo_session=")
    assert "httponly" in header
    assert "samesite=lax" in header
    assert "path=/" in header
    assert "domain=" not in header
    assert "secure" not in header
    assert "__host-" not in header


def test_https_cookie_uses_host_prefix_secure_and_path_root(database_url):
    response = Response()
    attach_session_cookie(
        response,
        production_settings(database_url),
        "synthetic-token",
        1_761_800_100.0,
        1_761_800_000.0,
    )
    header = _cookie_header(response)
    assert header.startswith("__Host-sinuo_session=")
    lowered = header.lower()
    assert "secure" in lowered
    assert "httponly" in lowered
    assert "samesite=lax" in lowered
    assert "path=/" in lowered
    assert "domain=" not in lowered


def test_auth_responses_have_no_wildcard_cors(database_url):
    app = create_app(production_settings(database_url))
    assert app.dependency_overrides == {}
    with TestClient(app) as client:
        responses = [client.get("/healthz"), client.get("/api/v1/session")]
    for response in responses:
        assert response.headers.get("access-control-allow-origin") != "*"
    assert not any("CORSMiddleware" in str(item) for item in app.user_middleware)


def test_bootstrap_refuses_password_on_command_line(tmp_path):
    database = tmp_path / "bootstrap.db"
    env = os.environ.copy()
    env["PYTHONPATH"] = os.pathsep.join([str(ROOT / "src"), env.get("PYTHONPATH", "")])
    env["SINUO_DATABASE_URL"] = f"sqlite:///{database.as_posix()}"
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "sinuo.application.auth.bootstrap",
            "--password",
            "synthetic-password-value",
        ],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 2
    assert "synthetic-password-value" not in completed.stdout
    assert "synthetic-password-value" not in completed.stderr
    assert not database.exists()


def test_production_create_app_registers_no_pytest_identity_override(database_url):
    app = create_app(production_settings(database_url))
    assert app.dependency_overrides == {}
    with TestClient(app) as client:
        assert client.get("/healthz").status_code == 200
    assert app.dependency_overrides == {}


def test_no_dev_login_route_on_production_app(database_url):
    app = create_app(production_settings(database_url))
    paths = {getattr(route, "path", "") for route in app.routes}
    auth_paths = {getattr(route, "path", "") for route in auth_router.routes}
    assert "/dev/login" not in paths
    assert "/dev/login" not in auth_paths
    assert not any(path.startswith("/dev") for path in paths | auth_paths)


def test_production_http_public_url_is_rejected():
    with pytest.raises(ValidationError):
        identity_settings(
            "sqlite:///:memory:",
            environment="production",
            public_url="http://127.0.0.1:8000",
            allowed_origins="http://127.0.0.1:8000",
        )


def test_forged_identity_header_and_query_do_not_authenticate(database_url):
    app = create_app(production_settings(database_url))
    with TestClient(app) as client:
        response = client.get(
            "/api/v1/session",
            headers={
                "X-Actor-Id": LEAD_ID,
                "X-Role": "lead_designer",
                "X-Sinuo-Account-Id": LEAD_ID,
                "X-Dev-User": "synthetic-lead",
                "X-Session-Id": "synthetic-session",
                "Authorization": "Bearer dev",
            },
            params={"actor_id": LEAD_ID, "role": "lead_designer", "allowed_tools": "acl.manage"},
        )
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "AUTH_REQUIRED"
    assert response.json()["error"]["details"] == {}
    assert "set-cookie" not in response.headers
    assert app.dependency_overrides == {}


def test_forged_actor_and_role_body_do_not_select_principal(database_url):
    app = create_app(production_settings(database_url))
    with TestClient(app) as client:
        response = client.post(
            "/api/v1/session",
            headers={"Origin": PRODUCTION_ORIGIN},
            json={
                "login_name": "synthetic-lead",
                "password": LEAD_PASSWORD,
                "actor_id": OTHER_ID,
                "role": "lead_designer",
                "owner_id": LEAD_ID,
                "allowed_tools": ["acl.manage"],
            },
        )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "INVALID_SCHEMA"
    assert "set-cookie" not in response.headers
    assert LEAD_ID not in response.text
    assert OTHER_ID not in response.text


def test_dev_auth_env_ignored_and_does_not_authenticate(database_url, monkeypatch):
    monkeypatch.setenv("SINUO_DEV_AUTH", "1")
    monkeypatch.setenv("SINUO_ALLOW_IDENTITY_HEADER", "1")
    settings = production_settings(database_url)
    assert "dev_auth" not in type(settings).model_fields
    assert "allow_identity_header" not in type(settings).model_fields
    assert not hasattr(settings, "dev_auth")
    assert not hasattr(settings, "allow_identity_header")
    app = create_app(settings)
    with TestClient(app) as client:
        response = client.get("/api/v1/session", headers={"X-Dev-User": "synthetic-lead"})
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "AUTH_REQUIRED"
    assert app.dependency_overrides == {}


@pytest.mark.integration
@pytest.mark.parametrize("rehash", [False, True])
def test_password_reset_between_verify_and_issue_does_not_mint_session(
    engine, database_url, monkeypatch, rehash
):
    settings = identity_settings(database_url)
    if rehash:
        stored = PasswordHasher(
            time_cost=4,
            memory_cost=19456,
            parallelism=1,
            hash_len=32,
            salt_len=16,
            type=Type.ID,
        ).hash(LEAD_PASSWORD)
    else:
        stored = hash_password(settings, LEAD_PASSWORD)
    replacement = hash_password(settings, "synthetic-reset-password")
    with write_uow(engine) as session:
        session.add(
            Account(
                id=LEAD_ID,
                login_name="synthetic-lead",
                display_name="Synthetic Lead",
                password_hash=stored,
                status="active",
                auth_epoch=1,
                can_create_project=1,
                created_at=NOW,
                disabled_at=None,
            )
        )
    started = Event()
    barrier = Barrier(2)
    real_verify = authenticate_login.__globals__["verify_password"]

    def pause_after_verify(active_settings, password, stored_hash):
        outcome = real_verify(active_settings, password, stored_hash)
        started.set()
        barrier.wait(timeout=5)
        return outcome

    monkeypatch.setattr(
        "sinuo.application.auth.sessions.verify_password",
        pause_after_verify,
    )

    def reset_password() -> None:
        started.wait(timeout=5)
        with write_uow(engine) as session:
            changed = session.execute(
                update(Account)
                .where(Account.id == LEAD_ID, Account.auth_epoch == 1)
                .values(password_hash=replacement, auth_epoch=2)
            )
            assert changed.rowcount == 1
        barrier.wait(timeout=5)

    worker = Thread(target=reset_password)
    worker.start()
    with pytest.raises(AuthRequired):
        authenticate_login(
            engine,
            settings,
            login_name="synthetic-lead",
            password=LEAD_PASSWORD,
            origin="http://127.0.0.1:8000",
            source_host="127.0.0.1",
            now=NOW,
        )
    worker.join(timeout=5)
    assert not worker.is_alive()
    with Session(engine) as session:
        account = session.get(Account, LEAD_ID)
        assert account is not None
        assert account.password_hash == replacement
        assert account.auth_epoch == 2
        assert session.scalar(select(AccountSession.id)) is None


@pytest.mark.concurrency
def test_parallel_login_failures_cannot_both_pass_the_limit(engine, database_url):
    settings = identity_settings(database_url, login_rate_limit_attempts=1)
    with write_uow(engine) as session:
        session.add(
            Account(
                id=LEAD_ID,
                login_name="synthetic-lead",
                display_name="Synthetic Lead",
                password_hash=hash_password(settings, LEAD_PASSWORD),
                status="active",
                auth_epoch=1,
                can_create_project=1,
                created_at=NOW,
                disabled_at=None,
            )
        )
    barrier = Barrier(2)
    outcomes: list[str] = []

    def attempt() -> None:
        barrier.wait(timeout=5)
        try:
            authenticate_login(
                engine,
                settings,
                login_name="synthetic-lead",
                password="synthetic-wrong-password",
                origin="http://127.0.0.1:8000",
                source_host="127.0.0.1",
                now=NOW,
            )
            outcomes.append("issued")
        except RateLimited:
            outcomes.append("limited")
        except AuthRequired:
            outcomes.append("auth")

    workers = [Thread(target=attempt), Thread(target=attempt)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(timeout=10)
        assert not worker.is_alive()
    assert sorted(outcomes) == ["auth", "limited"]
