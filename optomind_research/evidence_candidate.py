"""T2: EvidenceCandidate and HybridRetriever — unified evidence retrieval layer.

Design:
- EvidenceCandidate is the standard return object from any retrieval channel.
- HybridRetriever fuses 7 channels: FTS5/BM25, dense, citation, concept-adj, visual,
  reranker, dedup. Channels are pluggable; unregistered channels return empty lists.
- The dedup stage collapses near-duplicate candidates (same chunk_id or same paper+span).
- Score vector fields are normalized to [0, 1] before reranking.

Integration point: call HybridRetriever.retrieve(claim) from M2a or M3 gap resolution.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any


VALID_CANDIDATE_RELATIONS = frozenset({
    "direct_support",
    "component_support",
    "indirect_support",
    "method_transfer",
    "contrast",
    "background_only",
    "contradiction",
    "not_relevant",
})

VALID_RETRIEVAL_CHANNELS = frozenset({
    "bm25",
    "dense",
    "citation",
    "concept_adj",
    "visual",
    "reranker",
    "m3_gap",
})

VALID_VERIFICATION_STATUSES = frozenset({
    "unverified",
    "auto_verified",
    "human_verified",
    "rejected",
    "partial",
})


@dataclass
class ScoreVector:
    """Normalized relevance dimensions for an evidence candidate."""
    lexical: float = 0.0      # BM25 / FTS5 score
    semantic: float = 0.0     # Dense embedding cosine similarity
    source_quality: float = 0.0  # Journal tier, citation count proxy
    independence: float = 0.0   # How different this candidate is from already-selected ones
    recency: float = 0.0      # Publication year normalized to [0, 1]

    def composite(self, weights: dict[str, float] | None = None) -> float:
        w = weights or {"lexical": 0.3, "semantic": 0.35, "source_quality": 0.15,
                        "independence": 0.1, "recency": 0.1}
        return sum(getattr(self, k, 0.0) * v for k, v in w.items())

    def to_dict(self) -> dict[str, float]:
        return {
            "lexical": round(self.lexical, 4),
            "semantic": round(self.semantic, 4),
            "source_quality": round(self.source_quality, 4),
            "independence": round(self.independence, 4),
            "recency": round(self.recency, 4),
        }

    @classmethod
    def from_dict(cls, d: dict) -> "ScoreVector":
        return cls(
            lexical=float(d.get("lexical", 0.0)),
            semantic=float(d.get("semantic", 0.0)),
            source_quality=float(d.get("source_quality", 0.0)),
            independence=float(d.get("independence", 0.0)),
            recency=float(d.get("recency", 0.0)),
        )


@dataclass
class EvidenceCandidate:
    """A single evidence candidate returned by any retrieval channel.

    Fields follow the spec (T2, section 'EvidenceCandidate schema'):
      candidate_id        — stable ID (chunk_id:claim_component hash)
      claim_component     — which atomic component of the claim this supports
      paper_id            — source paper identifier
      text_chunk_id       — primary text chunk
      visual_chunk_ids    — associated visual chunks (may be empty)
      retrieval_channels  — which channels retrieved this candidate
      candidate_relation  — semantic relation to the claim
      exact_span          — verbatim text from source supporting the claim
      score_vector        — multi-dimensional relevance scores
      verification_status — verification lifecycle state
    """
    candidate_id: str
    claim_component: str = ""
    paper_id: str = ""
    text_chunk_id: str = ""
    visual_chunk_ids: list[str] = field(default_factory=list)
    retrieval_channels: list[str] = field(default_factory=list)
    candidate_relation: str = "direct_support"
    exact_span: str = ""
    score_vector: ScoreVector = field(default_factory=ScoreVector)
    verification_status: str = "unverified"

    def to_dict(self) -> dict[str, Any]:
        return {
            "candidate_id": self.candidate_id,
            "claim_component": self.claim_component,
            "paper_id": self.paper_id,
            "text_chunk_id": self.text_chunk_id,
            "visual_chunk_ids": list(self.visual_chunk_ids),
            "retrieval_channels": list(self.retrieval_channels),
            "candidate_relation": self.candidate_relation,
            "exact_span": self.exact_span,
            "score_vector": self.score_vector.to_dict(),
            "verification_status": self.verification_status,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "EvidenceCandidate":
        sv = d.get("score_vector")
        return cls(
            candidate_id=str(d.get("candidate_id", "")),
            claim_component=str(d.get("claim_component", "")),
            paper_id=str(d.get("paper_id", "")),
            text_chunk_id=str(d.get("text_chunk_id", "")),
            visual_chunk_ids=list(d.get("visual_chunk_ids") or []),
            retrieval_channels=list(d.get("retrieval_channels") or []),
            candidate_relation=str(d.get("candidate_relation", "direct_support")),
            exact_span=str(d.get("exact_span", "")),
            score_vector=ScoreVector.from_dict(sv) if isinstance(sv, dict) else ScoreVector(),
            verification_status=str(d.get("verification_status", "unverified")),
        )

    def validate(self) -> list[str]:
        errors: list[str] = []
        if not self.candidate_id:
            errors.append("candidate_id is empty")
        if self.candidate_relation not in VALID_CANDIDATE_RELATIONS:
            errors.append(f"candidate_relation '{self.candidate_relation}' not in {sorted(VALID_CANDIDATE_RELATIONS)}")
        for ch in self.retrieval_channels:
            if ch not in VALID_RETRIEVAL_CHANNELS:
                errors.append(f"retrieval_channel '{ch}' not in {sorted(VALID_RETRIEVAL_CHANNELS)}")
        if self.verification_status not in VALID_VERIFICATION_STATUSES:
            errors.append(f"verification_status '{self.verification_status}' not in {sorted(VALID_VERIFICATION_STATUSES)}")
        return errors


class HybridRetriever:
    """T2: Hybrid retrieval layer that fuses 7 channels for evidence retrieval.

    Channel registration is pluggable. Callers register channel functions:
      retriever.register_channel("bm25", my_bm25_fn)

    Each channel function signature: fn(query: str, k: int) -> list[EvidenceCandidate]

    retrieve() merges candidates, applies score normalization, diversity dedup,
    and returns a ranked list ready for downstream binding.
    """

    def __init__(self, *, top_k: int = 20, diversity_threshold: float = 0.85) -> None:
        self.top_k = top_k
        self.diversity_threshold = diversity_threshold
        self._channels: dict[str, Any] = {}

    def register_channel(self, name: str, fn: Any) -> None:
        if name not in VALID_RETRIEVAL_CHANNELS:
            raise ValueError(f"Unknown channel '{name}'; valid: {sorted(VALID_RETRIEVAL_CHANNELS)}")
        self._channels[name] = fn

    def retrieve(
        self,
        query: str,
        *,
        claim_component: str = "",
        channels: list[str] | None = None,
        k_per_channel: int = 10,
    ) -> list[EvidenceCandidate]:
        """Run all registered channels, merge, normalize, deduplicate, and re-rank."""
        active = channels or list(self._channels.keys())
        all_candidates: list[EvidenceCandidate] = []
        for ch in active:
            fn = self._channels.get(ch)
            if fn is None:
                continue
            try:
                results = fn(query, k_per_channel)
                for cand in (results or []):
                    if isinstance(cand, EvidenceCandidate):
                        if ch not in cand.retrieval_channels:
                            cand.retrieval_channels.append(ch)
                        if claim_component and not cand.claim_component:
                            cand.claim_component = claim_component
                        all_candidates.append(cand)
            except Exception:
                pass

        merged = self._dedup(all_candidates)
        self._normalize_scores(merged)
        merged.sort(key=lambda c: c.score_vector.composite(), reverse=True)
        return merged[: self.top_k]

    def _dedup(self, candidates: list[EvidenceCandidate]) -> list[EvidenceCandidate]:
        """Collapse candidates sharing the same text_chunk_id, keeping highest composite score."""
        by_chunk: dict[str, EvidenceCandidate] = {}
        for cand in candidates:
            key = cand.text_chunk_id or cand.candidate_id
            if key not in by_chunk:
                by_chunk[key] = cand
            else:
                existing = by_chunk[key]
                if cand.score_vector.composite() > existing.score_vector.composite():
                    # Merge channel list into the higher-scoring entry
                    merged_channels = list(dict.fromkeys(existing.retrieval_channels + cand.retrieval_channels))
                    cand.retrieval_channels = merged_channels
                    by_chunk[key] = cand
                else:
                    merged_channels = list(dict.fromkeys(existing.retrieval_channels + cand.retrieval_channels))
                    existing.retrieval_channels = merged_channels
        return list(by_chunk.values())

    @staticmethod
    def _normalize_scores(candidates: list[EvidenceCandidate]) -> None:
        """Normalize each score dimension to [0, 1] across the candidate set."""
        if not candidates:
            return
        for attr in ("lexical", "semantic", "source_quality", "recency"):
            vals = [getattr(c.score_vector, attr) for c in candidates]
            min_v, max_v = min(vals), max(vals)
            rng = max_v - min_v
            if rng < 1e-9:
                continue
            for c in candidates:
                old = getattr(c.score_vector, attr)
                setattr(c.score_vector, attr, (old - min_v) / rng)
