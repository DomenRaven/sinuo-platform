"""Export concept and method pages to runtime chunks.json and graph.json.

Reads wiki/concepts and wiki/methods only. Drops index and moc pages.
Chunk text is the page's own definition or steps section. Links are explicit
references that resolve to another concept or method page.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path

_TEXT_MAX = 8000
_PATH_MAX = 240
_ID_MAX = 128
_TITLE_MAX = 80
_SUMMARY_MAX = 160
_KIND_MAX = 64
_COURSE_MAX = 64
_LIBRARY_KEY = "course"
_ID_BREAKS = "\r\n。！？"
_KIND_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_SECTION_RE = re.compile(r"^## ([^\n]+)\n(.*?)(?=^## |\Z)", re.M | re.S)
_BACKTICK_RE = re.compile(r"`([^`\n]+)`")
_WIKI_RE = re.compile(r"\[\[([^\]\n]+)\]\]")
_MD_RE = re.compile(r"\[[^\]]*\]\(([^)\s]+)\)")
_FOLDERS = (("concepts", "concept"), ("methods", "method"))


def main(argv: list[str] | None = None) -> int:
    repo = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description="Export course theory pages.")
    parser.add_argument("--source", type=Path, default=Path("F:/工业设计理论库"))
    parser.add_argument("--out", type=Path, default=repo / "var" / "theory")
    args = parser.parse_args(argv)
    source = args.source
    if not source.is_dir():
        print("theory source is missing", file=sys.stderr)
        return 2
    chunks, graph = build_export(source)
    args.out.mkdir(parents=True, exist_ok=True)
    _write_json(args.out / "chunks.json", chunks)
    _write_json(args.out / "graph.json", graph)
    print(f"chunks={len(chunks)} pages={len(graph['pages'])} links={len(graph['links'])}")
    return 0


def build_export(source: Path) -> tuple[list[dict[str, str]], dict[str, object]]:
    records = _load_pages(source)
    by_id = {record["page_id"]: record for record in records}
    chunks = _chunks(records)
    links = _links(records, by_id)
    pages = {
        record["page_id"]: {"title": record["title"], "summary": record["summary"]}
        for record in sorted(records, key=lambda item: item["page_id"])
    }
    return chunks, {"pages": pages, "links": links}


def _load_pages(source: Path) -> list[dict[str, object]]:
    records: list[dict[str, object]] = []
    seen: set[str] = set()
    for folder, default_type in _FOLDERS:
        directory = source / "wiki" / folder
        if not directory.is_dir():
            continue
        for path in sorted(directory.glob("*.md")):
            record = _page_record(path, source, default_type)
            if record is None:
                continue
            page_id = str(record["page_id"])
            if page_id in seen:
                continue
            seen.add(page_id)
            records.append(record)
    return records


def _page_record(path: Path, source: Path, default_type: str) -> dict[str, object] | None:
    text = path.read_text(encoding="utf-8-sig").replace("\r\n", "\n").replace("\r", "\n")
    meta, body = _split_frontmatter(text)
    page_type = str(meta.get("type") or "").strip()
    if page_type in {"index", "moc"}:
        return None
    if page_type not in {"concept", "method"}:
        if page_type:
            return None
        page_type = default_type
    page_id = _clean_id(str(meta.get("id") or ""))
    if not page_id:
        return None
    sections = _sections(body)
    definition = _usable(sections.get("定义", ""))
    steps = _usable(sections.get("步骤", ""))
    fallback = ""
    fallback_slot = ""
    if not definition and not steps:
        for slot_name in ("要点", "何时使用", "产出物", "设计应用"):
            candidate = _usable(sections.get(slot_name, ""))
            if candidate:
                fallback = candidate
                fallback_slot = slot_name
                break
    title = _line(str(meta.get("title") or ""), _TITLE_MAX) or _line(page_id, _TITLE_MAX)
    summary_source = definition or steps or fallback or title
    summary = _line(summary_source, _SUMMARY_MAX) or title
    related = meta.get("related")
    related_items = related if isinstance(related, list) else []
    return {
        "page_id": page_id,
        "page_type": page_type,
        "course": _course(meta.get("course")),
        "title": title,
        "summary": summary,
        "definition": definition,
        "steps": steps,
        "fallback": fallback,
        "fallback_slot": fallback_slot,
        "body": body,
        "related": [str(item) for item in related_items],
        "source_path": _source_path(path, source),
    }


def _chunks(records: list[dict[str, object]]) -> list[dict[str, str]]:
    used: set[str] = set()
    chunks: list[dict[str, str]] = []
    ordered = sorted(records, key=lambda item: str(item["page_id"]))
    for record in ordered:
        course = str(record["course"])
        source_path = str(record["source_path"])
        if not course or not source_path:
            continue
        pieces = (
            ("definition", str(record["definition"])),
            ("steps", str(record["steps"])),
        )
        fallback = str(record.get("fallback", ""))
        fallback_slot = str(record.get("fallback_slot", ""))
        if fallback and fallback_slot:
            pieces = pieces + ((fallback_slot, fallback),)
        for slot, raw in pieces:
            text = _clip_text(raw)
            if not text:
                continue
            chunk_id = _chunk_id(str(record["page_id"]), slot, used)
            chunks.append(
                {
                    "library_key": _LIBRARY_KEY,
                    "course": course,
                    "page_id": str(record["page_id"]),
                    "chunk_id": chunk_id,
                    "text": text,
                    "text_hash": hashlib.sha256(text.encode("utf-8")).hexdigest(),
                    "source_path": source_path,
                }
            )
    chunks.sort(key=lambda item: item["chunk_id"])
    return chunks


def _links(
    records: list[dict[str, object]],
    by_id: dict[str, dict[str, object]],
) -> list[dict[str, str]]:
    edges: set[tuple[str, str, str]] = set()
    for record in records:
        source_id = str(record["page_id"])
        for item in record["related"]:
            _add_edge(edges, source_id, str(item), "related", by_id)
        _body_edges(edges, source_id, str(record["body"]), by_id)
    return [
        {"source_page_id": source, "target_page_id": target, "kind": kind}
        for source, target, kind in sorted(edges)
    ]


def _body_edges(
    edges: set[tuple[str, str, str]],
    source_id: str,
    body: str,
    by_id: dict[str, dict[str, object]],
) -> None:
    for token in _BACKTICK_RE.findall(body):
        _add_edge(edges, source_id, token, "related", by_id)
    for token in _WIKI_RE.findall(body):
        target, _, kind = token.partition("|")
        _add_edge(edges, source_id, target, kind or None, by_id)
    for href in _MD_RE.findall(body):
        _add_edge(edges, source_id, href, "related", by_id)


def _add_edge(
    edges: set[tuple[str, str, str]],
    source_id: str,
    token: str,
    kind: str | None,
    by_id: dict[str, dict[str, object]],
) -> None:
    target = _resolve(token, by_id)
    if target is None or target == source_id:
        return
    edges.add((source_id, target, _kind(kind)))


def _resolve(token: str, by_id: dict[str, dict[str, object]]) -> str | None:
    cleaned = token.strip().strip("<>")
    cleaned = cleaned.split("#", 1)[0].split("?", 1)[0].strip()
    if not cleaned or "://" in cleaned:
        return None
    name = cleaned.replace("\\", "/").rstrip("/")
    base = name.rsplit("/", 1)[-1]
    if base.endswith(".md"):
        base = base[:-3]
    for candidate in (cleaned, name, base):
        page_id = _clean_id(candidate)
        if page_id in by_id:
            return page_id
    return None


def _split_frontmatter(text: str) -> tuple[dict[str, str | list[str]], str]:
    if not text.startswith("---"):
        return {}, text
    parts = text.split("---", 2)
    if len(parts) < 3:
        return {}, text
    return _parse_frontmatter(parts[1]), parts[2]


def _parse_frontmatter(block: str) -> dict[str, str | list[str]]:
    data: dict[str, str | list[str]] = {}
    mode: str | None = None
    for raw in block.splitlines():
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        stripped = raw.strip()
        if stripped.startswith("- "):
            if mode is None:
                continue
            item = _unquote(stripped[2:])
            if not item or ":" in item:
                continue
            bucket = data.get(mode)
            if not isinstance(bucket, list):
                bucket = []
                data[mode] = bucket
            bucket.append(item)
            continue
        if raw[0] in " \t":
            continue
        if ":" not in raw:
            mode = None
            continue
        key, _, rest = raw.partition(":")
        key = key.strip()
        rest = rest.strip()
        if rest == "":
            mode = key
            data.setdefault(key, [])
            continue
        mode = None
        data[key] = _unquote(rest)
    return data


def _sections(body: str) -> dict[str, str]:
    found: dict[str, str] = {}
    for match in _SECTION_RE.finditer(body):
        heading = match.group(1).strip()
        if heading not in found:
            found[heading] = match.group(2).strip()
    return found


def _usable(text: str) -> str:
    if not text:
        return ""
    if text.startswith(">") and ("Gap" in text or "直接共现" in text):
        return ""
    return text


def _course(value: object) -> str:
    if isinstance(value, list):
        items = [str(item) for item in value]
    elif isinstance(value, str):
        raw = value.strip()
        if raw.startswith("[") and raw.endswith("]"):
            items = [part.strip() for part in raw[1:-1].split(",") if part.strip()]
        else:
            items = [raw]
    else:
        items = []
    for item in items:
        text = " ".join(_unquote(item).split()).strip()
        if text:
            clipped = text[:_COURSE_MAX].rstrip()
            return clipped or text[:_COURSE_MAX]
    return ""


def _source_path(path: Path, source: Path) -> str:
    relative = path.resolve().relative_to(source.resolve()).as_posix().strip()
    if len(relative) <= _PATH_MAX:
        return relative
    trimmed = relative[-_PATH_MAX:].lstrip("/")
    return trimmed or relative[:_PATH_MAX]


def _clip_text(value: str) -> str:
    text = value.strip()
    if len(text) > _TEXT_MAX:
        text = text[:_TEXT_MAX].rstrip()
    if not text.strip():
        return ""
    return text


def _chunk_id(page_id: str, slot: str, used: set[str]) -> str:
    suffix = "#" + slot
    ident = page_id + suffix
    if len(ident) > _ID_MAX:
        ident = page_id[: _ID_MAX - len(suffix)].rstrip() + suffix
    base = ident
    number = 2
    while ident in used:
        tail = f"-{number}"
        ident = base[: _ID_MAX - len(tail)] + tail
        number += 1
    used.add(ident)
    return ident


def _clean_id(value: str) -> str:
    text = "".join(char for char in value.strip() if char not in _ID_BREAKS).strip()
    if len(text) > _ID_MAX:
        text = text[:_ID_MAX].rstrip()
    return text


def _kind(value: str | None) -> str:
    if not value or not value.strip():
        return "related"
    token = value.strip()
    if len(token) > _KIND_MAX:
        token = token[:_KIND_MAX]
    if _KIND_RE.fullmatch(token):
        return token
    lowered = token.lower()
    if len(lowered) > _KIND_MAX:
        lowered = lowered[:_KIND_MAX]
    if _KIND_RE.fullmatch(lowered):
        return lowered
    return "related"


def _line(value: str, limit: int) -> str:
    flat = " ".join(value.split()).strip()
    if len(flat) > limit:
        flat = flat[:limit].rstrip()
    return flat


def _unquote(value: str) -> str:
    text = value.strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in {"'", '"'}:
        return text[1:-1]
    return text


def _write_json(path: Path, payload: object) -> None:
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


if __name__ == "__main__":
    raise SystemExit(main())
