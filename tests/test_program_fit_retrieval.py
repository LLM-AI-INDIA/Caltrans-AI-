"""
TDD suite for src/program_fit_retrieval.py — a dependency-minimal hybrid
(TF-IDF + optional OpenAI-embeddings) retriever for long OCR'd traffic-study text.

Runs fully offline (no API key) via local numpy TF-IDF.

Run:  python -m pytest tests/test_program_fit_retrieval.py -v
"""
import os

import numpy as np

from src import program_fit_retrieval as r


# --- small corpora -----------------------------------------------------------------
CORPUS = [
    "the truck bottleneck at the interchange causes freight delay",
    "zero emission vehicle charging stations",
    "pavement rehabilitation and drainage work",
    "bicycle and pedestrian safety improvements downtown",
    "grade separation reduces rail crossing conflicts",
]


# ============================================================================================
# 1. chunk_text
# ============================================================================================
def test_chunk_text_multiple_chunks_with_overlap():
    text = " ".join(f"word{i}" for i in range(1000))
    chunks = r.chunk_text(text, chunk_size=200, overlap=50)
    assert len(chunks) > 1
    # every chunk is non-empty
    assert all(c.strip() for c in chunks)
    # consecutive chunks share some overlap text
    for a, b in zip(chunks, chunks[1:]):
        tail = a[-50:]
        # some suffix fragment of `a` should appear at the head region of `b`
        shared = any(tok in b[:120] for tok in tail.split())
        assert shared, "consecutive chunks should share overlap text"


def test_chunk_text_deterministic():
    text = " ".join(f"tok{i}" for i in range(400))
    assert r.chunk_text(text, 150, 30) == r.chunk_text(text, 150, 30)


# ============================================================================================
# 2. TF-IDF retrieval (lexical)
# ============================================================================================
def test_tfidf_retrieval_ranks_lexical_match_first():
    ret = r.HybridRetriever(use_embeddings=False)
    ret.build(CORPUS)
    top = ret.retrieve("freight truck delay", k=1)
    assert isinstance(top, list) and len(top) == 1
    assert top[0]["chunk"] == CORPUS[0]
    assert isinstance(top[0]["score"], float)
    assert "source" in top[0]


def test_tfidf_retrieval_deterministic_and_k():
    ret = r.HybridRetriever()
    ret.build(CORPUS, sources=[f"s{i}" for i in range(len(CORPUS))])
    a = ret.retrieve("freight truck delay", k=3)
    b = ret.retrieve("freight truck delay", k=3)
    assert [x["chunk"] for x in a] == [x["chunk"] for x in b]
    assert len(a) == 3
    assert a[0]["source"] == "s0"


# ============================================================================================
# 3. save / load round-trip
# ============================================================================================
def test_save_load_roundtrip(tmp_path):
    ret = r.HybridRetriever()
    ret.build(CORPUS, sources=[f"s{i}" for i in range(len(CORPUS))])
    before = ret.retrieve("pavement drainage", k=2)

    path = tmp_path / "idx.npz"
    ret.save(str(path))
    assert path.exists()

    loaded = r.HybridRetriever.load(str(path))
    after = loaded.retrieve("pavement drainage", k=2)
    assert [x["chunk"] for x in before] == [x["chunk"] for x in after]
    assert [x["source"] for x in before] == [x["source"] for x in after]


# ============================================================================================
# 4. graceful embeddings degrade (no API key)
# ============================================================================================
def test_embeddings_graceful_degrade(monkeypatch):
    for var in ("OPENAI_API_KEY", "GROQ_API_KEY", "DATABRICKS_TOKEN", "DATABRICKS_HOST"):
        monkeypatch.delenv(var, raising=False)
    ret = r.HybridRetriever(use_embeddings=True)
    # must not raise even though embeddings cannot be produced
    ret.build(CORPUS)
    assert ret._emb is None
    top = ret.retrieve("freight truck delay", k=1)
    assert top[0]["chunk"] == CORPUS[0]


# ============================================================================================
# 5. build_or_load_index cache behaviour
# ============================================================================================
def test_build_or_load_index_caches(tmp_path):
    text = " ".join(CORPUS) + " " + " ".join(f"filler{i}" for i in range(500))
    key = "abc123"
    cache_dir = tmp_path / "cache"

    ret1 = r.build_or_load_index(text, str(cache_dir), key, use_embeddings=False)
    idx_path = cache_dir / f"idx_{key}.npz"
    assert idx_path.exists()
    first = ret1.retrieve("freight truck delay", k=2)

    # second call should load from cache and produce identical results
    ret2 = r.build_or_load_index(text, str(cache_dir), key, use_embeddings=False)
    second = ret2.retrieve("freight truck delay", k=2)
    assert [x["chunk"] for x in first] == [x["chunk"] for x in second]
