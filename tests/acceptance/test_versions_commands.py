"""First T04 slice: two connections cannot both win the same draft revision."""

from threading import Barrier, Thread

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from sinuo.application.idempotency import CommandResponse, CommandScope, execute_idempotent
from sinuo.application.versions.drafts import DraftConflict, save_draft, update_draft
from sinuo.domain.public_contract import Action
from sinuo.infrastructure.database import write_uow
from sinuo.infrastructure.domain_models import AuditEvent, PageDraft
from sinuo.infrastructure.models import IdempotencyRecord
from tests.acceptance.test_domain_schema import NOW, PAGE_A, PROJECT_A, digest, seed


@pytest.fixture
def ready(engine):
    from tests.acceptance.test_domain_schema import require_domain_head

    require_domain_head(engine)
    with engine.begin() as connection:
        seed(connection)
    return engine


@pytest.mark.concurrency
def test_two_connections_cannot_both_advance_the_same_draft(ready):
    barrier = Barrier(2)
    first_body = '{"edges":[],"nodes":[],"schema_version":1}'
    second_body = (
        '{"edges":[],"nodes":[{"node_id":"synthetic-other"}],"schema_version":1}'
    )
    submitted = {first_body: "first", second_body: "second"}
    outcomes: dict[str, str] = {}

    def attempt(body: str) -> None:
        barrier.wait(timeout=5)
        try:
            with write_uow(ready) as session:
                update_draft(
                    session,
                    page_id=PAGE_A,
                    project_id=PROJECT_A,
                    base_version_id="synthetic-pv-a2",
                    expected_revision=5,
                    content_json=body,
                    content_hash=digest(body),
                    now=NOW + 1,
                )
            outcomes[body] = "ok"
        except DraftConflict:
            outcomes[body] = "conflict"

    first = Thread(target=attempt, args=(first_body,))
    second = Thread(target=attempt, args=(second_body,))
    first.start()
    second.start()
    first.join(timeout=10)
    second.join(timeout=10)
    assert not first.is_alive()
    assert not second.is_alive()
    assert sorted(outcomes.values()) == ["conflict", "ok"]
    winner = next(body for body, status in outcomes.items() if status == "ok")
    loser = next(body for body, status in outcomes.items() if status == "conflict")
    with ready.connect() as connection:
        row = connection.execute(
            text(
                """
                SELECT content_revision, content_json, content_hash
                FROM page_drafts WHERE page_id = :page
                """
            ),
            {"page": PAGE_A},
        ).one()
    assert row.content_revision == 6
    assert row.content_json == winner
    assert row.content_hash == digest(winner)
    assert row.content_json != loser
    assert submitted[winner] != submitted[loser]


@pytest.mark.recovery
def test_draft_failure_before_audit_rolls_back(ready):
    with ready.connect() as connection:
        draft = connection.execute(
            text(
                """
                SELECT base_version_id, content_revision, content_hash
                FROM page_drafts WHERE page_id = :page
                """
            ),
            {"page": PAGE_A},
        ).one()
    body = '{"edges":[],"nodes":[{"node_id":"synthetic-audit"}],"schema_version":1}'
    request = {
        "page_id": PAGE_A,
        "base_version_id": draft.base_version_id,
        "expected_revision": int(draft.content_revision),
        "content_hash": digest(body),
    }
    actor = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaa1"

    def handler(session: Session) -> CommandResponse:
        def before_audit() -> None:
            current = session.get(PageDraft, PAGE_A)
            assert current is not None
            assert current.content_revision == int(draft.content_revision) + 1
            audits = session.scalar(
                select(func.count())
                .select_from(AuditEvent)
                .where(AuditEvent.command == Action.PAGE_DRAFT_WRITE.value)
            )
            assert audits == 0
            raise RuntimeError("injected")

        save_draft(
            session,
            page_id=PAGE_A,
            project_id=PROJECT_A,
            base_version_id=draft.base_version_id,
            expected_revision=int(draft.content_revision),
            content_json=body,
            content_hash=digest(body),
            actor_id=actor,
            now=NOW + 3,
            before_audit=before_audit,
        )
        raise AssertionError("audit must stay unwritten")

    with pytest.raises(RuntimeError, match="injected"):
        with write_uow(ready) as session:
            execute_idempotent(
                session,
                CommandScope(PROJECT_A, actor, "page.draft.write", "synthetic-draft-fail"),
                request,
                lambda _session: None,
                handler,
            )
    with ready.connect() as connection:
        restored = connection.execute(
            text(
                "SELECT content_revision, content_hash FROM page_drafts WHERE page_id = :page"
            ),
            {"page": PAGE_A},
        ).one()
    assert restored.content_revision == draft.content_revision
    assert restored.content_hash == draft.content_hash
    with Session(ready) as session:
        assert (
            session.get(
                IdempotencyRecord,
                (PROJECT_A, actor, "page.draft.write", "synthetic-draft-fail"),
            )
            is None
        )
