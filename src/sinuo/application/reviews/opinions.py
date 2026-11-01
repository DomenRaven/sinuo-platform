"""Leave and edit one review opinion. The caller owns write_uow and commit.

The opinion stores the path's project and the signed-in actor. It never copies
a client-supplied author. page_version_id and node_id are written once.
A later page version is a new row; this command does not retarget old opinions
and does not change design nodes.
"""

import json
import uuid
from dataclasses import dataclass

from sqlalchemy import select, update
from sqlalchemy.orm import Session
from sqlalchemy.orm.util import identity_key

from sinuo.application.versions.pages import ProjectRevisionConflict
from sinuo.domain.auth.errors import ActionForbidden, InvalidSchema, NotFoundOrHidden
from sinuo.domain.public_contract import Action
from sinuo.domain.schema_types import (
    VISIBILITY_LEVELS,
    DocumentSchemaError,
    assert_opinion_anchor,
    canonical_audit_payload,
)
from sinuo.infrastructure.domain_models import (
    AuditEvent,
    PageVersion,
    PrivacyTombstone,
    Project,
    ReviewOpinion,
)

_MAX_BODY = 8000


class OpinionRevisionConflict(Exception):
    """expected_revision did not match the stored opinion revision."""


class OpinionAnchorInvalid(Exception):
    """node_id is not a node in the anchored page version."""


@dataclass(frozen=True)
class OpinionAnchor:
    opinion_id: str
    project_id: str
    page_version_id: str
    author_account_id: str
    tombstoned: bool


@dataclass(frozen=True)
class OpinionView:
    opinion_id: str
    project_id: str
    author_account_id: str
    page_version_id: str
    node_id: str | None
    body: str
    revision: int
    visibility: str
    created_at: float
    updated_at: float
    audit_event_id: str | None = None
    project_revision: int | None = None


def load_opinion(session: Session, opinion_id: str) -> OpinionAnchor:
    """Resolve the row's project and anchor. Missing rows stay hidden."""

    row = session.get(ReviewOpinion, opinion_id)
    if row is None:
        raise NotFoundOrHidden()
    version = session.get(PageVersion, row.page_version_id)
    if version is None or version.project_id != row.project_id:
        raise NotFoundOrHidden()
    return OpinionAnchor(
        opinion_id=row.id,
        project_id=row.project_id,
        page_version_id=row.page_version_id,
        author_account_id=row.author_account_id,
        tombstoned=_tombstoned(session, row.project_id, row.id),
    )


def read_opinion(session: Session, opinion_id: str) -> OpinionView:
    """Return the stored opinion. The caller has already authorized the read."""

    row = session.get(ReviewOpinion, opinion_id)
    if row is None:
        raise NotFoundOrHidden()
    return _view(row)


def create_opinion(
    session: Session,
    *,
    project_id: str,
    actor_id: str,
    page_version_id: str,
    node_id: str | None,
    body: str,
    visibility: str,
    now: float,
) -> OpinionView:
    """Insert one opinion bound to this page version. Does not commit.

    Project revision advances by one. The page version row is not updated.
    """

    version = session.get(PageVersion, page_version_id)
    if version is None or version.project_id != project_id:
        raise NotFoundOrHidden()
    stored_body = _body(body)
    stored_visibility = _visibility(visibility)
    stored_node = _node_id(node_id)
    _require_anchor(version.content_json, stored_node)
    project_revision = _advance_project_revision(session, project_id)
    opinion_id = str(uuid.uuid4())
    session.add(
        ReviewOpinion(
            id=opinion_id,
            project_id=project_id,
            author_account_id=actor_id,
            page_version_id=version.id,
            node_id=stored_node,
            body=stored_body,
            revision=1,
            visibility=stored_visibility,
            created_at=now,
            updated_at=now,
        )
    )
    session.flush()
    audit_event_id = _audit(
        session,
        project_id=project_id,
        actor_id=actor_id,
        command=Action.OPINION_CREATE.value,
        opinion_id=opinion_id,
        page_version_id=version.id,
        node_id=stored_node,
        revision=1,
        visibility=stored_visibility,
        project_revision=project_revision,
        now=now,
    )
    row = session.get(ReviewOpinion, opinion_id)
    if row is None:
        raise NotFoundOrHidden()
    return _view(row, audit_event_id=audit_event_id, project_revision=project_revision)


def edit_opinion(
    session: Session,
    *,
    opinion_id: str,
    project_id: str,
    actor_id: str,
    expected_revision: int,
    body: str,
    now: float,
) -> OpinionView:
    """Replace the author's text when revision matches. Does not commit.

    The conditional update never writes page_version_id, node_id, or author.
    A later page version therefore cannot be applied by this command.
    """

    if type(expected_revision) is not int or expected_revision < 1:
        raise InvalidSchema()
    stored_body = _body(body)
    anchor = load_opinion(session, opinion_id)
    if anchor.project_id != project_id:
        raise NotFoundOrHidden()
    if anchor.author_account_id != actor_id:
        raise ActionForbidden(Action.OPINION_EDIT_OWN.value)
    changed = session.execute(
        update(ReviewOpinion)
        .where(
            ReviewOpinion.id == opinion_id,
            ReviewOpinion.project_id == project_id,
            ReviewOpinion.author_account_id == actor_id,
            ReviewOpinion.revision == expected_revision,
        )
        .values(
            body=stored_body,
            revision=expected_revision + 1,
            updated_at=now,
        ),
        execution_options={"synchronize_session": False},
    )
    if changed.rowcount != 1:
        raise OpinionRevisionConflict(opinion_id)
    project_revision = _advance_project_revision(session, project_id)
    row = _reload(session, opinion_id)
    if (
        row.page_version_id != anchor.page_version_id
        or row.author_account_id != actor_id
        or row.revision != expected_revision + 1
    ):
        raise OpinionRevisionConflict(opinion_id)
    audit_event_id = _audit(
        session,
        project_id=project_id,
        actor_id=actor_id,
        command=Action.OPINION_EDIT_OWN.value,
        opinion_id=opinion_id,
        page_version_id=row.page_version_id,
        node_id=row.node_id,
        revision=row.revision,
        visibility=row.visibility,
        project_revision=project_revision,
        now=now,
    )
    return _view(row, audit_event_id=audit_event_id, project_revision=project_revision)


def _advance_project_revision(session: Session, project_id: str) -> int:
    current = session.scalar(select(Project.revision).where(Project.id == project_id))
    if type(current) is not int:
        raise NotFoundOrHidden()
    result = session.execute(
        update(Project)
        .where(Project.id == project_id, Project.revision == current)
        .values(revision=current + 1),
        execution_options={"synchronize_session": False},
    )
    if result.rowcount != 1:
        raise ProjectRevisionConflict(project_id)
    cached = session.identity_map.get(identity_key(Project, (project_id,)))
    if cached is not None:
        session.expire(cached)
    return current + 1


def _reload(session: Session, opinion_id: str) -> ReviewOpinion:
    cached = session.identity_map.get(identity_key(ReviewOpinion, (opinion_id,)))
    if cached is not None:
        session.expire(cached)
    row = session.get(ReviewOpinion, opinion_id)
    if row is None:
        raise NotFoundOrHidden()
    return row


def _audit(
    session: Session,
    *,
    project_id: str,
    actor_id: str,
    command: str,
    opinion_id: str,
    page_version_id: str,
    node_id: str | None,
    revision: int,
    visibility: str,
    project_revision: int,
    now: float,
) -> str:
    payload: dict[str, object] = {
        "opinion_id": opinion_id,
        "page_version_id": page_version_id,
        "project_revision": project_revision,
        "revision": revision,
        "visibility": visibility,
    }
    if node_id is not None:
        payload["node_id"] = node_id
    audit_event_id = str(uuid.uuid4())
    session.add(
        AuditEvent(
            id=audit_event_id,
            project_id=project_id,
            actor_id=actor_id,
            command=command,
            subject_type="review_opinion",
            subject_id=opinion_id,
            project_revision=project_revision,
            payload=canonical_audit_payload(payload),
            created_at=now,
        )
    )
    session.flush()
    return audit_event_id


def _tombstoned(session: Session, project_id: str, opinion_id: str) -> bool:
    found = session.scalar(
        select(PrivacyTombstone.id).where(
            PrivacyTombstone.project_id == project_id,
            PrivacyTombstone.object_type == "review_opinion",
            PrivacyTombstone.object_id == opinion_id,
        )
    )
    return found is not None


def _body(body: str) -> str:
    if not isinstance(body, str) or not body.strip() or len(body) > _MAX_BODY:
        raise InvalidSchema()
    return body


def _visibility(visibility: str) -> str:
    if visibility not in VISIBILITY_LEVELS:
        raise InvalidSchema()
    return visibility


def _node_id(node_id: str | None) -> str | None:
    if node_id is None:
        return None
    if not isinstance(node_id, str) or not node_id.strip() or len(node_id) > 64:
        raise InvalidSchema()
    return node_id


def _require_anchor(content_json: str, node_id: str | None) -> None:
    if node_id is None:
        return
    try:
        document = json.loads(content_json)
    except json.JSONDecodeError as error:
        raise InvalidSchema() from error
    if not isinstance(document, dict):
        raise InvalidSchema()
    try:
        assert_opinion_anchor(document, node_id)
    except DocumentSchemaError as error:
        raise OpinionAnchorInvalid(node_id) from error


def _view(
    row: ReviewOpinion,
    *,
    audit_event_id: str | None = None,
    project_revision: int | None = None,
) -> OpinionView:
    return OpinionView(
        opinion_id=row.id,
        project_id=row.project_id,
        author_account_id=row.author_account_id,
        page_version_id=row.page_version_id,
        node_id=row.node_id,
        body=row.body,
        revision=row.revision,
        visibility=row.visibility,
        created_at=row.created_at,
        updated_at=row.updated_at,
        audit_event_id=audit_event_id,
        project_revision=project_revision,
    )
