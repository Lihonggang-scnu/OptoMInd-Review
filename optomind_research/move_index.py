"""P3: Vector retrieval index for M1 intellectual moves.

DashScope text-embedding-v3 (dim=1024) + FAISS IndexFlatIP.
Vectors are L2-normalised before storage so dot product == cosine similarity.

Build once:
    from optomind_research.move_index import MoveIndex
    idx = MoveIndex()
    idx.build()          # reads enriched library, calls DashScope, saves to disk
    idx.save()

Query at runtime:
    idx = MoveIndex()
    idx.load()
    results = idx.query("how to frame competing constraints in a review", top_k=20)
"""

from __future__ import annotations

import json
import math
import os
import re
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]

DEFAULT_ENRICHED_LIBRARY = (
    PROJECT_ROOT
    / "outputs"
    / "review_example_memory"
    / "final_canonical"
    / "intellectual_moves_enriched_by_category.json"
)
DEFAULT_ACTIVE_LIBRARY = (
    PROJECT_ROOT
    / "outputs"
    / "review_example_memory"
    / "final_canonical"
    / "intellectual_moves_active_by_category.json"
)
DEFAULT_INDEX_DIR = PROJECT_ROOT / "outputs" / "move_vector_index"

EMBEDDING_MODEL = "text-embedding-v3"
EMBEDDING_DIM = 1024
EMBEDDING_BATCH = 10  # DashScope text-embedding-v3 compatible-mode limit
DASHSCOPE_EMBED_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1/embeddings"


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _compact(value: Any, limit: int = 500) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()[:limit]


def _move_to_embed_text(move: Any) -> str:
    """Produce the text that will be embedded for a move.

    Individual fields (move, why_it_matters) lead so each move gets a unique vector.
    Shared category-level fields (transferable_rule, trigger_when) trail as context.
    This prevents Method-B category enrichment from collapsing all category vectors together.
    """
    if not isinstance(move, dict):
        return _compact(move, 800)
    parts = [
        move.get("move", ""),                                                   # unique per move
        move.get("why_it_matters", ""),                                         # unique per move
        move.get("reuse_for_our_review_system", ""),                            # unique per move
        move.get("transferable_rule", ""),                                      # shared (category)
        move.get("trigger_when", ""),                                           # shared (category)
    ]
    return " ".join(_compact(x, 300) for x in parts if x)


def _get_api_key() -> str:
    """Retrieve DashScope API key via the project's config layer."""
    try:
        from config.qwen_config import get_qwen_client_config
        cfg = get_qwen_client_config("standard_model")
        candidates = cfg.get("api_key_candidates") or []
        if candidates:
            return str(candidates[0].get("api_key") or "")
        return str(cfg.get("api_key") or "")
    except Exception:
        return os.environ.get("DASHSCOPE_API_KEY", "") or os.environ.get("QWEN_API_KEY", "")


def _l2_normalize(vec: list[float]) -> list[float]:
    norm = math.sqrt(sum(x * x for x in vec))
    if norm < 1e-10:
        return vec
    return [x / norm for x in vec]


# ---------------------------------------------------------------------------
# DashScope embedding call
# ---------------------------------------------------------------------------

def embed_texts(texts: list[str], api_key: str, *, max_retries: int = 2) -> list[list[float]]:
    """Embed a batch of texts using DashScope text-embedding-v3.

    Returns a list of L2-normalised float vectors (dim=1024).
    Raises RuntimeError on failure.
    """
    body = json.dumps(
        {"model": EMBEDDING_MODEL, "input": texts, "encoding_format": "float"},
        ensure_ascii=False,
    ).encode("utf-8")
    req = urllib.request.Request(
        url=DASHSCOPE_EMBED_URL,
        data=body,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    last_err = ""
    for attempt in range(max_retries + 1):
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            items = sorted(data["data"], key=lambda x: x["index"])
            return [_l2_normalize(item["embedding"]) for item in items]
        except Exception as exc:
            last_err = str(exc)
            if attempt < max_retries:
                time.sleep(1.0 * (attempt + 1))
    raise RuntimeError(f"DashScope embedding failed after {max_retries + 1} attempts: {last_err}")


def embed_texts_batched(
    texts: list[str],
    api_key: str,
    *,
    batch_size: int = EMBEDDING_BATCH,
    progress: bool = False,
) -> list[list[float]]:
    """Embed all texts in batches; returns L2-normalised vectors."""
    all_vecs: list[list[float]] = []
    total = len(texts)
    for start in range(0, total, batch_size):
        batch = texts[start : start + batch_size]
        vecs = embed_texts(batch, api_key)
        all_vecs.extend(vecs)
        if progress:
            done = min(start + batch_size, total)
            print(f"  embedded {done}/{total}", end="\r", flush=True)
    if progress:
        print()
    return all_vecs


# ---------------------------------------------------------------------------
# FAISS index helpers (optional dependency)
# ---------------------------------------------------------------------------

def _faiss_build(vectors: list[list[float]]) -> Any:
    import faiss  # type: ignore
    import numpy as np

    mat = np.array(vectors, dtype="float32")
    index = faiss.IndexFlatIP(EMBEDDING_DIM)
    index.add(mat)
    return index


def _faiss_search(index: Any, query_vec: list[float], top_k: int) -> list[tuple[float, int]]:
    import faiss  # type: ignore
    import numpy as np

    q = np.array([query_vec], dtype="float32")
    scores, ids = index.search(q, top_k)
    return [(float(scores[0][i]), int(ids[0][i])) for i in range(len(ids[0])) if ids[0][i] >= 0]


# ---------------------------------------------------------------------------
# MoveIndex
# ---------------------------------------------------------------------------

class MoveIndex:
    """Vector retrieval index over M1 intellectual moves."""

    def __init__(self, index_dir: Path = DEFAULT_INDEX_DIR) -> None:
        self.index_dir = Path(index_dir)
        self._index: Any = None               # faiss index
        self._metadata: list[dict] = []       # parallel list of move records

    # -- paths ---------------------------------------------------------------

    @property
    def _faiss_path(self) -> Path:
        return self.index_dir / "move_index.faiss"

    @property
    def _meta_path(self) -> Path:
        return self.index_dir / "move_metadata.json"

    def is_built(self) -> bool:
        return self._faiss_path.exists() and self._meta_path.exists()

    # -- build ---------------------------------------------------------------

    def build(
        self,
        library_path: Path | None = None,
        *,
        batch_size: int = EMBEDDING_BATCH,
        progress: bool = True,
    ) -> dict[str, Any]:
        """Embed all moves and build a FAISS index. Returns a report dict."""
        if library_path is None:
            library_path = (
                DEFAULT_ENRICHED_LIBRARY
                if DEFAULT_ENRICHED_LIBRARY.exists()
                else DEFAULT_ACTIVE_LIBRARY
            )
        raw = json.loads(Path(library_path).read_text(encoding="utf-8"))
        library: dict[str, list[dict]] = raw if isinstance(raw, dict) else {}

        records: list[dict] = []
        for cat, moves in library.items():
            for move in moves:
                txt = _move_to_embed_text(move)
                if not txt.strip():
                    continue
                records.append({"category": cat, "embed_text": txt, "move": move})

        if not records:
            return {"status": "error", "reason": "no moves found in library"}

        texts = [r["embed_text"] for r in records]
        api_key = _get_api_key()
        if not api_key:
            return {"status": "error", "reason": "no DashScope API key found"}

        if progress:
            print(f"Embedding {len(texts)} moves in batches of {batch_size}...")
        vectors = embed_texts_batched(texts, api_key, batch_size=batch_size, progress=progress)

        self._index = _faiss_build(vectors)
        self._metadata = [
            {
                "category": r["category"],
                "embed_text": r["embed_text"],
                **{k: v for k, v in r["move"].items() if k != "embed_text"},
            }
            for r in records
        ]
        return {
            "status": "ok",
            "total_moves": len(records),
            "library_path": str(library_path),
        }

    # -- persist -------------------------------------------------------------

    def save(self) -> None:
        import faiss  # type: ignore

        self.index_dir.mkdir(parents=True, exist_ok=True)
        faiss.write_index(self._index, str(self._faiss_path))
        self._meta_path.write_text(
            json.dumps(self._metadata, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    def load(self) -> None:
        import faiss  # type: ignore

        self._index = faiss.read_index(str(self._faiss_path))
        self._metadata = json.loads(self._meta_path.read_text(encoding="utf-8"))

    # -- query ---------------------------------------------------------------

    def query(self, text: str, top_k: int = 20) -> list[dict[str, Any]]:
        """Return top-k moves most similar to text. Each result includes a 'retrieval_score' key.

        Deduplicates by transferable_rule within each category so Method-B category-level
        enrichment (where all moves share one rule) does not flood results with duplicates.
        """
        if self._index is None or not self._metadata:
            raise RuntimeError("Index not loaded. Call load() or build() first.")
        api_key = _get_api_key()
        vecs = embed_texts([text], api_key)
        # Fetch many candidates to survive deduplication — Method-B gives all moves in a
        # category near-identical vectors, so we need to scan well beyond top_k to find diversity.
        fetch_k = max(top_k * 20, 200)
        hits = _faiss_search(self._index, vecs[0], min(fetch_k, len(self._metadata)))
        seen_rule_by_cat: dict[tuple[str, str], bool] = {}
        results = []
        for score, idx in hits:
            if len(results) >= top_k:
                break
            if idx < 0 or idx >= len(self._metadata):
                continue
            rec = dict(self._metadata[idx])
            cat = rec.get("category", "")
            rule_key = _compact(rec.get("transferable_rule", ""), 80)
            dedup_key = (cat, rule_key)
            if dedup_key in seen_rule_by_cat:
                continue
            seen_rule_by_cat[dedup_key] = True
            rec["retrieval_score"] = round(score, 4)
            results.append(rec)
        return results


# ---------------------------------------------------------------------------
# BM25MoveIndex — pure-stdlib fallback (no FAISS, no DashScope)
# ---------------------------------------------------------------------------

class BM25MoveIndex:
    """BM25 move index built from the in-memory library dict.

    Drop-in query interface for MoveIndex when FAISS is unavailable or the
    vector index has not been built yet. Zero network calls, zero disk I/O.
    """

    _K1 = 1.5
    _B = 0.75
    _STOPWORDS = frozenset({
        "the", "a", "an", "of", "in", "to", "and", "or", "for", "with", "that",
        "this", "from", "are", "is", "was", "were", "be", "been", "have", "has",
        "had", "not", "but", "which", "by", "at", "as", "on", "it", "its", "can",
        "may", "also", "than", "their", "they", "these", "those", "such", "into",
    })

    def __init__(self) -> None:
        self._records: list[dict] = []
        self._token_lists: list[list[str]] = []
        self._df: dict[str, int] = {}
        self._avgdl: float = 0.0
        self._built: bool = False

    def _tokenize(self, text: str) -> list[str]:
        return [t for t in re.findall(r"[a-zA-Z]{3,}", text.lower()) if t not in self._STOPWORDS]

    def build(self, library: dict[str, list[dict]]) -> None:
        """Build the BM25 index from a category → moves library dict."""
        self._records = []
        self._token_lists = []
        for cat, moves in (library or {}).items():
            for move in (moves or []):
                if not isinstance(move, dict):
                    continue
                text = _move_to_embed_text(move)
                self._records.append({"category": cat, **move})
                self._token_lists.append(self._tokenize(text))
        n = len(self._token_lists)
        self._avgdl = sum(len(t) for t in self._token_lists) / max(1, n)
        df: dict[str, int] = {}
        for tokens in self._token_lists:
            for t in set(tokens):
                df[t] = df.get(t, 0) + 1
        self._df = df
        self._built = True

    def is_built(self) -> bool:
        return self._built

    def query(self, text: str, top_k: int = 20) -> list[dict[str, Any]]:
        """Return top-k moves ranked by BM25 score against text."""
        if not self._built:
            raise RuntimeError("BM25MoveIndex not built. Call build() first.")
        q_tokens = self._tokenize(text)
        if not q_tokens:
            return []
        n = len(self._records)
        scores: list[float] = []
        for doc_tokens in self._token_lists:
            doc_len = len(doc_tokens)
            tf_map: dict[str, int] = {}
            for t in doc_tokens:
                tf_map[t] = tf_map.get(t, 0) + 1
            score = 0.0
            for qt in set(q_tokens):
                tf = tf_map.get(qt, 0)
                if tf == 0:
                    continue
                df_t = self._df.get(qt, 0)
                idf = math.log((n - df_t + 0.5) / (df_t + 0.5) + 1.0)
                norm_tf = tf * (self._K1 + 1) / (
                    tf + self._K1 * (1 - self._B + self._B * doc_len / max(1, self._avgdl))
                )
                score += idf * norm_tf
            scores.append(score)
        ranked = sorted(range(n), key=lambda i: scores[i], reverse=True)
        results = []
        for i in ranked[:top_k]:
            if scores[i] <= 0:
                break
            rec = dict(self._records[i])
            rec["retrieval_score"] = round(scores[i], 4)
            results.append(rec)
        return results
