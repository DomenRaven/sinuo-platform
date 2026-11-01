"""Replay returns the projected receipt after original read is revoked."""

import json
import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from sinuo.domain.auth.projection import project_receipt
from sinuo.domain.public_contract import Action
from sinuo.infrastructure.database import write_uow
from sinuo.infrastructure.domain_models import ObjectGrant
from sinuo.infrastructure.models import IdempotencyRecord
from tests.acceptance import test_candidate_commit as commit_tests

ready = commit_tests.ready

_CANDIDATE = "synthetic-projection"
_KEY = "synthetic-projection-key"
_PROSE_KEYS = frozenset(
    {
        "body",
        "content",
        "content_json",
        "excerpt",
        "original",
        "quote",
        "summary",
        "text",
        "title",
    }
)


def _grant_original_read(engine) -> None:
    with write_uow(engine) as session:
        session.add(
            ObjectGrant(
                id=str(uuid.uuid4()),
                project_id=commit_tests.PROJECT_A,
                account_id=commit_tests.ACCOUNT_A,
                action=Action.ARTIFACT_ORIGINAL_READ.value,
                scope_type="project",
                status="active",
                created_at=commit_tests.NOW,
                revoked_at=None,
                created_by_account_id=commit_tests.ACCOUNT_A,
                stage_id=None,
                page_id=None,
                page_version_id=None,
                artifact_id=None,
                artifact_version_id=None,
                evidence_anchor_id=None,
            )
        )


def _stored_receipt(engine) -> tuple[int, dict]:
    key = (
        commit_tests.PROJECT_A,
        commit_tests.ACCOUNT_A,
        "candidate.commit",
        _KEY,
    )
    with Session(engine) as session:
        stored = session.get(IdempotencyRecord, key)
        assert stored is not None
        body = stored.response_json
        assert isinstance(body, dict)
        return stored.response_status, json.loads(json.dumps(body))


def _grant_status(engine) -> dict[str, str]:
    with engine.connect() as connection:
        rows = connection.execute(
            text(
                """
                SELECT action, status FROM object_grants
                WHERE project_id = :project AND account_id = :account
                """
            ),
            {"project": commit_tests.PROJECT_A, "account": commit_tests.ACCOUNT_A},
        )
    return {row.action: row.status for row in rows}


def _counts(engine) -> dict[str, int]:
    names = ("candidates", "pages", "page_drafts", "candidate_dispositions")
    with engine.connect() as connection:
        found = {
            name: int(connection.execute(text(f"SELECT count(*) FROM {name}")).scalar_one())
            for name in names
        }
        found["this_disposition"] = int(
            connection.execute(
                text(
                    """
                    SELECT count(*) FROM candidate_dispositions
                    WHERE candidate_id = :candidate
                    """
                ),
                {"candidate": _CANDIDATE},
            ).scalar_one()
        )
    return found


def _keys(value: object) -> set[str]:
    found: set[str] = set()
    if isinstance(value, dict):
        for key, item in value.items():
            found.add(key)
            found.update(_keys(item))
    elif isinstance(value, list):
        for item in value:
            found.update(_keys(item))
    return found


def _draft_text(engine) -> str:
    with engine.connect() as connection:
        raw = connection.execute(
            text("SELECT content_json FROM page_drafts WHERE page_id = :page"),
            {"page": commit_tests.PAGE_A},
        ).scalar_one()
    assert isinstance(raw, str)
    return raw


@pytest.mark.integration
def test_replay_projects_stored_receipt_after_original_read_is_revoked(ready):
    _grant_original_read(ready)
    patch = {"node_id": "synthetic-projected-node"}
    view = commit_tests._preview(
        ready,
        _CANDIDATE,
        "draft_decision",
        None,
        commit_tests._draft_proposal(),
        patch,
    )
    with write_uow(ready) as session:
        first, replayed = commit_tests._commit(
            session,
            _CANDIDATE,
            view,
            key=_KEY,
            patch=patch,
        )
    assert replayed is False
    assert first.status == 201
    status, stored = _stored_receipt(ready)
    assert status == 201
    assert isinstance(stored["title"], str) and stored["title"]
    assert isinstance(stored["summary"], str) and stored["summary"]
    projected = project_receipt(stored)
    assert first.body == projected
    assert _PROSE_KEYS.isdisjoint(_keys(first.body))

    with ready.begin() as connection:
        revoked = connection.execute(
            text(
                """
                UPDATE object_grants
                SET status = 'revoked', revoked_at = :now
                WHERE project_id = :project AND account_id = :account
                  AND action = 'artifact.original.read' AND status = 'active'
                """
            ),
            {
                "now": commit_tests.NOW + 2,
                "project": commit_tests.PROJECT_A,
                "account": commit_tests.ACCOUNT_A,
            },
        )
    assert revoked.rowcount == 1
    grants = _grant_status(ready)
    assert grants[Action.ARTIFACT_ORIGINAL_READ.value] == "revoked"
    assert grants[Action.PAGE_DRAFT_WRITE.value] == "active"
    assert grants[Action.PAGE_READ.value] == "active"
    assert grants[Action.DECISION_CONFIRM.value] == "active"

    consumed = commit_tests._status(ready, _CANDIDATE)
    assert consumed == ("committed", 2)
    before = commit_tests._business(ready)
    counts = _counts(ready)
    assert counts["this_disposition"] == 1

    with write_uow(ready) as session:
        second, replayed = commit_tests._commit(
            session,
            _CANDIDATE,
            view,
            key=_KEY,
            patch=patch,
        )
    assert replayed is True
    assert second.status == 200
    assert second.body == projected
    assert second.body == project_receipt(stored)
    assert _PROSE_KEYS.isdisjoint(_keys(second.body))
    rendered = json.dumps(second.body, ensure_ascii=False)
    assert stored["title"] not in rendered
    assert stored["summary"] not in rendered
    assert _draft_text(ready) not in rendered

    kept_status, kept = _stored_receipt(ready)
    assert kept_status == 201
    assert kept["title"] == stored["title"]
    assert kept["summary"] == stored["summary"]
    assert commit_tests._status(ready, _CANDIDATE) == consumed
    assert commit_tests._business(ready) == before
    assert _counts(ready) == counts
