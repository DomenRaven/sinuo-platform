"""OXO case pages stay readable by the course-theory retriever."""

import importlib.util
from pathlib import Path

from sinuo.application.retrieval.graphrag import TheoryLink, TheoryPage, query_theory_graph
from sinuo.application.retrieval.vectors import search_theory_vectors

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "build_oxo_theory.py"


def _builder():
    spec = importlib.util.spec_from_file_location("build_oxo_theory", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_oxo_pages_are_recalled_as_course_chunks():
    chunks, pages, links = _builder().chunks_and_graph()
    found = search_theory_vectors(chunks, "削皮器把手鳍片", 5)
    assert found.hits
    assert all(hit.page_id.startswith("oxo-") for hit in found.hits)
    fins = search_theory_vectors(chunks, "鳍片", 3)
    assert any(hit.page_id == "oxo-concepts" for hit in fins.hits)
    molding = search_theory_vectors(chunks, "注塑", 5)
    assert any(hit.page_id == "oxo-molding" for hit in molding.hits)
    graph_pages = {key: TheoryPage(value["title"], value["summary"]) for key, value in pages.items()}
    graph_links = [
        TheoryLink(row["source_page_id"], row["target_page_id"], row["kind"])
        for row in links
        if row["target_page_id"].startswith("oxo-")
    ]
    walked = query_theory_graph(graph_pages, graph_links, "oxo-task", "削皮器", 2)
    assert "oxo-task" in walked.page_ids
    assert "oxo-research" in walked.page_ids
