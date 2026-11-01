"""Closed candidate proposals, user patches, and the server preview hash.

The hash is computed here. A caller-supplied digest is never stored.
"""

import hashlib
import json
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from sinuo.application.candidates.errors import NOT_IMPLEMENTED, CandidateCommandError
from sinuo.domain.auth.errors import InvalidSchema
from sinuo.domain.schema_types import canonical_json

_GRAPH_ACTIONS = frozenset({"add_node", "add_typed_edge", "attach_artifact_version"})
_DRAFT_ACTIONS = frozenset({"draft_decision", *_GRAPH_ACTIONS})
_PAGE_ACTIONS = frozenset({"create_page_from_versions", "suggest_stage_transition"})
_BLANK_MODES = frozenset({"stage_blank", "merge_blank"})
_DRAFT_PATCH = frozenset({"node_id", "artifact_version_id"})
_EDGE_PATCH = frozenset({"edge_id", "source_node_id", "target_node_id"})
_PAGE_PATCH = frozenset({"target_title"})
_KIND_PATCH = frozenset({"action", "page_mode"})
_SOURCE_KEYS = (
    "artifact_version_id",
    "draft_content_hash",
    "draft_content_revision",
    "draft_page_id",
    "page_version_id",
    "source_kind",
)


@dataclass(frozen=True)
class ParsedProposal:
    """One server-owned reading of a candidate. The action column wins over JSON."""

    action: str
    page_mode: str | None
    page_id: str | None = None
    base_version_id: str | None = None
    node_id: str | None = None
    artifact_version_id: str | None = None
    edge_id: str | None = None
    source_node_id: str | None = None
    target_node_id: str | None = None
    target_stage_id: str | None = None
    title: str | None = None
    source_version_id: str | None = None
    source_version_ids: tuple[str, ...] = ()
    citations: tuple[str, ...] = ()
    evidence_anchor_ids: tuple[str, ...] = ()
    conflicts: tuple[tuple[str, tuple[str, ...]], ...] = ()

    @property
    def kind(self) -> str:
        if self.action in _DRAFT_ACTIONS:
            return "draft"
        if self.page_mode == "history_branch":
            return "history"
        if self.page_mode == "merge_blank":
            return "merge"
        return "stage"

    def document(self) -> dict[str, Any]:
        """Effective proposal body. Action stays on the candidate row."""

        if self.action == "add_typed_edge":
            body = {
                "base_version_id": self.base_version_id,
                "edge_id": self.edge_id,
                "page_id": self.page_id,
                "schema_version": 1,
                "source_node_id": self.source_node_id,
                "target_node_id": self.target_node_id,
            }
            return _with_anchors(body, self.evidence_anchor_ids)
        if self.kind == "draft":
            body = {
                "base_version_id": self.base_version_id,
                "node_id": self.node_id,
                "page_id": self.page_id,
                "schema_version": 1,
            }
            if self.artifact_version_id is not None:
                body["artifact_version_id"] = self.artifact_version_id
            return _with_anchors(body, self.evidence_anchor_ids)
        if self.kind == "history":
            body = {
                "schema_version": 1,
                "source_version_id": self.source_version_id,
                "target_stage_id": self.target_stage_id,
                "title": self.title,
            }
            return _with_anchors(body, self.evidence_anchor_ids)
        body = {
            "schema_version": 1,
            "sources": [{"page_version_id": item} for item in self.source_version_ids],
            "target_stage_id": self.target_stage_id,
            "title": self.title,
        }
        if self.citations:
            body["citations"] = list(self.citations)
        body = _with_anchors(body, self.evidence_anchor_ids)
        if self.kind == "merge":
            body["conflicts"] = [
                {"code": code, "source_version_ids": list(version_ids)}
                for code, version_ids in self.conflicts
            ]
        return body


def parse_proposal(action: str, page_mode: str | None, document: object) -> ParsedProposal:
    """Read the stored proposal. An empty graph proposal stays unimplemented."""

    if action in _GRAPH_ACTIONS:
        return _parse_graph(action, page_mode, document)
    if not isinstance(document, Mapping) or isinstance(document, (str, bytes)):
        raise InvalidSchema()
    if document.get("schema_version") != 1:
        raise InvalidSchema()
    if action == "draft_decision":
        if page_mode is not None:
            raise InvalidSchema()
        _keys(
            document,
            required={"schema_version", "page_id", "base_version_id", "node_id"},
            optional={"artifact_version_id", "evidence_anchor_ids"},
        )
        artifact = document.get("artifact_version_id")
        return ParsedProposal(
            action=action,
            page_mode=None,
            page_id=_token(document["page_id"], limit=64),
            base_version_id=_token(document["base_version_id"], limit=64),
            node_id=_token(document["node_id"], limit=64),
            artifact_version_id=None if artifact is None else _token(artifact, limit=64),
            evidence_anchor_ids=_anchor_ids(document),
        )
    if action not in _PAGE_ACTIONS or not isinstance(page_mode, str):
        raise InvalidSchema()
    if action == "suggest_stage_transition" and page_mode not in _BLANK_MODES:
        raise InvalidSchema()
    if page_mode == "history_branch":
        if action != "create_page_from_versions":
            raise InvalidSchema()
        _keys(
            document,
            required={"schema_version", "target_stage_id", "title", "source_version_id"},
            optional={"evidence_anchor_ids"},
        )
        return ParsedProposal(
            action=action,
            page_mode=page_mode,
            target_stage_id=_token(document["target_stage_id"], limit=64),
            title=_title(document["title"]),
            source_version_id=_token(document["source_version_id"], limit=64),
            evidence_anchor_ids=_anchor_ids(document),
        )
    if page_mode not in _BLANK_MODES:
        raise InvalidSchema()
    required = {"schema_version", "target_stage_id", "title", "sources"}
    optional = {"citations", "evidence_anchor_ids"}
    if page_mode == "merge_blank":
        required = required | {"conflicts"}
    _keys(document, required=required, optional=optional)
    sources = _version_list(document["sources"], minimum=2 if page_mode == "merge_blank" else 1)
    citations = _citations(document.get("citations", []))
    conflicts: tuple[tuple[str, tuple[str, ...]], ...] = ()
    if page_mode == "merge_blank":
        conflicts = _conflicts(document["conflicts"], sources)
    return ParsedProposal(
        action=action,
        page_mode=page_mode,
        target_stage_id=_token(document["target_stage_id"], limit=64),
        title=_title(document["title"]),
        source_version_ids=sources,
        citations=citations,
        evidence_anchor_ids=_anchor_ids(document),
        conflicts=conflicts,
    )


def _parse_graph(action: str, page_mode: str | None, document: object) -> ParsedProposal:
    """Read one draft-graph proposal. The seeded empty object stays unimplemented."""

    if page_mode is not None:
        raise InvalidSchema()
    if not isinstance(document, Mapping) or isinstance(document, (str, bytes)):
        raise InvalidSchema()
    if set(document) == set():
        raise CandidateCommandError(NOT_IMPLEMENTED)
    if document.get("schema_version") != 1:
        raise InvalidSchema()
    if action == "add_node":
        _keys(
            document,
            required={"schema_version", "page_id", "base_version_id", "node_id"},
            optional={"artifact_version_id", "evidence_anchor_ids"},
        )
        artifact = document.get("artifact_version_id")
        return ParsedProposal(
            action=action,
            page_mode=None,
            page_id=_token(document["page_id"], limit=64),
            base_version_id=_token(document["base_version_id"], limit=64),
            node_id=_token(document["node_id"], limit=64),
            artifact_version_id=None if artifact is None else _token(artifact, limit=64),
            evidence_anchor_ids=_anchor_ids(document),
        )
    if action == "add_typed_edge":
        _keys(
            document,
            required={
                "schema_version",
                "page_id",
                "base_version_id",
                "edge_id",
                "source_node_id",
                "target_node_id",
            },
            optional={"evidence_anchor_ids"},
        )
        return ParsedProposal(
            action=action,
            page_mode=None,
            page_id=_token(document["page_id"], limit=64),
            base_version_id=_token(document["base_version_id"], limit=64),
            edge_id=_token(document["edge_id"], limit=64),
            source_node_id=_token(document["source_node_id"], limit=64),
            target_node_id=_token(document["target_node_id"], limit=64),
            evidence_anchor_ids=_anchor_ids(document),
        )
    _keys(
        document,
        required={
            "schema_version",
            "page_id",
            "base_version_id",
            "node_id",
            "artifact_version_id",
        },
        optional={"evidence_anchor_ids"},
    )
    return ParsedProposal(
        action=action,
        page_mode=None,
        page_id=_token(document["page_id"], limit=64),
        base_version_id=_token(document["base_version_id"], limit=64),
        node_id=_token(document["node_id"], limit=64),
        artifact_version_id=_token(document["artifact_version_id"], limit=64),
        evidence_anchor_ids=_anchor_ids(document),
    )


def apply_user_patch(
    proposal: ParsedProposal,
    patch: Mapping[str, Any] | None,
) -> tuple[ParsedProposal, dict[str, Any]]:
    """Return the effective proposal and the canonical patch. Action stays put."""

    body = _patch_object(patch)
    _reject_kind_change(proposal, body)
    if proposal.action == "add_typed_edge":
        _keys(body, required=set(), optional=set(_EDGE_PATCH) | set(_KIND_PATCH))
        for key in _EDGE_PATCH:
            if key in body:
                body[key] = _token(body[key], limit=64)
        effective = ParsedProposal(
            action=proposal.action,
            page_mode=proposal.page_mode,
            page_id=proposal.page_id,
            base_version_id=proposal.base_version_id,
            edge_id=str(body.get("edge_id", proposal.edge_id)),
            source_node_id=str(body.get("source_node_id", proposal.source_node_id)),
            target_node_id=str(body.get("target_node_id", proposal.target_node_id)),
            evidence_anchor_ids=proposal.evidence_anchor_ids,
        )
    elif proposal.kind == "draft":
        _keys(body, required=set(), optional=set(_DRAFT_PATCH) | set(_KIND_PATCH))
        if "node_id" in body:
            body["node_id"] = _token(body["node_id"], limit=64)
        if "artifact_version_id" in body:
            body["artifact_version_id"] = _token(body["artifact_version_id"], limit=64)
        if "artifact_version_id" in body:
            artifact: str | None = str(body["artifact_version_id"])
        else:
            artifact = proposal.artifact_version_id
        effective = ParsedProposal(
            action=proposal.action,
            page_mode=None,
            page_id=proposal.page_id,
            base_version_id=proposal.base_version_id,
            node_id=str(body.get("node_id", proposal.node_id)),
            artifact_version_id=artifact,
            evidence_anchor_ids=proposal.evidence_anchor_ids,
        )
    else:
        _keys(body, required=set(), optional=set(_PAGE_PATCH) | set(_KIND_PATCH))
        if "target_title" in body:
            body["target_title"] = _title(body["target_title"])
        title = str(body.get("target_title", proposal.title))
        effective = ParsedProposal(
            action=proposal.action,
            page_mode=proposal.page_mode,
            page_id=proposal.page_id,
            base_version_id=proposal.base_version_id,
            node_id=proposal.node_id,
            artifact_version_id=proposal.artifact_version_id,
            target_stage_id=proposal.target_stage_id,
            title=title,
            source_version_id=proposal.source_version_id,
            source_version_ids=proposal.source_version_ids,
            citations=proposal.citations,
            evidence_anchor_ids=proposal.evidence_anchor_ids,
            conflicts=proposal.conflicts,
        )
    stored = json.loads(canonical_json(body))
    if not isinstance(stored, dict):
        raise InvalidSchema()
    return effective, stored


def patch_hash(patch: Mapping[str, Any]) -> str:
    return hashlib.sha256(canonical_json(patch).encode("utf-8")).hexdigest()


def source_document(
    *,
    source_kind: str,
    page_version_id: str | None = None,
    artifact_version_id: str | None = None,
    draft_page_id: str | None = None,
    draft_content_revision: int | None = None,
    draft_content_hash: str | None = None,
    evidence_anchor_id: str | None = None,
) -> dict[str, Any]:
    """One preview source in the shape the hash and the table both use.

    ``evidence_anchor_id`` is optional. It is omitted when the candidate did not
    name an anchor, so older source rows keep the same hash.
    """

    body = {
        "artifact_version_id": artifact_version_id,
        "draft_content_hash": draft_content_hash,
        "draft_content_revision": draft_content_revision,
        "draft_page_id": draft_page_id,
        "page_version_id": page_version_id,
        "source_kind": source_kind,
    }
    if evidence_anchor_id is not None:
        body["evidence_anchor_id"] = evidence_anchor_id
    return body


def sorted_sources(sources: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    rows = [
        source_document(
            source_kind=str(item["source_kind"]),
            page_version_id=_optional_str(item.get("page_version_id")),
            artifact_version_id=_optional_str(item.get("artifact_version_id")),
            draft_page_id=_optional_str(item.get("draft_page_id")),
            draft_content_revision=_optional_int(item.get("draft_content_revision")),
            draft_content_hash=_optional_str(item.get("draft_content_hash")),
            evidence_anchor_id=_optional_str(item.get("evidence_anchor_id")),
        )
        for item in sources
    ]
    rows.sort(
        key=lambda row: (
            row["source_kind"],
            row["page_version_id"] or "",
            row["artifact_version_id"] or "",
            row["draft_page_id"] or "",
            row.get("evidence_anchor_id") or "",
        )
    )
    return rows


def bound_preview_hash(
    *,
    account_id: str,
    candidate_id: str,
    candidate_revision: int,
    action: str,
    page_mode: str | None,
    access_epoch: int,
    user_patch: Mapping[str, Any],
    effective_proposal: Mapping[str, Any],
    sources: Sequence[Mapping[str, Any]],
    expires_at: float,
) -> str:
    """Hash the bound preview fields. Callers persist this value and never a client digest."""

    payload = {
        "access_epoch": access_epoch,
        "account_id": account_id,
        "action": action,
        "candidate_id": candidate_id,
        "candidate_revision": candidate_revision,
        "effective_proposal": dict(effective_proposal),
        "expires_at": expires_token(expires_at),
        "page_mode": page_mode,
        "sources": sorted_sources(sources),
        "user_patch": dict(user_patch),
    }
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


def expires_token(value: float) -> str:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise InvalidSchema()
    number = float(value)
    if not math.isfinite(number):
        raise InvalidSchema()
    return format(number, ".6f")


def _reject_kind_change(proposal: ParsedProposal, body: Mapping[str, Any]) -> None:
    """A patch may repeat the candidate kind. It may not replace it."""

    if "action" in body and body["action"] != proposal.action:
        raise InvalidSchema()
    if "page_mode" in body and body["page_mode"] != proposal.page_mode:
        raise InvalidSchema()


def _patch_object(patch: Mapping[str, Any] | None) -> dict[str, Any]:
    if patch is None:
        return {}
    if not isinstance(patch, Mapping) or isinstance(patch, (str, bytes)):
        raise InvalidSchema()
    return dict(patch)


def _keys(document: Mapping[str, Any], *, required: set[str], optional: set[str]) -> None:
    keys = set(document)
    if not required <= keys or keys - required - optional:
        raise InvalidSchema()


def _token(value: object, *, limit: int) -> str:
    if not isinstance(value, str) or value.strip() != value or not value or len(value) > limit:
        raise InvalidSchema()
    if any(char.isspace() for char in value):
        raise InvalidSchema()
    return value


def _title(value: object) -> str:
    if not isinstance(value, str):
        raise InvalidSchema()
    cleaned = value.strip()
    if not cleaned or len(cleaned) > 200:
        raise InvalidSchema()
    return cleaned


def _version_list(value: object, *, minimum: int) -> tuple[str, ...]:
    if not isinstance(value, list) or len(value) < minimum:
        raise InvalidSchema()
    found: list[str] = []
    for item in value:
        if not isinstance(item, Mapping) or set(item) != {"page_version_id"}:
            raise InvalidSchema()
        found.append(_token(item["page_version_id"], limit=64))
    if len(set(found)) != len(found):
        raise InvalidSchema()
    return tuple(found)


def _with_anchors(body: dict[str, Any], anchor_ids: tuple[str, ...]) -> dict[str, Any]:
    if anchor_ids:
        body["evidence_anchor_ids"] = list(anchor_ids)
    return body


def _anchor_ids(document: Mapping[str, Any]) -> tuple[str, ...]:
    if "evidence_anchor_ids" not in document:
        return ()
    return _citations(document["evidence_anchor_ids"])


def _citations(value: object) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise InvalidSchema()
    found = [_token(item, limit=64) for item in value]
    if len(set(found)) != len(found):
        raise InvalidSchema()
    return tuple(found)


def _conflicts(
    value: object,
    sources: tuple[str, ...],
) -> tuple[tuple[str, tuple[str, ...]], ...]:
    if not isinstance(value, list) or not value:
        raise InvalidSchema()
    found: list[tuple[str, tuple[str, ...]]] = []
    allowed = set(sources)
    for item in value:
        if not isinstance(item, Mapping) or set(item) != {"code", "source_version_ids"}:
            raise InvalidSchema()
        code = _token(item["code"], limit=64)
        raw = item["source_version_ids"]
        if not isinstance(raw, list) or len(raw) < 2:
            raise InvalidSchema()
        version_ids = tuple(_token(version_id, limit=64) for version_id in raw)
        if set(version_ids) - allowed or len(set(version_ids)) != len(version_ids):
            raise InvalidSchema()
        found.append((code, version_ids))
    return tuple(found)


def _optional_str(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise InvalidSchema()
    return value


def _optional_int(value: object) -> int | None:
    if value is None:
        return None
    if type(value) is not int:
        raise InvalidSchema()
    return value
