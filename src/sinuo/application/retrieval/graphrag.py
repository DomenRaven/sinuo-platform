"""Bounded walk over caller-supplied course-page links.

The first GraphRAG step follows explicit directed links for at most two hops
and writes one sentence that cites the page ids actually reached. The caller
passes the pages and links. Targets missing from that page set are left out;
this module does not open a library, call a model, or write a community summary.
"""

from dataclasses import dataclass

_DEPTH_MAX = 2
_ID_MAX = 200
_KIND_MAX = 64
_TITLE_MAX = 80
_SUMMARY_MAX = 160
_QUERY_MAX = 80
_SENTENCE_BREAKS = "。！？"


@dataclass(frozen=True)
class TheoryPage:
    title: str
    summary: str


@dataclass(frozen=True)
class TheoryLink:
    source_page_id: str
    target_page_id: str
    kind: str


@dataclass(frozen=True)
class TheoryGraphResult:
    page_ids: tuple[str, ...]
    edge_kinds: tuple[str, ...]
    summary: str


_EMPTY = TheoryGraphResult((), (), "")


def query_theory_graph(
    pages: dict[str, TheoryPage],
    links: tuple[TheoryLink, ...] | list[TheoryLink],
    start_page_id: str,
    query: str,
    depth: int,
) -> TheoryGraphResult:
    """Walk outgoing links from ``start_page_id`` and cite the pages reached.

    ``depth`` above 2 is rejected. An unknown start page returns an empty
    result and does not read the other page texts. The summary is one sentence.
    """

    checked_start = _require_id(start_page_id, "start_page_id")
    checked_query = _require_query(query)
    checked_depth = _require_depth(depth)
    if not isinstance(pages, dict) or not isinstance(links, (list, tuple)):
        raise ValueError("pages" if not isinstance(pages, dict) else "links")
    if checked_start not in pages:
        return _EMPTY
    page_ids, edge_kinds, loaded = _walk(pages, links, checked_start, checked_depth)
    summary = _sentence(loaded, page_ids, edge_kinds, checked_query)
    return TheoryGraphResult(page_ids, edge_kinds, summary)


def _walk(
    pages: dict[str, TheoryPage],
    links: tuple[TheoryLink, ...] | list[TheoryLink],
    start: str,
    depth: int,
) -> tuple[tuple[str, ...], tuple[str, ...], dict[str, TheoryPage]]:
    adjacency = _adjacency(links)
    loaded = {start: _require_page(pages[start])}
    order = [start]
    kinds: list[str] = []
    seen_kinds: set[str] = set()
    layer = [start]
    for _hop in range(depth):
        nxt: list[str] = []
        for source in layer:
            for kind, target in adjacency.get(source, ()):
                if target not in pages or target == source:
                    continue
                if target not in loaded:
                    loaded[target] = _require_page(pages[target])
                    order.append(target)
                    nxt.append(target)
                if kind not in seen_kinds:
                    seen_kinds.add(kind)
                    kinds.append(kind)
        layer = nxt
    return tuple(order), tuple(kinds), loaded


def _adjacency(
    links: tuple[TheoryLink, ...] | list[TheoryLink],
) -> dict[str, tuple[tuple[str, str], ...]]:
    ordered: dict[str, list[tuple[str, str]]] = {}
    seen: set[tuple[str, str, str]] = set()
    for link in links:
        source, target, kind = _require_link(link)
        key = (source, target, kind)
        if key in seen:
            continue
        seen.add(key)
        ordered.setdefault(source, []).append((kind, target))
    return {source: tuple(items) for source, items in ordered.items()}


def _sentence(
    loaded: dict[str, TheoryPage],
    page_ids: tuple[str, ...],
    edge_kinds: tuple[str, ...],
    query: str,
) -> str:
    matched = _matched_clause(loaded, page_ids, query)
    if not edge_kinds:
        if matched:
            return f"{matched}含有「{query}」。"
        return f"页 {page_ids[0]} 的标题和短摘要没有支持「{query}」。"
    cited = "、".join(f"页 {page_id}" for page_id in page_ids)
    route = f"从页 {page_ids[0]} 沿 {'、'.join(edge_kinds)} 经过{cited}"
    if matched:
        return f"{route}，{matched}含有「{query}」。"
    return f"{route}，这些页的标题和短摘要没有支持「{query}」。"


def _matched_clause(
    loaded: dict[str, TheoryPage],
    page_ids: tuple[str, ...],
    query: str,
) -> str:
    ranked = sorted(
        (
            (-_score(loaded[page_id], query), index, page_id)
            for index, page_id in enumerate(page_ids)
            if _score(loaded[page_id], query) > 0
        )
    )
    return "、".join(
        f"页 {page_id} 的{_location(loaded[page_id], query)}"
        for _score_key, _index, page_id in ranked
    )


def _score(page: TheoryPage, query: str) -> int:
    score = 0
    if query in page.title:
        score += 2
    if query in page.summary:
        score += 1
    return score


def _location(page: TheoryPage, query: str) -> str:
    in_title = query in page.title
    in_summary = query in page.summary
    if in_title and in_summary:
        return "标题和短摘要"
    if in_title:
        return "标题"
    return "短摘要"


def _require_link(link: object) -> tuple[str, str, str]:
    if type(link) is not TheoryLink:
        raise ValueError("links")
    return (
        _require_id(link.source_page_id, "links"),
        _require_id(link.target_page_id, "links"),
        _require_kind(link.kind),
    )


def _require_page(page: object) -> TheoryPage:
    if type(page) is not TheoryPage:
        raise ValueError("pages")
    _require_text(page.title, _TITLE_MAX)
    _require_text(page.summary, _SUMMARY_MAX)
    return page


def _require_text(value: object, limit: int) -> None:
    if (
        type(value) is not str
        or value.strip() == ""
        or value != value.strip()
        or len(value) > limit
        or "\n" in value
        or "\r" in value
    ):
        raise ValueError("pages")


def _require_id(value: object, field: str) -> str:
    if (
        type(value) is not str
        or value.strip() == ""
        or value != value.strip()
        or len(value) > _ID_MAX
        or any(char in value for char in f"\n\r{_SENTENCE_BREAKS}")
    ):
        raise ValueError(field)
    return value


def _require_kind(value: object) -> str:
    if (
        type(value) is not str
        or not value
        or len(value) > _KIND_MAX
        or not value[0].islower()
        or any(char != "_" and not char.islower() and not char.isdigit() for char in value)
    ):
        raise ValueError("links")
    return value


def _require_query(value: object) -> str:
    if type(value) is not str:
        raise ValueError("query")
    query = value.strip()
    if (
        query == ""
        or len(query) > _QUERY_MAX
        or any(char in query for char in f"\n\r{_SENTENCE_BREAKS}")
    ):
        raise ValueError("query")
    return query


def _require_depth(depth: object) -> int:
    if type(depth) is not int or depth < 0 or depth > _DEPTH_MAX:
        raise ValueError("depth")
    return depth
