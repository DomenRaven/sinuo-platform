"""Bounded course-page link walk. Five synthetic pages. No library files."""

import pytest

from sinuo.application.retrieval.graphrag import (
    TheoryGraphResult,
    TheoryLink,
    TheoryPage,
    query_theory_graph,
)

PAGE_ANCHOR = "page-anchor"
PAGE_SUPPORT = "page-support"
PAGE_DERIVED = "page-derived"
PAGE_FAR = "page-far"
PAGE_DECOY = "page-decoy"
QUERY = "竹节连接"
DECOY_ONLY = "库外独有"
FAR_ONLY = "过远独有"
DEPTH_TWO = (
    "从页 page-anchor 沿 supports、derived_from 经过"
    "页 page-anchor、页 page-support、页 page-derived，"
    "页 page-derived 的标题、页 page-support 的短摘要含有「竹节连接」。"
)
DEPTH_ONE = (
    "从页 page-anchor 沿 supports 经过页 page-anchor、页 page-support，"
    "页 page-support 的短摘要含有「竹节连接」。"
)
DEPTH_ZERO = "页 page-anchor 的标题和短摘要没有支持「竹节连接」。"
GAP = (
    "从页 page-anchor 沿 supports、derived_from 经过"
    "页 page-anchor、页 page-support、页 page-derived，"
    "这些页的标题和短摘要没有支持「库外独有」。"
)
FAR_ONLY_SENTENCE = "页 page-far 的短摘要含有「过远独有」。"


class _LoggingPages(dict):
    def __init__(self, pages, opened: list[str]) -> None:
        super().__init__(pages)
        self._opened = opened

    def __getitem__(self, key):
        self._opened.append(key)
        return super().__getitem__(key)


def _pages() -> dict[str, TheoryPage]:
    return {
        PAGE_ANCHOR: TheoryPage("锚点页", "合成锚点只说明起点"),
        PAGE_SUPPORT: TheoryPage("支持页", "合成短摘要提到竹节连接"),
        PAGE_DERIVED: TheoryPage("竹节连接的派生页", "后续步骤仍是合成短句"),
        PAGE_FAR: TheoryPage("过远页", "过远独有的合成短句提到竹节连接"),
        PAGE_DECOY: TheoryPage("库外诱饵", "库外独有的合成短句提到竹节连接"),
    }


def _links() -> tuple[TheoryLink, ...]:
    return (
        TheoryLink(PAGE_ANCHOR, PAGE_SUPPORT, "supports"),
        TheoryLink(PAGE_ANCHOR, "page-ghost", "tests"),
        TheoryLink(PAGE_SUPPORT, PAGE_DERIVED, "derived_from"),
        TheoryLink(PAGE_DERIVED, PAGE_FAR, "supports"),
        TheoryLink(PAGE_DECOY, PAGE_ANCHOR, "contradicts"),
    )


def _assert_one_cited_sentence(result: TheoryGraphResult) -> None:
    assert result.summary.count("。") == 1
    assert result.summary.endswith("。")
    for page_id in result.page_ids:
        assert page_id in result.summary
    assert "本体推理" not in result.summary
    assert "社区摘要" not in result.summary


def test_depth_two_cites_only_pages_reached_by_outgoing_links():
    opened: list[str] = []
    result = query_theory_graph(_LoggingPages(_pages(), opened), _links(), PAGE_ANCHOR, QUERY, 2)
    assert result == TheoryGraphResult(
        (PAGE_ANCHOR, PAGE_SUPPORT, PAGE_DERIVED),
        ("supports", "derived_from"),
        DEPTH_TWO,
    )
    assert opened == [PAGE_ANCHOR, PAGE_SUPPORT, PAGE_DERIVED]
    assert PAGE_FAR not in result.summary
    assert PAGE_DECOY not in result.summary
    assert "page-ghost" not in result.page_ids
    assert "tests" not in result.edge_kinds
    assert "contradicts" not in result.edge_kinds
    _assert_one_cited_sentence(result)


def test_shallower_depth_stops_before_later_hops():
    one = query_theory_graph(_pages(), _links(), PAGE_ANCHOR, QUERY, 1)
    zero = query_theory_graph(_pages(), _links(), PAGE_ANCHOR, QUERY, 0)
    assert one == TheoryGraphResult(
        (PAGE_ANCHOR, PAGE_SUPPORT),
        ("supports",),
        DEPTH_ONE,
    )
    assert zero == TheoryGraphResult((PAGE_ANCHOR,), (), DEPTH_ZERO)
    assert PAGE_DERIVED not in one.page_ids
    assert zero.edge_kinds == ()
    _assert_one_cited_sentence(one)
    _assert_one_cited_sentence(zero)


def test_query_outside_the_reached_pages_reports_a_gap():
    result = query_theory_graph(_pages(), _links(), PAGE_ANCHOR, DECOY_ONLY, 2)
    assert result == TheoryGraphResult(
        (PAGE_ANCHOR, PAGE_SUPPORT, PAGE_DERIVED),
        ("supports", "derived_from"),
        GAP,
    )
    assert PAGE_DECOY not in result.summary
    assert PAGE_FAR not in result.summary
    _assert_one_cited_sentence(result)


def test_missing_start_returns_empty_without_opening_pages():
    opened: list[str] = []
    result = query_theory_graph(_LoggingPages(_pages(), opened), _links(), "page-missing", QUERY, 2)
    assert result == TheoryGraphResult((), (), "")
    assert opened == []


def test_incoming_edge_does_not_expand_and_far_page_stays_put():
    result = query_theory_graph(_pages(), _links(), PAGE_FAR, FAR_ONLY, 2)
    assert result == TheoryGraphResult((PAGE_FAR,), (), FAR_ONLY_SENTENCE)
    assert PAGE_DERIVED not in result.page_ids
    assert result.edge_kinds == ()
    _assert_one_cited_sentence(result)


def test_depth_above_two_is_rejected():
    with pytest.raises(ValueError, match="depth"):
        query_theory_graph(_pages(), _links(), PAGE_ANCHOR, QUERY, 3)
    with pytest.raises(ValueError, match="depth"):
        query_theory_graph(_pages(), _links(), PAGE_ANCHOR, QUERY, -1)
