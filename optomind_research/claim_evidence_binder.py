"""P0: Post-hoc BM25 evidence binding for M2a claims.

LLM generates claim statements only; this module assigns supporting_text_chunk_ids
by BM25-style overlap between claim statement and chunk preview tokens.
Pure stdlib, zero LLM calls, always succeeds.

Called from ClaimDecomposer._llm_decompose() after _parse_llm_claims().
"""

from __future__ import annotations

import math
import re
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from optomind_research.claim_decomposer import Claim

_STOPWORDS = frozenset({
    "the", "a", "an", "of", "in", "to", "and", "or", "for", "with", "that",
    "this", "from", "are", "is", "was", "were", "be", "been", "have", "has",
    "had", "not", "but", "which", "by", "at", "as", "on", "it", "its", "can",
    "may", "also", "than", "their", "they", "these", "those", "such", "into",
    "over", "more", "both", "each", "when", "while", "will", "would", "could",
    "should", "been", "being", "its", "used", "using", "show", "shows",
    "between", "through", "during", "however", "where", "here", "there",
})

_K1 = 1.5
_B = 0.75


def _tokenize(text: str) -> list[str]:
    return [t for t in re.findall(r"[a-zA-Z]{3,}", text.lower()) if t not in _STOPWORDS]


def _build_bm25_corpus(
    chunks: list[dict],
) -> tuple[list[str], list[list[str]], dict[str, int], float]:
    """Return (chunk_ids, doc_token_lists, df_map, avgdl)."""
    ids: list[str] = []
    token_lists: list[list[str]] = []
    for chunk in chunks:
        if not isinstance(chunk, dict):
            continue
        cid = chunk.get("chunk_id", "")
        preview = (chunk.get("text_preview") or "") + " " + (chunk.get("section_path") or "")
        ids.append(cid)
        token_lists.append(_tokenize(preview))

    n = len(token_lists)
    avgdl = sum(len(t) for t in token_lists) / max(1, n)
    df: dict[str, int] = {}
    for tokens in token_lists:
        for t in set(tokens):
            df[t] = df.get(t, 0) + 1
    return ids, token_lists, df, avgdl


def _bm25_score(
    query_tokens: list[str],
    doc_tokens: list[str],
    df: dict[str, int],
    n: int,
    avgdl: float,
) -> float:
    doc_len = len(doc_tokens)
    tf_map: dict[str, int] = {}
    for t in doc_tokens:
        tf_map[t] = tf_map.get(t, 0) + 1
    score = 0.0
    for qt in set(query_tokens):
        tf = tf_map.get(qt, 0)
        if tf == 0:
            continue
        df_t = df.get(qt, 0)
        idf = math.log((n - df_t + 0.5) / (df_t + 0.5) + 1.0)
        norm = tf * (_K1 + 1) / (tf + _K1 * (1 - _B + _B * doc_len / max(1, avgdl)))
        score += idf * norm
    return score


def bind_claims_to_chunks(
    claims: list[Any],
    candidate_text_chunks: list[dict],
    *,
    top_k: int = 3,
    replace_existing: bool = False,
    min_score: float = 0.0,
) -> list[Any]:
    """Fill empty supporting_text_chunk_ids on Claim objects via BM25.

    By default claims that already have supporting_text_chunk_ids are left
    unchanged.  ``replace_existing=True`` is used after a verifier failure so
    opaque IDs suggested by the generator are not trusted blindly.
    """
    if not candidate_text_chunks or not claims:
        return claims

    chunk_ids, token_lists, df, avgdl = _build_bm25_corpus(candidate_text_chunks)
    n = len(chunk_ids)
    if n == 0:
        return claims

    for claim in claims:
        if getattr(claim, "supporting_text_chunk_ids", None) and not replace_existing:
            continue  # already bound

        stmt = getattr(claim, "statement", "") or ""
        q_tokens = _tokenize(stmt)
        if not q_tokens:
            continue

        scored: list[tuple[float, str]] = []
        for cid, doc_tokens in zip(chunk_ids, token_lists):
            if not cid or not doc_tokens:
                continue
            s = _bm25_score(q_tokens, doc_tokens, df, n, avgdl)
            if s > 0:
                scored.append((s, cid))

        scored.sort(reverse=True)
        selected = [(score, cid) for score, cid in scored[:top_k] if score >= min_score]
        claim.supporting_text_chunk_ids = [cid for _, cid in selected]
        if hasattr(claim, "evidence_binding_status"):
            claim.evidence_binding_status = "unverified" if selected else "insufficient"
            claim.evidence_binding_confidence = "low"
            claim.evidence_binding_reason = (
                "Deterministic BM25 fallback selected lexical candidates; entailment was not verified."
                if selected
                else "No candidate chunk met the deterministic lexical threshold."
            )
            claim.saturation_score = min(float(getattr(claim, "saturation_score", 0.0)), 1.0 if selected else 0.5)

    return claims
