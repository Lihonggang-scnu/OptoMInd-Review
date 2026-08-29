"""Constrained semantic relation classification for an observed literature graph.

The classifier never turns an arbitrary pair of papers into a relation.  It
only evaluates an existing observed edge or an explicitly supplied candidate,
and a semantic decision must carry traceable chunk IDs.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from typing import Any, Iterable

from optomind_research.runtime.review_quality_contract import (
    CANONICAL_OBSERVED_RELATIONS,
    CANONICAL_RELATION_STATUSES,
    CANONICAL_SEMANTIC_RELATIONS,
)


_TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z0-9]{2,}")
_POSITIVE_CUES = {
    "extend", "extends", "extension", "improve", "improves", "builds",
    "overcome", "generalize", "generalizes", "based on", "follow-up",
    "complement", "complements", "combine", "combined", "tradeoff",
    "limit", "limitation", "boundary", "contradict", "contradicts",
    "different", "versus", "compared",
}

_RELATION_EVIDENCE_CUES = {
    "foundation", "founded", "defines", "defined", "extends", "extension",
    "builds on", "built on", "follow-up", "improves", "overcomes",
    "complements", "complementary", "combine", "combined", "tradeoff",
    "limitation", "boundary", "contradicts", "contradiction", "versus",
    "compared with", "compared to", "translates", "deployed", "application",
}


def _tokens(text: str) -> set[str]:
    return {item.casefold() for item in _TOKEN_RE.findall(text or "")}


@dataclass(slots=True)
class SemanticRelationDecision:
    edge_id: str
    source_paper_id: str
    target_paper_id: str
    observed_relation: str
    semantic_relation: str = ""
    relation_basis_chunk_ids: list[str] = field(default_factory=list)
    confidence: float = 0.0
    status: str = "observed"
    reason: str = ""
    candidate_restricted: bool = True
    model_tier: str = "deterministic"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class SemanticRelationClassifier:
    """Classify only relation candidates with auditable evidence."""

    def __init__(self, *, model_tier: str = "standard_model") -> None:
        self.model_tier = model_tier

    @staticmethod
    def _candidate_context(candidate: dict[str, Any]) -> str:
        return " ".join(
            str(candidate.get(key) or "")
            for key in (
                "relation_context", "citation_context", "source_text",
                "target_text",
            )
        ).casefold()

    @staticmethod
    def _active_papers(candidate: dict[str, Any]) -> tuple[bool, bool]:
        """Return source/target activity without treating a discovery lead as evidence."""

        active = candidate.get("active_paper_ids")
        if active is not None:
            active_ids = {str(item).strip() for item in active if str(item).strip()}
            return (
                str(candidate.get("source_paper_id") or "") in active_ids,
                str(candidate.get("target_paper_id") or "") in active_ids,
            )
        source_active = candidate.get("source_active")
        target_active = candidate.get("target_active")
        return (
            True if source_active is None else bool(source_active),
            True if target_active is None else bool(target_active),
        )

    def classify_one(self, candidate: dict[str, Any]) -> SemanticRelationDecision:
        edge_id = str(candidate.get("edge_id") or "")
        source = str(candidate.get("source_paper_id") or "")
        target = str(candidate.get("target_paper_id") or "")
        observed = str(
            candidate.get("observed_relation") or candidate.get("edge_type") or ""
        ).casefold()
        if observed not in CANONICAL_OBSERVED_RELATIONS:
            return SemanticRelationDecision(
                edge_id=edge_id,
                source_paper_id=source,
                target_paper_id=target,
                observed_relation=observed,
                reason="candidate_observed_relation_not_allowed",
            )
        proposed = str(
            candidate.get("semantic_relation")
            or candidate.get("candidate_semantic_relation")
            or ""
        ).casefold()
        if proposed not in CANONICAL_SEMANTIC_RELATIONS:
            return SemanticRelationDecision(
                edge_id=edge_id,
                source_paper_id=source,
                target_paper_id=target,
                observed_relation=observed,
                reason="no_valid_semantic_candidate",
            )
        source_active, target_active = self._active_papers(candidate)
        if not source_active or not target_active:
            return SemanticRelationDecision(
                edge_id=edge_id,
                source_paper_id=source,
                target_paper_id=target,
                observed_relation=observed,
                status="discovery_lead",
                reason="relation_target_or_source_not_in_active_material_library",
            )
        basis = list(
            dict.fromkeys(
                str(item).strip()
                for item in (
                    candidate.get("relation_basis_chunk_ids")
                    or candidate.get("basis_chunk_ids")
                    or []
                )
                if str(item).strip()
            )
        )
        context = self._candidate_context(candidate)
        shared_task = bool(candidate.get("shared_argument_task"))
        overlap = _tokens(str(candidate.get("source_text") or "")) & _tokens(
            str(candidate.get("target_text") or "")
        )
        cue = any(term in context for term in _POSITIVE_CUES)
        if not basis:
            return SemanticRelationDecision(
                edge_id=edge_id,
                source_paper_id=source,
                target_paper_id=target,
                observed_relation=observed,
                reason="semantic_relation_requires_basis_chunk_ids",
            )
        active_chunks = candidate.get("active_chunk_ids")
        if active_chunks is not None:
            active_chunk_set = {str(item).strip() for item in active_chunks if str(item).strip()}
            if any(item not in active_chunk_set for item in basis):
                return SemanticRelationDecision(
                    edge_id=edge_id,
                    source_paper_id=source,
                    target_paper_id=target,
                    observed_relation=observed,
                    relation_basis_chunk_ids=basis,
                    reason="relation_basis_chunk_not_in_active_material_library",
                )
        if observed in {"s2_recommended", "semantic_recommendation"} and not (
            shared_task and (cue or overlap)
        ):
            return SemanticRelationDecision(
                edge_id=edge_id,
                source_paper_id=source,
                target_paper_id=target,
                observed_relation=observed,
                relation_basis_chunk_ids=basis,
                reason="recommendation_is_not_semantic_without_shared_task_and_context",
            )
        relation_context = " ".join(
            str(candidate.get(key) or "")
            for key in ("relation_context", "citation_context")
        ).casefold()
        explicit_relation_evidence = any(
            cue_text in relation_context for cue_text in _RELATION_EVIDENCE_CUES
        )
        if not explicit_relation_evidence:
            return SemanticRelationDecision(
                edge_id=edge_id,
                source_paper_id=source,
                target_paper_id=target,
                observed_relation=observed,
                relation_basis_chunk_ids=basis,
                reason="shared_task_or_overlap_is_not_semantic_relation_evidence",
            )
        confidence = min(
            0.95,
            0.50
            + (0.20 if explicit_relation_evidence else 0.0)
            + (0.15 if overlap else 0.0)
            + (0.10 if shared_task else 0.0),
        )
        return SemanticRelationDecision(
            edge_id=edge_id,
            source_paper_id=source,
            target_paper_id=target,
            observed_relation=observed,
            semantic_relation=proposed,
            relation_basis_chunk_ids=basis,
            confidence=confidence,
            status="inferred",
            reason="candidate_supported_by_active_material_and_relation_context",
            model_tier="deterministic",
        )

    def classify_batch(
        self,
        candidates: Iterable[dict[str, Any]],
        *,
        real_llm: bool = False,
        max_items: int = 4,
    ) -> list[SemanticRelationDecision]:
        """Run a bounded batch; deterministic mode is the default."""

        items = [item for item in candidates if isinstance(item, dict)][: max(1, int(max_items))]
        if not real_llm:
            return [self.classify_one(item) for item in items]
        # Keep the network call optional and bounded.  If the model is not
        # available, fail closed to deterministic decisions instead of
        # inventing a semantic edge.
        try:
            from llm.qwen_chat_client import call_qwen_chat

            prompt = (
                "Classify only the supplied observed relation candidates. "
                "Return JSON {\"decisions\":[{\"edge_id\":\"...\","
                "\"semantic_relation\":\"\",\"basis_chunk_ids\":[],"
                "\"confidence\":0,\"status\":\"inferred|observed\"}]}. "
                "Never create a relation without the supplied basis chunk IDs.\n"
                + json.dumps(items, ensure_ascii=False)
            )
            response = call_qwen_chat(
                "SemanticRelationClassifier",
                [
                    {"role": "system", "content": "You are a conservative scientific relation classifier."},
                    {"role": "user", "content": prompt},
                ],
                model_tier=self.model_tier,
                temperature=0,
                max_tokens=1200,
                response_format={"type": "json_object"},
                force_mock=False,
                max_retries=1,
            )
            payload = json.loads(str(response.get("content") or "{}"))
            raw_decisions = payload.get("decisions") if isinstance(payload, dict) else []
            by_id = {
                str(item.get("edge_id")): item
                for item in raw_decisions
                if isinstance(item, dict)
            }
            output: list[SemanticRelationDecision] = []
            for item in items:
                decision = self.classify_one(item)
                model_item = by_id.get(decision.edge_id)
                if model_item and decision.relation_basis_chunk_ids:
                    candidate_semantic = str(model_item.get("semantic_relation") or "").casefold()
                    candidate_status = str(model_item.get("status") or "observed").casefold()
                    if (
                        decision.semantic_relation
                        and
                        candidate_semantic in CANONICAL_SEMANTIC_RELATIONS
                        and candidate_status in CANONICAL_RELATION_STATUSES
                        and candidate_status != "observed"
                    ):
                        decision.semantic_relation = candidate_semantic
                        decision.status = candidate_status
                        decision.confidence = max(
                            decision.confidence,
                            min(1.0, float(model_item.get("confidence") or 0.0)),
                        )
                        decision.model_tier = self.model_tier
                        decision.reason = "bounded_llm_review_after_deterministic_candidate_gate"
                output.append(decision)
            return output
        except Exception:
            return [self.classify_one(item) for item in items]


def revalidate_legacy_relation_edges(
    edges: Iterable[dict[str, Any]],
    *,
    active_paper_ids: Iterable[str],
    active_chunk_ids: Iterable[str],
    model_tier: str = "standard_model",
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Revalidate semantic edges imported from an older relationship graph.

    Older S2 graphs sometimes used ``historical_role=predecessor`` or a
    generic recommendation as if it were proof of a semantic relation.  This
    function preserves the observed edge, but only retains the semantic label
    when the current active material contains both endpoints, the basis chunks
    are present, and the existing context explicitly describes the relation.
    It never invents basis text or promotes an inactive discovery lead.
    """

    active_papers = {
        str(item).strip() for item in active_paper_ids if str(item).strip()
    }
    active_chunks = {
        str(item).strip() for item in active_chunk_ids if str(item).strip()
    }
    classifier = SemanticRelationClassifier(model_tier=model_tier)
    output: list[dict[str, Any]] = []
    audit = {
        "input_edges": 0,
        "observed_preserved": 0,
        "semantic_retained": 0,
        "downgraded_discovery_lead": 0,
        "downgraded_unverified_legacy": 0,
        "reasons": {},
        "downgraded_edge_ids": [],
    }

    def note(reason: str) -> None:
        audit["reasons"][reason] = audit["reasons"].get(reason, 0) + 1

    for raw in edges:
        if not isinstance(raw, dict):
            continue
        audit["input_edges"] += 1
        edge = dict(raw)
        edge_id = str(edge.get("edge_id") or "")
        source = str(edge.get("source_paper_id") or "")
        target = str(edge.get("target_paper_id") or "")
        semantic = str(edge.get("semantic_relation") or "").strip().casefold()
        if not semantic:
            edge["legacy_validation_status"] = "observed_preserved"
            audit["observed_preserved"] += 1
            output.append(edge)
            continue

        observed = str(
            edge.get("observed_relation") or edge.get("edge_type") or ""
        ).strip().casefold()
        basis = list(
            dict.fromkeys(
                str(item).strip()
                for item in (
                    edge.get("relation_basis_chunk_ids")
                    or edge.get("basis_chunk_ids")
                    or ([edge.get("source_chunk_id")] if edge.get("source_chunk_id") else [])
                )
                if str(item).strip()
            )
        )
        reason = ""
        status = "unverified_legacy"
        if source not in active_papers or target not in active_papers:
            status = "discovery_lead"
            reason = "legacy_semantic_endpoint_not_in_active_material"
        elif not basis:
            reason = "legacy_semantic_relation_has_no_basis_chunks"
        elif any(item not in active_chunks for item in basis):
            reason = "legacy_semantic_basis_chunk_not_in_active_material"
        else:
            candidate = {
                **edge,
                "observed_relation": observed,
                "semantic_relation": semantic,
                "relation_basis_chunk_ids": basis,
                "active_paper_ids": sorted(active_papers),
                "active_chunk_ids": sorted(active_chunks),
                # Legacy graph calls this field ``context``.  Do not use
                # historical_role as relation evidence.
                "relation_context": edge.get("relation_context") or edge.get("context") or "",
                "citation_context": edge.get("citation_context") or "",
            }
            decision = classifier.classify_one(candidate)
            if decision.semantic_relation and decision.status in {
                "inferred", "reviewed", "human_confirmed"
            }:
                edge.update(
                    {
                        "observed_relation": observed,
                        "semantic_relation": decision.semantic_relation,
                        "relation_basis_chunk_ids": decision.relation_basis_chunk_ids,
                        "confidence": decision.confidence,
                        "status": decision.status,
                        "legacy_validation_status": "retained_after_revalidation",
                        "legacy_validation_reason": decision.reason,
                    }
                )
                audit["semantic_retained"] += 1
                output.append(edge)
                continue
            reason = decision.reason or "legacy_semantic_candidate_rejected"

        # Keep the graph edge for provenance, but remove the semantic claim so
        # CoverageAtlas and SynthesisBundle cannot count it as evidence.
        edge["semantic_relation"] = ""
        edge["relation_basis_chunk_ids"] = basis
        edge["status"] = status
        edge["legacy_validation_status"] = "downgraded"
        edge["legacy_validation_reason"] = reason
        if status == "discovery_lead":
            audit["downgraded_discovery_lead"] += 1
        else:
            audit["downgraded_unverified_legacy"] += 1
        audit["downgraded_edge_ids"].append(edge_id)
        note(reason)
        output.append(edge)

    audit["output_semantic_edges"] = sum(
        1 for item in output if str(item.get("semantic_relation") or "").strip()
    )
    audit["passed"] = audit["output_semantic_edges"] == audit["semantic_retained"]
    return output, audit
