"""Course-library vector recall. Five synthetic sentences. No course files."""

import hashlib

import pytest

from sinuo.application.retrieval.vectors import search_theory_vectors

QUERY = "按压按钮需要明确的行程反馈"
PLANT = "叶绿体类囊体薄膜上进行光反应"
BUTTON = "按压按钮需要明确的行程反馈，用户才能确认操作已经生效。"
BUTTON_AGAIN = "按压按钮需要明确的行程反馈，并且确认操作已经生效。"
NIGHT = "夜间阅读界面宜降低对比并避免纯白大面积，以减轻视觉疲劳。"
NAV = "导航标签应使用用户已经理解的词语，而不是内部项目代号。"
SURVEY = "问卷发放前要先写清受访范围，避免把路人回答当成目标用户。"
NGRAM = "char-ngram-2-3"
BGE = "BAAI/bge-small-zh-v1.5"


def _chunk(
    library_key: str,
    course: str,
    page_id: str,
    chunk_id: str,
    text: str,
    source_path: str,
) -> dict[str, str]:
    return {
        "library_key": library_key,
        "course": course,
        "page_id": page_id,
        "chunk_id": chunk_id,
        "text": text,
        "text_hash": hashlib.sha256(text.encode("utf-8")).hexdigest(),
        "source_path": source_path,
    }


CHUNKS = (
    _chunk(
        "course",
        "design-psychology",
        "page-button",
        "chunk-button",
        BUTTON,
        "mem://synthetic/button.md",
    ),
    _chunk(
        "course",
        "design-psychology",
        "page-button-follow",
        "chunk-button-again",
        BUTTON_AGAIN,
        "mem://synthetic/button-follow.md",
    ),
    _chunk(
        "course",
        "ux-product-innovation",
        "page-night",
        "chunk-night",
        NIGHT,
        "mem://synthetic/night.md",
    ),
    _chunk(
        "course",
        "design-psychology",
        "page-nav",
        "chunk-nav",
        NAV,
        "mem://synthetic/nav.md",
    ),
    _chunk(
        "course",
        "ux-product-innovation",
        "page-survey",
        "chunk-survey",
        SURVEY,
        "mem://synthetic/survey.md",
    ),
    _chunk(
        "proj-synthetic",
        "design-psychology",
        "page-project-copy",
        "chunk-project-button",
        BUTTON,
        "mem://synthetic/project-copy.md",
    ),
)
COURSE_IDS = {item["chunk_id"] for item in CHUNKS if item["library_key"] == "course"}


def _backend_missing() -> bool:
    try:
        import sentence_transformers  # noqa: F401
    except ImportError:
        pass
    else:
        return False
    try:
        import torch  # noqa: F401
        import transformers  # noqa: F401
    except ImportError:
        return True
    return False


def test_button_query_returns_chunk_source_and_score():
    result = search_theory_vectors(CHUNKS, QUERY, 2)
    assert result.encoder in {NGRAM, BGE}
    if _backend_missing():
        assert result.encoder == NGRAM
    ids = [hit.chunk_id for hit in result.hits]
    assert "chunk-project-button" not in ids
    assert set(ids) <= {"chunk-button", "chunk-button-again"}
    assert len(ids) == 2
    assert result.hits[0].score >= result.hits[1].score
    by_id = {item["chunk_id"]: item for item in CHUNKS}
    for hit in result.hits:
        assert set(vars(hit)) == {"chunk_id", "page_id", "source", "score"}
        assert hit.page_id == by_id[hit.chunk_id]["page_id"]
        assert hit.source == by_id[hit.chunk_id]["source_path"]
        assert BUTTON not in str(hit)
    if result.encoder == NGRAM:
        assert ids == ["chunk-button-again", "chunk-button"]
        assert [hit.score for hit in result.hits] == [0.715678, 0.698836]


def test_other_library_and_unrelated_query_are_empty():
    project_only = tuple(item for item in CHUNKS if item["library_key"] != "course")
    hidden = search_theory_vectors(project_only, BUTTON, 3)
    assert hidden.hits == ()
    assert hidden.encoder in {NGRAM, BGE}

    miss = search_theory_vectors(CHUNKS, PLANT, 3)
    assert miss.hits == ()
    assert miss.encoder == hidden.encoder
    assert set(vars(miss)) == {"hits", "encoder"}


def test_limit_does_not_return_the_whole_course():
    one = search_theory_vectors(CHUNKS, QUERY, 1)
    assert len(one.hits) == 1
    assert one.hits[0].chunk_id in {"chunk-button", "chunk-button-again"}
    wide = search_theory_vectors(CHUNKS, QUERY, 20)
    returned = {hit.chunk_id for hit in wide.hits}
    assert returned
    assert returned < COURSE_IDS
    assert "chunk-project-button" not in returned
    assert "chunk-night" not in returned
    assert "chunk-nav" not in returned
    assert "chunk-survey" not in returned


def test_scores_and_encoder_are_reproducible():
    first = search_theory_vectors(CHUNKS, QUERY, 2)
    second = search_theory_vectors(CHUNKS, QUERY, 2)
    assert first.encoder == second.encoder
    assert [(hit.chunk_id, hit.score) for hit in first.hits] == [
        (hit.chunk_id, hit.score) for hit in second.hits
    ]


def test_chunk_identity_is_required():
    broken = dict(CHUNKS[0])
    del broken["text_hash"]
    with pytest.raises(ValueError, match="text_hash"):
        search_theory_vectors((broken,), QUERY, 1)
    forged = dict(CHUNKS[0])
    forged["text_hash"] = "0" * 64
    with pytest.raises(ValueError, match="text_hash"):
        search_theory_vectors((forged,), QUERY, 1)
    nameless = dict(CHUNKS[0])
    del nameless["course"]
    with pytest.raises(ValueError, match="course"):
        search_theory_vectors((nameless,), QUERY, 1)
    with pytest.raises(ValueError, match="query"):
        search_theory_vectors(CHUNKS, "   ", 1)
    with pytest.raises(ValueError, match="limit"):
        search_theory_vectors(CHUNKS, QUERY, 0)
    with pytest.raises(ValueError, match="chunk_id"):
        search_theory_vectors((CHUNKS[0], CHUNKS[0]), QUERY, 1)
