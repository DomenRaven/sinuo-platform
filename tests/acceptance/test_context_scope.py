"""T13b read set: context-scope endpoint and multi-version read set.

Covers S1 (candidate list endpoint), S2 (parameter rejection), and S3
(snapshot/ref persistence). The read set only affects the next run; a stale
or unauthorized id refuses the run instead of silently shrinking it.
"""

import hashlib
import json

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from sinuo.agents.contracts import SpecialistResult
from sinuo.application.agents.execute import (
    AgentRunRequest,
    RunPlanRejected,
    RunTaskSpec,
    open_agent_run,
)
from sinuo.domain.auth.principal import Principal
from sinuo.domain.schema_types import canonical_document, document_hash
from sinuo.infrastructure.database import write_uow
from sinuo.infrastructure.domain_models import (
    Account,
    Artifact,
    ArtifactVersion,
    ContextSnapshot,
    ContextSnapshotRef,
    Membership,
    ObjectGrant,
    Page,
    PageVersion,
    Project,
    Stage,
)

NOW = 1_762_000_200.0
PASSWORD = "$argon2id$synthetic-context-scope"
READER = "99999999-9999-4999-8999-999999999999"
PROJECT = "context-scope"
STAGE_ID = "context-scope-stage"
PAGE_ID = "context-scope-page"
PV_CURRENT = "context-scope-pv-current"
PV_ADJACENT = "context-scope-pv-adjacent"
PV_OUTSIDER = "context-scope-pv-outsider"
AV_ALLOWED = "context-scope-av-allowed"
AV_FORBIDDEN = "context-scope-av-forbidden"


class _Capture:
    async def execute(self, task, snapshot, upstream):
        del upstream
        return SpecialistResult(task.task_id, "completed", "{}", role=task.role)


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


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
                login_name="context-scope-reader",
                display_name="context-scope-reader",
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
                id="member-context-scope",
                project_id=PROJECT,
                account_id=READER,
                status="active",
                role_label=None,
                created_at=NOW,
                revoked_at=None,
            )
        )
        session.add(Stage(id=STAGE_ID, project_id=PROJECT, head_config_version_id=None, created_at=NOW))
        session.flush()
        session.add(
            Page(
                id=PAGE_ID,
                project_id=PROJECT,
                stage_id=STAGE_ID,
                head_version_id=None,
                status="active",
                title="context-scope-page",
                created_at=NOW,
            )
        )
        session.add(
            Artifact(id="context-scope-art-allowed", project_id=PROJECT, title="allowed", created_at=NOW)
        )
        session.add(
            Artifact(id="context-scope-art-forbidden", project_id=PROJECT, title="forbidden", created_at=NOW)
        )
        session.flush()
        # 邻页版本引用 AV_ALLOWED
        adjacent_content = {
            "schema_version": 1,
            "nodes": [{"node_id": "n-adj", "artifact_version_id": AV_ALLOWED}],
            "edges": [],
        }
        session.add(
            PageVersion(
                id=PV_ADJACENT,
                project_id=PROJECT,
                page_id=PAGE_ID,
                version_no=1,
                parent_version_id=None,
                content_json=canonical_document(adjacent_content),
                content_hash=document_hash(adjacent_content),
                schema_version=1,
                project_revision=1,
                created_by_account_id=READER,
                created_at=NOW,
            )
        )
        session.add(
            PageVersion(
                id=PV_CURRENT,
                project_id=PROJECT,
                page_id=PAGE_ID,
                version_no=2,
                parent_version_id=None,
                content_json=canonical_document({"schema_version": 1, "nodes": [], "edges": []}),
                content_hash=document_hash({"schema_version": 1, "nodes": [], "edges": []}),
                schema_version=1,
                project_revision=2,
                created_by_account_id=READER,
                created_at=NOW,
            )
        )
        session.add(
            ArtifactVersion(
                id=AV_ALLOWED,
                project_id=PROJECT,
                artifact_id="context-scope-art-allowed",
                version_no=1,
                content_hash=_digest(AV_ALLOWED),
                size_bytes=8,
                media_type="application/octet-stream",
                object_key="context-scope/allowed",
                license="synthetic",
                sensitivity="project",
                ai_processing_allowed=1,
                project_revision=1,
                created_by_account_id=READER,
                created_at=NOW,
            )
        )
        session.add(
            ArtifactVersion(
                id=AV_FORBIDDEN,
                project_id=PROJECT,
                artifact_id="context-scope-art-forbidden",
                version_no=1,
                content_hash=_digest(AV_FORBIDDEN),
                size_bytes=8,
                media_type="application/octet-stream",
                object_key="context-scope/forbidden",
                license="synthetic",
                sensitivity="project",
                ai_processing_allowed=0,
                project_revision=1,
                created_by_account_id=READER,
                created_at=NOW,
            )
        )
        session.flush()
        for grant_id, action, scope_type, page_version_id, artifact_version_id in (
            ("grant-cs-project", "project.read", "project", None, None),
            ("grant-cs-run", "run.create", "project", None, None),
            ("grant-cs-page-adj", "page.read", "page_version", PV_ADJACENT, None),
            ("grant-cs-page-cur", "page.read", "page_version", PV_CURRENT, None),
            ("grant-cs-art", "artifact.original.read", "artifact_version", None, AV_ALLOWED),
        ):
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
                    page_id=None,
                    page_version_id=page_version_id,
                    artifact_id=None,
                    artifact_version_id=artifact_version_id,
                    evidence_anchor_id=None,
                )
            )


def _request(now: float, *, page_version_ids=(), artifact_version_ids=()) -> AgentRunRequest:
    return AgentRunRequest(
        intent="问资料",
        tasks=(RunTaskSpec("ev-context-scope", "evidence"),),
        query="合成竹材",
        question="",
        situation="合成情境",
        constraints={"consent": True},
        page_version_id=PV_CURRENT,
        now=now,
        page_version_ids=tuple(page_version_ids),
        artifact_version_ids=tuple(artifact_version_ids),
    )


def _scope_json(engine, snapshot_id: str) -> dict:
    with Session(engine) as session:
        raw = session.scalar(
            select(ContextSnapshot.authorization_scope_json).where(
                ContextSnapshot.id == snapshot_id
            )
        )
    assert isinstance(raw, str)
    return json.loads(raw)


def _ref_pages(engine, snapshot_id: str) -> set[str]:
    with Session(engine) as session:
        rows = session.scalars(
            select(ContextSnapshotRef.page_version_id).where(
                ContextSnapshotRef.snapshot_id == snapshot_id
            )
        ).all()
    return set(rows)


def test_read_set_persisted_in_snapshot_and_refs(engine):
    _seed(engine)
    principal = Principal(READER, "context-scope-session", 1)
    opened = open_agent_run(
        engine,
        principal,
        PROJECT,
        _request(
            NOW,
            page_version_ids=(PV_ADJACENT,),
            artifact_version_ids=(AV_ALLOWED,),
        ),
    )
    scope = _scope_json(engine, opened.snapshot_id)
    assert scope["page_version_ids"] == [PV_ADJACENT]
    assert scope["artifact_version_ids"] == [AV_ALLOWED]
    # 当前页 + 邻页都写入 refs
    assert _ref_pages(engine, opened.snapshot_id) == {PV_CURRENT, PV_ADJACENT}


def test_read_set_rejects_unknown_artifact_version(engine):
    _seed(engine)
    principal = Principal(READER, "context-scope-session", 1)
    with pytest.raises(RunPlanRejected):
        open_agent_run(
            engine,
            principal,
            PROJECT,
            _request(NOW, artifact_version_ids=("context-scope-av-missing",)),
        )


def test_read_set_rejects_ai_forbidden_artifact_version(engine):
    _seed(engine)
    principal = Principal(READER, "context-scope-session", 1)
    with pytest.raises(RunPlanRejected):
        open_agent_run(
            engine,
            principal,
            PROJECT,
            _request(NOW, artifact_version_ids=(AV_FORBIDDEN,)),
        )


def test_read_set_rejects_unauthorized_page_version(engine):
    _seed(engine)
    principal = Principal(READER, "context-scope-session", 1)
    with pytest.raises(RunPlanRejected):
        open_agent_run(
            engine,
            principal,
            PROJECT,
            _request(NOW, page_version_ids=(PV_OUTSIDER,)),
        )


def test_read_set_defaults_to_empty_and_backward_compatible(engine):
    _seed(engine)
    principal = Principal(READER, "context-scope-session", 1)
    opened = open_agent_run(engine, principal, PROJECT, _request(NOW))
    scope = _scope_json(engine, opened.snapshot_id)
    assert scope["page_version_ids"] == []
    assert scope["artifact_version_ids"] == []
    assert _ref_pages(engine, opened.snapshot_id) == {PV_CURRENT}


def test_context_scope_endpoint_lists_includable_objects(engine, database_url):
    from fastapi.testclient import TestClient

    from sinuo.api.app import create_app
    from sinuo.config import Settings
    from tests.acceptance.identity_fixtures import (
        PROJECT_A,
        identity_settings,
        seed_identity_world,
    )

    settings = identity_settings(database_url)
    seed_identity_world(engine, settings)
    app = create_app(settings)
    origin = "http://127.0.0.1:8000"
    with TestClient(app) as client:
        client.post(
            "/api/v1/session",
            headers={"Origin": origin},
            json={"login_name": "Synthetic-Lead", "password": "synthetic-pass-lead"},
        )
        response = client.get(f"/api/v1/projects/{PROJECT_A}/context-scope")
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert set(data) == {
        "page_versions",
        "evidence_anchors",
        "artifact_versions",
        "method_cards",
        "has_hidden",
    }
    assert isinstance(data["page_versions"], list)
    assert isinstance(data["evidence_anchors"], list)
    assert isinstance(data["artifact_versions"], list)
    assert isinstance(data["method_cards"], list)
    # LEAD 是创建者，能看到自己的页版本、证据锚点、作品版本
    assert len(data["page_versions"]) >= 1
    assert len(data["evidence_anchors"]) >= 1
    assert len(data["artifact_versions"]) >= 1
    for item in data["artifact_versions"]:
        assert "ai_processing_allowed" in item
        assert item["can_include"] is True
