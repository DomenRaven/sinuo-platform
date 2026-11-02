"""Import two allowed public method kits into runtime chunks and a page graph.

Downloads only 18F Methods (CC0) and Open Design Kit (CC BY 4.0). Writes
chunks.json and graph.json. Method prose stays under the output directory,
which is gitignored when that directory is var/theory-open.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import stat
import sys
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

_TEXT_MAX = 8000
_PATH_MAX = 240
_ID_MAX = 120
_TITLE_MAX = 80
_SUMMARY_MAX = 160
_COURSE_MAX = 64
_LIBRARY_KEY = "course"
_KIND = "related"
_MAX_ZIP_BYTES = 120 * 1024 * 1024
_ID_BREAKS = set("\r\n。！？")
_SECTION_RE = re.compile(r"^## ([^\n]+)\n(.*?)(?=^## |\Z)", re.M | re.S)
_MD_LINK_RE = re.compile(r"\[[^\]]*\]\(([^)\s]+)\)")
_LIQUID_LINK_RE = re.compile(r"\{%\s*link\s+([^%]+?)\s*%\}")
_IMG_RE = re.compile(r"!\[[^\]]*\]\([^)]*\)")
_LINK_LABEL_RE = re.compile(r"\[([^\]]*)\]\([^)]*\)")
_LIQUID_RE = re.compile(r"\{%.*?%\}|\{\{.*?\}\}", re.S)
_TAG_RE = re.compile(r"<[^>]+>")
_STEPS = ("how to do it", "one way to do it")
_WHY = ("why do it",)
_WHEN = ("when to do it",)
_LIMITS = ("considerations for use in government",)
_EXAMPLES = (
    "example from 18f",
    "examples from 18f",
    "examples",
    "applied in government research",
)
_TIME = ("time estimate",)
_LICENSE_NAMES = frozenset({"LICENSE", "LICENSE.md", "LICENSE.txt", "COPYING"})
_METHOD_HOSTS = frozenset(
    {
        "opendesignkit.org",
        "www.opendesignkit.org",
        "methods.18f.gov",
        "www.methods.18f.gov",
    }
)

REPO_18F = "https://github.com/18F/methods"
REPO_ODK = "https://github.com/open-design-kit/opendesignkit"
LICENSE_BY = "https://creativecommons.org/licenses/by/4.0/"
ARCHIVE_URLS = (
    f"{REPO_18F}/archive/refs/heads/main.zip",
    f"{REPO_ODK}/archive/refs/heads/master.zip",
)
ALLOWED_REPOS = (REPO_18F, REPO_ODK)


@dataclass(frozen=True)
class _Source:
    prefix: str
    folder: str
    course: str
    license_id: str
    repo_url: str
    archive_url: str
    credit: str
    needles: tuple[str, ...]
    banned: tuple[str, ...]


@dataclass(frozen=True)
class _Card:
    page_id: str
    course: str
    title: str
    summary: str
    text: str
    source_path: str
    refs: tuple[str, ...]


class _Stop(Exception):
    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


_SOURCES = (
    _Source(
        prefix="18f",
        folder="18f-methods",
        course="18F Methods",
        license_id="CC0-1.0",
        repo_url=REPO_18F,
        archive_url=ARCHIVE_URLS[0],
        credit=f"来源：18F Methods。{REPO_18F} 许可：CC0 1.0。",
        needles=("CC0",),
        banned=("NonCommercial", "NoDerivatives"),
    ),
    _Source(
        prefix="odk",
        folder="open-design-kit",
        course="Open Design Kit",
        license_id="CC-BY-4.0",
        repo_url=REPO_ODK,
        archive_url=ARCHIVE_URLS[1],
        credit=(
            "来源：Open Design Kit。作者：Open Design Kit contributors。"
            f"{REPO_ODK} 许可：CC BY 4.0。{LICENSE_BY}"
        ),
        needles=("Attribution 4.0",),
        banned=("NonCommercial", "NoDerivatives", "ShareAlike"),
    ),
)


def main(argv: list[str] | None = None) -> int:
    repo = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description="Import CC0 and CC BY 4.0 method cards.")
    parser.add_argument("--out", type=Path, default=repo / "var" / "theory-open")
    parser.add_argument("--18f", dest="source_18f", type=Path)
    parser.add_argument("--odk", dest="source_odk", type=Path)
    args = parser.parse_args(argv)
    try:
        roots = _resolve_roots(args.out, args.source_18f, args.source_odk)
        cards = _load_cards(roots)
        chunks, graph = _corpus(cards)
    except _Stop as exc:
        print(exc.message, file=sys.stderr)
        return 2
    args.out.mkdir(parents=True, exist_ok=True)
    _write_json(args.out / "chunks.json", chunks)
    _write_json(args.out / "graph.json", graph)
    for spec in _SOURCES:
        count = sum(1 for card in cards if card.course == spec.course)
        print(f"source={spec.course} license={spec.license_id} cards={count}")
    print(
        f"chunks={len(chunks)} pages={len(graph['pages'])} links={len(graph['links'])}"
    )
    return 0


def _resolve_roots(
    out: Path,
    source_18f: Path | None,
    source_odk: Path | None,
) -> tuple[Path, Path]:
    if source_18f is None and source_odk is None:
        cache = out / "sources"
        return _download(_SOURCES[0], cache), _download(_SOURCES[1], cache)
    if source_18f is None or source_odk is None:
        raise _Stop("method source is missing")
    if not source_18f.is_dir() or not source_odk.is_dir():
        raise _Stop("method source is missing")
    return source_18f, source_odk


def _download(spec: _Source, cache: Path) -> Path:
    dest = cache / spec.folder
    if dest.exists():
        _rmtree(dest)
    dest.mkdir(parents=True, exist_ok=True)
    zip_path = cache / f"{spec.prefix}.zip"
    try:
        _fetch(spec.archive_url, zip_path)
        _extract_archive(zip_path, dest)
        license_text = _read_license(dest)
        if license_text is None or not _license_ok(license_text, spec.needles, spec.banned):
            _rmtree(dest)
            raise _Stop(f"license rejected: {spec.course}")
    except _Stop:
        raise
    except (OSError, urllib.error.URLError, zipfile.BadZipFile, TimeoutError, ValueError):
        if dest.exists():
            _rmtree(dest)
        raise _Stop("method download failed") from None
    finally:
        if zip_path.exists():
            zip_path.unlink()
    return dest


def _fetch(url: str, dest: Path) -> None:
    if url not in ARCHIVE_URLS:
        raise ValueError("archive")
    request = urllib.request.Request(url, headers={"User-Agent": "sinuo-open-methods"})
    total = 0
    dest.parent.mkdir(parents=True, exist_ok=True)
    with urllib.request.urlopen(request, timeout=180) as response, dest.open("wb") as handle:
        while True:
            block = response.read(65536)
            if not block:
                break
            total += len(block)
            if total > _MAX_ZIP_BYTES:
                raise ValueError("download")
            handle.write(block)


def _extract_archive(zip_path: Path, dest: Path) -> None:
    with zipfile.ZipFile(zip_path) as archive:
        for info in archive.infolist():
            name = info.filename.replace("\\", "/")
            parts = PurePosixPath(name).parts
            if not parts or parts[0] == ".." or any(part == ".." for part in parts):
                continue
            if len(parts) < 2:
                continue
            inner = PurePosixPath(*parts[1:])
            if not _keep_member(inner):
                continue
            target = dest.joinpath(*inner.parts)
            if not _inside(dest, target):
                continue
            if info.is_dir() or name.endswith("/"):
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(info) as source, target.open("wb") as handle:
                shutil.copyfileobj(source, handle)


def _keep_member(inner: PurePosixPath) -> bool:
    if inner.as_posix() in _LICENSE_NAMES:
        return True
    return bool(inner.parts) and inner.parts[0] == "_methods"


def _inside(root: Path, target: Path) -> bool:
    root_resolved = root.resolve()
    target_resolved = target.resolve()
    return target_resolved == root_resolved or root_resolved in target_resolved.parents


def _load_cards(roots: tuple[Path, Path]) -> list[_Card]:
    cards: list[_Card] = []
    for spec, root in zip(_SOURCES, roots, strict=True):
        cards.extend(_cards_from_root(spec, root))
    if not cards:
        raise _Stop("no method cards")
    return cards


def _cards_from_root(spec: _Source, root: Path) -> list[_Card]:
    license_text = _read_license(root)
    if license_text is None or not _license_ok(license_text, spec.needles, spec.banned):
        raise _Stop(f"license rejected: {spec.course}")
    methods = root / "_methods"
    if not methods.is_dir():
        raise _Stop(f"no method cards: {spec.course}")
    used: set[str] = set()
    found: list[tuple[str, dict[str, str], Path, tuple[str, ...]]] = []
    for path in sorted(item for item in methods.rglob("*") if item.is_file()):
        if path.name.startswith("_"):
            continue
        if path.suffix.casefold() not in {"", ".md", ".markdown"}:
            continue
        raw = _read(path)
        meta, body = _split_frontmatter(raw)
        sections = _sections(body)
        if not any(name in sections for name in _STEPS):
            continue
        page_id = _page_id(spec.prefix, _stem(path.name), used)
        found.append((page_id, meta, path, _refs(raw)))
    lookup = _lookup(found)
    cards: list[_Card] = []
    for page_id, meta, path, refs in found:
        card = _card(spec, root, path, page_id, meta, lookup, refs)
        if card is not None:
            cards.append(card)
    if not cards:
        raise _Stop(f"no method cards: {spec.course}")
    return cards


def _card(
    spec: _Source,
    root: Path,
    path: Path,
    page_id: str,
    meta: dict[str, str],
    lookup: dict[str, str],
    refs: tuple[str, ...],
) -> _Card | None:
    raw = _read(path)
    _meta, body = _split_frontmatter(raw)
    sections = _sections(body)
    title = _line(meta.get("title", ""), _TITLE_MAX) or _line(page_id, _TITLE_MAX)
    summary_source = _first_text(
        meta.get("description", ""),
        meta.get("what", ""),
        meta.get("teaser", ""),
        meta.get("why", ""),
        _section_text(sections, _WHY),
        title,
    )
    summary = _line(summary_source, _SUMMARY_MAX) or title
    text = _compose(spec, meta, sections, title)
    if not text:
        return None
    relative = path.resolve().relative_to(root.resolve()).as_posix()
    source_path = _source_path(f"{spec.folder}/{relative}")
    targets = tuple(
        target
        for target in (_resolve(ref, lookup) for ref in refs)
        if target is not None and target != page_id
    )
    return _Card(page_id, spec.course, title, summary, text, source_path, targets)


def _compose(
    spec: _Source,
    meta: dict[str, str],
    sections: dict[str, str],
    title: str,
) -> str:
    problem = _problem(meta, sections)
    steps = _plain(_section_text(sections, _STEPS))
    when = _plain(_section_text(sections, _WHEN))
    limits = _plain(_section_text(sections, _LIMITS))
    examples = _plain(_section_text(sections, _EXAMPLES))
    clock = _plain(meta.get("timeRequired", "") or _section_text(sections, _TIME))
    parts = (
        ("名称", title),
        ("适用问题", problem),
        ("何时", when),
        ("操作步骤", steps),
        ("时间", clock),
        ("局限", limits),
        ("示例", examples),
    )
    blocks = [f"{label}：{value}" for label, value in parts if value]
    body = "\n".join(blocks).strip()
    credit = spec.credit.strip()
    room = _TEXT_MAX - len(credit) - 1
    if room < 1:
        raise _Stop("invalid corpus")
    if len(body) > room:
        body = body[:room].rstrip()
    text = f"{body}\n{credit}".strip()
    if not text or len(text) > _TEXT_MAX:
        raise _Stop("invalid corpus")
    return text


def _problem(meta: dict[str, str], sections: dict[str, str]) -> str:
    pieces: list[str] = []
    for piece in (meta.get("why", ""), _section_text(sections, _WHY)):
        cleaned = _plain(piece)
        if cleaned and cleaned not in pieces:
            pieces.append(cleaned)
    if pieces:
        return "\n".join(pieces)
    return _plain(
        _first_text(meta.get("teaser", ""), meta.get("description", ""), meta.get("what", ""))
    )


def _corpus(cards: list[_Card]) -> tuple[list[dict[str, str]], dict[str, object]]:
    pages = {card.page_id for card in cards}
    chunks: list[dict[str, str]] = []
    edges: set[tuple[str, str, str]] = set()
    seen: set[str] = set()
    for card in cards:
        ident = chunk_id_of(card.page_id)
        if card.page_id in seen or ident in seen:
            raise _Stop("invalid corpus")
        seen.add(card.page_id)
        seen.add(ident)
        chunks.append(_chunk(card))
        for target in card.refs:
            if target in pages and target != card.page_id:
                edges.add((card.page_id, target, _KIND))
    chunks.sort(key=lambda item: item["chunk_id"])
    graph = {
        "pages": {
            card.page_id: {"title": card.title, "summary": card.summary}
            for card in sorted(cards, key=lambda item: item.page_id)
        },
        "links": [
            {"source_page_id": source, "target_page_id": target, "kind": kind}
            for source, target, kind in sorted(edges)
        ],
    }
    _require_corpus(chunks, graph)
    return chunks, graph


def _chunk(card: _Card) -> dict[str, str]:
    if len(card.course) > _COURSE_MAX or card.course != card.course.strip():
        raise _Stop("invalid corpus")
    return {
        "library_key": _LIBRARY_KEY,
        "course": card.course,
        "page_id": card.page_id,
        "chunk_id": chunk_id_of(card.page_id),
        "text": card.text,
        "text_hash": hashlib.sha256(card.text.encode("utf-8")).hexdigest(),
        "source_path": card.source_path,
    }


def _require_corpus(chunks: list[dict[str, str]], graph: dict[str, object]) -> None:
    pages = graph["pages"]
    links = graph["links"]
    if not isinstance(pages, dict) or not isinstance(links, list):
        raise _Stop("invalid corpus")
    ids = {item["page_id"] for item in chunks}
    if set(pages) != ids:
        raise _Stop("invalid corpus")
    for item in chunks:
        if item["library_key"] != _LIBRARY_KEY:
            raise _Stop("invalid corpus")
        if item["text_hash"] != hashlib.sha256(item["text"].encode("utf-8")).hexdigest():
            raise _Stop("invalid corpus")
        if not item["text"].strip() or len(item["text"]) > _TEXT_MAX:
            raise _Stop("invalid corpus")
        if ":" in item["source_path"] or "\\" in item["source_path"]:
            raise _Stop("invalid corpus")
        source_path = item["source_path"]
        if len(source_path) > _PATH_MAX or source_path != source_path.strip():
            raise _Stop("invalid corpus")
        _require_id(item["page_id"])
        _require_id(item["chunk_id"])
    for page_id, body in pages.items():
        _require_id(page_id)
        if not isinstance(body, dict):
            raise _Stop("invalid corpus")
        _require_line(str(body.get("title", "")), _TITLE_MAX)
        _require_line(str(body.get("summary", "")), _SUMMARY_MAX)
    for link in links:
        if not isinstance(link, dict):
            raise _Stop("invalid corpus")
        source = str(link.get("source_page_id", ""))
        target = str(link.get("target_page_id", ""))
        if source not in ids or target not in ids or source == target:
            raise _Stop("invalid corpus")
        if link.get("kind") != _KIND:
            raise _Stop("invalid corpus")


def _require_id(value: str) -> None:
    if (
        not value
        or value != value.strip()
        or len(value) > 128
        or any(char in value for char in _ID_BREAKS)
    ):
        raise _Stop("invalid corpus")


def _require_line(value: str, limit: int) -> None:
    if (
        not value
        or value != value.strip()
        or len(value) > limit
        or "\n" in value
        or "\r" in value
    ):
        raise _Stop("invalid corpus")


def _stem(name: str) -> str:
    stem = name
    for suffix in (".markdown", ".md"):
        if stem.casefold().endswith(suffix):
            return stem[: -len(suffix)]
    return stem


def _lookup(
    found: list[tuple[str, dict[str, str], Path, tuple[str, ...]]],
) -> dict[str, str]:
    lookup: dict[str, str] = {}
    for page_id, meta, path, _refs_found in found:
        lookup.setdefault(_slug(_stem(path.name)), page_id)
        permalink = meta.get("permalink", "").strip().strip("/")
        if permalink:
            lookup.setdefault(_slug(permalink.rsplit("/", 1)[-1]), page_id)
    return lookup


def _refs(raw: str) -> tuple[str, ...]:
    found = _MD_LINK_RE.findall(raw) + _LIQUID_LINK_RE.findall(raw)
    return tuple(item.strip() for item in found if item.strip())


def _resolve(token: str, lookup: dict[str, str]) -> str | None:
    cleaned = token.strip().strip("<>")
    cleaned = cleaned.split("#", 1)[0].split("?", 1)[0].strip()
    if not cleaned:
        return None
    if "://" in cleaned:
        parsed = urllib.parse.urlparse(cleaned)
        if parsed.netloc.casefold() not in _METHOD_HOSTS:
            return None
        cleaned = parsed.path
    while "//" in cleaned:
        cleaned = cleaned.replace("//", "/")
    cleaned = cleaned.replace("\\", "/").strip().strip("/")
    if not cleaned:
        return None
    base = cleaned.rsplit("/", 1)[-1]
    for suffix in (".markdown", ".md"):
        if base.casefold().endswith(suffix):
            base = base[: -len(suffix)]
            break
    for candidate in (base, cleaned.rsplit("/", 1)[-1]):
        page_id = lookup.get(_slug(candidate))
        if page_id:
            return page_id
    return None


def _read_license(root: Path) -> str | None:
    for name in ("LICENSE.md", "LICENSE", "LICENSE.txt", "COPYING"):
        path = root / name
        if path.is_file():
            return path.read_text(encoding="utf-8-sig")
    return None


def _license_ok(text: str, needles: tuple[str, ...], banned: tuple[str, ...]) -> bool:
    folded = text.casefold()
    if any(word.casefold() in folded for word in banned):
        return False
    return all(word.casefold() in folded for word in needles)


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8-sig").replace("\r\n", "\n").replace("\r", "\n")
    except UnicodeError as exc:
        raise _Stop("unreadable method file") from exc


def _split_frontmatter(text: str) -> tuple[dict[str, str], str]:
    if not text.startswith("---"):
        return {}, text
    parts = text.split("---", 2)
    if len(parts) < 3:
        return {}, text
    return _parse_frontmatter(parts[1]), parts[2]


def _parse_frontmatter(block: str) -> dict[str, str]:
    data: dict[str, str] = {}
    lines = block.splitlines()
    index = 0
    while index < len(lines):
        raw = lines[index]
        if not raw.strip() or raw.lstrip().startswith("#"):
            index += 1
            continue
        if raw[0].isspace() or ":" not in raw:
            index += 1
            continue
        key, _, rest = raw.partition(":")
        key = key.strip()
        rest = rest.strip()
        index += 1
        if not key:
            continue
        if rest in {"|", ">"}:
            value, index = _collect_block(lines, index)
            data[key] = value
            continue
        if rest == "":
            value, index = _collect_block(lines, index)
            data[key] = value
            continue
        data[key] = _unquote(rest)
    return data


def _collect_block(lines: list[str], index: int) -> tuple[str, int]:
    collected: list[str] = []
    while index < len(lines):
        raw = lines[index]
        if raw.strip() == "":
            nxt = index + 1
            while nxt < len(lines) and lines[nxt].strip() == "":
                nxt += 1
            if nxt >= len(lines) or not lines[nxt][0].isspace():
                break
            index += 1
            continue
        if not raw[0].isspace():
            break
        stripped = raw.strip()
        if stripped.startswith("- "):
            stripped = stripped[2:].strip()
        if stripped:
            collected.append(stripped)
        index += 1
    return " ".join(collected), index


def _sections(body: str) -> dict[str, str]:
    found: dict[str, str] = {}
    for match in _SECTION_RE.finditer(body):
        key = _norm_heading(match.group(1))
        value = match.group(2).strip()
        if not key:
            continue
        if key in found:
            found[key] = f"{found[key]}\n{value}"
        else:
            found[key] = value
    return found


def _section_text(sections: dict[str, str], names: Iterable[str]) -> str:
    parts = [sections[name] for name in names if sections.get(name)]
    return "\n".join(parts)


def _norm_heading(value: str) -> str:
    text = " ".join(value.replace("’", "'").replace("‘", "'").split())
    return text.rstrip(":").strip().casefold()


def _plain(value: str) -> str:
    text = value.replace("\r\n", "\n").replace("\r", "\n")
    text = _IMG_RE.sub(" ", text)
    text = _LINK_LABEL_RE.sub(r"\1", text)
    text = _LIQUID_RE.sub(" ", text)
    text = _TAG_RE.sub(" ", text)
    lines = [" ".join(line.split()) for line in text.split("\n")]
    return "\n".join(line for line in lines if line).strip()


def _line(value: str, limit: int) -> str:
    flat = " ".join(value.split()).strip()
    if len(flat) > limit:
        flat = flat[:limit].rstrip()
    return flat


def _first_text(*values: str) -> str:
    for value in values:
        cleaned = _plain(value)
        if cleaned:
            return cleaned
    return ""


def _page_id(prefix: str, stem: str, used: set[str]) -> str:
    slug = _slug(stem) or "method"
    ident = f"{prefix}-{slug}"
    ident = "".join(char for char in ident if char not in _ID_BREAKS).strip()
    if len(ident) > _ID_MAX:
        ident = ident[:_ID_MAX].rstrip("-")
    base = ident or prefix
    number = 2
    while ident in used or not ident:
        tail = f"-{number}"
        ident = f"{base[: _ID_MAX - len(tail)].rstrip('-')}{tail}"
        number += 1
    used.add(ident)
    return ident


def _slug(value: str) -> str:
    text = value.casefold()
    for suffix in (".markdown", ".md"):
        if text.endswith(suffix):
            text = text[: -len(suffix)]
            break
    chars: list[str] = []
    for char in text:
        if char.isascii() and char.isalnum():
            chars.append(char)
        elif char in "-_.":
            chars.append("-")
    slug = "".join(chars).strip("-")
    while "--" in slug:
        slug = slug.replace("--", "-")
    return slug


def _source_path(value: str) -> str:
    relative = value.replace("\\", "/").strip().lstrip("/")
    if len(relative) <= _PATH_MAX:
        return relative
    trimmed = relative[-_PATH_MAX :].lstrip("/")
    return trimmed or relative[:_PATH_MAX]


def _unquote(value: str) -> str:
    text = value.strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in {'"', "'"}:
        return text[1:-1]
    return text


def _write_json(path: Path, payload: object) -> None:
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def _rmtree(path: Path) -> None:
    def onexc(func: object, target: str, _exc: BaseException) -> None:
        Path(target).chmod(stat.S_IWRITE)
        if callable(func):
            func(target)

    shutil.rmtree(path, onexc=onexc)


def chunk_id_of(page_id: str) -> str:
    return f"{page_id}#method"


if __name__ == "__main__":
    raise SystemExit(main())
