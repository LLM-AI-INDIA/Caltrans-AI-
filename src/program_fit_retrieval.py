"""
Lightweight, dependency-minimal hybrid vector store for long OCR'd traffic-study text.

Goal: let the Program Fit scorer retrieve only rubric-relevant chunks instead of
dumping hundreds of pages into a prompt.

Design constraints:
  * Works fully OFFLINE (no API key) for CI via a pure-numpy TF-IDF index.
  * OPTIONAL upgrade path to OpenAI-style embeddings when a client/key is available;
    if embedding fails for ANY reason we degrade gracefully to TF-IDF (never raise).
  * Allowed libs only: numpy + stdlib. No faiss / chroma / sklearn.

Lexical + semantic fusion (when embeddings are present) uses Reciprocal Rank Fusion
(RRF): each backend ranks the chunks, and the fused score is the sum of
1 / (RRF_K + rank) across backends. This is robust to the two score distributions
having very different scales.
"""
from __future__ import annotations

import json
import math
import os
import re

import numpy as np

_TOKEN_RE = re.compile(r"\w+")
_RRF_K = 60  # standard reciprocal-rank-fusion constant


# ------------------------------------------------------------------------------------
# Chunking
# ------------------------------------------------------------------------------------
def chunk_text(text, chunk_size=800, overlap=150):
    """Split `text` on whitespace into ~`chunk_size`-char chunks with `overlap`-char
    overlap between consecutive chunks. Empty chunks are dropped. Deterministic.
    """
    if not text:
        return []
    words = text.split()
    if not words:
        return []
    if overlap >= chunk_size:
        overlap = max(0, chunk_size // 2)

    chunks = []
    cur = []
    cur_len = 0
    for word in words:
        # +1 accounts for the joining space
        add_len = len(word) + (1 if cur else 0)
        if cur and cur_len + add_len > chunk_size:
            chunk = " ".join(cur)
            chunks.append(chunk)
            # start next chunk from an overlapping tail (by characters)
            if overlap > 0:
                tail_words = []
                tail_len = 0
                for w in reversed(cur):
                    wl = len(w) + (1 if tail_words else 0)
                    if tail_len + wl > overlap:
                        break
                    tail_words.insert(0, w)
                    tail_len += wl
                cur = tail_words
                cur_len = tail_len
            else:
                cur = []
                cur_len = 0
            # place current word after the overlap tail
            add_len = len(word) + (1 if cur else 0)
        cur.append(word)
        cur_len += add_len

    if cur:
        chunks.append(" ".join(cur))

    return [c for c in chunks if c.strip()]


# ------------------------------------------------------------------------------------
# TF-IDF helpers (pure numpy)
# ------------------------------------------------------------------------------------
def _tokenize(text):
    return _TOKEN_RE.findall(text.lower())


def _l2_normalize(mat):
    norms = np.linalg.norm(mat, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    return mat / norms


# ------------------------------------------------------------------------------------
# HybridRetriever
# ------------------------------------------------------------------------------------
class HybridRetriever:
    def __init__(self, use_embeddings=False, model_name="text-embedding-3-small"):
        self.use_embeddings = use_embeddings
        self.model_name = model_name
        self.chunks = []
        self.sources = None
        self._vocab = {}          # token -> column index
        self._idf = None          # numpy vector, len(vocab)
        self._tfidf = None        # numpy ndarray (N, V), L2-normalized rows
        self._emb = None          # numpy ndarray (N, D) or None

    # -- build -----------------------------------------------------------------------
    def build(self, chunks, sources=None):
        self.chunks = list(chunks)
        self.sources = list(sources) if sources is not None else None

        self._build_tfidf(self.chunks)

        self._emb = None
        if self.use_embeddings:
            self._try_build_embeddings(self.chunks)
        return self

    def _build_tfidf(self, chunks):
        # vocabulary
        tokenized = [_tokenize(c) for c in chunks]
        vocab = {}
        for toks in tokenized:
            for t in toks:
                if t not in vocab:
                    vocab[t] = len(vocab)
        self._vocab = vocab
        V = len(vocab)
        N = len(chunks)

        tf = np.zeros((N, V), dtype=np.float64)
        for i, toks in enumerate(tokenized):
            for t in toks:
                tf[i, vocab[t]] += 1.0

        # log tf
        with np.errstate(divide="ignore"):
            log_tf = np.where(tf > 0, 1.0 + np.log(tf), 0.0)

        # idf = log(N / df) + 1
        df = np.count_nonzero(tf, axis=0)
        df_safe = np.where(df == 0, 1, df)
        idf = np.log(N / df_safe) + 1.0
        self._idf = idf

        tfidf = log_tf * idf
        self._tfidf = _l2_normalize(tfidf)

    def _try_build_embeddings(self, chunks):
        """Attempt to embed chunks; on ANY failure fall back to TF-IDF only."""
        try:
            from src.project_delivery_evaluator import _get_client

            client = _get_client(self.model_name)
            vectors = []
            batch_size = 64
            for start in range(0, len(chunks), batch_size):
                batch = chunks[start:start + batch_size]
                resp = client.embeddings.create(model=self.model_name, input=batch)
                vectors.extend([d.embedding for d in resp.data])
            emb = np.asarray(vectors, dtype=np.float64)
            if emb.ndim != 2 or emb.shape[0] != len(chunks):
                raise ValueError("unexpected embedding shape")
            self._emb = _l2_normalize(emb)
        except Exception:
            self._emb = None

    # -- query projection ------------------------------------------------------------
    def _project_query_tfidf(self, query):
        V = len(self._vocab)
        vec = np.zeros(V, dtype=np.float64)
        toks = _tokenize(query)
        if not toks or V == 0:
            return vec
        counts = {}
        for t in toks:
            if t in self._vocab:
                counts[t] = counts.get(t, 0) + 1
        for t, c in counts.items():
            j = self._vocab[t]
            vec[j] = (1.0 + math.log(c)) * self._idf[j]
        norm = np.linalg.norm(vec)
        if norm > 0:
            vec = vec / norm
        return vec

    def _embed_query(self, query):
        try:
            from src.project_delivery_evaluator import _get_client

            client = _get_client(self.model_name)
            resp = client.embeddings.create(model=self.model_name, input=[query])
            vec = np.asarray(resp.data[0].embedding, dtype=np.float64)
            norm = np.linalg.norm(vec)
            if norm > 0:
                vec = vec / norm
            return vec
        except Exception:
            return None

    # -- retrieve --------------------------------------------------------------------
    def retrieve(self, query, k=5):
        n = len(self.chunks)
        if n == 0:
            return []

        # A reloaded index whose vocab was empty (e.g. token-free chunks) has _tfidf=None; fall
        # back to returning the chunks in order rather than crashing on a None matmul.
        if self._tfidf is None or getattr(self._tfidf, "size", 0) == 0:
            return [{"chunk": self.chunks[i], "score": 0.0,
                     "source": self.sources[i] if self.sources is not None else None}
                    for i in range(min(n, max(0, k)))]

        q_tfidf = self._project_query_tfidf(query)
        tfidf_scores = self._tfidf @ q_tfidf  # cosine (rows already normalized)

        fused = None
        if self._emb is not None:
            q_emb = self._embed_query(query)
            if q_emb is not None and q_emb.shape[0] == self._emb.shape[1]:
                emb_scores = self._emb @ q_emb
                fused = self._rrf_fuse([tfidf_scores, emb_scores])

        final = fused if fused is not None else tfidf_scores

        # stable top-k ordering: sort by (-score, index)
        order = sorted(range(n), key=lambda i: (-float(final[i]), i))
        top = order[: max(0, k)]

        results = []
        for i in top:
            results.append({
                "chunk": self.chunks[i],
                "score": float(final[i]),
                "source": self.sources[i] if self.sources is not None else None,
            })
        return results

    @staticmethod
    def _rrf_fuse(score_arrays):
        """Reciprocal Rank Fusion. Each array ranks chunks (higher = better);
        fused score = sum over backends of 1 / (_RRF_K + rank)."""
        n = len(score_arrays[0])
        fused = np.zeros(n, dtype=np.float64)
        for scores in score_arrays:
            # rank 0 = best. Stable: ties broken by index.
            order = sorted(range(n), key=lambda i: (-float(scores[i]), i))
            for rank, idx in enumerate(order):
                fused[idx] += 1.0 / (_RRF_K + rank + 1)
        return fused

    # -- persistence -----------------------------------------------------------------
    def save(self, path):
        meta = {
            "use_embeddings": self.use_embeddings,
            "model_name": self.model_name,
            "chunks": self.chunks,
            "sources": self.sources,
            "vocab": self._vocab,
            "has_emb": self._emb is not None,
        }
        arrays = {
            "meta": np.array(json.dumps(meta)),
            "idf": self._idf if self._idf is not None else np.zeros(0),
            "tfidf": self._tfidf if self._tfidf is not None else np.zeros((0, 0)),
        }
        if self._emb is not None:
            arrays["emb"] = self._emb
        np.savez(path, **arrays)

    @classmethod
    def load(cls, path):
        # np.savez appends .npz if missing; tolerate both spellings.
        if not os.path.exists(path) and os.path.exists(path + ".npz"):
            path = path + ".npz"
        with np.load(path, allow_pickle=True) as data:
            meta = json.loads(str(data["meta"].item()))
            obj = cls(
                use_embeddings=meta.get("use_embeddings", False),
                model_name=meta.get("model_name", "text-embedding-3-small"),
            )
            obj.chunks = list(meta.get("chunks", []))
            obj.sources = meta.get("sources", None)
            if obj.sources is not None:
                obj.sources = list(obj.sources)
            obj._vocab = {k: int(v) for k, v in meta.get("vocab", {}).items()}
            idf = data["idf"]
            obj._idf = idf if idf.size else None
            tfidf = data["tfidf"]
            obj._tfidf = tfidf if tfidf.size else None
            if meta.get("has_emb") and "emb" in data:
                obj._emb = data["emb"]
            else:
                obj._emb = None
        return obj


# ------------------------------------------------------------------------------------
# build_or_load_index
# ------------------------------------------------------------------------------------
def build_or_load_index(text, cache_dir, key, use_embeddings=False):
    """Chunk `text`, build a HybridRetriever, and persist it to
    `<cache_dir>/idx_<key>.npz`. If that path already exists, load and return it
    instead of rebuilding. `key` is caller-supplied (e.g. a file hash)."""
    os.makedirs(cache_dir, exist_ok=True)
    path = os.path.join(cache_dir, f"idx_{key}.npz")

    if os.path.exists(path):
        return HybridRetriever.load(path)

    chunks = chunk_text(text)
    retriever = HybridRetriever(use_embeddings=use_embeddings)
    retriever.build(chunks)
    retriever.save(path)
    # np.savez writes exactly `path` since it already ends in .npz
    if not os.path.exists(path) and os.path.exists(path + ".npz"):
        os.rename(path + ".npz", path)
    return retriever
