"""Server-built candidate previews. A preview does not create a business version."""

import json
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from sinuo.application.candidates.commit import projected_draft
from sinuo.application.candidates.errors import CandidateCommandError
from sinuo.application.candidates.gate import authorize_proposal
from sinuo.application.candidates.proposal import (
    _GRAPH_ACTIONS,
    ParsedProposal,
    apply_user_patch,
    bound_preview_hash,
    expires_token,
    parse_proposal,
    patch_hash,
    sorted_sources,
    source_document,
)
from sinuo.domain.auth.errors import InvalidSchema, NotFoundOrHidden
from sinuo.domain.auth.principal import Principal
from sinuo.domain.public_contract import ErrorCode
from sinuo.domain.schema_types import canonical_json
from sinuo.infrastructure.domain_models import (
    Candidate,
    CandidatePreview,
    CandidatePreviewSource,
    Page,
    PageDraft,
    PageVersion,
    Project,
    Stage,
)

DEFAULT_PREVIEW_TTL_SECONDS = 600.0


@dataclass(frozen=True)
class CandidatePreviewView:
    preview_id: str
    preview_hash: str
    expires_at: float
    candidate_id: str
    candidate_revision: int
    access_epoch: int
    user_patch: dict[str, Any]
    effective_proposal: dict[str, Any]
    preconditions: tuple[dict[str, Any], ...]
    affected_objects: tuple[dict[str, str], ...]


def preview_candidate(
    session: Session,
    *,
    principal: Principal,
    project_id: str,
    candidate_id: str,
    now: float,
    user_patch: Mapping[str, Any] | None = None,
    ttl_seconds: float = DEFAULT_PREVIEW_TTL_SECONDS,
    expected_page_version_id: str | None = None,
    expected_draft_content_revision: int | None = None,
) -> CandidatePreviewView:
    """Insert one new preview row. Does not commit, and does not replace an older preview."""

    expires_at = _expires_at(now, ttl_seconds)
    candidate = _candidate(session, project_id, candidate_id)
    if candidate.status != "pending":
        raise CandidateCommandError(ErrorCode.CANDIDATE_ALREADY_RESOLVED.value)
    proposal = parse_proposal(
        candidate.action,
        candidate.page_mode,
        _object(candidate.proposal_json),
    )
    effective, patch = apply_user_patch(proposal, user_patch)
    authorize_proposal(session, principal, project_id, effective)
    sources, affected = _preconditions(session, project_id, effective)
    _require_current_source(
        session,
        project_id,
        effective,
        sources,
        expected_page_version_id,
        expected_draft_content_revision,
    )
    if effective.action in _GRAPH_ACTIONS:
        draft = _draft(session, project_id, effective.page_id or "")
        projected_draft(draft.content_json, effective)
    access_epoch = _access_epoch(session, project_id)
    ordered = sorted_sources(sources)
    digest = bound_preview_hash(
        account_id=principal.account_id,
        candidate_id=candidate.id,
        candidate_revision=candidate.revision,
        action=candidate.action,
        page_mode=candidate.page_mode,
        access_epoch=access_epoch,
        user_patch=patch,
        effective_proposal=effective.document(),
        sources=ordered,
        expires_at=expires_at,
    )
    preview_id = str(uuid.uuid4())
    session.add(
        CandidatePreview(
            id=preview_id,
            project_id=project_id,
            candidate_id=candidate.id,
            candidate_revision=candidate.revision,
            account_id=principal.account_id,
            access_epoch=access_epoch,
            user_patch_json=canonical_json(patch),
            user_patch_hash=patch_hash(patch),
            effective_proposal_json=canonical_json(effective.document()),
            preview_hash=digest,
            expires_at=expires_at,
            created_at=float(now),
        )
    )
    session.flush()
    for item in ordered:
        session.add(
            CandidatePreviewSource(
                id=str(uuid.uuid4()),
                project_id=project_id,
                preview_id=preview_id,
                source_kind=item["source_kind"],
                page_version_id=item["page_version_id"],
                artifact_version_id=item["artifact_version_id"],
                draft_page_id=item["draft_page_id"],
                draft_content_revision=item["draft_content_revision"],
                draft_content_hash=item["draft_content_hash"],
            )
        )
    session.flush()
    return CandidatePreviewView(
        preview_id=preview_id,
        preview_hash=digest,
        expires_at=expires_at,
        candidate_id=candidate.id,
        candidate_revision=candidate.revision,
        access_epoch=access_epoch,
        user_patch=patch,
        effective_proposal=effective.document(),
        preconditions=tuple(ordered),
        affected_objects=tuple(affected),
    )


def _expires_at(now: float, ttl_seconds: float) -> float:
    if isinstance(now, bool) or isinstance(ttl_seconds, bool):
        raise InvalidSchema()
    if not isinstance(now, (int, float)) or not isinstance(ttl_seconds, (int, float)):
        raise InvalidSchema()
    if ttl_seconds <= 0:
        raise InvalidSchema()
    expires_token(float(now))
    expires_token(float(now) + float(ttl_seconds))
    return float(now) + float(ttl_seconds)


def _require_current_source(
    session: Session,
    project_id: str,
    proposal: ParsedProposal,
    sources: list[dict[str, Any]],
    expected_page_version_id: str | None,
    expected_draft_content_revision: int | None,
) -> None:
    """Refuse a preview whose caller names a source the server would not bind."""

    if expected_page_version_id is None and expected_draft_content_revision is None:
        return
    changed = ErrorCode.PREVIEW_CHANGED.value
    stale = ErrorCode.CANDIDATE_STALE.value
    if expected_page_version_id is not None and not isinstance(expected_page_version_id, str):
        raise CandidateCommandError(changed)
    if (
        expected_draft_content_revision is not None
        and type(expected_draft_content_revision) is not int
    ):
        raise CandidateCommandError(changed)
    if proposal.kind == "history":
        if (
            expected_page_version_id is not None
            and expected_page_version_id != proposal.source_version_id
        ):
            raise CandidateCommandError(changed)
        if expected_draft_content_revision is not None:
            raise CandidateCommandError(changed)
        return
    if proposal.kind == "draft":
        if (
            expected_page_version_id is not None
            and expected_page_version_id != proposal.base_version_id
        ):
            raise CandidateCommandError(changed)
        if expected_draft_content_revision is not None:
            stamp = _stamp(sources, proposal.page_id or "")
            if stamp is None or stamp["draft_content_revision"] != expected_draft_content_revision:
                raise CandidateCommandError(stale)
        return
    version_ids = [
        item["page_version_id"] for item in sources if item["source_kind"] == "page_version"
    ]
    if expected_page_version_id is not None and expected_page_version_id not in version_ids:
        raise CandidateCommandError(changed)
    if expected_draft_content_revision is None:
        return
    if expected_page_version_id is None:
        stamps = [item for item in sources if item["source_kind"] == "draft_stamp"]
        if not stamps or any(
            item["draft_content_revision"] != expected_draft_content_revision for item in stamps
        ):
            raise CandidateCommandError(stale)
        return
    version = session.get(PageVersion, expected_page_version_id)
    if version is None or version.project_id != project_id:
        raise CandidateCommandError(changed)
    stamp = _stamp(sources, version.page_id)
    if stamp is None or stamp["draft_content_revision"] != expected_draft_content_revision:
        raise CandidateCommandError(stale)


def _stamp(sources: list[dict[str, Any]], page_id: str) -> dict[str, Any] | None:
    for item in sources:
        if item["source_kind"] == "draft_stamp" and item["draft_page_id"] == page_id:
            return item
    return None


def _candidate(session: Session, project_id: str, candidate_id: str) -> Candidate:
    candidate = session.get(Candidate, candidate_id)
    if candidate is None or candidate.project_id != project_id:
        raise NotFoundOrHidden()
    return candidate


def _access_epoch(session: Session, project_id: str) -> int:
    epoch = session.scalar(select(Project.access_epoch).where(Project.id == project_id))
    if type(epoch) is not int:
        raise NotFoundOrHidden()
    return epoch


def _object(payload: str) -> object:
    try:
        return json.loads(payload)
    except json.JSONDecodeError as error:
        raise InvalidSchema() from error


def _preconditions(
    session: Session,
    project_id: str,
    proposal: ParsedProposal,
) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    if proposal.kind == "draft":
        version = _version(session, project_id, proposal.base_version_id or "")
        if version.page_id != proposal.page_id:
            raise NotFoundOrHidden()
        draft = _draft(session, project_id, proposal.page_id or "")
        if draft.base_version_id != version.id:
            raise CandidateCommandError(ErrorCode.CANDIDATE_STALE.value)
        sources = [
            source_document(source_kind="page_version", page_version_id=version.id),
            source_document(
                source_kind="draft_stamp",
                draft_page_id=draft.page_id,
                draft_content_revision=draft.content_revision,
                draft_content_hash=draft.content_hash,
            ),
        ]
        affected = [{"page_id": draft.page_id}, {"page_version_id": version.id}]
        return sources, affected
    if proposal.kind == "history":
        version = _version(session, project_id, proposal.source_version_id or "")
        _stage(session, project_id, proposal.target_stage_id or "")
        sources = [source_document(source_kind="page_version", page_version_id=version.id)]
        affected = [
            {"page_version_id": version.id},
            {"stage_id": proposal.target_stage_id or ""},
        ]
        return sources, affected
    sources: list[dict[str, Any]] = []
    affected = [{"stage_id": proposal.target_stage_id or ""}]
    _stage(session, project_id, proposal.target_stage_id or "")
    for version_id in proposal.source_version_ids:
        version = _version(session, project_id, version_id)
        page = _page(session, project_id, version.page_id)
        if page.head_version_id != version.id:
            raise CandidateCommandError(ErrorCode.CANDIDATE_STALE.value)
        sources.append(source_document(source_kind="page_version", page_version_id=version.id))
        affected.append({"page_version_id": version.id})
        draft = session.get(PageDraft, page.id)
        if draft is not None and draft.project_id == project_id:
            sources.append(
                source_document(
                    source_kind="draft_stamp",
                    draft_page_id=draft.page_id,
                    draft_content_revision=draft.content_revision,
                    draft_content_hash=draft.content_hash,
                )
            )
    for artifact_version_id in proposal.citations:
        sources.append(
            source_document(source_kind="artifact_version", artifact_version_id=artifact_version_id)
        )
        affected.append({"artifact_version_id": artifact_version_id})
    return sources, affected


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
        raise CandidateCommandError(ErrorCode.CANDIDATE_STALE.value)
    return draft


def _stage(session: Session, project_id: str, stage_id: str) -> None:
    stage = session.get(Stage, stage_id)
    if stage is None or stage.project_id != project_id:
        raise NotFoundOrHidden()
