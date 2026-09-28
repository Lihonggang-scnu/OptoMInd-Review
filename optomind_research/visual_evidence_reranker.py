"""M4c — Visual Evidence Reranker.

Upgrades M4b lexical candidates from "relevant" to "best candidate for this claim"
via rule-based scoring (mock) or VL-LLM judgment (real).

Safety contract:
  - evidence_mode != "vision_image_text" → support_strength ≤ medium,
    best_use ≤ supporting_figure, needs_human_review = True,
    risk_or_caveat says "not visually verified".
  - LLM failure → support_strength = "reject", fit_score = 0.0, failure_reason set.
  - No result silently claims main_figure / strong without confirmed image inspection.

Concurrency: real-LLM mode uses ThreadPoolExecutor (configurable workers, default 4).
Cache: in-memory + optional JSONL file; key = SHA256(claim_id|chunk_id|tier|prompt_version).
"""
from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from llm.json_guard import extract_json_from_text

# ---------------------------------------------------------------------------
# Schema constants
# ---------------------------------------------------------------------------

PROMPT_VERSION = "v6_entity_scope_retry_noncoverage"  # bump when protocol changes

VALID_SUPPORT_STRENGTHS = frozenset({"strong", "medium", "weak", "decorative", "reject"})
VALID_BEST_USES = frozenset({"main_figure", "supporting_figure", "background", "reject"})
VALID_DIRECTNESS = frozenset({"direct", "partial", "contextual", "unrelated"})
VALID_EVIDENCE_MODES = frozenset({
    "vision_image_text",
    "text_only",
    "text_only_image_unavailable",
    "text_only_client_lacks_vision_support",
})

_TYPE_TO_ASPECTS: dict[str, str] = {
    "mechanism_anchor":        "physical mechanism / underlying principle",
    "taxonomy_or_roadmap":     "classification landscape / research timeline",
    "method_or_workflow":      "experimental procedure / fabrication method",
    "quantitative_comparison": "performance metrics / numerical benchmarks",
    "trend_or_parameter_map":  "parameter dependence / trend over conditions",
    "representative_example":  "real-world demonstration / morphology evidence",
    "anomaly_or_limitation":   "deviation / failure case / limitation",
    "synthesis_overview":      "integrated summary / cross-study synthesis",
}

_EVIDENCE_TYPE_TO_STRONG_TYPES: dict[str, frozenset[str]] = {
    "mechanism":   frozenset({"mechanism_anchor", "method_or_workflow", "taxonomy_or_roadmap"}),
    "measurement": frozenset({"quantitative_comparison", "trend_or_parameter_map", "representative_example"}),
    "comparison":  frozenset({"quantitative_comparison", "trend_or_parameter_map", "anomaly_or_limitation"}),
    "application": frozenset({"representative_example", "method_or_workflow", "synthesis_overview"}),
}

_STOPWORDS: frozenset[str] = frozenset({
    "the", "and", "for", "this", "that", "with", "from", "are", "was", "has",
    "have", "its", "can", "may", "but", "not", "also", "all", "than", "been",
    "more", "such", "these", "their", "they", "both", "some", "well", "any",
    "our", "one", "two", "fig", "figure", "table", "above", "below", "shown",
    "show", "used", "using", "where", "when", "which", "were", "will", "into",
    "each", "other", "while", "here", "thus", "then", "result", "results",
    "study", "analysis", "value", "sample", "method", "data", "use", "due",
    "per", "via", "see", "high", "low", "large", "small",
})


# ---------------------------------------------------------------------------
# Output dataclass
# ---------------------------------------------------------------------------

@dataclass
class RerankResult:
    chunk_id: str
    fit_score: float                   # 0.0–5.0
    support_strength: str              # strong|medium|weak|decorative|reject
    best_use: str                      # main_figure|supporting_figure|background|reject
    supported_claim_aspect: str
    why_this_visual: str
    risk_or_caveat: str
    recommended_caption_sentence: str
    source: str                        # provided_by_blueprint | auto_recommended_from_kb
    needs_human_review: bool
    evidence_mode: str                 # vision_image_text | text_only | …
    directness: str = "contextual"     # direct|partial|contextual|unrelated
    supported_claim_components: list[str] | None = None
    unsupported_claim_components: list[str] | None = None
    provable_claim_part: str = ""      # the exact portion this visual can prove
    failure_reason: str = ""           # non-empty when LLM/input validation failed
    entity_alignment: str = "unclear"  # exact|compatible|unclear|mismatch
    entity_mismatch_reason: str = ""
    vision_attempt_count: int = 0
    vision_model_tier_used: str = ""
    recovery_action: str = ""

    def to_dict(self) -> dict[str, Any]:
        d = {
            "chunk_id": self.chunk_id,
            "fit_score": self.fit_score,
            "support_strength": self.support_strength,
            "best_use": self.best_use,
            "supported_claim_aspect": self.supported_claim_aspect,
            "why_this_visual": self.why_this_visual,
            "risk_or_caveat": self.risk_or_caveat,
            "recommended_caption_sentence": self.recommended_caption_sentence,
            "source": self.source,
            "needs_human_review": self.needs_human_review,
            "evidence_mode": self.evidence_mode,
            "directness": self.directness,
            "supported_claim_components": list(self.supported_claim_components or []),
            "unsupported_claim_components": list(self.unsupported_claim_components or []),
            "provable_claim_part": self.provable_claim_part,
            "entity_alignment": self.entity_alignment,
            "entity_mismatch_reason": self.entity_mismatch_reason,
            "vision_attempt_count": self.vision_attempt_count,
            "vision_model_tier_used": self.vision_model_tier_used,
            "recovery_action": self.recovery_action,
        }
        if self.failure_reason:
            d["failure_reason"] = self.failure_reason
        return d


# ---------------------------------------------------------------------------
# Reranker
# ---------------------------------------------------------------------------

class VisualEvidenceReranker:
    """Rule-based (mock) or VL-LLM reranker for visual evidence candidates."""

    def __init__(
        self,
        real_llm: bool = False,
        model_tier: str = "vision_model",
        workers: int = 4,
        cache_path: Path | str | None = None,
    ) -> None:
        self.real_llm = real_llm
        self.model_tier = model_tier
        self.workers = max(1, workers)
        self.cache_path = Path(cache_path) if cache_path else None
        self._cache: dict[str, dict] = {}
        if self.cache_path:
            # The reranker is frequently invoked from a fresh review output
            # directory.  Create the cache parent eagerly so successful model
            # calls are not silently lost and charged again on resume.
            self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        if self.cache_path and self.cache_path.exists():
            self._load_cache()

    # ------------------------------------------------------------------
    # Cache
    # ------------------------------------------------------------------

    @staticmethod
    def _cache_key(claim_id: str, chunk_id: str, model_tier: str) -> str:
        raw = f"{claim_id}|{chunk_id}|{model_tier}|{PROMPT_VERSION}"
        return hashlib.sha256(raw.encode()).hexdigest()[:16]

    def _load_cache(self) -> None:
        try:
            for line in self.cache_path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                entry = json.loads(line)
                key = entry.get("_key", "")
                if key:
                    self._cache[key] = entry
        except Exception:
            pass

    def _save_to_cache(self, key: str, result: RerankResult) -> None:
        entry = result.to_dict()
        entry["_key"] = key
        self._cache[key] = entry
        if self.cache_path:
            try:
                self.cache_path.parent.mkdir(parents=True, exist_ok=True)
                with open(self.cache_path, "a", encoding="utf-8") as f:
                    f.write(json.dumps(self._cache[key], ensure_ascii=False) + "\n")
            except Exception:
                pass

    def _lookup_cache(self, key: str) -> RerankResult | None:
        entry = self._cache.get(key)
        if entry is None:
            return None
        try:
            result = RerankResult(
                chunk_id=entry["chunk_id"],
                fit_score=float(entry["fit_score"]),
                support_strength=entry["support_strength"],
                best_use=entry["best_use"],
                supported_claim_aspect=entry.get("supported_claim_aspect", ""),
                why_this_visual=entry.get("why_this_visual", ""),
                risk_or_caveat=entry.get("risk_or_caveat", ""),
                recommended_caption_sentence=entry.get("recommended_caption_sentence", ""),
                source=entry.get("source", ""),
                needs_human_review=bool(entry.get("needs_human_review", True)),
                evidence_mode=entry.get("evidence_mode", "text_only"),
                directness=entry.get("directness", "contextual"),
                supported_claim_components=list(entry.get("supported_claim_components") or []),
                unsupported_claim_components=list(entry.get("unsupported_claim_components") or []),
                provable_claim_part=entry.get("provable_claim_part", ""),
                failure_reason=entry.get("failure_reason", ""),
                entity_alignment=entry.get("entity_alignment", "unclear"),
                entity_mismatch_reason=entry.get("entity_mismatch_reason", ""),
                vision_attempt_count=int(entry.get("vision_attempt_count") or 0),
                vision_model_tier_used=str(entry.get("vision_model_tier_used") or ""),
                recovery_action=str(entry.get("recovery_action") or ""),
            )
            # Re-apply current deterministic safety rules even to a cached
            # model judgment.  Cache persistence must never freeze an older,
            # weaker promotion policy into future runs.
            return self._calibrate_result(result)
        except Exception:
            return None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def rerank(
        self,
        candidates: list[dict],
        section: dict,
        claim: dict,
        chunk_index: dict[str, dict],
        retrieval_index_by_id: dict[str, dict] | None = None,
        max_items: int | None = None,
    ) -> list[RerankResult]:
        """Score and rank visual candidates for a specific claim."""
        if max_items is not None:
            candidates = candidates[:max_items]

        if not self.real_llm:
            seen_types: dict[str, int] = {}
            results: list[RerankResult] = []
            for cand in candidates:
                chunk_data = self._resolve_chunk(
                    cand.get("chunk_id", ""), chunk_index, retrieval_index_by_id
                )
                r = self._mock_rerank(cand, section, claim, chunk_data, seen_types)
                if r.support_strength != "reject":
                    vt = cand.get("visual_argument_type", "")
                    seen_types[vt] = seen_types.get(vt, 0) + 1
                results.append(r)
            results.sort(key=lambda r: -r.fit_score)
            return results

        # Real LLM: parallel + cache
        claim_id = claim.get("claim_id", "")

        def _score_one(cand: dict) -> RerankResult:
            chunk_id = cand.get("chunk_id", "")
            key = self._cache_key(claim_id, chunk_id, self.model_tier)
            cached = self._lookup_cache(key)
            if cached is not None:
                return cached
            chunk_data = self._resolve_chunk(chunk_id, chunk_index, retrieval_index_by_id)
            result = self._llm_rerank(cand, section, claim, chunk_data)
            # Transport failures and malformed model output are transient.  A
            # permanent cache entry would prevent every later run from
            # recovering, so only cache a completed scientific judgment.
            if not result.failure_reason:
                self._save_to_cache(key, result)
            return result

        results_out: list[RerankResult] = []
        if self.workers <= 1 or len(candidates) <= 1:
            for cand in candidates:
                results_out.append(_score_one(cand))
        else:
            futures: dict = {}
            with ThreadPoolExecutor(max_workers=self.workers) as pool:
                for cand in candidates:
                    futures[pool.submit(_score_one, cand)] = cand
                for f in as_completed(futures):
                    try:
                        results_out.append(f.result())
                    except Exception as exc:
                        cand = futures[f]
                        results_out.append(self._failure_result(
                            cand.get("chunk_id", ""),
                            cand.get("source", ""),
                            cand.get("visual_argument_type", ""),
                            str(exc),
                        ))

        results_out.sort(key=lambda r: -r.fit_score)
        return results_out

    def rerank_claim_support(
        self,
        claim_support: list[dict],
        blueprint: dict,
        chunk_index: dict[str, dict],
        retrieval_index_by_id: dict[str, dict] | None = None,
        max_items: int | None = None,
        load_bearing_only: bool = False,
    ) -> list[dict]:
        """Apply rerank() over all claims; add reranked_visual_chunks + rejected_visual_chunks.

        max_items: global budget — total candidates scored across ALL claims. Once the budget
        is exhausted, remaining claims get empty reranked lists. This makes --rerank-max-items 4
        process exactly 4 candidates total (useful for smoke tests and cost control).
        """
        section_by_id = {s["section_id"]: s for s in (blueprint.get("sections") or [])}
        claim_by_id = self._build_blueprint_claim_index(blueprint)
        enriched: list[dict] = []
        remaining_budget: int | None = max_items  # global counter
        for crec in claim_support:
            crec = dict(crec)
            section = section_by_id.get(crec.get("section_id", ""), {})
            all_candidates = list(crec.get("candidate_visual_recommendations") or [])
            claim_id = str(crec.get("claim_id", ""))
            blueprint_claim = dict(claim_by_id.get(claim_id) or {})
            claim_statement = str(blueprint_claim.get("statement") or "").strip()
            claim_dict = {
                **blueprint_claim,
                "claim_id": claim_id,
                "statement": claim_statement,
                "evidence_type": (
                    blueprint_claim.get("evidence_type")
                    or crec.get("evidence_type")
                    or ""
                ),
            }
            if "evidence_type" not in crec and claim_dict.get("evidence_type"):
                crec["evidence_type"] = claim_dict["evidence_type"]
            crec["claim_statement"] = claim_statement

            if load_bearing_only and not bool(blueprint_claim.get("load_bearing")):
                crec["reranked_visual_chunks"] = []
                crec["rejected_visual_chunks"] = []
                crec["rerank_skipped_reason"] = "non_load_bearing_claim"
                enriched.append(crec)
                continue

            if not blueprint_claim:
                integrity_status = "missing_claim_in_blueprint"
            elif not claim_statement:
                integrity_status = "missing_claim_statement"
            else:
                integrity_status = "ok"
            crec["claim_input_integrity"] = integrity_status

            if not all_candidates:
                crec["reranked_visual_chunks"] = []
                crec["rejected_visual_chunks"] = []
                enriched.append(crec)
                continue

            if integrity_status != "ok":
                results = [
                    self._input_reject_result(
                        cand.get("chunk_id", ""),
                        cand.get("source", ""),
                        cand.get("visual_argument_type", ""),
                        integrity_status,
                    )
                    for cand in all_candidates
                ]
                crec["reranked_visual_chunks"] = []
                crec["rejected_visual_chunks"] = [
                    {**r.to_dict(), "rejection_reason": r.risk_or_caveat or r.failure_reason}
                    for r in results
                ]
                crec["rerank_skipped_reason"] = integrity_status
                enriched.append(crec)
                continue

            candidates = all_candidates
            # Apply global budget only to candidates that will actually be scored.
            if remaining_budget is not None:
                candidates = candidates[:remaining_budget]
                remaining_budget -= len(candidates)

            if not candidates:
                crec["reranked_visual_chunks"] = []
                crec["rejected_visual_chunks"] = []
                enriched.append(crec)
                continue

            results = self.rerank(
                candidates, section, claim_dict, chunk_index,
                retrieval_index_by_id,
            )
            crec["reranked_visual_chunks"] = [
                r.to_dict() for r in results if r.support_strength != "reject"
            ]
            crec["rejected_visual_chunks"] = [
                {**r.to_dict(), "rejection_reason": r.risk_or_caveat or r.failure_reason or "low fit_score"}
                for r in results if r.support_strength == "reject"
            ]
            enriched.append(crec)
        return enriched

    def compute_rerank_stats(self, claim_support: list[dict]) -> dict:
        """Aggregate rerank statistics from enriched claim_support."""
        strength_counts: Counter = Counter()
        mode_counts: Counter = Counter()
        directness_counts: Counter = Counter()
        vision_success = 0
        fallback_count = 0
        failed_count = 0
        human_review_count = 0
        total_vision_attempts = 0
        recovered_retry_count = 0
        premium_retry_count = 0
        for crec in claim_support:
            all_results = (
                (crec.get("reranked_visual_chunks") or [])
                + (crec.get("rejected_visual_chunks") or [])
            )
            for r in all_results:
                strength_counts[r.get("support_strength", "unknown")] += 1
                mode = r.get("evidence_mode", "unknown")
                mode_counts[mode] += 1
                directness_counts[r.get("directness", "unknown")] += 1
                if mode == "vision_image_text":
                    vision_success += 1
                elif mode != "text_only":
                    fallback_count += 1
                if r.get("failure_reason"):
                    failed_count += 1
                if r.get("needs_human_review"):
                    human_review_count += 1
                try:
                    total_vision_attempts += max(0, int(r.get("vision_attempt_count") or 0))
                except (TypeError, ValueError):
                    pass
                if r.get("recovery_action") and not r.get("failure_reason"):
                    recovered_retry_count += 1
                if str(r.get("vision_model_tier_used") or "") == "vision_premium_model":
                    premium_retry_count += 1
        return {
            "support_strength_distribution": dict(strength_counts),
            "evidence_mode_distribution": dict(mode_counts),
            "directness_distribution": dict(directness_counts),
            "vision_success_count": vision_success,
            "fallback_count": fallback_count,
            "failed_count": failed_count,
            "human_review_count": human_review_count,
            "total_vision_attempts": total_vision_attempts,
            "recovered_retry_count": recovered_retry_count,
            "premium_retry_count": premium_retry_count,
        }

    # ------------------------------------------------------------------
    # Mock scoring
    # ------------------------------------------------------------------

    def _mock_rerank(
        self,
        candidate: dict,
        section: dict,
        claim: dict,
        chunk_data: dict,
        seen_types: dict[str, int],
    ) -> RerankResult:
        chunk_id = candidate.get("chunk_id", "")
        vtype = candidate.get("visual_argument_type", "")
        etype = claim.get("evidence_type", "")
        source = candidate.get("source", "provided_by_blueprint")
        confidence = (
            chunk_data.get("visual_argument_confidence", "")
            or candidate.get("visual_argument_confidence", "")
        )
        strong_types = _EVIDENCE_TYPE_TO_STRONG_TYPES.get(etype, frozenset())
        type_match = vtype in strong_types
        type_score = 2.0 if type_match else 0.0

        claim_tokens = self._tokenize(" ".join([
            claim.get("statement") or "",
            claim.get("claim_seed") or "",
            section.get("argument_role") or "",
            section.get("title") or "",
        ]))
        chunk_tokens = self._tokenize(" ".join([
            chunk_data.get("title") or "",
            chunk_data.get("caption") or "",
            chunk_data.get("search_text") or "",
            chunk_data.get("visual_argument_claim") or "",
        ]))
        matched_terms: list[str] = []
        lexical_score = 0.0
        if claim_tokens and chunk_tokens:
            overlap = claim_tokens & chunk_tokens
            matched_terms = sorted(overlap)[:5]
            jaccard = len(overlap) / len(claim_tokens | chunk_tokens)
            lexical_score = min(jaccard * 1.5, 1.5)

        conf_score = 0.5 if confidence == "high" else (0.25 if confidence == "medium" else 0.0)
        pre_score_capped = min(float(candidate.get("score", 0.0)) * 0.3, 0.3)
        saturation_penalty = -0.5 if seen_types.get(vtype, 0) >= 2 else 0.0

        fit_score = type_score + lexical_score + conf_score + pre_score_capped + saturation_penalty
        if not type_match and lexical_score == 0.0 and conf_score < 0.25:
            fit_score = 0.1  # force into reject zone
        fit_score = round(min(max(fit_score, 0.0), 5.0), 3)
        support_strength = self._fit_to_strength(fit_score)
        best_use = self._fit_to_best_use(fit_score)
        aspect = _TYPE_TO_ASPECTS.get(vtype, "visual evidence")

        if matched_terms and type_match:
            why = (
                f"Type '{vtype}' structurally matches evidence_type '{etype}'; "
                f"lexical overlap on [{', '.join(matched_terms[:3])}]."
            )
        elif type_match:
            why = f"Type '{vtype}' structurally matches evidence_type '{etype}' — addressing {aspect}."
        elif matched_terms:
            why = (
                f"Lexical overlap on [{', '.join(matched_terms[:3])}]; "
                f"but type '{vtype}' does not directly match evidence_type '{etype}'."
            )
        else:
            why = f"Type '{vtype}' does not match '{etype}'; no lexical overlap."

        if support_strength == "reject":
            caveat = f"Rejected: type '{vtype}' mismatches '{etype}' and no content overlap."
        elif support_strength == "decorative":
            caveat = "Marginal fit; adds no direct evidence for this claim."
        elif support_strength == "weak":
            caveat = "Moderate fit; human review recommended."
        elif seen_types.get(vtype, 0) >= 1:
            caveat = f"Type '{vtype}' already present; confirm this adds distinct evidence."
        else:
            caveat = ""

        caption_core = (chunk_data.get("caption") or chunk_data.get("title") or chunk_id)[:80].rstrip(",. ")
        caption_sentence = f"({vtype.replace('_', ' ').title()}) {caption_core} — demonstrating {aspect}."

        if support_strength == "reject":
            directness = "unrelated"
        elif type_match and matched_terms:
            directness = "direct"
        elif type_match or matched_terms:
            directness = "contextual"
        else:
            directness = "unrelated"

        result = RerankResult(
            chunk_id=chunk_id,
            fit_score=fit_score,
            support_strength=support_strength,
            best_use=best_use,
            supported_claim_aspect=aspect,
            why_this_visual=why,
            risk_or_caveat=caveat,
            recommended_caption_sentence=caption_sentence,
            source=source,
            needs_human_review=(support_strength in ("weak", "decorative", "reject")),
            evidence_mode="text_only",
            directness=directness,
            supported_claim_components=matched_terms,
            unsupported_claim_components=[] if directness == "direct" else ["claim-specific core components not visually verified"],
            provable_claim_part=", ".join(matched_terms) if matched_terms else "",
            entity_alignment="exact" if directness == "direct" else "unclear",
        )
        return self._calibrate_result(result)

    # ------------------------------------------------------------------
    # LLM rerank — vision-first, text-only fallback
    # ------------------------------------------------------------------

    def _llm_rerank(
        self,
        candidate: dict,
        section: dict,
        claim: dict,
        chunk_data: dict,
    ) -> RerankResult:
        chunk_id = candidate.get("chunk_id", "")
        vtype = candidate.get("visual_argument_type", "")
        source = candidate.get("source", "provided_by_blueprint")
        caption = chunk_data.get("caption") or chunk_data.get("title") or ""
        search_text = chunk_data.get("search_text") or ""
        claim_text = claim.get("statement") or claim.get("claim_seed") or ""
        section_role = section.get("argument_role") or section.get("title") or ""
        etype = claim.get("evidence_type", "")
        aspect = _TYPE_TO_ASPECTS.get(vtype, "visual evidence")

        image_path_str = (
            chunk_data.get("local_image_path")
            or candidate.get("local_image_path")
            or ""
        )
        local_image: Path | None = Path(image_path_str) if image_path_str else None

        binding_reason = str(claim.get("evidence_binding_reason") or "").strip()
        component_map = claim.get("evidence_component_map") or []
        missing_components = claim.get("missing_evidence_components") or []
        core_components = self._claim_components(claim)

        ctx_parts = [f"Figure type: {vtype}", f"Evidence type required: {etype}"]
        if core_components:
            ctx_parts.append(
                "Core claim components to verify: "
                + "; ".join(core_components[:8])
            )
        if binding_reason:
            ctx_parts.append(f"Text-evidence binding reason: {binding_reason[:300]}")
        if component_map:
            ctx_parts.append(
                "Text-evidence component map: "
                + json.dumps(component_map[:6], ensure_ascii=False)[:600]
            )
        if missing_components:
            ctx_parts.append(
                "Known missing evidence components: "
                + json.dumps(missing_components[:6], ensure_ascii=False)[:300]
            )
        if caption:
            ctx_parts.append(f"Caption: {caption[:300]}")
        if search_text:
            ctx_parts.append(f"Nearby figure/context text: {search_text[:500]}")
        prompt = (
            "Evaluate whether this figure, its caption, and nearby/context text directly "
            "support the following scientific claim. Judge the exact claim, not just the "
            "section topic or a same-domain relationship.\n\n"
            f"Claim: {claim_text}\n"
            f"Section role: {section_role}\n"
            + "\n".join(ctx_parts)
            + "\n\nDirectness protocol:\n"
            "- direct: the image/caption/context covers the claim's core variables, mechanism, comparison, or application.\n"
            "- partial: it supports only a subset of the claim components.\n"
            "- contextual: it is in the same topic area but does not prove claim-specific components.\n"
            "- unrelated: it does not support the claim.\n\n"
            "Entity and scope gate:\n"
            "- Identify the concrete system, material, application object, and operating context in both the claim and the visual.\n"
            "- A visual from a different material system, organism, device, boundary condition, or application does not directly prove the target claim; shared broad terminology is insufficient.\n"
            "- Use entity_alignment=exact only when the scientific object and application context match; compatible only when the transfer is physically justified by the supplied caption/context; mismatch when the objects or applications differ; unclear when the evidence is insufficient.\n"
            "- entity_alignment=mismatch must be rejected. entity_alignment=unclear cannot be medium or strong.\n\n"
            "Return JSON only. Be explicit about what this visual can and cannot prove:\n"
            '{"fit_score": <float 0-5>, '
            '"support_strength": "strong|medium|weak|decorative|reject", '
            '"best_use": "main_figure|supporting_figure|background|reject", '
            '"directness": "direct|partial|contextual|unrelated", '
            '"supported_claim_components": ["specific component supported by image/caption/context"], '
            '"unsupported_claim_components": ["specific core component not covered"], '
            '"provable_claim_part": "<the narrow part of the claim this visual can prove>", '
            '"claim_entities": ["system/material/application entities in the claim"], '
            '"visual_entities": ["system/material/application entities in the visual"], '
            '"entity_alignment": "exact|compatible|unclear|mismatch", '
            '"entity_mismatch_reason": "<why scopes match or differ>", '
            '"why_this_visual": "<one sentence>", '
            '"risk_or_caveat": "<one sentence or empty string>"}'
        )

        # First use the configured vision model.  If transport fails or the
        # model emits malformed JSON, make one bounded recovery attempt with a
        # normalized RGB/JPEG image and the premium vision tier.  Exhausted
        # failures remain rejected; they are never converted into evidence.
        try:
            from llm.qwen_vision_client import call_qwen_vision
            from optomind_research.visual_argument_classifier import prepare_image_for_vlm
        except Exception as exc:
            return self._failure_result(chunk_id, source, vtype, str(exc))

        attempt_specs: list[tuple[str, Path | None, str]] = [
            (self.model_tier, local_image, "primary_vision_call")
        ]
        if local_image is not None and local_image.exists():
            cache_dir = (
                self.cache_path.parent / "vision_image_cache"
                if self.cache_path
                else Path(__file__).resolve().parents[1]
                / "outputs"
                / "visual_argument_alignment"
                / "vision_image_cache"
            )
            prepared = prepare_image_for_vlm(local_image, cache_dir, max_side=1200)
        else:
            prepared = local_image
        recovery_tier = (
            self.model_tier
            if self.model_tier == "vision_premium_model"
            else "vision_premium_model"
        )
        attempt_specs.append((recovery_tier, prepared, "normalized_image_premium_retry"))

        parsed: dict[str, Any] = {}
        evidence_mode = "text_only"
        last_failure = "vision_rerank_failed"
        successful_attempt = 0
        successful_tier = ""
        recovery_action = ""
        for attempt_number, (tier, image_for_call, action) in enumerate(attempt_specs, 1):
            try:
                resp = call_qwen_vision(
                    agent_name="visual_reranker",
                    text_prompt=prompt,
                    local_image_path=image_for_call,
                    model_tier=tier,
                    temperature=0.1 if attempt_number == 1 else 0.0,
                    max_tokens=750,
                    response_format={"type": "json_object"},
                    max_retries=0,
                    timeout_seconds=90,
                    max_transport_key_candidates=1,
                )
            except Exception as exc:
                last_failure = type(exc).__name__
                continue

            evidence_mode = str(resp.get("_evidence_mode") or "text_only")
            raw_content = str(resp.get("content") or "")
            failure_reason = str(resp.get("_failure_reason") or "")
            if not raw_content or raw_content.startswith("[fallback]") or failure_reason:
                last_failure = failure_reason or "empty_or_fallback_response"
                continue
            try:
                parsed = extract_json_from_text(raw_content)
            except Exception:
                last_failure = "json_decode_error"
                continue
            successful_attempt = attempt_number
            successful_tier = tier
            recovery_action = "" if attempt_number == 1 else action
            break

        if not parsed:
            failed = self._failure_result(
                chunk_id,
                source,
                vtype,
                f"{last_failure}_after_{len(attempt_specs)}_attempts",
            )
            failed.vision_attempt_count = len(attempt_specs)
            failed.vision_model_tier_used = attempt_specs[-1][0]
            failed.recovery_action = attempt_specs[-1][2]
            return failed

        fit_score = round(min(max(float(parsed.get("fit_score", 0.0)), 0.0), 5.0), 3)
        support_strength = parsed.get("support_strength", "")
        if support_strength not in VALID_SUPPORT_STRENGTHS:
            support_strength = self._fit_to_strength(fit_score)
        best_use = parsed.get("best_use", "")
        if best_use not in VALID_BEST_USES:
            best_use = self._fit_to_best_use(fit_score)
        directness = str(parsed.get("directness") or "").lower().strip()
        if directness not in VALID_DIRECTNESS:
            directness = "contextual"
        supported_components = self._string_list(parsed.get("supported_claim_components"))
        unsupported_components = self._string_list(parsed.get("unsupported_claim_components"))
        provable_part = str(parsed.get("provable_claim_part") or "").strip()
        entity_alignment = str(parsed.get("entity_alignment") or "unclear").lower().strip()
        if entity_alignment not in {"exact", "compatible", "unclear", "mismatch"}:
            entity_alignment = "unclear"
        entity_mismatch_reason = str(parsed.get("entity_mismatch_reason") or "").strip()

        # Safety: text-only modes cannot claim main_figure or strong
        if evidence_mode != "vision_image_text":
            if support_strength == "strong":
                support_strength = "medium"
            if best_use == "main_figure":
                best_use = "supporting_figure"
            caveat = (
                f"Not visually verified ({evidence_mode}); text-only. "
                + (parsed.get("risk_or_caveat") or "")
            ).strip()
            needs_review = True
        else:
            caveat = parsed.get("risk_or_caveat", "") or ""
            needs_review = support_strength in ("weak", "decorative", "reject")

        caption_sentence = (
            f"({vtype.replace('_', ' ').title()}) {caption[:80].rstrip(',. ')} — "
            f"demonstrating {aspect}."
        )
        result = RerankResult(
            chunk_id=chunk_id,
            fit_score=fit_score,
            support_strength=support_strength,
            best_use=best_use,
            supported_claim_aspect=aspect,
            why_this_visual=parsed.get("why_this_visual", "LLM-assessed fit."),
            risk_or_caveat=caveat,
            recommended_caption_sentence=caption_sentence,
            source=source,
            needs_human_review=needs_review,
            evidence_mode=evidence_mode,
            directness=directness,
            supported_claim_components=supported_components,
            unsupported_claim_components=unsupported_components,
            provable_claim_part=provable_part,
            entity_alignment=entity_alignment,
            entity_mismatch_reason=entity_mismatch_reason,
            vision_attempt_count=successful_attempt,
            vision_model_tier_used=successful_tier,
            recovery_action=recovery_action,
        )
        return self._calibrate_result(result)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _failure_result(
        self,
        chunk_id: str,
        source: str,
        vtype: str,
        reason: str,
    ) -> RerankResult:
        aspect = _TYPE_TO_ASPECTS.get(vtype, "visual evidence")
        return RerankResult(
            chunk_id=chunk_id,
            fit_score=0.0,
            support_strength="reject",
            best_use="reject",
            supported_claim_aspect=aspect,
            why_this_visual=f"LLM call failed: {reason}",
            risk_or_caveat=f"Reranking failed: {reason}. Chunk excluded from recommendations.",
            recommended_caption_sentence="",
            source=source,
            needs_human_review=True,
            evidence_mode="text_only",
            directness="unrelated",
            supported_claim_components=[],
            unsupported_claim_components=[],
            provable_claim_part="",
            failure_reason=reason,
        )

    def _input_reject_result(
        self,
        chunk_id: str,
        source: str,
        vtype: str,
        reason: str,
    ) -> RerankResult:
        aspect = _TYPE_TO_ASPECTS.get(vtype, "visual evidence")
        return RerankResult(
            chunk_id=chunk_id,
            fit_score=0.0,
            support_strength="reject",
            best_use="reject",
            supported_claim_aspect=aspect,
            why_this_visual=f"Rerank skipped before model call: {reason}",
            risk_or_caveat=(
                f"Claim input integrity failed ({reason}); chunk excluded from recommendations."
            ),
            recommended_caption_sentence="",
            source=source,
            needs_human_review=True,
            evidence_mode="text_only",
            directness="unrelated",
            supported_claim_components=[],
            unsupported_claim_components=[],
            provable_claim_part="",
            failure_reason=f"claim_input_integrity_failed:{reason}",
        )

    @staticmethod
    def _build_blueprint_claim_index(blueprint: dict) -> dict[str, dict]:
        claim_by_id: dict[str, dict] = {}
        for section in blueprint.get("sections") or []:
            sid = section.get("section_id", "")
            for claim in section.get("claims") or []:
                cid = str(claim.get("claim_id") or "")
                if cid and cid not in claim_by_id:
                    claim_by_id[cid] = {**dict(claim), "section_id": claim.get("section_id") or sid}
        return claim_by_id

    @staticmethod
    def _string_list(value: Any, *, limit: int = 12) -> list[str]:
        if value is None:
            return []
        if isinstance(value, str):
            values = [value]
        elif isinstance(value, list):
            values = value
        else:
            return []
        out: list[str] = []
        for item in values:
            text = str(item).strip()
            if text:
                out.append(text[:240])
            if len(out) >= limit:
                break
        return out

    @classmethod
    def _claim_components(cls, claim: dict) -> list[str]:
        components: list[str] = []
        for row in claim.get("evidence_component_map") or []:
            if isinstance(row, dict):
                text = str(row.get("component") or row.get("claim_component") or "").strip()
                if text:
                    components.append(text[:240])
        components.extend(cls._string_list(claim.get("missing_evidence_components")))
        if not components:
            statement = str(claim.get("statement") or "").strip()
            if statement:
                components.append(statement[:300])
        return list(dict.fromkeys(components))

    @staticmethod
    def _admits_noncoverage(*texts: str) -> bool:
        text = " ".join(t for t in texts if t).lower()
        if not text:
            return False
        patterns = (
            r"\bdoes not\s+(?:explicitly\s+)?(?:address|cover|support|show|demonstrate|prove|define|represent|include|display|depict|quantify|resolve)\b",
            r"\bnot\s+(?:directly|explicitly)\s+(?:address|cover|support|show|demonstrate|prove|define|represent|include|display|depict|quantify|resolve)\b",
            r"\b(?:no|without|lacks?|missing|fails?\s+to)\s+(?:direct\s+)?(?:evidence|coverage|support|link|component|aspect)\b",
            r"\bonly\s+(?:contextual|background|same[-\s]?domain)\b",
            r"\b(?:key|core)\s+(?:components?|aspects?)\b.*\b(?:not|missing|absent|unaddressed|unsupported)\b",
        )
        return any(re.search(pattern, text) for pattern in patterns)

    @staticmethod
    def _max_best_use(best_use: str, max_use: str) -> str:
        order = {"reject": 0, "background": 1, "supporting_figure": 2, "main_figure": 3}
        if order.get(best_use, 0) > order.get(max_use, 0):
            return max_use
        return best_use if best_use in VALID_BEST_USES else "reject"

    def _cap_result(
        self,
        result: RerankResult,
        *,
        max_score: float,
        max_strength: str,
        max_best_use: str,
    ) -> None:
        strength_order = {"reject": 0, "decorative": 1, "weak": 2, "medium": 3, "strong": 4}
        result.fit_score = round(min(result.fit_score, max_score), 3)
        if strength_order.get(result.support_strength, 0) > strength_order[max_strength]:
            result.support_strength = max_strength
        mapped = self._fit_to_strength(result.fit_score)
        if strength_order.get(result.support_strength, 0) > strength_order.get(mapped, 0):
            result.support_strength = mapped
        result.best_use = self._max_best_use(result.best_use, max_best_use)

    def _reject_result(self, result: RerankResult, reason: str) -> RerankResult:
        result.fit_score = 0.0
        result.support_strength = "reject"
        result.best_use = "reject"
        result.needs_human_review = True
        result.directness = "unrelated"
        if reason and reason not in result.risk_or_caveat:
            result.risk_or_caveat = (result.risk_or_caveat + " " + reason).strip()
        return result

    def _calibrate_result(self, result: RerankResult) -> RerankResult:
        """Deterministically enforce visual-evidence safety contracts."""
        if result.support_strength not in VALID_SUPPORT_STRENGTHS:
            result.support_strength = self._fit_to_strength(result.fit_score)
        if result.best_use not in VALID_BEST_USES:
            result.best_use = self._fit_to_best_use(result.fit_score)
        result.directness = (result.directness or "contextual").lower().strip()
        if result.directness not in VALID_DIRECTNESS:
            result.directness = "contextual"

        result.entity_alignment = (result.entity_alignment or "unclear").lower().strip()
        if result.entity_alignment not in {"exact", "compatible", "unclear", "mismatch"}:
            result.entity_alignment = "unclear"
        if result.entity_alignment == "mismatch":
            reason = result.entity_mismatch_reason or "Scientific object/application scope mismatch."
            return self._reject_result(result, f"Entity mismatch: {reason}")
        if result.entity_alignment == "unclear":
            self._cap_result(
                result,
                max_score=1.9,
                max_strength="weak",
                max_best_use="supporting_figure",
            )
            result.needs_human_review = True
            if "Entity alignment is unclear" not in result.risk_or_caveat:
                result.risk_or_caveat = (
                    result.risk_or_caveat + " Entity alignment is unclear; direct use is not allowed."
                ).strip()

        unsupported = list(result.unsupported_claim_components or [])
        noncoverage = bool(unsupported) or self._admits_noncoverage(
            result.risk_or_caveat,
            result.why_this_visual,
            result.provable_claim_part,
        )

        if result.directness == "unrelated":
            return self._reject_result(result, "Directness=unrelated; visual rejected.")

        if result.directness == "contextual":
            self._cap_result(
                result,
                max_score=0.9,
                max_strength="decorative",
                max_best_use="background",
            )
            result.needs_human_review = True
            if not result.risk_or_caveat:
                result.risk_or_caveat = "Contextual only; does not prove claim-specific components."

        elif result.directness == "partial":
            self._cap_result(
                result,
                max_score=1.9,
                max_strength="weak",
                max_best_use="supporting_figure",
            )
            result.needs_human_review = True
            if not result.risk_or_caveat:
                result.risk_or_caveat = "Partial support only; human review required."

        elif noncoverage:
            self._cap_result(
                result,
                max_score=1.9,
                max_strength="weak",
                max_best_use="supporting_figure",
            )
            result.needs_human_review = True
            if "Unsupported core claim components" not in result.risk_or_caveat:
                suffix = "Unsupported core claim components prevent medium/strong support."
                result.risk_or_caveat = (result.risk_or_caveat + " " + suffix).strip()

        # Image inspection is mandatory for strong support or main-figure use.
        if result.evidence_mode != "vision_image_text":
            if result.support_strength == "strong":
                result.support_strength = "medium"
                result.fit_score = min(result.fit_score, 2.9)
            if result.best_use == "main_figure":
                result.best_use = "supporting_figure"
            result.needs_human_review = True
            prefix = f"Not visually verified ({result.evidence_mode}); text-only."
            if prefix not in result.risk_or_caveat:
                result.risk_or_caveat = (prefix + " " + result.risk_or_caveat).strip()

        # Medium support still requires a direct claim match.
        if result.support_strength in ("medium", "strong") and result.directness != "direct":
            self._cap_result(
                result,
                max_score=1.9,
                max_strength="weak",
                max_best_use="supporting_figure",
            )
            result.needs_human_review = True

        if result.best_use == "main_figure" and (
            result.evidence_mode != "vision_image_text" or result.directness != "direct"
        ):
            result.best_use = "supporting_figure"
            result.needs_human_review = True

        if result.support_strength in ("weak", "decorative", "reject"):
            result.needs_human_review = True
        if result.support_strength == "reject":
            result.best_use = "reject"
        return result

    @staticmethod
    def _tokenize(text: str) -> set[str]:
        tokens: set[str] = set()
        for w in re.split(r'[\W_]+', text.lower()):
            if len(w) < 4 or len(w) > 18:
                continue
            if w in _STOPWORDS:
                continue
            if re.fullmatch(r'\d+[a-z]?', w):
                continue
            tokens.add(w)
        return tokens

    @staticmethod
    def _resolve_chunk(
        chunk_id: str,
        chunk_index: dict[str, dict],
        retrieval_index_by_id: dict[str, dict] | None,
    ) -> dict:
        if chunk_id in chunk_index:
            return chunk_index[chunk_id]
        if retrieval_index_by_id and chunk_id in retrieval_index_by_id:
            return retrieval_index_by_id[chunk_id]
        return {}

    @staticmethod
    def _fit_to_strength(score: float) -> str:
        """Map fit_score to support_strength.

        Calibrated: type match (2.0) + high conf (0.5) → medium (≥ 2.0).
        strong requires type match + lexical overlap.
        reject covers type mismatch + zero lexical + low confidence.
        """
        if score >= 3.0:
            return "strong"
        if score >= 2.0:
            return "medium"
        if score >= 1.0:
            return "weak"
        if score >= 0.3:
            return "decorative"
        return "reject"

    @staticmethod
    def _fit_to_best_use(score: float) -> str:
        if score >= 3.0:
            return "main_figure"
        if score >= 2.0:
            return "supporting_figure"
        if score >= 0.3:
            return "background"
        return "reject"
