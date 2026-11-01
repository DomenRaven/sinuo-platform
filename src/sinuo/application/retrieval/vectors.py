"""In-memory vector recall for course-library chunks.

The caller passes chunks it already holds. This module does not open course
files, does not write an index, and does not call a chat model. Identity is
checked first. Only ``library_key="course"`` is embedded. A miss is an empty
hit list, not the rest of the course.
"""

import hashlib
import math
import unicodedata
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

_ENCODER_BGE = "BAAI/bge-small-zh-v1.5"
_ENCODER_NGRAM = "char-ngram-2-3"
_QUERY_PREFIX = "为这个句子生成表示以用于检索相关文章："
_COURSE = "course"
_MODEL_ROOT = Path("D:/huggingface/hub/models--BAAI--bge-small-zh-v1.5")
_MODEL_REVISION = "4bf3c54884c552e68da7eb27f3e9bdc5a32e32d4"
_KEY_MAX = 64
_ID_MAX = 128
_PATH_MAX = 240
_TEXT_MAX = 8000
_QUERY_MAX = 200
_LIMIT_MIN = 1
_LIMIT_MAX = 20
_MAX_TOKENS = 512
_NGRAM_DIM = 512
# Lecture 11 allows a similarity floor but does not publish a cosine cutoff.
# The character encoder was measured on short synthetic sentences: a shared
# phrase scores about 0.70 and a disjoint sentence stays under 0.10.
_NGRAM_MIN_SCORE = 0.45
_BGE_MIN_SCORE = 0.55

_Encode = Callable[[list[str], bool], list[list[float]]]


@dataclass(frozen=True)
class TheoryVectorHit:
    chunk_id: str
    page_id: str
    source: str
    score: float


@dataclass(frozen=True)
class TheoryVectorResult:
    hits: tuple[TheoryVectorHit, ...]
    encoder: str


@dataclass(frozen=True)
class _Chunk:
    library_key: str
    course: str
    page_id: str
    chunk_id: str
    text: str
    text_hash: str
    source_path: str


@dataclass(frozen=True)
class _Encoder:
    name: str
    min_score: float
    encode: _Encode


_CACHED: _Encoder | None = None


def search_theory_vectors(
    chunks: Sequence[Mapping[str, object]],
    query: str,
    limit: int,
) -> TheoryVectorResult:
    """Return course-chunk ids, sources, and scores for one query.

    Each chunk needs ``library_key``, ``course``, ``page_id``, ``chunk_id``,
    ``text``, ``text_hash``, and ``source_path``. ``text_hash`` is the SHA-256
    hex digest of the UTF-8 text. Rows with any other ``library_key`` are
    dropped before similarity is computed. ``source`` in a hit is that
    ``source_path``. The hit does not include the chunk text.

    The encoder is the local ``BAAI/bge-small-zh-v1.5`` snapshot when its
    library imports. Otherwise it is a reproducible character n-gram vector.
    The encoder name is always on the result. Scores under that encoder's
    floor, and queries with no remaining row, produce an empty hit list.
    """

    checked = _require_query(query)
    bounded = _require_limit(limit)
    rows = _require_chunks(chunks)
    selected = tuple(row for row in rows if row.library_key == _COURSE)
    encoder = _encoder()
    if not selected:
        return TheoryVectorResult((), encoder.name)
    query_vector = encoder.encode([checked], True)[0]
    embedded = encoder.encode([row.text for row in selected], False)
    scored: list[tuple[float, _Chunk]] = []
    for row, vector in zip(selected, embedded, strict=True):
        score = round(_cosine(query_vector, vector), 6)
        if score >= encoder.min_score:
            scored.append((score, row))
    scored.sort(key=lambda item: (-item[0], item[1].chunk_id))
    hits = tuple(
        TheoryVectorHit(row.chunk_id, row.page_id, row.source_path, score)
        for score, row in scored[:bounded]
    )
    return TheoryVectorResult(hits, encoder.name)


def _require_query(query: object) -> str:
    if not isinstance(query, str):
        raise ValueError("query")
    checked = query.strip()
    if checked == "" or len(checked) > _QUERY_MAX:
        raise ValueError("query")
    return checked


def _require_limit(limit: object) -> int:
    if type(limit) is not int or limit < _LIMIT_MIN or limit > _LIMIT_MAX:
        raise ValueError("limit")
    return limit


def _require_chunks(chunks: object) -> tuple[_Chunk, ...]:
    if isinstance(chunks, (str, bytes)) or not isinstance(chunks, Sequence):
        raise ValueError("chunks")
    rows: list[_Chunk] = []
    seen: set[str] = set()
    for item in chunks:
        if not isinstance(item, Mapping):
            raise ValueError("chunks")
        chunk_id = _token(item.get("chunk_id"), "chunk_id", _ID_MAX)
        if chunk_id in seen:
            raise ValueError("chunk_id")
        seen.add(chunk_id)
        text = _body(item.get("text"))
        text_hash = _token(item.get("text_hash"), "text_hash", 64)
        if text_hash != _digest(text):
            raise ValueError("text_hash")
        rows.append(
            _Chunk(
                library_key=_token(item.get("library_key"), "library_key", _KEY_MAX),
                course=_token(item.get("course"), "course", _KEY_MAX),
                page_id=_token(item.get("page_id"), "page_id", _ID_MAX),
                chunk_id=chunk_id,
                text=text,
                text_hash=text_hash,
                source_path=_token(item.get("source_path"), "source_path", _PATH_MAX),
            )
        )
    return tuple(rows)


def _token(value: object, name: str, limit: int) -> str:
    if (
        not isinstance(value, str)
        or value == ""
        or value != value.strip()
        or len(value) > limit
    ):
        raise ValueError(name)
    return value


def _body(value: object) -> str:
    if not isinstance(value, str) or value.strip() == "" or len(value) > _TEXT_MAX:
        raise ValueError("text")
    return value


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _encoder() -> _Encoder:
    global _CACHED
    if _CACHED is None:
        _CACHED = _resolve_encoder()
    return _CACHED


def _resolve_encoder() -> _Encoder:
    snapshot = _snapshot()
    if snapshot is not None:
        encode = _try_sentence_transformers(snapshot)
        if encode is None:
            encode = _try_torch_cls(snapshot)
        if encode is not None:
            return _Encoder(_ENCODER_BGE, _BGE_MIN_SCORE, encode)
    return _Encoder(_ENCODER_NGRAM, _NGRAM_MIN_SCORE, _encode_ngram)


def _snapshot() -> Path | None:
    folder = _MODEL_ROOT / "snapshots" / _MODEL_REVISION
    if (folder / "config.json").is_file() and (folder / "pytorch_model.bin").is_file():
        return folder
    return None


def _try_sentence_transformers(snapshot: Path) -> _Encode | None:
    try:
        from sentence_transformers import SentenceTransformer
    except ImportError:
        return None
    try:
        try:
            model = SentenceTransformer(str(snapshot), device="cpu")
        except TypeError:
            model = SentenceTransformer(str(snapshot))
        model.max_seq_length = _MAX_TOKENS
    except Exception:
        return None

    def encode(texts: list[str], is_query: bool) -> list[list[float]]:
        payload = _with_prefix(texts, is_query)
        _reject_long_tokens(model.tokenizer, payload, "query" if is_query else "text")
        try:
            rows = model.encode(
                payload,
                normalize_embeddings=True,
                show_progress_bar=False,
            )
        except TypeError:
            rows = model.encode(payload, normalize_embeddings=True)
        return [[float(value) for value in row] for row in rows]

    return encode


def _try_torch_cls(snapshot: Path) -> _Encode | None:
    try:
        import torch
        from transformers import AutoConfig, AutoModel, AutoTokenizer
    except ImportError:
        return None
    try:
        tokenizer = AutoTokenizer.from_pretrained(str(snapshot), local_files_only=True)
        config = AutoConfig.from_pretrained(str(snapshot), local_files_only=True)
        model = AutoModel.from_config(config)
        state = torch.load(
            snapshot / "pytorch_model.bin",
            map_location="cpu",
            weights_only=True,
        )
        missing, unexpected = model.load_state_dict(state, strict=False)
        bad_missing = [key for key in missing if not str(key).endswith("position_ids")]
        bad_unexpected = [
            key for key in unexpected if not str(key).endswith("position_ids")
        ]
        if bad_missing or bad_unexpected:
            return None
        model.eval()
    except Exception:
        return None

    def encode(texts: list[str], is_query: bool) -> list[list[float]]:
        payload = _with_prefix(texts, is_query)
        _reject_long_tokens(tokenizer, payload, "query" if is_query else "text")
        batch = tokenizer(
            payload,
            padding=True,
            truncation=False,
            max_length=_MAX_TOKENS,
            return_tensors="pt",
        )
        with torch.no_grad():
            hidden = model(**batch).last_hidden_state[:, 0]
            hidden = torch.nn.functional.normalize(hidden, p=2, dim=1)
        return hidden.detach().cpu().tolist()

    return encode


def _with_prefix(texts: list[str], is_query: bool) -> list[str]:
    if is_query:
        return [_QUERY_PREFIX + text for text in texts]
    return list(texts)


def _reject_long_tokens(tokenizer: object, texts: list[str], name: str) -> None:
    encoded = tokenizer(texts, add_special_tokens=True, truncation=False, padding=False)
    token_ids = encoded["input_ids"]
    if any(len(row) > _MAX_TOKENS for row in token_ids):
        raise ValueError(name)


def _encode_ngram(texts: list[str], is_query: bool) -> list[list[float]]:
    # The query and the chunk stay in one hashed space. No retrieval prefix.
    del is_query
    return [_ngram_vector(text) for text in texts]


def _ngram_vector(text: str) -> list[float]:
    folded = _fold(text)
    counts = [0.0] * _NGRAM_DIM
    grams: list[str] = []
    for width in (2, 3):
        if len(folded) < width:
            if folded:
                grams.append(folded)
            continue
        grams.extend(
            folded[index : index + width] for index in range(len(folded) - width + 1)
        )
    for gram in grams:
        digest = hashlib.sha256(gram.encode("utf-8")).digest()
        bucket = int.from_bytes(digest[:4], "big") % _NGRAM_DIM
        sign = 1.0 if digest[4] % 2 == 0 else -1.0
        counts[bucket] += sign
    norm = math.sqrt(sum(value * value for value in counts))
    if norm == 0.0:
        return counts
    return [value / norm for value in counts]


def _fold(text: str) -> str:
    folded = unicodedata.normalize("NFC", text).casefold()
    kept: list[str] = []
    for char in folded:
        if "\u4e00" <= char <= "\u9fff" or (char.isascii() and char.isalnum()):
            kept.append(char)
    return "".join(kept)


def _cosine(left: Sequence[float], right: Sequence[float]) -> float:
    if len(left) != len(right) or not left:
        raise ValueError("encoder")
    return sum(one * other for one, other in zip(left, right, strict=True))
