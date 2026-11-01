"""Commit or reject one pending candidate inside the caller's write unit.

``consume_candidate`` is the only status write. The target, disposition, audit,
outbox, run event, and idempotency receipt share that unit.
"""

import json
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.orm import Session
from sqlalchemy.orm.util import identity_key

from sinuo.api.events.sequence import allocate_run_event
from sinuo.application.auth.authorizer import SessionAuthorizer
from sinuo.application.candidates.errors import CandidateCommandError
from sinuo.application.candidates.gate import authorize_proposal
from sinuo.application.candidates.proposal import (
    ParsedProposal,
    bound_preview_hash,
    parse_proposal,
    sorted_sources,
    source_document,
)
from sinuo.application.cas import CasMismatch, consume_candidate
from sinuo.application.idempotency import (
    CommandResponse,
    CommandScope,
    execute_idempotent,
)
from sinuo.application.versions.branches import _clone_document
from sinuo.application.versions.drafts import DraftConflict, update_draft
from sinuo.domain.auth.errors import AuthRequired, InvalidSchema, NotFoundOrHidden
from sinuo.domain.auth.principal import Principal
from sinuo.domain.auth.projection import project_receipt
from sinuo.domain.auth.resource import ResourceRef
from sinuo.domain.public_contract import Action, ErrorCode
from sinuo.domain.schema_types import (
    LINEAGE_KINDS,
    DocumentSchemaError,
    assert_closed,
    canonical_audit_payload,
    canonical_document,
    canonical_json,
    canonical_reason,
    document_hash,
)
from sinuo.infrastructure.domain_models import (
    AgentRun,
    AuditEvent,
    Candidate,
    CandidateDisposition,
    CandidatePreview,
    CandidatePreviewSource,
    Decision,
    Page,
    PageDraft,
    PageEdge,
    PageVersion,
    Project,
)
from sinuo.infrastructure.models import IdempotencyRecord, OutboxEvent

COMMIT_COMMAND = "candidate.commit"
REJECT_COMMAND = "candidate.reject"
_CHANGED = ErrorCode.PREVIEW_CHANGED.value
_EXPIRED = ErrorCode.PREVIEW_EXPIRED.value
_STALE = ErrorCode.CANDIDATE_STALE.value
_RESOLVED = ErrorCode.CANDIDATE_ALREADY_RESOLVED.value


@dataclass(frozen=True)
class _Effect:
    page_id: str | None
    page_version_id: str | None
    content_revision: int | None
    content_hash: str | None
    project_revision: int | None
    edge_ids: tuple[str, ...]
    decision_id: str | None
    title: str


def commit_candidate(
    session: Session,
    *,
    principal: Principal,
    project_id: str,
    candidate_id: str,
    preview_id: str,
    preview_hash: str,
    expected_page_version_id: str,
    expected_draft_content_revision: int | None,
    request_key: str,
    now: float,
    user_patch: Mapping[str, Any] | None = None,
    before_effect: Callable[[Session], None] | None = None,
) -> tuple[CommandResponse, bool]:
    """Consume one saved preview. Does not commit the caller's unit."""

    scope = CommandScope(project_id, principal.account_id, COMMIT_COMMAND, request_key)
    request = {
        "candidate_id": candidate_id,
        "expected_draft_content_revision": expected_draft_content_revision,
        "expected_page_version_id": expected_page_version_id,
        "preview_hash": preview_hash,
        "preview_id": preview_id,
        "user_patch": None if user_patch is None else dict(user_patch),
    }
    return _run(
        session,
        scope,
        request,
        principal,
        lambda active: _proposal_for_auth(active, project_id, candidate_id, preview_id),
        lambda active: _commit(
            active,
            principal=principal,
            project_id=project_id,
            candidate_id=candidate_id,
            preview_id=preview_id,
            preview_hash=preview_hash,
            expected_page_version_id=expected_page_version_id,
            expected_draft_content_revision=expected_draft_content_revision,
            now=now,
            user_patch=user_patch,
            before_effect=before_effect,
        ),
    )


def reject_candidate(
    session: Session,
    *,
    principal: Principal,
    project_id: str,
    candidate_id: str,
    request_key: str,
    now: float,
    reason: str | None = None,
    before_effect: Callable[[Session], None] | None = None,
) -> tuple[CommandResponse, bool]:
    """Record a rejection. Does not commit, and does not change the business graph."""

    cleaned = _reason(reason)
    scope = CommandScope(project_id, principal.account_id, REJECT_COMMAND, request_key)
    request = {"candidate_id": candidate_id, "reason": cleaned}
    return _run(
        session,
        scope,
        request,
        principal,
        lambda active: _proposal_for_auth(active, project_id, candidate_id, None),
        lambda active: _reject(
            active,
            principal=principal,
            project_id=project_id,
            candidate_id=candidate_id,
            now=now,
            reason=cleaned,
            before_effect=before_effect,
        ),
    )


def _run(
    session: Session,
    scope: CommandScope,
    request: dict[str, Any],
    principal: Principal,
    load_proposal: Callable[[Session], ParsedProposal],
    handler: Callable[[Session], CommandResponse],
) -> tuple[CommandResponse, bool]:
    if scope.actor_id != principal.account_id:
        raise AuthRequired()
    key = (scope.project_id, scope.actor_id, scope.command, scope.request_key)
    replayed = session.get(IdempotencyRecord, key) is not None

    def authorize(active: Session) -> None:
        authorize_proposal(active, principal, scope.project_id, load_proposal(active))

    response = execute_idempotent(session, scope, request, authorize, handler)
    status = 200 if replayed else response.status
    return CommandResponse(status, project_receipt(response.body)), replayed


def _commit(
    session: Session,
    *,
    principal: Principal,
    project_id: str,
    candidate_id: str,
    preview_id: str,
    preview_hash: str,
    expected_page_version_id: str,
    expected_draft_content_revision: int | None,
    now: float,
    user_patch: Mapping[str, Any] | None,
    before_effect: Callable[[Session], None] | None,
) -> CommandResponse:
    candidate = _candidate(session, project_id, candidate_id)
    preview = _preview(session, project_id, candidate_id, preview_id)
    sources = _stored_sources(session, preview.id)
    _bind(
        session,
        principal=principal,
        project_id=project_id,
        candidate=candidate,
        preview=preview,
        sources=sources,
        preview_hash=preview_hash,
        now=now,
        user_patch=user_patch,
    )
    if candidate.status != "pending":
        raise _resolved(session, project_id, candidate_id)
    proposal = parse_proposal(
        candidate.action,
        candidate.page_mode,
        _object(preview.effective_proposal_json),
    )
    _expected(
        session,
        proposal,
        sources,
        expected_page_version_id,
        expected_draft_content_revision,
    )
    authorize_proposal(session, principal, project_id, proposal)
    try:
        _live(session, principal, project_id, proposal, sources)
    except CandidateCommandError as exc:
        if exc.code != _STALE or _status_now(session, project_id, candidate_id) == "pending":
            raise
        raise _resolved(session, project_id, candidate_id) from exc
    if candidate.revision != preview.candidate_revision:
        raise _resolved(session, project_id, candidate_id)
    run_id = candidate.run_id
    snapshot_id = candidate.context_snapshot_id
    revision = candidate.revision
    _consume(session, project_id, candidate_id, revision, "committed")
    effect = _apply(session, project_id, principal.account_id, proposal, now)
    session.flush()
    if before_effect is not None:
        before_effect(session)
    return _finish(
        session,
        principal=principal,
        project_id=project_id,
        candidate_id=candidate_id,
        run_id=run_id,
        snapshot_id=snapshot_id,
        preview_id=preview.id,
        outcome="committed",
        reason=None,
        now=now,
        effect=effect,
        event_type="candidate.committed",
        command=_command_name(proposal),
    )


def _reject(
    session: Session,
    *,
    principal: Principal,
    project_id: str,
    candidate_id: str,
    now: float,
    reason: str | None,
    before_effect: Callable[[Session], None] | None,
) -> CommandResponse:
    candidate = _candidate(session, project_id, candidate_id)
    if candidate.status != "pending":
        raise _resolved(session, project_id, candidate_id)
    run_id = candidate.run_id
    snapshot_id = candidate.context_snapshot_id
    revision = candidate.revision
    _consume(session, project_id, candidate_id, revision, "rejected")
    session.flush()
    if before_effect is not None:
        before_effect(session)
    return _finish(
        session,
        principal=principal,
        project_id=project_id,
        candidate_id=candidate_id,
        run_id=run_id,
        snapshot_id=snapshot_id,
        preview_id=None,
        outcome="rejected",
        reason=reason,
        now=now,
        effect=None,
        event_type="candidate.rejected",
        command=REJECT_COMMAND,
    )


def _bind(
    session: Session,
    *,
    principal: Principal,
    project_id: str,
    candidate: Candidate,
    preview: CandidatePreview,
    sources: list[dict[str, Any]],
    preview_hash: str,
    now: float,
    user_patch: Mapping[str, Any] | None,
) -> None:
    client_hash = _hex_hash(preview_hash)
    recomputed = bound_preview_hash(
        account_id=preview.account_id,
        candidate_id=preview.candidate_id,
        candidate_revision=preview.candidate_revision,
        action=candidate.action,
        page_mode=candidate.page_mode,
        access_epoch=preview.access_epoch,
        user_patch=_object(preview.user_patch_json),
        effective_proposal=_object(preview.effective_proposal_json),
        sources=sources,
        expires_at=float(preview.expires_at),
    )
    if client_hash != preview.preview_hash or recomputed != preview.preview_hash:
        raise CandidateCommandError(_CHANGED)
    if preview.account_id != principal.account_id:
        raise CandidateCommandError(_CHANGED)
    epoch = session.scalar(select(Project.access_epoch).where(Project.id == project_id))
    if epoch != preview.access_epoch:
        raise CandidateCommandError(_CHANGED)
    if user_patch is not None and _presented_patch(user_patch) != preview.user_patch_json:
        raise CandidateCommandError(_CHANGED)
    if isinstance(now, bool) or not isinstance(now, (int, float)):
        raise CandidateCommandError(_EXPIRED)
    if float(now) >= float(preview.expires_at):
        raise CandidateCommandError(_EXPIRED)


def _expected(
    session: Session,
    proposal: ParsedProposal,
    sources: list[dict[str, Any]],
    expected_page_version_id: str,
    expected_draft_content_revision: int | None,
) -> None:
    if not isinstance(expected_page_version_id, str):
        raise CandidateCommandError(_CHANGED)
    revision = expected_draft_content_revision
    if revision is not None and type(revision) is not int:
        raise CandidateCommandError(_CHANGED)
    if proposal.kind == "history":
        if expected_page_version_id != proposal.source_version_id or revision is not None:
            raise CandidateCommandError(_CHANGED)
        return
    if proposal.kind == "draft":
        stamp = _stamp(sources, proposal.page_id or "")
        if (
            expected_page_version_id != proposal.base_version_id
            or stamp is None
            or expected_draft_content_revision != stamp["draft_content_revision"]
        ):
            raise CandidateCommandError(_CHANGED)
        return
    version_ids = [
        item["page_version_id"] for item in sources if item["source_kind"] == "page_version"
    ]
    if expected_page_version_id not in version_ids:
        raise CandidateCommandError(_CHANGED)
    version = session.get(PageVersion, expected_page_version_id)
    if version is None:
        raise CandidateCommandError(_CHANGED)
    stamp = _stamp(sources, version.page_id)
    if stamp is None:
        if expected_draft_content_revision is not None:
            raise CandidateCommandError(_CHANGED)
        return
    if expected_draft_content_revision != stamp["draft_content_revision"]:
        raise CandidateCommandError(_CHANGED)


def _live(
    session: Session,
    principal: Principal,
    project_id: str,
    proposal: ParsedProposal,
    sources: list[dict[str, Any]],
) -> None:
    if proposal.kind == "draft":
        version = _version(session, project_id, proposal.base_version_id or "")
        if version.page_id != proposal.page_id:
            raise NotFoundOrHidden()
        draft = _draft(session, project_id, proposal.page_id or "")
        stamp = _stamp(sources, draft.page_id)
        if (
            stamp is None
            or draft.base_version_id != version.id
            or draft.content_revision != stamp["draft_content_revision"]
            or draft.content_hash != stamp["draft_content_hash"]
        ):
            raise CandidateCommandError(_STALE)
        return
    if proposal.kind == "history":
        version = _version(session, project_id, proposal.source_version_id or "")
        _authorize_referenced_artifacts(session, principal, project_id, version.content_json)
        return
    for item in sources:
        if item["source_kind"] != "page_version":
            continue
        version = _version(session, project_id, str(item["page_version_id"]))
        page = _page(session, project_id, version.page_id)
        if page.head_version_id != version.id or page.head_version_id is None:
            raise CandidateCommandError(_STALE)
        head = _version(session, project_id, page.head_version_id)
        draft = session.get(PageDraft, page.id)
        if draft is not None and draft.content_hash != head.content_hash:
            raise CandidateCommandError(_STALE)
        stamp = _stamp(sources, page.id)
        if draft is None:
            if stamp is not None:
                raise CandidateCommandError(_STALE)
            continue
        if (
            stamp is None
            or draft.content_revision != stamp["draft_content_revision"]
            or draft.content_hash != stamp["draft_content_hash"]
        ):
            raise CandidateCommandError(_STALE)


def _apply(
    session: Session,
    project_id: str,
    actor_id: str,
    proposal: ParsedProposal,
    now: float,
) -> _Effect:
    if proposal.kind == "draft":
        return _apply_draft(session, project_id, proposal, now)
    if proposal.kind == "history":
        return _apply_history(session, project_id, actor_id, proposal, now)
    return _apply_blank(session, project_id, actor_id, proposal, now)


def _apply_draft(
    session: Session,
    project_id: str,
    proposal: ParsedProposal,
    now: float,
) -> _Effect:
    page_id = proposal.page_id or ""
    draft = _draft(session, project_id, page_id)
    body, digest = projected_draft(draft.content_json, proposal)
    title = proposal.edge_id if proposal.action == "add_typed_edge" else proposal.node_id
    try:
        revision = update_draft(
            session,
            page_id=page_id,
            project_id=project_id,
            base_version_id=proposal.base_version_id or "",
            expected_revision=draft.content_revision,
            content_json=body,
            content_hash=digest,
            now=now,
        )
    except DraftConflict as error:
        raise CandidateCommandError(ErrorCode.DRAFT_REVISION_CONFLICT.value) from error
    loaded = session.get(PageDraft, page_id)
    if loaded is not None:
        session.expire(loaded)
    return _Effect(page_id, None, revision, digest, None, (), None, title or page_id)


def _apply_blank(
    session: Session,
    project_id: str,
    actor_id: str,
    proposal: ParsedProposal,
    now: float,
) -> _Effect:
    project_revision = _advance_project_revision(session, project_id)
    body, digest = _blank_content(proposal.citations)
    page_id, version_id = _insert_page(
        session,
        project_id=project_id,
        stage_id=proposal.target_stage_id or "",
        title=proposal.title or "",
        content_json=body,
        content_hash=digest,
        project_revision=project_revision,
        actor_id=actor_id,
        now=now,
    )
    kind_name = "merge_source" if proposal.kind == "merge" else "stage_source"
    kind = assert_closed(kind_name, LINEAGE_KINDS, name="lineage_kind")
    edge_ids: list[str] = []
    for version_id_source in proposal.source_version_ids:
        edge_id = str(uuid.uuid4())
        session.add(
            PageEdge(
                id=edge_id,
                project_id=project_id,
                source_version_id=version_id_source,
                target_version_id=version_id,
                kind=kind,
                project_revision=project_revision,
                reason=_edge_reason(proposal, version_id_source),
                created_at=now,
            )
        )
        edge_ids.append(edge_id)
    decision_id = str(uuid.uuid4())
    session.add(
        Decision(
            id=decision_id,
            project_id=project_id,
            account_id=actor_id,
            body=proposal.title or "",
            reason=proposal.page_mode or kind_name,
            page_version_id=version_id,
            artifact_version_id=None,
            stage_id=None,
            supersedes_id=None,
            project_revision=project_revision,
            created_at=now,
        )
    )
    session.flush()
    return _Effect(
        page_id,
        version_id,
        1,
        digest,
        project_revision,
        tuple(edge_ids),
        decision_id,
        proposal.title or "",
    )


def _apply_history(
    session: Session,
    project_id: str,
    actor_id: str,
    proposal: ParsedProposal,
    now: float,
) -> _Effect:
    source = _version(session, project_id, proposal.source_version_id or "")
    content_json, content_hash, reason = _clone_document(source.content_json)
    project_revision = _advance_project_revision(session, project_id)
    page_id, version_id = _insert_page(
        session,
        project_id=project_id,
        stage_id=proposal.target_stage_id or "",
        title=proposal.title or "",
        content_json=content_json,
        content_hash=content_hash,
        project_revision=project_revision,
        actor_id=actor_id,
        now=now,
    )
    edge_id = str(uuid.uuid4())
    session.add(
        PageEdge(
            id=edge_id,
            project_id=project_id,
            source_version_id=source.id,
            target_version_id=version_id,
            kind=assert_closed("branches_from", LINEAGE_KINDS, name="lineage_kind"),
            project_revision=project_revision,
            reason=reason,
            created_at=now,
        )
    )
    session.flush()
    return _Effect(
        page_id,
        version_id,
        1,
        content_hash,
        project_revision,
        (edge_id,),
        None,
        proposal.title or "",
    )


def _finish(
    session: Session,
    *,
    principal: Principal,
    project_id: str,
    candidate_id: str,
    run_id: str,
    snapshot_id: str,
    preview_id: str | None,
    outcome: str,
    reason: str | None,
    now: float,
    effect: _Effect | None,
    event_type: str,
    command: str,
) -> CommandResponse:
    disposition_id = str(uuid.uuid4())
    session.add(
        CandidateDisposition(
            id=disposition_id,
            candidate_id=candidate_id,
            project_id=project_id,
            account_id=principal.account_id,
            outcome=outcome,
            preview_id=preview_id,
            reason=reason,
            created_at=float(now),
        )
    )
    audit_event_id = str(uuid.uuid4())
    page_id = None if effect is None else effect.page_id
    version_id = None if effect is None else effect.page_version_id
    subject_id = candidate_id if page_id is None else page_id
    subject_type = "candidate" if version_id is None else "page"
    if version_id is None and page_id is not None:
        subject_type = "page_draft"
    payload: dict[str, Any] = {
        "candidate_id": candidate_id,
        "disposition_id": disposition_id,
        "synthetic": True,
    }
    if effect is not None and effect.page_id is not None:
        payload["page_id"] = effect.page_id
    if effect is not None and effect.page_version_id is not None:
        payload["page_version_id"] = effect.page_version_id
    if effect is not None and effect.project_revision is not None:
        payload["project_revision"] = effect.project_revision
    session.add(
        AuditEvent(
            id=audit_event_id,
            project_id=project_id,
            actor_id=principal.account_id,
            command=command,
            subject_type=subject_type,
            subject_id=subject_id,
            project_revision=None if effect is None else effect.project_revision,
            payload=canonical_audit_payload(payload),
            created_at=float(now),
        )
    )
    created_page = (
        effect is not None
        and effect.page_version_id is not None
        and effect.project_revision is not None
    )
    if created_page:
        outbox_type = "page.version_created"
        outbox_payload: dict[str, Any] = {
            "page_id": effect.page_id,
            "page_version_id": effect.page_version_id,
            "project_id": project_id,
            "project_revision": effect.project_revision,
            "synthetic": True,
        }
    else:
        outbox_type = event_type
        outbox_payload = {
            "candidate_id": candidate_id,
            "disposition_id": disposition_id,
            "status": outcome,
            "synthetic": True,
        }
    session.add(
        OutboxEvent(
            event_id=str(uuid.uuid4()),
            project_id=project_id,
            event_type=outbox_type,
            schema_version=1,
            payload=outbox_payload,
            created_at=float(now),
        )
    )
    expected_seq = session.scalar(
        select(AgentRun.next_event_seq).where(
            AgentRun.id == run_id,
            AgentRun.project_id == project_id,
        )
    )
    if type(expected_seq) is not int:
        raise NotFoundOrHidden()
    allocate_run_event(
        session,
        project_id=project_id,
        run_id=run_id,
        expected_seq=expected_seq,
        event_id=str(uuid.uuid4()),
        event_type=event_type,
        payload={
            "candidate_id": candidate_id,
            "disposition_id": disposition_id,
            "status": outcome,
            "synthetic": True,
        },
        trace_id=str(uuid.uuid4()),
        created_at=float(now),
        context_snapshot_id=snapshot_id,
    )
    body: dict[str, Any] = {
        "audit_event_id": audit_event_id,
        "candidate_id": candidate_id,
        "disposition_id": disposition_id,
        "status": outcome,
        "summary": effect.title if effect is not None else (reason or "synthetic-reject"),
        "title": effect.title if effect is not None else "synthetic-reject",
    }
    if preview_id is not None:
        body["preview_id"] = preview_id
    if effect is not None:
        if effect.page_id is not None:
            body["page_id"] = effect.page_id
        if effect.page_version_id is not None:
            body["page_version_id"] = effect.page_version_id
        if effect.content_revision is not None:
            body["content_revision"] = effect.content_revision
        if effect.content_hash is not None:
            body["content_hash"] = effect.content_hash
        if effect.project_revision is not None:
            body["project_revision"] = effect.project_revision
        if effect.edge_ids:
            body["edge_ids"] = list(effect.edge_ids)
        if effect.decision_id is not None:
            body["decision_id"] = effect.decision_id
    session.flush()
    return CommandResponse(201, body)


def _consume(
    session: Session,
    project_id: str,
    candidate_id: str,
    revision: int,
    new_status: str,
) -> None:
    try:
        consume_candidate(
            session,
            project_id=project_id,
            candidate_id=candidate_id,
            expected_revision=revision,
            new_status=new_status,
        )
    except CasMismatch:
        raise CandidateCommandError(
            _RESOLVED,
            _visible_disposition(session, project_id, candidate_id),
        ) from None
    loaded = session.get(Candidate, candidate_id)
    if loaded is not None:
        session.expire(loaded)


def _insert_page(
    session: Session,
    *,
    project_id: str,
    stage_id: str,
    title: str,
    content_json: str,
    content_hash: str,
    project_revision: int,
    actor_id: str,
    now: float,
) -> tuple[str, str]:
    page_id = str(uuid.uuid4())
    version_id = str(uuid.uuid4())
    page = Page(
        id=page_id,
        project_id=project_id,
        stage_id=stage_id,
        head_version_id=None,
        status="active",
        title=title,
        created_at=now,
    )
    session.add(page)
    session.flush()
    session.add(
        PageVersion(
            id=version_id,
            project_id=project_id,
            page_id=page_id,
            version_no=1,
            parent_version_id=None,
            content_json=content_json,
            content_hash=content_hash,
            schema_version=1,
            project_revision=project_revision,
            created_by_account_id=actor_id,
            created_at=now,
        )
    )
    session.flush()
    page.head_version_id = version_id
    session.flush()
    session.add(
        PageDraft(
            page_id=page_id,
            project_id=project_id,
            base_version_id=version_id,
            content_revision=1,
            content_hash=content_hash,
            content_json=content_json,
            schema_version=1,
            updated_at=now,
        )
    )
    session.flush()
    return page_id, version_id


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
        raise CandidateCommandError(ErrorCode.HEAD_CHANGED.value)
    cached = session.identity_map.get(identity_key(Project, (project_id,)))
    if cached is not None:
        session.expire(cached)
    return current + 1


def _proposal_for_auth(
    session: Session,
    project_id: str,
    candidate_id: str,
    preview_id: str | None,
) -> ParsedProposal:
    candidate = _candidate(session, project_id, candidate_id)
    if preview_id is not None:
        preview = session.get(CandidatePreview, preview_id)
        if (
            preview is not None
            and preview.project_id == project_id
            and preview.candidate_id == candidate.id
        ):
            return parse_proposal(
                candidate.action,
                candidate.page_mode,
                _object(preview.effective_proposal_json),
            )
    return parse_proposal(candidate.action, candidate.page_mode, _object(candidate.proposal_json))


def _candidate(session: Session, project_id: str, candidate_id: str) -> Candidate:
    candidate = session.get(Candidate, candidate_id)
    if candidate is None or candidate.project_id != project_id:
        raise NotFoundOrHidden()
    return candidate


def _preview(
    session: Session,
    project_id: str,
    candidate_id: str,
    preview_id: str,
) -> CandidatePreview:
    preview = session.get(CandidatePreview, preview_id)
    if preview is None or preview.project_id != project_id or preview.candidate_id != candidate_id:
        raise CandidateCommandError(_CHANGED)
    return preview


def _stored_sources(session: Session, preview_id: str) -> list[dict[str, Any]]:
    rows = session.scalars(
        select(CandidatePreviewSource).where(CandidatePreviewSource.preview_id == preview_id)
    ).all()
    return sorted_sources(
        [
            source_document(
                source_kind=row.source_kind,
                page_version_id=row.page_version_id,
                artifact_version_id=row.artifact_version_id,
                draft_page_id=row.draft_page_id,
                draft_content_revision=row.draft_content_revision,
                draft_content_hash=row.draft_content_hash,
            )
            for row in rows
        ]
    )


def _status_now(session: Session, project_id: str, candidate_id: str) -> str | None:
    loaded = session.get(Candidate, candidate_id)
    if loaded is not None:
        session.expire(loaded)
    status = session.scalar(
        select(Candidate.status).where(
            Candidate.id == candidate_id,
            Candidate.project_id == project_id,
        )
    )
    if isinstance(status, str):
        return status
    return None


def _visible_disposition(session: Session, project_id: str, candidate_id: str) -> dict[str, Any]:
    disposition_id = session.scalar(
        select(CandidateDisposition.id).where(
            CandidateDisposition.project_id == project_id,
            CandidateDisposition.candidate_id == candidate_id,
        )
    )
    if isinstance(disposition_id, str):
        return {"disposition_id": disposition_id}
    return {}


def _resolved(session: Session, project_id: str, candidate_id: str) -> CandidateCommandError:
    loaded = session.get(Candidate, candidate_id)
    if loaded is not None:
        session.expire(loaded)
    status = session.scalar(
        select(Candidate.status).where(
            Candidate.id == candidate_id,
            Candidate.project_id == project_id,
        )
    )
    if status == "pending":
        return CandidateCommandError(_STALE)
    return CandidateCommandError(
        _RESOLVED,
        _visible_disposition(session, project_id, candidate_id),
    )


def _stamp(sources: list[dict[str, Any]], page_id: str) -> dict[str, Any] | None:
    for item in sources:
        if item["source_kind"] == "draft_stamp" and item["draft_page_id"] == page_id:
            return item
    return None


def _version(session: Session, project_id: str, version_id: str) -> PageVersion:
    version = session.get(PageVersion, version_id)
    if version is None or version.project_id != project_id:
        raise NotFoundOrHidden()
    return version


def _page(session: Session, project_id: str, page_id: str) -> Page:
    page = session.get(Page, page_id)
    if page is None or page.project_id != project_id:
        raise NotFoundOrHidden()
    return page


def _draft(session: Session, project_id: str, page_id: str) -> PageDraft:
    draft = session.get(PageDraft, page_id)
    if draft is None or draft.project_id != project_id:
        raise CandidateCommandError(_STALE)
    return draft


def _authorize_referenced_artifacts(
    session: Session,
    principal: Principal,
    project_id: str,
    content_json: str,
) -> None:
    document = json.loads(content_json)
    authorizer = SessionAuthorizer(session)
    for node in document.get("nodes", []):
        artifact_version_id = node.get("artifact_version_id")
        if not isinstance(artifact_version_id, str):
            continue
        authorizer.authorize(
            Action.PREVIEW_READ,
            ResourceRef(
                project_id=project_id,
                scope_type="artifact_version",
                artifact_version_id=artifact_version_id,
            ),
            principal,
        )


def projected_draft(content_json: str, proposal: ParsedProposal) -> tuple[str, str]:
    """Canonical draft text for one content candidate. Does not write."""

    if proposal.action == "add_typed_edge":
        return _with_edge(
            content_json,
            proposal.edge_id or "",
            proposal.source_node_id or "",
            proposal.target_node_id or "",
        )
    if proposal.action == "attach_artifact_version":
        return _attach_artifact(
            content_json,
            proposal.node_id or "",
            proposal.artifact_version_id or "",
        )
    return _with_node(content_json, proposal.node_id or "", proposal.artifact_version_id)


def _with_node(content_json: str, node_id: str, artifact_version_id: str | None) -> tuple[str, str]:
    document = json.loads(content_json)
    nodes = list(document["nodes"])
    if any(isinstance(node, dict) and node.get("node_id") == node_id for node in nodes):
        raise InvalidSchema()
    item: dict[str, str] = {"node_id": node_id}
    if artifact_version_id is not None:
        item["artifact_version_id"] = artifact_version_id
    nodes.append(item)
    document["nodes"] = nodes
    return _canonical_draft(document)


def _with_edge(
    content_json: str,
    edge_id: str,
    source_node_id: str,
    target_node_id: str,
) -> tuple[str, str]:
    document = json.loads(content_json)
    edges = list(document["edges"])
    if any(isinstance(edge, dict) and edge.get("edge_id") == edge_id for edge in edges):
        raise InvalidSchema()
    edges.append(
        {
            "edge_id": edge_id,
            "source_node_id": source_node_id,
            "target_node_id": target_node_id,
        }
    )
    document["edges"] = edges
    return _canonical_draft(document)


def _attach_artifact(content_json: str, node_id: str, artifact_version_id: str) -> tuple[str, str]:
    document = json.loads(content_json)
    nodes: list[Any] = []
    found = False
    for node in document["nodes"]:
        if isinstance(node, dict) and node.get("node_id") == node_id:
            found = True
            nodes.append({"node_id": node_id, "artifact_version_id": artifact_version_id})
            continue
        nodes.append(node)
    if not found:
        raise InvalidSchema()
    document["nodes"] = nodes
    return _canonical_draft(document)


def _canonical_draft(document: dict[str, Any]) -> tuple[str, str]:
    try:
        body = canonical_document(document)
    except DocumentSchemaError as error:
        raise InvalidSchema() from error
    return body, document_hash(document)


def _blank_content(citations: tuple[str, ...]) -> tuple[str, str]:
    nodes = [
        {"node_id": str(uuid.uuid4()), "artifact_version_id": artifact_version_id}
        for artifact_version_id in citations
    ]
    document = {"schema_version": 1, "nodes": nodes, "edges": []}
    return canonical_document(document), document_hash(document)


def _edge_reason(proposal: ParsedProposal, source_version_id: str) -> str:
    if proposal.kind != "merge":
        return canonical_reason({"source_version_id": source_version_id})
    codes = [code for code, version_ids in proposal.conflicts if source_version_id in version_ids]
    return canonical_reason({"conflict_codes": codes, "source_version_id": source_version_id})


def _presented_patch(user_patch: Mapping[str, Any]) -> str:
    if not isinstance(user_patch, Mapping) or isinstance(user_patch, (str, bytes)):
        raise CandidateCommandError(_CHANGED)
    try:
        return canonical_json(dict(user_patch))
    except DocumentSchemaError as error:
        raise CandidateCommandError(_CHANGED) from error


def _hex_hash(value: object) -> str:
    if not isinstance(value, str):
        raise CandidateCommandError(_CHANGED)
    token = value[7:] if value.startswith("sha256:") else value
    if len(token) != 64 or any(char not in "0123456789abcdef" for char in token):
        raise CandidateCommandError(_CHANGED)
    return token


def _object(payload: str) -> dict[str, Any]:
    try:
        loaded = json.loads(payload)
    except json.JSONDecodeError as error:
        raise InvalidSchema() from error
    if not isinstance(loaded, dict):
        raise InvalidSchema()
    return loaded


def _reason(value: str | None) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or len(value) > 500:
        raise InvalidSchema()
    cleaned = value.strip()
    return cleaned or None


def _command_name(proposal: ParsedProposal) -> str:
    if proposal.kind == "draft":
        return Action.PAGE_DRAFT_WRITE.value
    if proposal.kind == "history":
        return Action.PAGE_BRANCH.value
    return Action.PAGE_CREATE.value
