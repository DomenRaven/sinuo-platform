"""Pure checks for domain documents, counters, and append-only history.

Action names, grant scopes, sensitivity, availability, and role-template names
are imported from ``public_contract``. This module does not import SQLAlchemy,
FastAPI, or ``sinuo.domain.auth``.
"""

import hashlib
import json
import math
import re
import unicodedata
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from sinuo.domain.public_contract import (
    AVAILABILITY_STATES,
    GRANT_SCOPE_TYPES,
    OBJECT_GRANT_ACTIONS,
    SENSITIVITY_LEVELS,
    RoleTemplate,
)

GRANT_ACTIONS: tuple[str, ...] = tuple(action.value for action in OBJECT_GRANT_ACTIONS)
ROLE_TEMPLATES: tuple[str, ...] = tuple(item.value for item in RoleTemplate)

LINEAGE_KINDS: tuple[str, ...] = ("branches_from", "stage_source", "merge_source")
CANDIDATE_ACTIONS: tuple[str, ...] = (
    "add_node",
    "add_typed_edge",
    "attach_artifact_version",
    "draft_decision",
    "suggest_stage_transition",
    "create_page_from_versions",
)
PAGE_MODES: tuple[str, ...] = ("stage_blank", "history_branch", "merge_blank")
EXECUTION_STATUSES: tuple[str, ...] = (
    "queued",
    "running",
    "completed",
    "partial",
    "failed",
    "cancelled",
)
TASK_STATUSES: tuple[str, ...] = (
    "blocked",
    "queued",
    "running",
    "succeeded",
    "retry_wait",
    "failed",
    "cancelled",
)
RUN_ROLES: tuple[str, ...] = ("clarification", "evidence", "method", "critic")
RESULT_DISPOSITIONS: tuple[str, ...] = ("published", "discarded_late")
CANDIDATE_STATUSES: tuple[str, ...] = ("pending", "committed", "rejected", "expired")
JOB_KINDS: tuple[str, ...] = (
    "export",
    "derivative",
    "privacy_cleanup",
    "index",
    "upload_finalize",
    "suggestion_trigger",
)
JOB_STATUSES: tuple[str, ...] = (
    "queued",
    "running",
    "succeeded",
    "failed",
    "cancelled",
    "superseded",
)
TOMBSTONE_OBJECT_TYPES: tuple[str, ...] = (
    "project",
    "page",
    "page_version",
    "artifact",
    "artifact_version",
    "evidence_anchor",
    "decision",
    "test_record",
    "review_opinion",
    "context_snapshot",
    "candidate",
    "run_read_record",
)
TOMBSTONE_REASON_CATEGORIES: tuple[str, ...] = (
    "consent_withdrawn",
    "subject_request",
    "policy",
    "operator_correction",
)
ACCOUNT_STATUSES: tuple[str, ...] = ("active", "disabled")
MEMBERSHIP_STATUSES: tuple[str, ...] = ("active", "revoked")
GRANT_STATUSES: tuple[str, ...] = ("active", "revoked")
PAGE_STATUSES: tuple[str, ...] = ("active", "abandoned")
LOCATOR_TYPES: tuple[str, ...] = ("page", "region", "timecode", "text_range")
VISIBILITY_LEVELS: tuple[str, ...] = ("project", "restricted")
PREVIEW_SOURCE_KINDS: tuple[str, ...] = ("page_version", "artifact_version", "draft_stamp")
DISPOSITION_OUTCOMES: tuple[str, ...] = ("committed", "rejected", "expired")
TERMINAL_EXECUTION_STATUSES: tuple[str, ...] = ("completed", "partial", "failed", "cancelled")
TERMINAL_TASK_STATUSES: tuple[str, ...] = ("succeeded", "failed", "cancelled")

INDEPENDENT_COUNTERS: tuple[str, ...] = (
    "content_revision",
    "layout_revision",
    "page_version_no",
    "project_revision",
    "stage_version_no",
    "artifact_version_no",
    "candidate_revision",
    "opinion_revision",
    "next_event_seq",
)

_PAGE_ACTIONS_WITH_MODE: tuple[str, ...] = (
    "create_page_from_versions",
    "suggest_stage_transition",
)
_LOCATOR_FORBIDDEN: frozenset[str] = frozenset({"text", "excerpt", "body"})
_REASON_FORBIDDEN: frozenset[str] = frozenset({"excerpt", "body", "content"})
_RESULT_FORBIDDEN: frozenset[str] = frozenset({"prompt", "api_key", "password", "token"})
_AUDIT_FORBIDDEN: frozenset[str] = frozenset(
    {"prompt", "api_key", "password", "token", "excerpt", "secret"}
)
_WRITE_SQL = re.compile(
    r"\b(INSERT|UPDATE|DELETE|REPLACE|CREATE|DROP|ALTER|VACUUM|ATTACH|DETACH|"
    r"REINDEX|BEGIN|COMMIT|ROLLBACK|PRAGMA)\b",
    re.IGNORECASE,
)


class ClosedSetError(ValueError):
    """A value is outside a domain closed set."""


class DocumentSchemaError(ValueError):
    """A canonical document failed structural validation."""


class ImmutableRecord(Exception):
    """An append-only record was corrected in place."""


class CrossProjectDocument(Exception):
    """A document or pointer names an object from another project."""


class AclEpochError(Exception):
    """An existing project's access epoch was not increased by exactly one."""


class HistoryMutation(Exception):
    """A history read wrote, or a head pointer / counter moved."""


CLOSED_SETS: dict[str, tuple[str, ...]] = {
    "grant_action": GRANT_ACTIONS,
    "grant_scope": GRANT_SCOPE_TYPES,
    "sensitivity": SENSITIVITY_LEVELS,
    "availability": AVAILABILITY_STATES,
    "role_template": ROLE_TEMPLATES,
    "lineage_kind": LINEAGE_KINDS,
    "candidate_action": CANDIDATE_ACTIONS,
    "page_mode": PAGE_MODES,
    "execution_status": EXECUTION_STATUSES,
    "task_status": TASK_STATUSES,
    "role": RUN_ROLES,
    "result_disposition": RESULT_DISPOSITIONS,
    "candidate_status": CANDIDATE_STATUSES,
    "job_kind": JOB_KINDS,
    "job_status": JOB_STATUSES,
    "tombstone_object_type": TOMBSTONE_OBJECT_TYPES,
    "tombstone_reason": TOMBSTONE_REASON_CATEGORIES,
    "account_status": ACCOUNT_STATUSES,
    "membership_status": MEMBERSHIP_STATUSES,
    "grant_status": GRANT_STATUSES,
    "page_status": PAGE_STATUSES,
    "locator_type": LOCATOR_TYPES,
    "visibility": VISIBILITY_LEVELS,
    "preview_source_kind": PREVIEW_SOURCE_KINDS,
    "disposition_outcome": DISPOSITION_OUTCOMES,
}


def assert_closed(value: str, allowed: tuple[str, ...], *, name: str) -> str:
    if value not in allowed:
        raise ClosedSetError(f"{name} rejects {value!r}")
    return value


def assert_closed_set(name: str, value: str) -> str:
    try:
        allowed = CLOSED_SETS[name]
    except KeyError as error:
        raise ClosedSetError(f"unknown closed set {name}") from error
    return assert_closed(value, allowed, name=name)


def normalize_login_name(value: str) -> str:
    """NFKC, casefold, and strip. Stored login names are 1 to 64 code points."""
    if not isinstance(value, str):
        raise ClosedSetError("login_name must be a string")
    normalized = unicodedata.normalize("NFKC", value).strip().casefold()
    if not 1 <= len(normalized) <= 64:
        raise ClosedSetError("login_name must be 1 to 64 code points after normalization")
    return normalized


def login_name_for_sql(value: object) -> str | None:
    """Canonical login name for the SQLite guard. Invalid input returns None."""

    if not isinstance(value, str):
        return None
    try:
        normalized = normalize_login_name(value)
    except ClosedSetError:
        return None
    for char in normalized:
        if char.isspace() or unicodedata.category(char)[:1] in {"C", "Z"}:
            return None
    return normalized


def _reject_keys(value: Any, forbidden: frozenset[str], *, label: str) -> None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            if not isinstance(key, str):
                raise DocumentSchemaError(f"{label} keys must be strings")
            if key in forbidden:
                raise DocumentSchemaError(f"{label} rejects {key}")
            _reject_keys(item, forbidden, label=label)
        return
    if isinstance(value, list):
        for item in value:
            _reject_keys(item, forbidden, label=label)
        return
    if isinstance(value, bool) or value is None or isinstance(value, (str, int)):
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise DocumentSchemaError(f"{label} rejects non-finite numbers")
        return
    raise DocumentSchemaError(f"{label} rejects {type(value).__name__}")


def canonical_json(value: Mapping[str, Any], *, forbidden: Iterable[str] = ()) -> str:
    if not isinstance(value, Mapping) or isinstance(value, (str, bytes)):
        raise DocumentSchemaError("document must be an object")
    _reject_keys(value, frozenset(forbidden), label="document")
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _string_field(value: Any, *, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise DocumentSchemaError(f"{label} must be a non-empty string")
    return value


def canonical_document(document: Mapping[str, Any]) -> str:
    """Canonical page content: schema_version 1, stable node and edge ids, no extras."""
    if not isinstance(document, Mapping):
        raise DocumentSchemaError("document must be an object")
    if set(document) != {"schema_version", "nodes", "edges"}:
        raise DocumentSchemaError("page document keys must be schema_version, nodes, edges")
    if type(document["schema_version"]) is not int or document["schema_version"] != 1:
        raise DocumentSchemaError("schema_version must be 1")
    nodes = document["nodes"]
    edges = document["edges"]
    if not isinstance(nodes, list) or not isinstance(edges, list):
        raise DocumentSchemaError("nodes and edges must be arrays")
    seen_nodes: set[str] = set()
    normalized_nodes: list[dict[str, str]] = []
    for node in nodes:
        if not isinstance(node, Mapping) or set(node) - {"node_id", "artifact_version_id"}:
            raise DocumentSchemaError("node has an unknown key")
        node_id = _string_field(node.get("node_id"), label="node_id")
        if node_id in seen_nodes:
            raise DocumentSchemaError("node_id must be unique in the document")
        seen_nodes.add(node_id)
        item = {"node_id": node_id}
        if "artifact_version_id" in node:
            item["artifact_version_id"] = _string_field(
                node.get("artifact_version_id"),
                label="artifact_version_id",
            )
        normalized_nodes.append(item)
    seen_edges: set[str] = set()
    normalized_edges: list[dict[str, str]] = []
    for edge in edges:
        if not isinstance(edge, Mapping):
            raise DocumentSchemaError("edge must be an object")
        if set(edge) != {"edge_id", "source_node_id", "target_node_id"}:
            raise DocumentSchemaError("edge keys are fixed")
        edge_id = _string_field(edge.get("edge_id"), label="edge_id")
        source = _string_field(edge.get("source_node_id"), label="source_node_id")
        target = _string_field(edge.get("target_node_id"), label="target_node_id")
        if edge_id in seen_edges:
            raise DocumentSchemaError("edge_id must be unique in the document")
        if source not in seen_nodes or target not in seen_nodes:
            raise DocumentSchemaError("edge endpoints must be nodes in this document")
        seen_edges.add(edge_id)
        normalized_edges.append(
            {"edge_id": edge_id, "source_node_id": source, "target_node_id": target}
        )
    payload = {"schema_version": 1, "nodes": normalized_nodes, "edges": normalized_edges}
    return canonical_json(payload)


def document_hash(document: Mapping[str, Any]) -> str:
    return hashlib.sha256(canonical_document(document).encode("utf-8")).hexdigest()


def _collect_strings(value: Any, key: str, found: list[str]) -> None:
    if isinstance(value, Mapping):
        for item_key, item in value.items():
            if item_key == key:
                if not isinstance(item, str):
                    raise DocumentSchemaError(key)
                found.append(item)
            else:
                _collect_strings(item, key, found)
        return
    if isinstance(value, list):
        for item in value:
            _collect_strings(item, key, found)


def assert_same_project_document(
    project_id: str,
    document: Mapping[str, Any],
    artifact_version_projects: Mapping[str, str],
) -> None:
    """Reject page JSON that cites an artifact version from another project."""
    parsed = json.loads(canonical_document(document))
    referenced: list[str] = []
    _collect_strings(parsed, "artifact_version_id", referenced)
    for version_id in referenced:
        if artifact_version_projects.get(version_id) != project_id:
            raise CrossProjectDocument(version_id)


def assert_opinion_anchor(document: Mapping[str, Any], node_id: str | None) -> None:
    if node_id is None:
        return
    parsed = json.loads(canonical_document(document))
    if node_id not in {node["node_id"] for node in parsed["nodes"]}:
        raise DocumentSchemaError("node_id is not in the anchored page version")


def canonical_locator(document: Mapping[str, Any]) -> str:
    return canonical_json(document, forbidden=_LOCATOR_FORBIDDEN)


def canonical_reason(document: Mapping[str, Any]) -> str:
    return canonical_json(document, forbidden=_REASON_FORBIDDEN)


def canonical_result(document: Mapping[str, Any]) -> str:
    return canonical_json(document, forbidden=_RESULT_FORBIDDEN)


def canonical_audit_payload(document: Mapping[str, Any]) -> str:
    return canonical_json(document, forbidden=_AUDIT_FORBIDDEN)


def assert_append_only(*, old_id: str, new_id: str, old_digest: str, new_digest: str) -> None:
    if not isinstance(old_id, str) or not isinstance(new_id, str) or not old_id or not new_id:
        raise ImmutableRecord("append-only records need string ids")
    if not isinstance(old_digest, str) or not isinstance(new_digest, str):
        raise ImmutableRecord("append-only digests must be strings")
    if old_id == new_id:
        raise ImmutableRecord("append-only correction cannot reuse the record id")


def correct_page_version(
    current: Mapping[str, Any],
    new_content: Mapping[str, Any],
    *,
    new_id: str,
) -> dict[str, Any]:
    content_json = canonical_document(new_content)
    new_digest = hashlib.sha256(content_json.encode("utf-8")).hexdigest()
    assert_append_only(
        old_id=str(current["id"]),
        new_id=new_id,
        old_digest=str(current["content_hash"]),
        new_digest=new_digest,
    )
    return {
        "id": new_id,
        "page_id": current["page_id"],
        "project_id": current["project_id"],
        "version_no": int(current["version_no"]) + 1,
        "parent_version_id": current["id"],
        "content_json": content_json,
        "content_hash": new_digest,
    }


def correct_artifact_version(
    current: Mapping[str, Any],
    *,
    new_id: str,
    license: str | None = None,
    sensitivity: str | None = None,
) -> dict[str, Any]:
    digest = str(current["content_hash"])
    assert_append_only(
        old_id=str(current["id"]),
        new_id=new_id,
        old_digest=digest,
        new_digest=digest,
    )
    chosen = str(current["sensitivity"] if sensitivity is None else sensitivity)
    assert_closed(chosen, SENSITIVITY_LEVELS, name="sensitivity")
    return {
        "id": new_id,
        "artifact_id": current["artifact_id"],
        "project_id": current["project_id"],
        "version_no": int(current["version_no"]) + 1,
        "content_hash": digest,
        "object_key": current["object_key"],
        "license": current["license"] if license is None else license,
        "sensitivity": chosen,
    }


def correct_decision(
    current: Mapping[str, Any],
    *,
    new_id: str,
    body: str,
    reason: str,
) -> dict[str, Any]:
    if new_id == current["id"]:
        raise ImmutableRecord("decision corrections append a new id")
    if not isinstance(body, str) or not isinstance(reason, str):
        raise DocumentSchemaError("decision text must be a string")
    targets = (
        current.get("page_version_id"),
        current.get("artifact_version_id"),
        current.get("stage_id"),
    )
    if all(target is None for target in targets):
        raise DocumentSchemaError("decision needs a page, artifact version, or stage")
    return {
        "id": new_id,
        "project_id": current["project_id"],
        "account_id": current["account_id"],
        "body": body,
        "reason": reason,
        "supersedes_id": current["id"],
        "page_version_id": current.get("page_version_id"),
        "artifact_version_id": current.get("artifact_version_id"),
        "stage_id": current.get("stage_id"),
    }


def correct_test_record(
    current: Mapping[str, Any],
    *,
    new_id: str,
    observation: str,
    result: str,
) -> dict[str, Any]:
    if new_id == current["id"]:
        raise ImmutableRecord("test record corrections append a new id")
    if not isinstance(observation, str) or not isinstance(result, str):
        raise DocumentSchemaError("test record text must be a string")
    return {
        "id": new_id,
        "project_id": current["project_id"],
        "account_id": current["account_id"],
        "artifact_version_id": current["artifact_version_id"],
        "observation": observation,
        "result": result,
        "supersedes_id": current["id"],
    }


def assert_role_set(roles: Iterable[str]) -> None:
    """One run may repeat a role, but four distinct responsibilities do not fit."""
    distinct: set[str] = set()
    for role in roles:
        assert_closed(role, RUN_ROLES, name="role")
        distinct.add(role)
    if len(distinct) > 3:
        raise ClosedSetError("role_cap")


def initial_project_counters() -> tuple[int, int]:
    """Revision and access epoch inserted for a new project. Not an increment."""
    return (1, 1)


def acl_commit(*, previous_epoch: int, next_epoch: int) -> int:
    """Require ``previous + 1`` for an existing project. Do not call this on create."""
    if type(previous_epoch) is not int or type(next_epoch) is not int:
        raise AclEpochError("access epochs must be integers")
    if previous_epoch < 1:
        raise AclEpochError("acl_commit applies only to an existing project")
    if next_epoch != previous_epoch + 1:
        raise AclEpochError("access_epoch must increase by 1")
    return next_epoch


def assert_command_project(*, project_id: str, transaction_project_id: str) -> None:
    if project_id.startswith("account:"):
        return
    if project_id != transaction_project_id:
        raise CrossProjectDocument("command project_id")


def assert_job_subject(*, job_project_id: str, subject_project_id: str) -> None:
    if job_project_id != subject_project_id:
        raise CrossProjectDocument("jobs.subject_id")


def assert_snapshot_reference(
    *,
    snapshot_project_id: str,
    referenced_project_id: str,
    membership_project_ids: Iterable[str],
) -> None:
    """Membership in both projects does not authorize a cross-project snapshot."""
    memberships = set(membership_project_ids)
    if snapshot_project_id not in memberships:
        raise CrossProjectDocument("snapshot project is not one of the account memberships")
    if referenced_project_id != snapshot_project_id:
        raise CrossProjectDocument("snapshot reference leaves its project")


def assert_counter_moved_only(
    before: Mapping[str, int],
    after: Mapping[str, int],
    *,
    changed: str,
) -> None:
    if set(before) != set(INDEPENDENT_COUNTERS) or set(after) != set(before):
        raise HistoryMutation("counter snapshot does not match the independent set")
    if changed not in INDEPENDENT_COUNTERS:
        raise HistoryMutation("unknown counter")
    for name in INDEPENDENT_COUNTERS:
        if name == changed:
            if after[name] != before[name] + 1:
                raise HistoryMutation(name)
        elif after[name] != before[name]:
            raise HistoryMutation(name)


@dataclass(frozen=True)
class PointerSnapshot:
    project_revision: int
    head_version_id: str | None
    head_config_version_id: str | None
    content_revision: int
    layout_revision: int


def history_statements() -> tuple[str, ...]:
    return (
        "SELECT revision, access_epoch FROM projects WHERE id = :project_id",
        "SELECT head_version_id FROM pages WHERE id = :page_id AND project_id = :project_id",
        "SELECT head_config_version_id FROM stages "
        "WHERE id = :stage_id AND project_id = :project_id",
        "SELECT content_revision FROM page_drafts WHERE page_id = :page_id",
        "SELECT layout_revision FROM view_states WHERE id = :view_state_id",
    )


def assert_history_statements(statements: Iterable[str] | None = None) -> None:
    chosen = tuple(history_statements() if statements is None else statements)
    if not chosen:
        raise HistoryMutation("history read has no statement")
    for statement in chosen:
        body = statement.strip()
        if not body.upper().startswith("SELECT"):
            raise HistoryMutation("history statements must be SELECT")
        if _WRITE_SQL.search(body):
            raise HistoryMutation("history statements cannot write")


def assert_no_pointer_movement(before: PointerSnapshot, after: PointerSnapshot) -> None:
    if before != after:
        raise HistoryMutation("history read moved a head pointer or counter")


def candidate_page_mode_required(action: str) -> bool:
    assert_closed(action, CANDIDATE_ACTIONS, name="candidate_action")
    return action in _PAGE_ACTIONS_WITH_MODE
