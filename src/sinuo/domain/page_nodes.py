"""Page-node kinds inside a draft document.

Nodes stay in the page draft JSON. There is no nodes table. A node that only
has ``node_id`` (and maybe ``artifact_version_id``) is an older draft and stays
readable. A new node must use one of the seven contract kinds.

Duties follow the course table. Problems belong to clarification. Evidence and
insight belong to evidence. Requirements belong to critic. Artifacts, test
records, and decisions are marked ``human``: a person confirms them, and a
model writer cannot store them as formal records. Method is still a model
duty name, but none of these seven kinds is a method card, a concept path, or
a test plan, so this module does not assign ``method`` to them.
"""

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

NODE_KINDS: tuple[str, ...] = (
    "problem",
    "evidence",
    "insight",
    "requirement",
    "artifact",
    "test",
    "decision",
)

NODE_LABELS: dict[str, str] = {
    "problem": "问题",
    "evidence": "证据",
    "insight": "洞察",
    "requirement": "要求",
    "artifact": "作品",
    "test": "测试",
    "decision": "决定",
}

# Typed edges between nodes carry a semantic kind. A missing kind means the
# older three-key edge and reads back as ``derives`` so the outline stays
# readable; new writes are expected to send one of these four kinds.
EDGE_KINDS: tuple[str, ...] = ("derives", "supports", "refutes", "cites")
EDGE_LABELS: dict[str, str] = {
    "derives": "承接",
    "supports": "支持",
    "refutes": "反例",
    "cites": "引用",
}

# ``human`` is not a fifth model role. It marks 由人确认.
HUMAN_CONFIRMATION = "human"

DUTY_OF_KIND: dict[str, str] = {
    "problem": "clarification",
    "evidence": "evidence",
    "insight": "evidence",
    "requirement": "critic",
    "artifact": HUMAN_CONFIRMATION,
    "test": HUMAN_CONFIRMATION,
    "decision": HUMAN_CONFIRMATION,
}

HUMAN_KINDS: frozenset[str] = frozenset({"artifact", "test", "decision"})
_WRITERS: frozenset[str] = frozenset({"model", "human"})
_LEGACY_KEYS: frozenset[str] = frozenset({"node_id", "artifact_version_id"})
_NODE_KEYS: frozenset[str] = frozenset(
    {"node_id", "kind", "duty", "record", "content", "artifact_version_id"}
)
_STORED_KEYS: frozenset[str] = frozenset({"node_id", "kind", "duty", "record"})
_EDGE_KEYS: frozenset[str] = frozenset(
    {"edge_id", "source_node_id", "target_node_id", "kind"}
)
_EDGE_REQUIRED: frozenset[str] = frozenset(
    {"edge_id", "source_node_id", "target_node_id"}
)
_PAGE_KEYS: frozenset[str] = frozenset({"schema_version", "nodes", "edges"})
_MISSING = object()


class PageNodeRejected(ValueError):
    """A page node violates the kind, duty, or confirmation contract."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class StoredNode:
    node_id: str
    kind: str | None = None
    duty: str | None = None
    record: str | None = None
    content: str | None = None
    artifact_version_id: str | None = None

    @property
    def legacy(self) -> bool:
        return self.kind is None


@dataclass(frozen=True)
class StoredEdge:
    edge_id: str
    source_node_id: str
    target_node_id: str
    kind: str | None = None

    @property
    def kind_or_default(self) -> str:
        return self.kind if self.kind in EDGE_KINDS else "derives"


def canonical_typed_document(document: Mapping[str, Any], *, writer: str) -> str:
    """Canonical JSON for a new draft. Does not write the database."""

    if writer not in _WRITERS:
        raise PageNodeRejected("writer")
    normalized = _normalize(document, writer=writer)
    return _dump(normalized)


def content_digest(content_json: str) -> str:
    return hashlib.sha256(content_json.encode("utf-8")).hexdigest()


def accept_draft_content(content_json: str, *, human_confirmed: bool = False) -> None:
    """Reject illegal new nodes. Older node_id documents pass through.

    Formal artifacts, test records, and decisions also need
    ``human_confirmed``. The existing draft writers leave that flag false, so
    a model payload cannot store those three by calling the draft update.
    """

    document = _object_or_none(content_json)
    if document is None:
        return
    nodes = document.get("nodes")
    if not isinstance(nodes, list) or all(_is_legacy_node(node) for node in nodes):
        return
    stored = _require_stored(document)
    if any(node.kind in HUMAN_KINDS for node in stored) and not human_confirmed:
        raise PageNodeRejected("formal")


def read_page_nodes(content_json: str) -> tuple[StoredNode, ...]:
    """Read nodes from a draft. Missing kinds stay missing."""

    try:
        parsed = json.loads(content_json)
    except json.JSONDecodeError as error:
        raise PageNodeRejected("document") from error
    if not isinstance(parsed, dict):
        raise PageNodeRejected("document")
    nodes = parsed.get("nodes")
    if not isinstance(nodes, list):
        raise PageNodeRejected("nodes")
    if all(_is_legacy_node(node) for node in nodes):
        return tuple(_stored_legacy(node) for node in nodes)
    return _require_stored(parsed)


def read_page_edges(content_json: str) -> tuple[StoredEdge, ...]:
    """Read typed edges with their semantic kind.

    Malformed edges are skipped rather than rejecting the whole draft, so the
    outline stays readable even when an edge is unusable. An edge without a
    kind still returns with ``kind=None`` (read back as ``derives``).
    """

    try:
        parsed = json.loads(content_json)
    except json.JSONDecodeError:
        return ()
    if not isinstance(parsed, dict):
        return ()
    raw = parsed.get("edges")
    if not isinstance(raw, list):
        return ()
    edges: list[StoredEdge] = []
    for edge in raw:
        if not isinstance(edge, Mapping) or set(edge) - _EDGE_KEYS or not _EDGE_REQUIRED <= set(edge):
            continue
        edge_id = edge.get("edge_id")
        source = edge.get("source_node_id")
        target = edge.get("target_node_id")
        if not all(isinstance(value, str) and value for value in (edge_id, source, target)):
            continue
        kind = edge.get("kind")
        if "kind" in edge and (not isinstance(kind, str) or kind not in EDGE_KINDS):
            continue
        edges.append(
            StoredEdge(
                edge_id=edge_id,
                source_node_id=source,
                target_node_id=target,
                kind=kind if isinstance(kind, str) else None,
            )
        )
    return tuple(edges)


def _normalize(document: Mapping[str, Any], *, writer: str) -> dict[str, Any]:
    if not isinstance(document, Mapping) or set(document) != _PAGE_KEYS:
        raise PageNodeRejected("document")
    if type(document["schema_version"]) is not int or document["schema_version"] != 1:
        raise PageNodeRejected("document")
    raw_nodes = document["nodes"]
    raw_edges = document["edges"]
    if not isinstance(raw_nodes, list) or not isinstance(raw_edges, list):
        raise PageNodeRejected("document")
    seen: set[str] = set()
    nodes: list[dict[str, str]] = []
    for node in raw_nodes:
        if not isinstance(node, Mapping) or set(node) - _NODE_KEYS:
            raise PageNodeRejected("node")
        node_id = _token(node.get("node_id"), code="node")
        if node_id in seen:
            raise PageNodeRejected("node")
        seen.add(node_id)
        kind = node.get("kind")
        if not isinstance(kind, str) or kind not in DUTY_OF_KIND:
            raise PageNodeRejected("kind")
        duty, record = _bind(
            kind,
            writer,
            node.get("duty", _MISSING),
            node.get("record", _MISSING),
        )
        item = {"node_id": node_id, "kind": kind, "duty": duty, "record": record}
        if "content" in node:
            content = node.get("content")
            if not isinstance(content, str):
                raise PageNodeRejected("content")
            item["content"] = content
        if "artifact_version_id" in node:
            item["artifact_version_id"] = _token(node.get("artifact_version_id"), code="node")
        nodes.append(item)
    return {"schema_version": 1, "nodes": nodes, "edges": _edges(raw_edges, seen)}


def _bind(kind: str, writer: str, duty: object, record: object) -> tuple[str, str]:
    expected = DUTY_OF_KIND[kind]
    if duty is not _MISSING and duty != expected:
        raise PageNodeRejected("duty")
    if writer == "model":
        if kind in HUMAN_KINDS:
            raise PageNodeRejected("formal")
        if record is not _MISSING and record != "proposal":
            raise PageNodeRejected("formal")
        return expected, "proposal"
    if record is not _MISSING and record != "formal":
        raise PageNodeRejected("record")
    return expected, "formal"


def _require_stored(document: Mapping[str, Any]) -> tuple[StoredNode, ...]:
    if set(document) != _PAGE_KEYS:
        raise PageNodeRejected("document")
    if type(document.get("schema_version")) is not int or document.get("schema_version") != 1:
        raise PageNodeRejected("document")
    raw_nodes = document.get("nodes")
    raw_edges = document.get("edges")
    if not isinstance(raw_nodes, list) or not isinstance(raw_edges, list):
        raise PageNodeRejected("document")
    seen: set[str] = set()
    stored: list[StoredNode] = []
    for node in raw_nodes:
        item = _stored_typed(node)
        if item.node_id in seen:
            raise PageNodeRejected("node")
        seen.add(item.node_id)
        stored.append(item)
    _edges(raw_edges, seen)
    return tuple(stored)


def _stored_typed(node: object) -> StoredNode:
    if not isinstance(node, dict) or set(node) - _NODE_KEYS or not _STORED_KEYS <= set(node):
        raise PageNodeRejected("node")
    kind = node.get("kind")
    if not isinstance(kind, str) or kind not in DUTY_OF_KIND:
        raise PageNodeRejected("kind")
    node_id = _token(node.get("node_id"), code="node")
    duty = node.get("duty")
    record = node.get("record")
    if duty != DUTY_OF_KIND[kind]:
        raise PageNodeRejected("duty")
    if kind in HUMAN_KINDS:
        if record != "formal":
            raise PageNodeRejected("formal")
    elif record not in ("proposal", "formal"):
        raise PageNodeRejected("record")
    artifact = node.get("artifact_version_id")
    if "artifact_version_id" in node:
        artifact = _token(artifact, code="node")
    content = node.get("content")
    if "content" in node and not isinstance(content, str):
        raise PageNodeRejected("content")
    return StoredNode(
        node_id=node_id,
        kind=kind,
        duty=str(duty),
        record=str(record),
        content=content if isinstance(content, str) else None,
        artifact_version_id=artifact if isinstance(artifact, str) else None,
    )


def _stored_legacy(node: Mapping[str, Any]) -> StoredNode:
    artifact = node.get("artifact_version_id")
    return StoredNode(
        node_id=str(node["node_id"]),
        artifact_version_id=artifact if isinstance(artifact, str) else None,
    )


def _is_legacy_node(node: object) -> bool:
    if not isinstance(node, dict) or set(node) - _LEGACY_KEYS:
        return False
    node_id = node.get("node_id")
    if not isinstance(node_id, str) or node_id == "":
        return False
    if "artifact_version_id" not in node:
        return True
    artifact = node.get("artifact_version_id")
    return isinstance(artifact, str) and artifact != ""


def _edges(raw: list[Any], seen: set[str]) -> list[dict[str, str]]:
    found: set[str] = set()
    edges: list[dict[str, str]] = []
    for edge in raw:
        if not isinstance(edge, Mapping) or set(edge) - _EDGE_KEYS or not _EDGE_REQUIRED <= set(edge):
            raise PageNodeRejected("edge")
        edge_id = _token(edge.get("edge_id"), code="edge")
        source = _token(edge.get("source_node_id"), code="edge")
        target = _token(edge.get("target_node_id"), code="edge")
        if edge_id in found or source not in seen or target not in seen:
            raise PageNodeRejected("edge")
        found.add(edge_id)
        item = {
            "edge_id": edge_id,
            "source_node_id": source,
            "target_node_id": target,
        }
        if "kind" in edge:
            kind = edge.get("kind")
            if not isinstance(kind, str) or kind not in EDGE_KINDS:
                raise PageNodeRejected("edge")
            item["kind"] = kind
        edges.append(item)
    return edges


def _token(value: object, *, code: str) -> str:
    if not isinstance(value, str) or value == "":
        raise PageNodeRejected(code)
    return value


def _dump(document: Mapping[str, Any]) -> str:
    return json.dumps(
        document,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _object_or_none(content_json: str) -> dict[str, Any] | None:
    try:
        parsed = json.loads(content_json)
    except json.JSONDecodeError:
        return None
    if not isinstance(parsed, dict):
        return None
    return parsed
