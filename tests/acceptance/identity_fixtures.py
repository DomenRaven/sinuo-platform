"""Synthetic identities for T02. Passwords and titles are fabricated."""

import hashlib
import json
import uuid
from dataclasses import dataclass

from sqlalchemy import select

from sinuo.application.auth.acl import ensure_membership, insert_grant, seed_creator_grants
from sinuo.application.auth.passwords import canonical_login_name, hash_password
from sinuo.application.auth.sessions import issue_session
from sinuo.config import Settings
from sinuo.domain.auth.roles import grants_for_template
from sinuo.domain.public_contract import RoleTemplate
from sinuo.domain.schema_types import canonical_document, document_hash
from sinuo.infrastructure.database import write_uow
from sinuo.infrastructure.domain_models import (
    Account,
    Artifact,
    ArtifactVersion,
    EvidenceAnchor,
    Page,
    PageVersion,
    Project,
    Stage,
)

NOW = 1_761_800_000.0
LEAD_PASSWORD = "synthetic-pass-lead"
TEAM_PASSWORD = "synthetic-pass-team"
REVIEWER_PASSWORD = "synthetic-pass-review"
OTHER_PASSWORD = "synthetic-pass-other"
LEAD_LOGIN = "Synthetic-Lead"
TEAM_LOGIN = "Synthetic-Team"
REVIEWER_LOGIN = "Synthetic-Reviewer"
OTHER_LOGIN = "Synthetic-Other"
PROJECT_A = "synthetic-project-alpha"
PROJECT_B = "synthetic-project-beta"
PROJECT_A_TITLE = "synthetic-alpha-display"
PROJECT_B_TITLE = "synthetic-canary-project-title"
PAGE_A = "synthetic-page-shared"
PAGE_B = "synthetic-page-foreign"
PAGE_A_TITLE = "synthetic-shared-page"
PAGE_B_TITLE = "synthetic-canary-page-title"
SENSITIVE_TITLE = "synthetic-canary-artifact-title"
RELEASED_TITLE = "synthetic-released-preview-title"
SENSITIVE_OBJECT_KEY = "synthetic/canary-object-key"
RELEASED_OBJECT_KEY = "synthetic/released-object-key"
CITATION = "synthetic-canary-citation"
EXCERPT = "synthetic-canary-excerpt"
THUMBNAIL = "synthetic-canary-thumbnail"
LEAD_ID = "11111111-1111-4111-8111-111111111111"
TEAM_ID = "22222222-2222-4222-8222-222222222222"
REVIEWER_ID = "33333333-3333-4333-8333-333333333333"
OTHER_ID = "44444444-4444-4444-8444-444444444444"

_HASHES: dict[str, str] = {}


def identity_settings(database_url: str, **overrides: object) -> Settings:
    values: dict[str, object] = {
        "_env_file": None,
        "database_url": database_url,
        "argon2_memory_kib": 19456,
        "argon2_parallelism": 1,
        "argon2_time_cost": 3,
    }
    values.update(overrides)
    return Settings(**values)


def production_settings(database_url: str) -> Settings:
    return identity_settings(
        database_url,
        environment="production",
        public_url="https://sinuo.example",
        allowed_origins="https://sinuo.example",
    )


def cached_password_hash(settings: Settings, password: str) -> str:
    found = _HASHES.get(password)
    if found is None:
        found = hash_password(settings, password)
        _HASHES[password] = found
    return found


@dataclass(frozen=True)
class SignedIn:
    account_id: str
    session_id: str
    token: str
    auth_epoch: int


@dataclass(frozen=True)
class IdentityWorld:
    lead: SignedIn
    team: SignedIn
    reviewer: SignedIn
    other: SignedIn
    reviewer_page_grant_ids: tuple[str, ...]
    sensitive_version_id: str
    released_version_id: str
    evidence_id: str
    foreign_artifact_id: str
    foreign_evidence_id: str
    now: float


def _digest(label: str) -> str:
    return hashlib.sha256(label.encode("utf-8")).hexdigest()


def _account(
    session,
    *,
    account_id: str,
    login_name: str,
    display_name: str,
    password_hash: str,
    can_create: int,
) -> None:
    if len(account_id) != 36:
        raise RuntimeError("account id must be 36 characters")
    if not password_hash.startswith("$argon2id$"):
        raise RuntimeError("password_hash must use argon2id")
    session.add(
        Account(
            id=account_id,
            login_name=canonical_login_name(login_name),
            display_name=display_name,
            password_hash=password_hash,
            status="active",
            auth_epoch=1,
            can_create_project=can_create,
            created_at=NOW,
            disabled_at=None,
        )
    )


def seed_identity_world(engine, settings: Settings) -> IdentityWorld:
    """Two synthetic projects and four synthetic identities."""

    hashes = {
        LEAD_ID: cached_password_hash(settings, LEAD_PASSWORD),
        TEAM_ID: cached_password_hash(settings, TEAM_PASSWORD),
        REVIEWER_ID: cached_password_hash(settings, REVIEWER_PASSWORD),
        OTHER_ID: cached_password_hash(settings, OTHER_PASSWORD),
    }
    sensitive_version_id = str(uuid.uuid4())
    released_version_id = str(uuid.uuid4())
    evidence_id = str(uuid.uuid4())
    foreign_artifact_id = "synthetic-artifact-foreign"
    foreign_evidence_id = str(uuid.uuid4())
    page_version_id = str(uuid.uuid4())
    foreign_version_id = str(uuid.uuid4())
    content = {
        "schema_version": 1,
        "nodes": [
            {"node_id": "synthetic-node-sensitive", "artifact_version_id": sensitive_version_id},
            {"node_id": "synthetic-node-released", "artifact_version_id": released_version_id},
        ],
        "edges": [],
    }
    with write_uow(engine) as session:
        session.add_all(
            [
                Project(
                    id=PROJECT_A,
                    title=PROJECT_A_TITLE,
                    brief="synthetic",
                    template_key=None,
                    revision=1,
                    access_epoch=1,
                    created_at=NOW,
                ),
                Project(
                    id=PROJECT_B,
                    title=PROJECT_B_TITLE,
                    brief="synthetic",
                    template_key=None,
                    revision=1,
                    access_epoch=1,
                    created_at=NOW,
                ),
            ]
        )
        _account(
            session,
            account_id=LEAD_ID,
            login_name=LEAD_LOGIN,
            display_name="synthetic-lead-designer",
            password_hash=hashes[LEAD_ID],
            can_create=1,
        )
        _account(
            session,
            account_id=TEAM_ID,
            login_name=TEAM_LOGIN,
            display_name="synthetic-team-member",
            password_hash=hashes[TEAM_ID],
            can_create=0,
        )
        _account(
            session,
            account_id=REVIEWER_ID,
            login_name=REVIEWER_LOGIN,
            display_name="synthetic-reviewer",
            password_hash=hashes[REVIEWER_ID],
            can_create=0,
        )
        _account(
            session,
            account_id=OTHER_ID,
            login_name=OTHER_LOGIN,
            display_name="synthetic-other-member",
            password_hash=hashes[OTHER_ID],
            can_create=0,
        )
        session.flush()
        session.add_all(
            [
                Stage(
                    id="synthetic-stage-a",
                    project_id=PROJECT_A,
                    head_config_version_id=None,
                    created_at=NOW,
                ),
                Stage(
                    id="synthetic-stage-b",
                    project_id=PROJECT_B,
                    head_config_version_id=None,
                    created_at=NOW,
                ),
            ]
        )
        session.flush()
        session.add_all(
            [
                Page(
                    id=PAGE_A,
                    project_id=PROJECT_A,
                    stage_id="synthetic-stage-a",
                    head_version_id=None,
                    status="active",
                    title=PAGE_A_TITLE,
                    created_at=NOW,
                ),
                Page(
                    id=PAGE_B,
                    project_id=PROJECT_B,
                    stage_id="synthetic-stage-b",
                    head_version_id=None,
                    status="active",
                    title=PAGE_B_TITLE,
                    created_at=NOW,
                ),
            ]
        )
        session.add_all(
            [
                Artifact(
                    id="synthetic-artifact-sensitive",
                    project_id=PROJECT_A,
                    title=SENSITIVE_TITLE,
                    created_at=NOW,
                ),
                Artifact(
                    id="synthetic-artifact-released",
                    project_id=PROJECT_A,
                    title=RELEASED_TITLE,
                    created_at=NOW,
                ),
                Artifact(
                    id=foreign_artifact_id,
                    project_id=PROJECT_B,
                    title=PAGE_B_TITLE,
                    created_at=NOW,
                ),
            ]
        )
        session.flush()
        session.add_all(
            [
                _version(
                    sensitive_version_id,
                    PROJECT_A,
                    "synthetic-artifact-sensitive",
                    SENSITIVE_OBJECT_KEY,
                    "sensitive",
                ),
                _version(
                    released_version_id,
                    PROJECT_A,
                    "synthetic-artifact-released",
                    RELEASED_OBJECT_KEY,
                    "released",
                ),
                _version(
                    foreign_version_id,
                    PROJECT_B,
                    foreign_artifact_id,
                    "synthetic/foreign-object-key",
                    "sensitive",
                ),
            ]
        )
        session.flush()
        session.add(
            PageVersion(
                id=page_version_id,
                project_id=PROJECT_A,
                page_id=PAGE_A,
                version_no=1,
                parent_version_id=None,
                content_json=canonical_document(content),
                content_hash=document_hash(content),
                schema_version=1,
                project_revision=1,
                created_by_account_id=LEAD_ID,
                created_at=NOW,
            )
        )
        session.flush()
        page = session.get(Page, PAGE_A)
        page.head_version_id = page_version_id
        session.add(
            EvidenceAnchor(
                id=evidence_id,
                project_id=PROJECT_A,
                artifact_version_id=sensitive_version_id,
                locator_type="text_range",
                locator_json=json.dumps({"marker": CITATION}, separators=(",", ":")),
                excerpt_hash=_digest(CITATION),
                extractor_version="synthetic-1",
                visibility="restricted",
                sensitivity="sensitive",
                supersedes_id=None,
                schema_version=1,
                created_by_account_id=LEAD_ID,
                created_at=NOW,
            )
        )
        session.add(
            EvidenceAnchor(
                id=foreign_evidence_id,
                project_id=PROJECT_B,
                artifact_version_id=foreign_version_id,
                locator_type="text_range",
                locator_json=json.dumps({"marker": CITATION}, separators=(",", ":")),
                excerpt_hash=_digest("synthetic-foreign-citation"),
                extractor_version="synthetic-1",
                visibility="restricted",
                sensitivity="sensitive",
                supersedes_id=None,
                schema_version=1,
                created_by_account_id=OTHER_ID,
                created_at=NOW,
            )
        )
        session.flush()
        for project_id, account_id in (
            (PROJECT_A, LEAD_ID),
            (PROJECT_A, TEAM_ID),
            (PROJECT_A, REVIEWER_ID),
            (PROJECT_B, OTHER_ID),
        ):
            ensure_membership(session, project_id=project_id, account_id=account_id, now=NOW)
        seed_creator_grants(session, project_id=PROJECT_A, account_id=LEAD_ID, now=NOW)
        seed_creator_grants(session, project_id=PROJECT_B, account_id=OTHER_ID, now=NOW)
        for spec in grants_for_template(RoleTemplate.LEAD_DESIGNER):
            insert_grant(
                session,
                project_id=PROJECT_A,
                account_id=LEAD_ID,
                spec=spec,
                created_by_account_id=LEAD_ID,
                now=NOW,
            )
        for spec in grants_for_template(RoleTemplate.TEAM_MEMBER, (PAGE_A,)):
            insert_grant(
                session,
                project_id=PROJECT_A,
                account_id=TEAM_ID,
                spec=spec,
                created_by_account_id=LEAD_ID,
                now=NOW,
            )
        reviewer_pages: list[str] = []
        for spec in grants_for_template(RoleTemplate.REVIEWER, (PAGE_A,)):
            grant_id = insert_grant(
                session,
                project_id=PROJECT_A,
                account_id=REVIEWER_ID,
                spec=spec,
                created_by_account_id=LEAD_ID,
                now=NOW,
            )
            if spec.scope_type == "page":
                reviewer_pages.append(grant_id)
        for spec in grants_for_template(RoleTemplate.LEAD_DESIGNER):
            insert_grant(
                session,
                project_id=PROJECT_B,
                account_id=OTHER_ID,
                spec=spec,
                created_by_account_id=OTHER_ID,
                now=NOW,
            )
        accounts = {row.id: row for row in session.scalars(select(Account)).all()}
        signed = {
            row_id: _signed(issue_session(session, accounts[row_id], settings, NOW))
            for row_id in (LEAD_ID, TEAM_ID, REVIEWER_ID, OTHER_ID)
        }
    return IdentityWorld(
        lead=signed[LEAD_ID],
        team=signed[TEAM_ID],
        reviewer=signed[REVIEWER_ID],
        other=signed[OTHER_ID],
        reviewer_page_grant_ids=tuple(reviewer_pages),
        sensitive_version_id=sensitive_version_id,
        released_version_id=released_version_id,
        evidence_id=evidence_id,
        foreign_artifact_id=foreign_artifact_id,
        foreign_evidence_id=foreign_evidence_id,
        now=NOW,
    )


def _signed(result) -> SignedIn:
    return SignedIn(
        account_id=result.principal.account_id,
        session_id=result.principal.session_id,
        token=result.token,
        auth_epoch=result.principal.auth_epoch,
    )


def _version(version_id: str, project_id: str, artifact_id: str, object_key: str, sensitivity: str):
    return ArtifactVersion(
        id=version_id,
        project_id=project_id,
        artifact_id=artifact_id,
        version_no=1,
        content_hash=_digest(version_id),
        size_bytes=8,
        media_type="application/octet-stream",
        object_key=object_key,
        license="synthetic",
        sensitivity=sensitivity,
        ai_processing_allowed=0,
        project_revision=1,
        created_by_account_id=LEAD_ID if project_id == PROJECT_A else OTHER_ID,
        created_at=NOW,
    )
