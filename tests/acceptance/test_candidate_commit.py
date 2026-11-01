"""T09 first slice: server preview binding and one-shot candidate commit."""

import hashlib
import json
import uuid
from threading import Barrier, Lock, Thread

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from sinuo.application.candidates import (
    bound_preview_hash,
    commit_candidate,
    preview_candidate,
    reject_candidate,
)
from sinuo.application.candidates.errors import NOT_IMPLEMENTED, CandidateCommandError
from sinuo.application.versions.drafts import update_draft
from sinuo.application.versions.layouts import save_layout
from sinuo.domain.auth.errors import ActionForbidden, InvalidSchema, SourceRedacted
from sinuo.domain.auth.principal import Principal
from sinuo.domain.errors import IdempotencyConflict
from sinuo.domain.public_contract import Action, ErrorCode
from sinuo.domain.schema_types import canonical_document, canonical_json, document_hash
from sinuo.infrastructure.database import write_uow
from sinuo.infrastructure.domain_models import Candidate, ObjectGrant, PrivacyTombstone
from sinuo.infrastructure.models import IdempotencyRecord
from tests.acceptance.test_domain_schema import (
    ACCOUNT_A,
    ACCOUNT_B,
    NOW,
    PAGE_A,
    PAGE_B,
    PROJECT_A,
    digest,
    page_body,
    require_domain_head,
    seed,
)

_BUSINESS = (
    "pages",
    "page_versions",
    "page_edges",
    "decisions",
    "test_records",
    "candidate_dispositions",
    "audit_events",
    "outbox_events",
    "idempotency_records",
    "run_events",
)
_GRANTS = (
    Action.PROJECT_READ,
    Action.PAGE_READ,
    Action.PAGE_DRAFT_WRITE,
    Action.PAGE_CREATE,
    Action.PAGE_BRANCH,
    Action.DECISION_CONFIRM,
    Action.PREVIEW_READ,
)
_CHANGED = ErrorCode.PREVIEW_CHANGED.value
_EXPIRED = ErrorCode.PREVIEW_EXPIRED.value
_STALE = ErrorCode.CANDIDATE_STALE.value
_RESOLVED = ErrorCode.CANDIDATE_ALREADY_RESOLVED.value


def _principal(account_id: str) -> Principal:
    return Principal(account_id=account_id, session_id="synthetic-session", auth_epoch=1)


def _business(engine) -> dict[str, object]:
    with engine.connect() as connection:
        found: dict[str, object] = {
            name: connection.execute(text(f"SELECT count(*) FROM {name}")).scalar_one()
            for name in _BUSINESS
        }
        found["project_revision"] = connection.execute(
            text("SELECT revision FROM projects WHERE id = :project"),
            {"project": PROJECT_A},
        ).scalar_one()
        draft = connection.execute(
            text(
                """
                SELECT content_revision, content_hash, base_version_id
                FROM page_drafts WHERE page_id = :page
                """
            ),
            {"page": PAGE_A},
        ).one()
        found["draft_revision"] = draft.content_revision
        found["draft_hash"] = draft.content_hash
        found["draft_base"] = draft.base_version_id
        found["head_a"] = connection.execute(
            text("SELECT head_version_id FROM pages WHERE id = :page"),
            {"page": PAGE_A},
        ).scalar_one()
        found["head_b"] = connection.execute(
            text("SELECT head_version_id FROM pages WHERE id = :page"),
            {"page": PAGE_B},
        ).scalar_one()
    return found


def _assert_same(before: dict[str, object], after: dict[str, object]) -> None:
    assert before == after


@pytest.fixture
def ready(engine):
    require_domain_head(engine)
    with engine.begin() as connection:
        seed(connection)
    with write_uow(engine) as session:
        for account_id in (ACCOUNT_A, ACCOUNT_B):
            for action in _GRANTS:
                session.add(
                    ObjectGrant(
                        id=str(uuid.uuid4()),
                        project_id=PROJECT_A,
                        account_id=account_id,
                        action=action.value,
                        scope_type="project",
                        status="active",
                        created_at=NOW,
                        revoked_at=None,
                        created_by_account_id=ACCOUNT_A,
                        stage_id=None,
                        page_id=None,
                        page_version_id=None,
                        artifact_id=None,
                        artifact_version_id=None,
                        evidence_anchor_id=None,
                    )
                )
    return engine


def _add_candidate(
    session: Session,
    candidate_id: str,
    action: str,
    page_mode: str | None,
    proposal: dict,
) -> None:
    body = canonical_json(proposal)
    session.add(
        Candidate(
            id=candidate_id,
            project_id=PROJECT_A,
            run_id="synthetic-run",
            action=action,
            page_mode=page_mode,
            status="pending",
            revision=1,
            proposal_json=body,
            proposal_hash=hashlib.sha256(body.encode("utf-8")).hexdigest(),
            input_hash=digest(candidate_id),
            context_snapshot_id="synthetic-snapshot",
            schema_version=1,
            created_at=NOW,
        )
    )


def _draft_proposal(node_id: str = "synthetic-decision-node") -> dict:
    return {
        "base_version_id": "synthetic-pv-a2",
        "node_id": node_id,
        "page_id": PAGE_A,
        "schema_version": 1,
    }


def _stage_proposal(citations: list[str] | None = None) -> dict:
    body = {
        "schema_version": 1,
        "sources": [{"page_version_id": "synthetic-pv-a2"}],
        "target_stage_id": "synthetic-stage",
        "title": "synthetic stage page",
    }
    if citations:
        body["citations"] = citations
    return body


def _merge_proposal() -> dict:
    return {
        "conflicts": [
            {
                "code": "synthetic-overlap",
                "source_version_ids": ["synthetic-pv-a2", "synthetic-pv-b1"],
            }
        ],
        "schema_version": 1,
        "sources": [
            {"page_version_id": "synthetic-pv-a2"},
            {"page_version_id": "synthetic-pv-b1"},
        ],
        "target_stage_id": "synthetic-stage",
        "title": "synthetic merge page",
    }


def _node_proposal(node_id: str = "synthetic-added-node", artifact: str | None = None) -> dict:
    body = {
        "base_version_id": "synthetic-pv-a2",
        "node_id": node_id,
        "page_id": PAGE_A,
        "schema_version": 1,
    }
    if artifact is not None:
        body["artifact_version_id"] = artifact
    return body


def _edge_proposal(
    source: str = "synthetic-node-1",
    target: str = "synthetic-node-2",
    edge_id: str = "synthetic-edge-1",
) -> dict:
    return {
        "base_version_id": "synthetic-pv-a2",
        "edge_id": edge_id,
        "page_id": PAGE_A,
        "schema_version": 1,
        "source_node_id": source,
        "target_node_id": target,
    }


def _attach_proposal(
    node_id: str = "synthetic-node-1",
    artifact: str = "synthetic-art-v3",
) -> dict:
    return {
        "artifact_version_id": artifact,
        "base_version_id": "synthetic-pv-a2",
        "node_id": node_id,
        "page_id": PAGE_A,
        "schema_version": 1,
    }


def _history_proposal(version_id: str = "synthetic-pv-b1") -> dict:
    return {
        "schema_version": 1,
        "source_version_id": version_id,
        "target_stage_id": "synthetic-stage",
        "title": "synthetic history page",
    }


def _preview(
    engine,
    candidate_id: str,
    action: str,
    page_mode,
    proposal: dict,
    patch=None,
    account=ACCOUNT_A,
    *,
    expected_page_version_id=None,
    expected_draft_content_revision=None,
):
    with write_uow(engine) as session:
        _add_candidate(session, candidate_id, action, page_mode, proposal)
        return preview_candidate(
            session,
            principal=_principal(account),
            project_id=PROJECT_A,
            candidate_id=candidate_id,
            now=NOW,
            user_patch=patch,
            expected_page_version_id=expected_page_version_id,
            expected_draft_content_revision=expected_draft_content_revision,
        )


def _commit(
    session,
    candidate_id,
    view,
    account=ACCOUNT_A,
    key="synthetic-commit",
    patch=None,
    now=NOW + 1,
    draft_revision=5,
    version_id="synthetic-pv-a2",
    before_effect=None,
):
    return commit_candidate(
        session,
        principal=_principal(account),
        project_id=PROJECT_A,
        candidate_id=candidate_id,
        preview_id=view.preview_id,
        preview_hash=view.preview_hash,
        expected_page_version_id=version_id,
        expected_draft_content_revision=draft_revision,
        request_key=key,
        now=now,
        user_patch=patch,
        before_effect=before_effect,
    )


def _status(engine, candidate_id: str) -> tuple[str, int]:
    with engine.connect() as connection:
        row = connection.execute(
            text("SELECT status, revision FROM candidates WHERE id = :id"),
            {"id": candidate_id},
        ).one()
    return row.status, row.revision


def _previews(engine) -> int:
    with engine.connect() as connection:
        return int(connection.execute(text("SELECT count(*) FROM candidate_previews")).scalar_one())


def _keep_candidate(engine, candidate_id: str, action: str, page_mode, proposal: dict) -> None:
    with write_uow(engine) as session:
        _add_candidate(session, candidate_id, action, page_mode, proposal)


def _draft_document(engine, page_id: str = PAGE_A) -> dict:
    with engine.connect() as connection:
        raw = connection.execute(
            text("SELECT content_json, content_hash FROM page_drafts WHERE page_id = :page"),
            {"page": page_id},
        ).one()
    document = json.loads(raw.content_json)
    assert raw.content_json == canonical_document(document)
    assert raw.content_hash == document_hash(document)
    return document


def _replace_draft(engine, document: dict, expected_revision: int = 5) -> None:
    body = canonical_document(document)
    with write_uow(engine) as session:
        update_draft(
            session,
            page_id=PAGE_A,
            project_id=PROJECT_A,
            base_version_id="synthetic-pv-a2",
            expected_revision=expected_revision,
            content_json=body,
            content_hash=document_hash(document),
            now=NOW,
        )


def _add_artifact_version(engine, version_id: str = "synthetic-art-v3") -> None:
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO artifact_versions (
                    id, project_id, artifact_id, version_no, content_hash, size_bytes,
                    media_type, object_key, license, sensitivity, ai_processing_allowed,
                    project_revision, created_by_account_id, created_at
                ) VALUES (
                    :version, :project, 'synthetic-art', 3, :content_hash, 11, 'image/png',
                    'synthetic/object-a-v3', 'synthetic-license', 'project', 0, 4,
                    :account, :now
                )
                """
            ),
            {
                "version": version_id,
                "project": PROJECT_A,
                "content_hash": digest(version_id),
                "account": ACCOUNT_A,
                "now": NOW,
            },
        )


@pytest.mark.integration
def test_preview_is_server_bound_and_creates_no_version(ready):
    before = _business(ready)
    patch = {"node_id": "synthetic-patched-node"}
    view = _preview(
        ready,
        "synthetic-preview-bind",
        "draft_decision",
        None,
        _draft_proposal(),
        patch,
    )
    client_hash = hashlib.sha256(b"client-supplied").hexdigest()
    assert view.preview_hash != client_hash
    assert view.user_patch == patch
    assert view.effective_proposal["node_id"] == "synthetic-patched-node"
    assert view.effective_proposal["page_id"] == PAGE_A
    assert view.candidate_revision == 1
    assert view.access_epoch == 1
    assert view.expires_at == NOW + 600
    again = bound_preview_hash(
        account_id=ACCOUNT_A,
        candidate_id="synthetic-preview-bind",
        candidate_revision=view.candidate_revision,
        action="draft_decision",
        page_mode=None,
        access_epoch=view.access_epoch,
        user_patch=view.user_patch,
        effective_proposal=view.effective_proposal,
        sources=view.preconditions,
        expires_at=view.expires_at,
    )
    assert again == view.preview_hash
    with ready.connect() as connection:
        row = connection.execute(
            text(
                """
                SELECT preview_hash, account_id, candidate_revision, access_epoch, user_patch_json
                FROM candidate_previews WHERE id = :id
                """
            ),
            {"id": view.preview_id},
        ).one()
        kinds = connection.execute(
            text(
                """
                SELECT source_kind FROM candidate_preview_sources
                WHERE preview_id = :id ORDER BY source_kind
                """
            ),
            {"id": view.preview_id},
        ).scalars().all()
    assert row.preview_hash == view.preview_hash
    assert row.account_id == ACCOUNT_A
    assert row.candidate_revision == 1
    assert row.access_epoch == 1
    assert json.loads(row.user_patch_json) == patch
    assert kinds == ["draft_stamp", "page_version"]
    after = _business(ready)
    assert after["pages"] == before["pages"]
    assert after["page_versions"] == before["page_versions"]
    assert after["decisions"] == before["decisions"]
    assert after["project_revision"] == before["project_revision"]
    assert after["draft_revision"] == before["draft_revision"]
    assert _status(ready, "synthetic-preview-bind") == ("pending", 1)


@pytest.mark.integration
def test_second_preview_does_not_replace_the_first(ready):
    first = _preview(
        ready,
        "synthetic-preview-again",
        "draft_decision",
        None,
        _draft_proposal(),
        {"node_id": "synthetic-first-node"},
    )
    with write_uow(ready) as session:
        second = preview_candidate(
            session,
            principal=_principal(ACCOUNT_A),
            project_id=PROJECT_A,
            candidate_id="synthetic-preview-again",
            now=NOW + 5,
            user_patch={"node_id": "synthetic-second-node"},
        )
    assert first.preview_id != second.preview_id
    with ready.connect() as connection:
        rows = connection.execute(
            text(
                """
                SELECT id, preview_hash, user_patch_json
                FROM candidate_previews WHERE candidate_id = :id ORDER BY created_at, id
                """
            ),
            {"id": "synthetic-preview-again"},
        ).all()
    assert [row.id for row in rows] == [first.preview_id, second.preview_id]
    assert rows[0].preview_hash == first.preview_hash
    assert json.loads(rows[0].user_patch_json)["node_id"] == "synthetic-first-node"
    assert json.loads(rows[1].user_patch_json)["node_id"] == "synthetic-second-node"


@pytest.mark.integration
def test_commit_rejects_tampered_or_expired_preview(ready):
    view = _preview(
        ready,
        "synthetic-tamper",
        "draft_decision",
        None,
        _draft_proposal(),
        {"node_id": "synthetic-patched-node"},
    )
    patch = {"node_id": "synthetic-patched-node"}

    def refuse(session_call) -> None:
        before = _business(ready)
        with pytest.raises(CandidateCommandError) as caught:
            with write_uow(ready) as session:
                session_call(session)
        assert caught.value.code in {_CHANGED, _EXPIRED}
        _assert_same(before, _business(ready))

    refuse(
        lambda session: _commit(
            session,
            "synthetic-tamper",
            view,
            patch={"node_id": "synthetic-other-node"},
        )
    )
    refuse(
        lambda session: _commit(
            session,
            "synthetic-tamper",
            view,
            account=ACCOUNT_B,
            patch=patch,
        )
    )
    refuse(
        lambda session: commit_candidate(
            session,
            principal=_principal(ACCOUNT_A),
            project_id=PROJECT_A,
            candidate_id="synthetic-tamper",
            preview_id=view.preview_id,
            preview_hash="sha256:" + ("ab" * 32),
            expected_page_version_id="synthetic-pv-a2",
            expected_draft_content_revision=5,
            request_key="synthetic-bad-hash",
            now=NOW + 1,
            user_patch=patch,
        )
    )
    before = _business(ready)
    with pytest.raises(CandidateCommandError) as caught:
        with write_uow(ready) as session:
            _commit(session, "synthetic-tamper", view, patch=patch, now=view.expires_at)
    assert caught.value.code == _EXPIRED
    _assert_same(before, _business(ready))
    swapped = [dict(item) for item in view.preconditions]
    swapped[0] = {**swapped[0], "page_version_id": "synthetic-pv-b1"}
    assert (
        bound_preview_hash(
            account_id=ACCOUNT_A,
            candidate_id="synthetic-tamper",
            candidate_revision=view.candidate_revision,
            action="draft_decision",
            page_mode=None,
            access_epoch=view.access_epoch,
            user_patch=view.user_patch,
            effective_proposal=view.effective_proposal,
            sources=swapped,
            expires_at=view.expires_at,
        )
        != view.preview_hash
    )
    before = _business(ready)
    with pytest.raises(CandidateCommandError) as caught:
        with write_uow(ready) as session:
            _commit(
                session,
                "synthetic-tamper",
                view,
                patch=patch,
                version_id="synthetic-pv-b1",
            )
    assert caught.value.code == _CHANGED
    _assert_same(before, _business(ready))
    with ready.begin() as connection:
        connection.execute(
            text("UPDATE projects SET access_epoch = access_epoch + 1 WHERE id = :project"),
            {"project": PROJECT_A},
        )
    before = _business(ready)
    with pytest.raises(CandidateCommandError) as caught:
        with write_uow(ready) as session:
            _commit(session, "synthetic-tamper", view, patch=patch)
    assert caught.value.code == _CHANGED
    _assert_same(before, _business(ready))
    assert _status(ready, "synthetic-tamper") == ("pending", 1)


@pytest.mark.integration
def test_replay_projects_receipt_and_rejects_a_reused_key(ready):
    patch = {"node_id": "synthetic-patched-node"}
    view = _preview(ready, "synthetic-replay", "draft_decision", None, _draft_proposal(), patch)
    with write_uow(ready) as session:
        first, replayed = _commit(
            session,
            "synthetic-replay",
            view,
            key="synthetic-replay-key",
            patch=patch,
        )
    assert replayed is False
    assert first.status == 201
    assert "title" not in first.body
    assert "summary" not in first.body
    assert first.body["page_id"] == PAGE_A
    assert first.body["content_revision"] == 6
    with write_uow(ready) as session:
        second, replayed = _commit(
            session,
            "synthetic-replay",
            view,
            key="synthetic-replay-key",
            patch=patch,
        )
    assert replayed is True
    assert second.status == 200
    assert second.body == first.body
    with ready.connect() as connection:
        drafts = connection.execute(
            text("SELECT content_revision FROM page_drafts WHERE page_id = :page"),
            {"page": PAGE_A},
        ).scalar_one()
        receipts = connection.execute(
            text(
                "SELECT count(*) FROM idempotency_records "
                "WHERE request_key = 'synthetic-replay-key'"
            )
        ).scalar_one()
    assert drafts == 6
    assert receipts == 1
    with Session(ready) as session:
        stored = session.get(
            IdempotencyRecord,
            (PROJECT_A, ACCOUNT_A, "candidate.commit", "synthetic-replay-key"),
        )
    assert stored is not None
    assert stored.response_status == 201
    assert "title" in stored.response_json
    assert "summary" in stored.response_json
    with pytest.raises(IdempotencyConflict):
        with write_uow(ready) as session:
            _commit(
                session,
                "synthetic-replay",
                view,
                key="synthetic-replay-key",
                patch={"node_id": "synthetic-other-node"},
            )
    with ready.begin() as connection:
        connection.execute(
            text(
                """
                UPDATE object_grants
                SET status = 'revoked', revoked_at = :now
                WHERE project_id = :project AND account_id = :account
                  AND action = 'page.draft.write'
                """
            ),
            {"now": NOW + 2, "project": PROJECT_A, "account": ACCOUNT_A},
        )
    with pytest.raises(ActionForbidden):
        with write_uow(ready) as session:
            _commit(session, "synthetic-replay", view, key="synthetic-replay-key", patch=patch)
    assert _status(ready, "synthetic-replay") == ("committed", 2)


@pytest.mark.concurrency
def test_two_actors_confirm_once(ready):
    with write_uow(ready) as session:
        _add_candidate(session, "synthetic-race", "draft_decision", None, _draft_proposal())
        left = preview_candidate(
            session,
            principal=_principal(ACCOUNT_A),
            project_id=PROJECT_A,
            candidate_id="synthetic-race",
            now=NOW,
            user_patch={"node_id": "synthetic-race-node"},
        )
        right = preview_candidate(
            session,
            principal=_principal(ACCOUNT_B),
            project_id=PROJECT_A,
            candidate_id="synthetic-race",
            now=NOW,
            user_patch={"node_id": "synthetic-race-node"},
        )
    with ready.connect() as connection:
        stamps = connection.execute(
            text(
                """
                SELECT preview_id, draft_content_revision, draft_content_hash
                FROM candidate_preview_sources
                WHERE source_kind = 'draft_stamp'
                  AND preview_id IN (:left, :right)
                """
            ),
            {"left": left.preview_id, "right": right.preview_id},
        ).all()
        live = connection.execute(
            text(
                "SELECT content_revision, content_hash FROM page_drafts WHERE page_id = :page"
            ),
            {"page": PAGE_A},
        ).one()
    assert {row.draft_content_revision for row in stamps} == {live.content_revision}
    assert {row.draft_content_hash for row in stamps} == {live.content_hash}
    barrier = Barrier(2)
    lock = Lock()
    outcomes: list[tuple[str, object]] = []

    def attempt(account_id: str, key: str, view) -> None:
        barrier.wait(timeout=5)
        try:
            with write_uow(ready) as session:
                response, _replayed = commit_candidate(
                    session,
                    principal=_principal(account_id),
                    project_id=PROJECT_A,
                    candidate_id="synthetic-race",
                    preview_id=view.preview_id,
                    preview_hash=view.preview_hash,
                    expected_page_version_id="synthetic-pv-a2",
                    expected_draft_content_revision=5,
                    request_key=key,
                    now=NOW + 1,
                    user_patch={"node_id": "synthetic-race-node"},
                )
            result = ("ok", response.body["disposition_id"])
        except CandidateCommandError as exc:
            result = (exc.code, exc.details.get("disposition_id"))
        with lock:
            outcomes.append(result)

    first = Thread(target=attempt, args=(ACCOUNT_A, "synthetic-race-a", left))
    second = Thread(target=attempt, args=(ACCOUNT_B, "synthetic-race-b", right))
    first.start()
    second.start()
    first.join(timeout=15)
    second.join(timeout=15)
    assert not first.is_alive()
    assert not second.is_alive()
    assert sorted(item[0] for item in outcomes) == sorted(["ok", _RESOLVED])
    winner = next(item[1] for item in outcomes if item[0] == "ok")
    loser = next(item[1] for item in outcomes if item[0] == _RESOLVED)
    assert winner == loser
    with ready.connect() as connection:
        dispositions = connection.execute(
            text(
                "SELECT count(*) FROM candidate_dispositions "
                "WHERE candidate_id = 'synthetic-race'"
            )
        ).scalar_one()
        draft = connection.execute(
            text("SELECT content_revision FROM page_drafts WHERE page_id = :page"),
            {"page": PAGE_A},
        ).scalar_one()
        versions = connection.execute(text("SELECT count(*) FROM page_versions")).scalar_one()
    assert dispositions == 1
    assert draft == 6
    assert versions == 3
    assert _status(ready, "synthetic-race") == ("committed", 2)


@pytest.mark.integration
def test_draft_decision_writes_draft_and_disposition_only(ready):
    before = _business(ready)
    view = _preview(
        ready,
        "synthetic-draft-commit",
        "draft_decision",
        None,
        _draft_proposal(),
        {"node_id": "synthetic-patched-node"},
    )
    with write_uow(ready) as session:
        response, _replayed = _commit(
            session,
            "synthetic-draft-commit",
            view,
            patch={"node_id": "synthetic-patched-node"},
        )
    after = _business(ready)
    assert response.body["content_revision"] == before["draft_revision"] + 1
    assert after["pages"] == before["pages"]
    assert after["page_versions"] == before["page_versions"]
    assert after["page_edges"] == before["page_edges"]
    assert after["decisions"] == before["decisions"]
    assert after["test_records"] == before["test_records"]
    assert after["project_revision"] == before["project_revision"]
    assert after["head_a"] == before["head_a"]
    assert after["draft_revision"] == before["draft_revision"] + 1
    assert after["candidate_dispositions"] == before["candidate_dispositions"] + 1
    assert after["outbox_events"] == before["outbox_events"] + 1
    assert after["audit_events"] == before["audit_events"] + 1
    assert after["run_events"] == before["run_events"] + 1
    with ready.connect() as connection:
        draft = connection.execute(
            text("SELECT content_json FROM page_drafts WHERE page_id = :page"),
            {"page": PAGE_A},
        ).scalar_one()
        outcome = connection.execute(
            text(
                """
                SELECT outcome, preview_id FROM candidate_dispositions
                WHERE candidate_id = 'synthetic-draft-commit'
                """
            )
        ).one()
        event_type = connection.execute(
            text("SELECT event_type FROM outbox_events ORDER BY created_at DESC LIMIT 1")
        ).scalar_one()
    document = json.loads(draft)
    assert {node["node_id"] for node in document["nodes"]} == {
        "synthetic-node-1",
        "synthetic-patched-node",
    }
    assert outcome.outcome == "committed"
    assert outcome.preview_id == view.preview_id
    assert event_type == "candidate.committed"
    assert _status(ready, "synthetic-draft-commit") == ("committed", 2)


@pytest.mark.integration
def test_reject_writes_disposition_only(ready):
    before = _business(ready)
    with write_uow(ready) as session:
        _add_candidate(session, "synthetic-reject", "draft_decision", None, _draft_proposal())
        first, replayed = reject_candidate(
            session,
            principal=_principal(ACCOUNT_A),
            project_id=PROJECT_A,
            candidate_id="synthetic-reject",
            request_key="synthetic-reject-key",
            now=NOW + 1,
            reason="synthetic-no",
        )
    assert replayed is False
    assert first.status == 201
    assert "title" not in first.body
    after = _business(ready)
    assert after["pages"] == before["pages"]
    assert after["page_versions"] == before["page_versions"]
    assert after["page_edges"] == before["page_edges"]
    assert after["decisions"] == before["decisions"]
    assert after["draft_revision"] == before["draft_revision"]
    assert after["draft_hash"] == before["draft_hash"]
    assert after["project_revision"] == before["project_revision"]
    assert after["head_a"] == before["head_a"]
    assert after["head_b"] == before["head_b"]
    with ready.connect() as connection:
        row = connection.execute(
            text(
                """
                SELECT outcome, preview_id, reason FROM candidate_dispositions
                WHERE candidate_id = 'synthetic-reject'
                """
            )
        ).one()
    assert row.outcome == "rejected"
    assert row.preview_id is None
    assert row.reason == "synthetic-no"
    with write_uow(ready) as session:
        second, replayed = reject_candidate(
            session,
            principal=_principal(ACCOUNT_A),
            project_id=PROJECT_A,
            candidate_id="synthetic-reject",
            request_key="synthetic-reject-key",
            now=NOW + 1,
            reason="synthetic-no",
        )
    assert replayed is True
    assert second.status == 200
    assert second.body["disposition_id"] == first.body["disposition_id"]
    with pytest.raises(IdempotencyConflict):
        with write_uow(ready) as session:
            reject_candidate(
                session,
                principal=_principal(ACCOUNT_A),
                project_id=PROJECT_A,
                candidate_id="synthetic-reject",
                request_key="synthetic-reject-key",
                now=NOW + 1,
                reason="synthetic-other",
            )
    assert _status(ready, "synthetic-reject") == ("rejected", 2)
    assert _business(ready)["candidate_dispositions"] == before["candidate_dispositions"] + 1


@pytest.mark.integration
def test_stage_blank_and_merge_blank_keep_explicit_sources(ready):
    stage = _preview(
        ready,
        "synthetic-stage",
        "create_page_from_versions",
        "stage_blank",
        _stage_proposal(["synthetic-art-v1"]),
        {"target_title": "夜间取物需求定义"},
    )
    with write_uow(ready) as session:
        stage_result, _replayed = _commit(
            session,
            "synthetic-stage",
            stage,
            patch={"target_title": "夜间取物需求定义"},
            key="synthetic-stage-key",
        )
    merge = _preview(
        ready,
        "synthetic-merge",
        "create_page_from_versions",
        "merge_blank",
        _merge_proposal(),
        {"target_title": "synthetic merge title"},
    )
    with write_uow(ready) as session:
        merge_result, _replayed = commit_candidate(
            session,
            principal=_principal(ACCOUNT_A),
            project_id=PROJECT_A,
            candidate_id="synthetic-merge",
            preview_id=merge.preview_id,
            preview_hash=merge.preview_hash,
            expected_page_version_id="synthetic-pv-a2",
            expected_draft_content_revision=5,
            request_key="synthetic-merge-key",
            now=NOW + 1,
            user_patch={"target_title": "synthetic merge title"},
        )
    with ready.connect() as connection:
        stage_page = connection.execute(
            text(
                """
                SELECT title, stage_id, head_version_id FROM pages WHERE id = :page
                """
            ),
            {"page": stage_result.body["page_id"]},
        ).one()
        stage_body = connection.execute(
            text("SELECT content_json FROM page_versions WHERE id = :version"),
            {"version": stage_result.body["page_version_id"]},
        ).scalar_one()
        stage_edges = connection.execute(
            text(
                """
                SELECT source_version_id, kind FROM page_edges
                WHERE target_version_id = :version ORDER BY source_version_id
                """
            ),
            {"version": stage_result.body["page_version_id"]},
        ).all()
        decision = connection.execute(
            text("SELECT page_version_id, reason FROM decisions WHERE id = :id"),
            {"id": stage_result.body["decision_id"]},
        ).one()
        merge_body = connection.execute(
            text("SELECT content_json FROM page_versions WHERE id = :version"),
            {"version": merge_result.body["page_version_id"]},
        ).scalar_one()
        merge_edges = connection.execute(
            text(
                """
                SELECT source_version_id, kind, reason FROM page_edges
                WHERE target_version_id = :version ORDER BY source_version_id
                """
            ),
            {"version": merge_result.body["page_version_id"]},
        ).all()
        heads = connection.execute(
            text("SELECT id, head_version_id FROM pages WHERE id IN (:a, :b) ORDER BY id"),
            {"a": PAGE_A, "b": PAGE_B},
        ).all()
        revision = connection.execute(
            text("SELECT revision FROM projects WHERE id = :project"),
            {"project": PROJECT_A},
        ).scalar_one()
    assert stage_page.title == "夜间取物需求定义"
    assert stage_page.stage_id == "synthetic-stage"
    stage_document = json.loads(stage_body)
    assert stage_document["edges"] == []
    assert [node["node_id"] for node in stage_document["nodes"]] != ["synthetic-node-1"]
    assert stage_document["nodes"][0]["artifact_version_id"] == "synthetic-art-v1"
    assert stage_document["nodes"][0]["node_id"] != "synthetic-node-1"
    assert stage_edges == [("synthetic-pv-a2", "stage_source")]
    assert decision.page_version_id == stage_result.body["page_version_id"]
    assert decision.reason == "stage_blank"
    merge_document = json.loads(merge_body)
    assert merge_document["nodes"] == []
    assert merge_document["edges"] == []
    assert [(row.source_version_id, row.kind) for row in merge_edges] == [
        ("synthetic-pv-a2", "merge_source"),
        ("synthetic-pv-b1", "merge_source"),
    ]
    assert all("synthetic-overlap" in row.reason for row in merge_edges)
    assert {(row.id, row.head_version_id) for row in heads} == {
        (PAGE_A, "synthetic-pv-a2"),
        (PAGE_B, "synthetic-pv-b1"),
    }
    assert revision == 6
    assert stage_page.head_version_id == stage_result.body["page_version_id"]


@pytest.mark.integration
def test_history_branch_survives_a_later_head(ready):
    view = _preview(
        ready,
        "synthetic-history",
        "create_page_from_versions",
        "history_branch",
        _history_proposal(),
        {"target_title": "synthetic branched title"},
    )
    with ready.begin() as connection:
        original = connection.execute(
            text("SELECT content_hash FROM page_versions WHERE id = 'synthetic-pv-b1'")
        ).scalar_one()
        connection.execute(
            text(
                """
                INSERT INTO page_versions (
                    id, project_id, page_id, version_no, parent_version_id, content_json,
                    content_hash, schema_version, project_revision,
                    created_by_account_id, created_at
                ) VALUES (
                    'synthetic-pv-b2', :project, :page, 2, 'synthetic-pv-b1', :body, :hash,
                    1, 4, :account, :now
                )
                """
            ),
            {
                "project": PROJECT_A,
                "page": PAGE_B,
                "body": page_body(),
                "hash": document_hash(json.loads(page_body())),
                "account": ACCOUNT_A,
                "now": NOW,
            },
        )
        connection.execute(
            text("UPDATE pages SET head_version_id = 'synthetic-pv-b2' WHERE id = :page"),
            {"page": PAGE_B},
        )
    decisions_before = _business(ready)["decisions"]
    tests_before = _business(ready)["test_records"]
    with write_uow(ready) as session:
        result, _replayed = _commit(
            session,
            "synthetic-history",
            view,
            key="synthetic-history-key",
            patch={"target_title": "synthetic branched title"},
            version_id="synthetic-pv-b1",
            draft_revision=None,
        )
    with ready.connect() as connection:
        content = connection.execute(
            text("SELECT content_json FROM page_versions WHERE id = :version"),
            {"version": result.body["page_version_id"]},
        ).scalar_one()
        edge = connection.execute(
            text(
                """
                SELECT source_version_id, kind, reason FROM page_edges
                WHERE target_version_id = :version
                """
            ),
            {"version": result.body["page_version_id"]},
        ).one()
        source_hash = connection.execute(
            text("SELECT content_hash FROM page_versions WHERE id = 'synthetic-pv-b1'")
        ).scalar_one()
        head = connection.execute(
            text("SELECT head_version_id FROM pages WHERE id = :page"),
            {"page": PAGE_B},
        ).scalar_one()
        decisions = connection.execute(text("SELECT count(*) FROM decisions")).scalar_one()
        tests = connection.execute(text("SELECT count(*) FROM test_records")).scalar_one()
        title = connection.execute(
            text("SELECT title FROM pages WHERE id = :page"),
            {"page": result.body["page_id"]},
        ).scalar_one()
    document = json.loads(content)
    origin = json.loads(edge.reason)
    assert document["nodes"][0]["node_id"] != "synthetic-node-1"
    assert document["nodes"][0]["artifact_version_id"] == "synthetic-art-v1"
    assert origin["nodes"][0]["origin_node_id"] == "synthetic-node-1"
    assert origin["nodes"][0]["node_id"] == document["nodes"][0]["node_id"]
    assert edge.source_version_id == "synthetic-pv-b1"
    assert edge.kind == "branches_from"
    assert source_hash == original
    assert head == "synthetic-pv-b2"
    assert decisions == decisions_before
    assert tests == tests_before
    assert title == "synthetic branched title"
    assert "decision_id" not in result.body
    assert _status(ready, "synthetic-history") == ("committed", 2)


@pytest.mark.integration
def test_invalid_source_or_revoked_permission_adds_nothing(ready):
    stale = _preview(
        ready,
        "synthetic-head-moved",
        "create_page_from_versions",
        "stage_blank",
        _stage_proposal(),
    )
    with ready.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO page_versions (
                    id, project_id, page_id, version_no, parent_version_id, content_json,
                    content_hash, schema_version, project_revision,
                    created_by_account_id, created_at
                ) VALUES (
                    'synthetic-pv-a3', :project, :page, 3, 'synthetic-pv-a2', :body, :hash,
                    1, 4, :account, :now
                )
                """
            ),
            {
                "project": PROJECT_A,
                "page": PAGE_A,
                "body": page_body(),
                "hash": document_hash(json.loads(page_body())),
                "account": ACCOUNT_A,
                "now": NOW,
            },
        )
        connection.execute(
            text("UPDATE pages SET head_version_id = 'synthetic-pv-a3' WHERE id = :page"),
            {"page": PAGE_A},
        )
    before = _business(ready)
    with pytest.raises(CandidateCommandError) as caught:
        with write_uow(ready) as session:
            _commit(session, "synthetic-head-moved", stale, key="synthetic-head-key")
    assert caught.value.code == _STALE
    _assert_same(before, _business(ready))
    assert _status(ready, "synthetic-head-moved") == ("pending", 1)

    history = _preview(
        ready,
        "synthetic-tomb",
        "create_page_from_versions",
        "history_branch",
        _history_proposal(),
    )
    with write_uow(ready) as session:
        session.add(
            PrivacyTombstone(
                id="synthetic-tomb-b1",
                project_id=PROJECT_A,
                object_type="page_version",
                object_id="synthetic-pv-b1",
                reason_category="policy",
                access_epoch=1,
                audit_event_id="synthetic-audit",
                created_at=NOW,
            )
        )
    before = _business(ready)
    with pytest.raises(SourceRedacted):
        with write_uow(ready) as session:
            _commit(
                session,
                "synthetic-tomb",
                history,
                key="synthetic-tomb-key",
                version_id="synthetic-pv-b1",
                draft_revision=None,
            )
    _assert_same(before, _business(ready))
    assert _status(ready, "synthetic-tomb") == ("pending", 1)

    draft = _preview(ready, "synthetic-revoked", "draft_decision", None, _draft_proposal())
    with ready.begin() as connection:
        connection.execute(
            text(
                """
                UPDATE object_grants
                SET status = 'revoked', revoked_at = :now
                WHERE project_id = :project AND account_id = :account
                  AND action = 'page.draft.write'
                """
            ),
            {"now": NOW + 2, "project": PROJECT_A, "account": ACCOUNT_A},
        )
    before = _business(ready)
    with pytest.raises(ActionForbidden):
        with write_uow(ready) as session:
            _commit(session, "synthetic-revoked", draft, key="synthetic-revoked-key")
    _assert_same(before, _business(ready))
    assert _status(ready, "synthetic-revoked") == ("pending", 1)


@pytest.mark.integration
def test_unconfirmed_draft_blocks_stage_commit(ready):
    dirty = canonical_document(
        {
            "schema_version": 1,
            "nodes": [
                {"node_id": "synthetic-node-1", "artifact_version_id": "synthetic-art-v1"},
                {"node_id": "synthetic-dirty"},
            ],
            "edges": [],
        }
    )
    with write_uow(ready) as session:
        update_draft(
            session,
            page_id=PAGE_A,
            project_id=PROJECT_A,
            base_version_id="synthetic-pv-a2",
            expected_revision=5,
            content_json=dirty,
            content_hash=document_hash(json.loads(dirty)),
            now=NOW,
        )
    view = _preview(
        ready,
        "synthetic-dirty-stage",
        "create_page_from_versions",
        "stage_blank",
        _stage_proposal(),
    )
    before = _business(ready)
    with pytest.raises(CandidateCommandError) as caught:
        with write_uow(ready) as session:
            _commit(
                session,
                "synthetic-dirty-stage",
                view,
                key="synthetic-dirty-key",
                draft_revision=6,
            )
    assert caught.value.code == _STALE
    _assert_same(before, _business(ready))
    assert _status(ready, "synthetic-dirty-stage") == ("pending", 1)


@pytest.mark.recovery
def test_failure_after_consume_rolls_candidate_and_draft_back(ready):
    view = _preview(ready, "synthetic-inject", "draft_decision", None, _draft_proposal())
    before = _business(ready)
    seen: dict[str, object] = {}

    def before_effect(session: Session) -> None:
        candidate = session.execute(
            text("SELECT status, revision FROM candidates WHERE id = 'synthetic-inject'")
        ).one()
        draft = session.execute(
            text("SELECT content_revision FROM page_drafts WHERE page_id = :page"),
            {"page": PAGE_A},
        ).one()
        seen["status"] = candidate.status
        seen["revision"] = candidate.revision
        seen["draft"] = draft.content_revision
        raise RuntimeError("injected")

    with pytest.raises(RuntimeError, match="injected"):
        with write_uow(ready) as session:
            _commit(
                session,
                "synthetic-inject",
                view,
                key="synthetic-inject-key",
                before_effect=before_effect,
            )
    assert seen == {"status": "committed", "revision": 2, "draft": 6}
    _assert_same(before, _business(ready))
    assert _status(ready, "synthetic-inject") == ("pending", 1)
    with Session(ready) as session:
        assert (
            session.get(
                IdempotencyRecord,
                (PROJECT_A, ACCOUNT_A, "candidate.commit", "synthetic-inject-key"),
            )
            is None
        )


@pytest.mark.recovery
def test_stage_blank_failure_after_page_insert_rolls_back(ready):
    candidate_id = "synthetic-blank-rollback"
    view = _preview(
        ready,
        candidate_id,
        "create_page_from_versions",
        "stage_blank",
        _stage_proposal(),
    )
    before = _business(ready)
    pending = _status(ready, candidate_id)
    seen: dict[str, object] = {}

    def before_effect(session: Session) -> None:
        candidate = session.execute(
            text("SELECT status, revision FROM candidates WHERE id = :id"),
            {"id": candidate_id},
        ).one()
        page = session.execute(
            text(
                """
                SELECT id, head_version_id FROM pages
                WHERE project_id = :project AND id NOT IN (:page_a, :page_b)
                """
            ),
            {"project": PROJECT_A, "page_a": PAGE_A, "page_b": PAGE_B},
        ).one()
        version = session.execute(
            text("SELECT id FROM page_versions WHERE id = :id AND page_id = :page"),
            {"id": page.head_version_id, "page": page.id},
        ).one()
        edge = session.execute(
            text(
                """
                SELECT source_version_id, kind FROM page_edges
                WHERE target_version_id = :version
                """
            ),
            {"version": version.id},
        ).one()
        dispositions = session.execute(
            text("SELECT count(*) FROM candidate_dispositions WHERE candidate_id = :id"),
            {"id": candidate_id},
        ).scalar_one()
        seen["status"] = candidate.status
        seen["revision"] = candidate.revision
        seen["page_id"] = page.id
        seen["version_id"] = version.id
        seen["edge"] = (edge.source_version_id, edge.kind)
        seen["dispositions"] = dispositions
        raise RuntimeError("injected")

    with pytest.raises(RuntimeError, match="injected"):
        with write_uow(ready) as session:
            _commit(
                session,
                candidate_id,
                view,
                key="synthetic-blank-rollback-key",
                before_effect=before_effect,
            )
    assert pending == ("pending", 1)
    assert seen["status"] == "committed"
    assert seen["revision"] == 2
    assert seen["edge"] == ("synthetic-pv-a2", "stage_source")
    assert seen["dispositions"] == 0
    page_id = seen["page_id"]
    version_id = seen["version_id"]
    assert isinstance(page_id, str)
    assert page_id not in {PAGE_A, PAGE_B}
    assert isinstance(version_id, str)
    assert version_id != ""
    _assert_same(before, _business(ready))
    assert _status(ready, candidate_id) == pending
    with ready.connect() as connection:
        assert (
            connection.execute(
                text("SELECT count(*) FROM pages WHERE id = :id"),
                {"id": page_id},
            ).scalar_one()
            == 0
        )
        assert (
            connection.execute(
                text("SELECT count(*) FROM page_versions WHERE id = :id"),
                {"id": version_id},
            ).scalar_one()
            == 0
        )
        assert (
            connection.execute(
                text("SELECT count(*) FROM page_versions WHERE page_id = :id"),
                {"id": page_id},
            ).scalar_one()
            == 0
        )
        assert (
            connection.execute(
                text("SELECT count(*) FROM page_edges WHERE target_version_id = :id"),
                {"id": version_id},
            ).scalar_one()
            == 0
        )
        assert (
            connection.execute(
                text("SELECT count(*) FROM page_drafts WHERE page_id = :id"),
                {"id": page_id},
            ).scalar_one()
            == 0
        )
        assert (
            connection.execute(
                text("SELECT count(*) FROM decisions WHERE page_version_id = :id"),
                {"id": version_id},
            ).scalar_one()
            == 0
        )
        assert (
            connection.execute(
                text("SELECT count(*) FROM candidate_dispositions WHERE candidate_id = :id"),
                {"id": candidate_id},
            ).scalar_one()
            == 0
        )
    with Session(ready) as session:
        assert (
            session.get(
                IdempotencyRecord,
                (PROJECT_A, ACCOUNT_A, "candidate.commit", "synthetic-blank-rollback-key"),
            )
            is None
        )


@pytest.mark.integration
def test_unimplemented_action_does_not_write_a_preview(ready):
    before = _business(ready)
    with pytest.raises(CandidateCommandError) as caught:
        with write_uow(ready) as session:
            preview_candidate(
                session,
                principal=_principal(ACCOUNT_A),
                project_id=PROJECT_A,
                candidate_id="synthetic-candidate",
                now=NOW,
            )
    assert caught.value.code == NOT_IMPLEMENTED
    _assert_same(before, _business(ready))
    with ready.connect() as connection:
        previews = connection.execute(text("SELECT count(*) FROM candidate_previews")).scalar_one()
    assert previews == 1


def _assert_draft_only(before: dict[str, object], after: dict[str, object]) -> None:
    assert after["pages"] == before["pages"]
    assert after["page_versions"] == before["page_versions"]
    assert after["page_edges"] == before["page_edges"]
    assert after["decisions"] == before["decisions"]
    assert after["test_records"] == before["test_records"]
    assert after["project_revision"] == before["project_revision"]
    assert after["head_a"] == before["head_a"]
    assert after["head_b"] == before["head_b"]
    assert after["draft_revision"] == before["draft_revision"] + 1
    assert after["candidate_dispositions"] == before["candidate_dispositions"] + 1
    assert after["audit_events"] == before["audit_events"] + 1
    assert after["outbox_events"] == before["outbox_events"] + 1
    assert after["run_events"] == before["run_events"] + 1
    assert after["idempotency_records"] == before["idempotency_records"] + 1


def _assert_nothing_new(engine, before: dict[str, object], candidate_id: str, previews: int) -> None:
    _assert_same(before, _business(engine))
    assert _previews(engine) == previews
    assert _status(engine, candidate_id) == ("pending", 1)


@pytest.mark.integration
def test_add_node_commits_into_the_draft(ready):
    before = _business(ready)
    patch = {"action": "add_node", "node_id": "synthetic-added-node"}
    view = _preview(
        ready,
        "synthetic-add-node",
        "add_node",
        None,
        _node_proposal("synthetic-proposed-node"),
        patch,
        expected_page_version_id="synthetic-pv-a2",
        expected_draft_content_revision=5,
    )
    with write_uow(ready) as session:
        response, replayed = _commit(
            session,
            "synthetic-add-node",
            view,
            patch=patch,
            key="synthetic-add-node-key",
        )
    assert replayed is False
    assert response.status == 201
    assert response.body["page_id"] == PAGE_A
    assert response.body["content_revision"] == before["draft_revision"] + 1
    assert "page_version_id" not in response.body
    assert "project_revision" not in response.body
    assert "decision_id" not in response.body
    after = _business(ready)
    _assert_draft_only(before, after)
    document = _draft_document(ready)
    assert set(document) == {"schema_version", "nodes", "edges"}
    assert document["schema_version"] == 1
    assert document["edges"] == []
    added = next(node for node in document["nodes"] if node["node_id"] == "synthetic-added-node")
    assert set(added) == {"node_id"}
    assert {node["node_id"] for node in document["nodes"]} == {
        "synthetic-node-1",
        "synthetic-added-node",
    }
    with ready.connect() as connection:
        outcome = connection.execute(
            text(
                """
                SELECT outcome, preview_id FROM candidate_dispositions
                WHERE candidate_id = 'synthetic-add-node'
                """
            )
        ).one()
        event_type = connection.execute(
            text("SELECT event_type FROM outbox_events ORDER BY created_at DESC LIMIT 1")
        ).scalar_one()
    assert outcome.outcome == "committed"
    assert outcome.preview_id == view.preview_id
    assert event_type == "candidate.committed"
    assert _status(ready, "synthetic-add-node") == ("committed", 2)


@pytest.mark.integration
def test_add_node_rejects_a_different_kind_without_new_rows(ready):
    _keep_candidate(ready, "synthetic-kind-action", "add_node", None, _node_proposal())
    _keep_candidate(ready, "synthetic-kind-mode", "add_node", None, _node_proposal("synthetic-mode-node"))
    before = _business(ready)
    previews = _previews(ready)
    with pytest.raises(InvalidSchema):
        with write_uow(ready) as session:
            preview_candidate(
                session,
                principal=_principal(ACCOUNT_A),
                project_id=PROJECT_A,
                candidate_id="synthetic-kind-action",
                now=NOW,
                user_patch={"action": "add_typed_edge"},
            )
    _assert_nothing_new(ready, before, "synthetic-kind-action", previews)
    with pytest.raises(InvalidSchema):
        with write_uow(ready) as session:
            preview_candidate(
                session,
                principal=_principal(ACCOUNT_A),
                project_id=PROJECT_A,
                candidate_id="synthetic-kind-mode",
                now=NOW,
                user_patch={"page_mode": "stage_blank"},
            )
    _assert_nothing_new(ready, before, "synthetic-kind-mode", previews)


@pytest.mark.integration
def test_add_typed_edge_commits_into_the_draft(ready):
    _replace_draft(
        ready,
        {
            "schema_version": 1,
            "nodes": [
                {"node_id": "synthetic-node-1", "artifact_version_id": "synthetic-art-v1"},
                {"node_id": "synthetic-node-2"},
            ],
            "edges": [],
        },
    )
    before = _business(ready)
    view = _preview(
        ready,
        "synthetic-add-edge",
        "add_typed_edge",
        None,
        _edge_proposal(),
        expected_page_version_id="synthetic-pv-a2",
        expected_draft_content_revision=6,
    )
    with write_uow(ready) as session:
        response, _replayed = _commit(
            session,
            "synthetic-add-edge",
            view,
            key="synthetic-add-edge-key",
            draft_revision=6,
        )
    assert "page_version_id" not in response.body
    assert "project_revision" not in response.body
    _assert_draft_only(before, _business(ready))
    document = _draft_document(ready)
    assert len(document["edges"]) == 1
    edge = document["edges"][0]
    assert set(edge) == {"edge_id", "source_node_id", "target_node_id"}
    assert edge["edge_id"] == "synthetic-edge-1"
    assert edge["source_node_id"] == "synthetic-node-1"
    assert edge["target_node_id"] == "synthetic-node-2"
    assert _status(ready, "synthetic-add-edge") == ("committed", 2)


@pytest.mark.integration
def test_add_typed_edge_rejects_a_missing_endpoint_without_new_rows(ready):
    _keep_candidate(
        ready,
        "synthetic-missing-edge",
        "add_typed_edge",
        None,
        _edge_proposal(target="synthetic-missing"),
    )
    before = _business(ready)
    previews = _previews(ready)
    with pytest.raises(InvalidSchema):
        with write_uow(ready) as session:
            preview_candidate(
                session,
                principal=_principal(ACCOUNT_A),
                project_id=PROJECT_A,
                candidate_id="synthetic-missing-edge",
                now=NOW,
            )
    _assert_nothing_new(ready, before, "synthetic-missing-edge", previews)


@pytest.mark.integration
def test_attach_artifact_version_commits_into_the_draft(ready):
    _add_artifact_version(ready)
    before = _business(ready)
    view = _preview(
        ready,
        "synthetic-attach",
        "attach_artifact_version",
        None,
        _attach_proposal(),
        expected_page_version_id="synthetic-pv-a2",
        expected_draft_content_revision=5,
    )
    with write_uow(ready) as session:
        response, _replayed = _commit(
            session,
            "synthetic-attach",
            view,
            key="synthetic-attach-key",
        )
    assert response.body["page_id"] == PAGE_A
    assert "page_version_id" not in response.body
    assert "project_revision" not in response.body
    _assert_draft_only(before, _business(ready))
    document = _draft_document(ready)
    assert document["edges"] == []
    assert len(document["nodes"]) == 1
    node = document["nodes"][0]
    assert set(node) == {"node_id", "artifact_version_id"}
    assert node["node_id"] == "synthetic-node-1"
    assert node["artifact_version_id"] == "synthetic-art-v3"
    assert _status(ready, "synthetic-attach") == ("committed", 2)


@pytest.mark.integration
def test_attach_artifact_version_rejects_a_tombstone_without_new_rows(ready):
    _keep_candidate(
        ready,
        "synthetic-attach-tomb",
        "attach_artifact_version",
        None,
        _attach_proposal(artifact="synthetic-art-v1"),
    )
    with ready.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO privacy_tombstones (
                    id, project_id, object_type, object_id, reason_category, access_epoch,
                    audit_event_id, created_at
                ) VALUES (
                    'synthetic-tomb-art', :project, 'artifact_version', 'synthetic-art-v1',
                    'policy', 1, 'synthetic-audit', :now
                )
                """
            ),
            {"project": PROJECT_A, "now": NOW},
        )
    before = _business(ready)
    previews = _previews(ready)
    with pytest.raises(SourceRedacted):
        with write_uow(ready) as session:
            preview_candidate(
                session,
                principal=_principal(ACCOUNT_A),
                project_id=PROJECT_A,
                candidate_id="synthetic-attach-tomb",
                now=NOW,
            )
    _assert_nothing_new(ready, before, "synthetic-attach-tomb", previews)


@pytest.mark.integration
def test_layout_revision_does_not_invalidate_a_content_candidate(ready):
    view = _preview(
        ready,
        "synthetic-layout-ok",
        "add_node",
        None,
        _node_proposal("synthetic-layout-node"),
    )
    with write_uow(ready) as session:
        revision = save_layout(
            session,
            view_state_id="synthetic-view",
            account_id=ACCOUNT_A,
            project_id=PROJECT_A,
            page_id=PAGE_A,
            expected_layout_revision=7,
            layout={"marker": "synthetic-layout"},
            now=NOW + 1,
        )
    assert revision == 8
    before = _business(ready)
    with write_uow(ready) as session:
        _commit(session, "synthetic-layout-ok", view, key="synthetic-layout-key")
    _assert_draft_only(before, _business(ready))
    document = _draft_document(ready)
    assert "synthetic-layout-node" in {node["node_id"] for node in document["nodes"]}
    with ready.connect() as connection:
        layout_revision = connection.execute(
            text("SELECT layout_revision FROM view_states WHERE id = 'synthetic-view'")
        ).scalar_one()
    assert layout_revision == 8
    assert _status(ready, "synthetic-layout-ok") == ("committed", 2)


@pytest.mark.integration
def test_draft_content_change_requires_recheck(ready):
    view = _preview(
        ready,
        "synthetic-recheck",
        "add_node",
        None,
        _node_proposal("synthetic-recheck-node"),
    )
    dirty = {
        "schema_version": 1,
        "nodes": [
            {"node_id": "synthetic-node-1", "artifact_version_id": "synthetic-art-v1"},
            {"node_id": "synthetic-dirty"},
        ],
        "edges": [],
    }
    _replace_draft(ready, dirty)
    before = _business(ready)
    previews = _previews(ready)
    with pytest.raises(CandidateCommandError) as caught:
        with write_uow(ready) as session:
            _commit(session, "synthetic-recheck", view, key="synthetic-recheck-old")
    assert caught.value.code == _STALE
    _assert_same(before, _business(ready))
    assert _status(ready, "synthetic-recheck") == ("pending", 1)
    assert _previews(ready) == previews
    with pytest.raises(CandidateCommandError) as stale_preview:
        with write_uow(ready) as session:
            preview_candidate(
                session,
                principal=_principal(ACCOUNT_A),
                project_id=PROJECT_A,
                candidate_id="synthetic-recheck",
                now=NOW,
                expected_draft_content_revision=5,
            )
    assert stale_preview.value.code == _STALE
    assert _previews(ready) == previews
    with pytest.raises(CandidateCommandError) as changed_preview:
        with write_uow(ready) as session:
            preview_candidate(
                session,
                principal=_principal(ACCOUNT_A),
                project_id=PROJECT_A,
                candidate_id="synthetic-recheck",
                now=NOW,
                expected_page_version_id="synthetic-pv-a1",
            )
    assert changed_preview.value.code == _CHANGED
    assert _previews(ready) == previews
    _assert_same(before, _business(ready))
    with write_uow(ready) as session:
        again = preview_candidate(
            session,
            principal=_principal(ACCOUNT_A),
            project_id=PROJECT_A,
            candidate_id="synthetic-recheck",
            now=NOW + 2,
            expected_page_version_id="synthetic-pv-a2",
            expected_draft_content_revision=6,
        )
    with write_uow(ready) as session:
        response, _replayed = _commit(
            session,
            "synthetic-recheck",
            again,
            key="synthetic-recheck-new",
            draft_revision=6,
        )
    assert response.body["content_revision"] == 7
    assert "project_revision" not in response.body
    document = _draft_document(ready)
    assert {node["node_id"] for node in document["nodes"]} == {
        "synthetic-node-1",
        "synthetic-dirty",
        "synthetic-recheck-node",
    }
    assert _status(ready, "synthetic-recheck") == ("committed", 2)


@pytest.mark.integration
def test_suggest_stage_transition_confirms_like_stage_blank(ready):
    before = _business(ready)
    view = _preview(
        ready,
        "synthetic-suggest",
        "suggest_stage_transition",
        "stage_blank",
        _stage_proposal(),
    )
    with write_uow(ready) as session:
        result, _replayed = _commit(
            session,
            "synthetic-suggest",
            view,
            key="synthetic-suggest-key",
        )
    after = _business(ready)
    assert after["pages"] == before["pages"] + 1
    assert after["page_versions"] == before["page_versions"] + 1
    assert after["page_edges"] == before["page_edges"] + 1
    assert after["decisions"] == before["decisions"] + 1
    assert after["project_revision"] == before["project_revision"] + 1
    assert after["head_a"] == before["head_a"]
    assert result.body["project_revision"] == before["project_revision"] + 1
    with ready.connect() as connection:
        candidate = connection.execute(
            text("SELECT action, page_mode FROM candidates WHERE id = 'synthetic-suggest'")
        ).one()
        edge = connection.execute(
            text(
                """
                SELECT source_version_id, kind FROM page_edges
                WHERE target_version_id = :version
                """
            ),
            {"version": result.body["page_version_id"]},
        ).one()
        reason = connection.execute(
            text("SELECT reason FROM decisions WHERE id = :id"),
            {"id": result.body["decision_id"]},
        ).scalar_one()
        content = connection.execute(
            text("SELECT content_json FROM page_versions WHERE id = :version"),
            {"version": result.body["page_version_id"]},
        ).scalar_one()
    assert candidate.action == "suggest_stage_transition"
    assert candidate.page_mode == "stage_blank"
    assert edge.source_version_id == "synthetic-pv-a2"
    assert edge.kind == "stage_source"
    assert reason == "stage_blank"
    document = json.loads(content)
    assert content == canonical_document(document)
    assert document["nodes"] == []
    assert document["edges"] == []
    assert _status(ready, "synthetic-suggest") == ("committed", 2)
