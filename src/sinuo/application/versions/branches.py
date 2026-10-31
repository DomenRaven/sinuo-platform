"""Open a new page from an old version. The caller owns write_uow and commit."""

import hashlib
import json
import uuid
from collections.abc import Callable
from dataclasses import dataclass

from sqlalchemy import insert, select, update
from sqlalchemy.orm import Session

from sinuo.domain.public_contract import Action
from sinuo.domain.schema_types import (
    LINEAGE_KINDS,
    PAGE_STATUSES,
    assert_closed,
    canonical_audit_payload,
    canonical_document,
    canonical_json,
)
from sinuo.infrastructure.domain_models import (
    AuditEvent,
    Page,
    PageDraft,
    PageEdge,
    PageVersion,
    Project,
)
from sinuo.infrastructure.models import OutboxEvent


class BranchConflict(Exception):
    """The source version is missing or a conditional write did not match one row."""


@dataclass(frozen=True)
class BranchResult:
    page_id: str
    page_version_id: str
    source_page_id: str
    source_version_id: str
    edge_id: str
    project_revision: int


def _one(result, token: str) -> None:
    if result.rowcount != 1:
        raise BranchConflict(token)


def _clone_document(content_json: str) -> tuple[str, str, str]:
    normalized = json.loads(canonical_document(json.loads(content_json)))
    node_ids: dict[str, str] = {}
    nodes: list[dict[str, str]] = []
    origin_nodes: list[dict[str, str]] = []
    for node in normalized["nodes"]:
        new_id = str(uuid.uuid4())
        old_id = node["node_id"]
        node_ids[old_id] = new_id
        cloned: dict[str, str] = {"node_id": new_id}
        artifact_version_id = node.get("artifact_version_id")
        if artifact_version_id is not None:
            cloned["artifact_version_id"] = artifact_version_id
        nodes.append(cloned)
        origin_nodes.append({"node_id": new_id, "origin_node_id": old_id})
    edges: list[dict[str, str]] = []
    origin_edges: list[dict[str, str]] = []
    for edge in normalized["edges"]:
        new_id = str(uuid.uuid4())
        edges.append(
            {
                "edge_id": new_id,
                "source_node_id": node_ids[edge["source_node_id"]],
                "target_node_id": node_ids[edge["target_node_id"]],
            }
        )
        origin_edges.append({"edge_id": new_id, "origin_edge_id": edge["edge_id"]})
    cloned_json = canonical_document({"schema_version": 1, "nodes": nodes, "edges": edges})
    cloned_hash = hashlib.sha256(cloned_json.encode("utf-8")).hexdigest()
    reason = canonical_json({"nodes": origin_nodes, "edges": origin_edges})
    return cloned_json, cloned_hash, reason


def branch_from_version(
    session: Session,
    *,
    project_id: str,
    source_version_id: str,
    actor_id: str,
    title: str,
    now: float,
    before_audit: Callable[[], None] | None = None,
) -> BranchResult:
    """Create a new page and v1. Does not commit.

    v1.parent_version_id stays null. The source is recorded as
    page_edges.kind 'branches_from'. The source page is not updated.
    """

    cleaned = title.strip()
    if not cleaned:
        raise BranchConflict(source_version_id)
    source = session.execute(
        select(
            PageVersion.id,
            PageVersion.page_id,
            PageVersion.content_json,
            Page.stage_id,
        )
        .join(
            Page,
            (Page.id == PageVersion.page_id) & (Page.project_id == PageVersion.project_id),
        )
        .where(PageVersion.id == source_version_id, PageVersion.project_id == project_id)
    ).one_or_none()
    if source is None:
        raise BranchConflict(source_version_id)
    content_json, content_hash, reason = _clone_document(source.content_json)
    current_revision = session.scalar(select(Project.revision).where(Project.id == project_id))
    if type(current_revision) is not int:
        raise BranchConflict(project_id)
    next_revision = current_revision + 1
    page_id = str(uuid.uuid4())
    version_id = str(uuid.uuid4())
    edge_id = str(uuid.uuid4())
    audit_event_id = str(uuid.uuid4())
    outbox_event_id = str(uuid.uuid4())
    status = assert_closed("active", PAGE_STATUSES, name="page_status")
    kind = assert_closed("branches_from", LINEAGE_KINDS, name="lineage_kind")
    _one(
        session.execute(
            insert(Page).values(
                id=page_id,
                project_id=project_id,
                stage_id=source.stage_id,
                head_version_id=None,
                status=status,
                title=cleaned,
                created_at=now,
            )
        ),
        page_id,
    )
    _one(
        session.execute(
            insert(PageVersion).values(
                id=version_id,
                project_id=project_id,
                page_id=page_id,
                version_no=1,
                parent_version_id=None,
                content_json=content_json,
                content_hash=content_hash,
                schema_version=1,
                project_revision=next_revision,
                created_by_account_id=actor_id,
                created_at=now,
            )
        ),
        version_id,
    )
    _one(
        session.execute(
            update(Page)
            .where(
                Page.id == page_id,
                Page.project_id == project_id,
                Page.head_version_id.is_(None),
            )
            .values(head_version_id=version_id)
        ),
        page_id,
    )
    _one(
        session.execute(
            insert(PageDraft).values(
                page_id=page_id,
                project_id=project_id,
                base_version_id=version_id,
                content_revision=1,
                content_hash=content_hash,
                content_json=content_json,
                schema_version=1,
                updated_at=now,
            )
        ),
        page_id,
    )
    _one(
        session.execute(
            insert(PageEdge).values(
                id=edge_id,
                project_id=project_id,
                source_version_id=source_version_id,
                target_version_id=version_id,
                kind=kind,
                project_revision=next_revision,
                reason=reason,
                created_at=now,
            )
        ),
        edge_id,
    )
    _one(
        session.execute(
            update(Project)
            .where(Project.id == project_id, Project.revision == current_revision)
            .values(revision=next_revision)
        ),
        project_id,
    )
    if before_audit is not None:
        before_audit()
    payload = {
        "edge_id": edge_id,
        "kind": kind,
        "page_id": page_id,
        "page_version_id": version_id,
        "project_revision": next_revision,
        "source_page_id": source.page_id,
        "source_version_id": source_version_id,
        "version_no": 1,
    }
    _one(
        session.execute(
            insert(AuditEvent).values(
                id=audit_event_id,
                project_id=project_id,
                actor_id=actor_id,
                command=Action.PAGE_BRANCH.value,
                subject_type="page",
                subject_id=page_id,
                project_revision=next_revision,
                payload=canonical_audit_payload(payload),
                created_at=now,
            )
        ),
        page_id,
    )
    _one(
        session.execute(
            insert(OutboxEvent).values(
                event_id=outbox_event_id,
                project_id=project_id,
                event_type="page.version_created",
                schema_version=1,
                payload=payload,
                created_at=now,
            )
        ),
        page_id,
    )
    return BranchResult(
        page_id=page_id,
        page_version_id=version_id,
        source_page_id=source.page_id,
        source_version_id=source_version_id,
        edge_id=edge_id,
        project_revision=next_revision,
    )
