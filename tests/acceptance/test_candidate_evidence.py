"""Confirm rechecks each evidence anchor the candidate already named."""

import pytest
from sqlalchemy import text

from sinuo.application.candidates import preview_candidate
from sinuo.domain.auth.errors import ActionForbidden, SourceRedacted
from sinuo.domain.public_contract import Action
from sinuo.infrastructure.database import write_uow
from sinuo.infrastructure.domain_models import ObjectGrant, PrivacyTombstone
from tests.acceptance import test_candidate_commit as commit_tests

ready = commit_tests.ready

_ANCHOR = "synthetic-evidence"


def _named(proposal: dict) -> dict:
    body = dict(proposal)
    body["evidence_anchor_ids"] = [_ANCHOR]
    return body


def _grant_evidence_read(engine, grant_id: str) -> None:
    with write_uow(engine) as session:
        session.add(
            ObjectGrant(
                id=grant_id,
                project_id=commit_tests.PROJECT_A,
                account_id=commit_tests.ACCOUNT_A,
                action=Action.EVIDENCE_READ.value,
                scope_type="evidence_anchor",
                status="active",
                created_at=commit_tests.NOW,
                revoked_at=None,
                created_by_account_id=commit_tests.ACCOUNT_A,
                stage_id=None,
                page_id=None,
                page_version_id=None,
                artifact_id=None,
                artifact_version_id=None,
                evidence_anchor_id=_ANCHOR,
            )
        )


@pytest.mark.integration
def test_tombstoned_evidence_anchor_rejects_commit_without_new_rows(ready):
    _grant_evidence_read(ready, "synthetic-grant-evidence-tomb")
    candidate_id = "synthetic-evidence-tomb"
    view = commit_tests._preview(
        ready,
        candidate_id,
        "create_page_from_versions",
        "stage_blank",
        _named(commit_tests._stage_proposal()),
    )
    with write_uow(ready) as session:
        session.add(
            PrivacyTombstone(
                id="synthetic-tomb-evidence",
                project_id=commit_tests.PROJECT_A,
                object_type="evidence_anchor",
                object_id=_ANCHOR,
                reason_category="policy",
                access_epoch=1,
                audit_event_id="synthetic-audit",
                created_at=commit_tests.NOW,
            )
        )
    before = commit_tests._business(ready)
    previews = commit_tests._previews(ready)
    with pytest.raises(SourceRedacted):
        with write_uow(ready) as session:
            commit_tests._commit(session, candidate_id, view, key="synthetic-evidence-tomb-key")
    commit_tests._assert_same(before, commit_tests._business(ready))
    assert commit_tests._status(ready, candidate_id) == ("pending", 1)
    assert commit_tests._previews(ready) == previews
    with pytest.raises(SourceRedacted):
        with write_uow(ready) as session:
            preview_candidate(
                session,
                principal=commit_tests._principal(commit_tests.ACCOUNT_A),
                project_id=commit_tests.PROJECT_A,
                candidate_id=candidate_id,
                now=commit_tests.NOW + 2,
            )
    commit_tests._assert_same(before, commit_tests._business(ready))
    assert commit_tests._previews(ready) == previews
    assert commit_tests._status(ready, candidate_id) == ("pending", 1)


@pytest.mark.integration
def test_revoked_evidence_read_rejects_commit_without_new_rows(ready):
    _grant_evidence_read(ready, "synthetic-grant-evidence-revoked")
    candidate_id = "synthetic-evidence-revoked"
    view = commit_tests._preview(
        ready,
        candidate_id,
        "draft_decision",
        None,
        _named(commit_tests._draft_proposal("synthetic-evidence-node")),
    )
    with ready.begin() as connection:
        connection.execute(
            text(
                """
                UPDATE object_grants
                SET status = 'revoked', revoked_at = :now
                WHERE id = :grant
                """
            ),
            {"now": commit_tests.NOW + 2, "grant": "synthetic-grant-evidence-revoked"},
        )
    before = commit_tests._business(ready)
    previews = commit_tests._previews(ready)
    with pytest.raises(ActionForbidden):
        with write_uow(ready) as session:
            commit_tests._commit(
                session,
                candidate_id,
                view,
                key="synthetic-evidence-revoked-key",
            )
    commit_tests._assert_same(before, commit_tests._business(ready))
    assert commit_tests._status(ready, candidate_id) == ("pending", 1)
    assert commit_tests._previews(ready) == previews
    with pytest.raises(ActionForbidden):
        with write_uow(ready) as session:
            preview_candidate(
                session,
                principal=commit_tests._principal(commit_tests.ACCOUNT_A),
                project_id=commit_tests.PROJECT_A,
                candidate_id=candidate_id,
                now=commit_tests.NOW + 3,
            )
    commit_tests._assert_same(before, commit_tests._business(ready))
    assert commit_tests._previews(ready) == previews
    assert commit_tests._status(ready, candidate_id) == ("pending", 1)
