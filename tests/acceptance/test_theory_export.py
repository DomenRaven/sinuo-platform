"""Export synthetic concept and method pages. No course library files."""

import json
import subprocess
import sys
from pathlib import Path

from sinuo.application.retrieval.graphrag import TheoryLink, TheoryPage, query_theory_graph
from sinuo.application.retrieval.vectors import search_theory_vectors

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "export_theory_corpus.py"
ANCHOR = "page-anchor"
SUPPORT = "page-support"
QUERY = "按压按钮需要明确的行程反馈"
DEFINITION = "按压按钮需要明确的行程反馈，用户才能确认操作已经生效。"
STEPS = "1. 叶绿体类囊体薄膜上进行光反应"
SENTINEL = "SYNTHETIC_EXTRA_SENTINEL"

ANCHOR_PAGE = f"""---
id: {ANCHOR}
type: concept
title: 合成锚点
course:
- design-psychology
- ux-product-innovation
related:
- {SUPPORT}
- lecture-not-a-page
---
# 合成锚点

## 定义

{DEFINITION}

## 要点

{SENTINEL}

## Evidence

> Evidence | nowhere | {SENTINEL}

## 相关

- `{SUPPORT}`
- `page-missing`
"""

SUPPORT_PAGE = f"""---
id: {SUPPORT}
type: method
title: 合成步骤
course: ux-product-innovation
---
# 合成步骤

## 何时使用

{SENTINEL}

## 步骤

{STEPS}

## 产出物

{SENTINEL}
"""

INDEX_PAGE = f"""---
id: page-index
type: index
title: 目录
course:
- design-psychology
---
# 目录

## 定义

{DEFINITION}
"""


def test_synthetic_export_is_searchable(tmp_path: Path) -> None:
    source = tmp_path / "library"
    (source / "wiki" / "concepts").mkdir(parents=True)
    (source / "wiki" / "methods").mkdir(parents=True)
    (source / "wiki" / "concepts" / "page-anchor.md").write_text(ANCHOR_PAGE, encoding="utf-8")
    (source / "wiki" / "methods" / "page-support.md").write_text(SUPPORT_PAGE, encoding="utf-8")
    (source / "wiki" / "concepts" / "page-index.md").write_text(INDEX_PAGE, encoding="utf-8")
    out = tmp_path / "theory"

    completed = subprocess.run(
        [sys.executable, str(SCRIPT), "--source", str(source), "--out", str(out)],
        check=True,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0

    chunks = json.loads((out / "chunks.json").read_text(encoding="utf-8"))
    graph = json.loads((out / "graph.json").read_text(encoding="utf-8"))
    assert {item["page_id"] for item in chunks} == {ANCHOR, SUPPORT}
    assert set(graph["pages"]) == {ANCHOR, SUPPORT}
    assert graph["links"] == [
        {"source_page_id": ANCHOR, "target_page_id": SUPPORT, "kind": "related"}
    ]
    by_id = {item["page_id"]: item for item in chunks}
    anchor = by_id[ANCHOR]
    assert anchor["library_key"] == "course"
    assert anchor["course"] == "design-psychology"
    assert anchor["text"] == DEFINITION
    assert anchor["source_path"] == "wiki/concepts/page-anchor.md"
    assert ":" not in anchor["source_path"]
    assert by_id[SUPPORT]["text"] == STEPS
    assert by_id[SUPPORT]["source_path"] == "wiki/methods/page-support.md"
    for item in chunks:
        assert SENTINEL not in item["text"]
        assert len(item["text"]) <= 8000
        assert len(item["source_path"]) <= 240
    for body in graph["pages"].values():
        assert body["title"] == body["title"].strip()
        assert body["summary"] == body["summary"].strip()
        assert "\n" not in body["title"]
        assert "\n" not in body["summary"]
        assert body["title"]
        assert body["summary"]
        assert len(body["title"]) <= 80
        assert len(body["summary"]) <= 160

    found = search_theory_vectors(chunks, QUERY, 5)
    assert found.hits
    assert {hit.page_id for hit in found.hits} == {ANCHOR}

    pages = {
        page_id: TheoryPage(body["title"], body["summary"])
        for page_id, body in graph["pages"].items()
    }
    links = [
        TheoryLink(item["source_page_id"], item["target_page_id"], item["kind"])
        for item in graph["links"]
    ]
    walked = query_theory_graph(pages, links, found.hits[0].page_id, QUERY, 2)
    assert ANCHOR in walked.summary
    assert SUPPORT in walked.summary
