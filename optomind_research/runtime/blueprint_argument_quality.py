"""Deterministic argument craft for the review blueprint stage.

The planner already chooses sections and candidate material.  This module adds
the missing contract between a planning claim and the evidence it will need
later.  It is deliberately local-only: no network, no LLM, and no scientific
claim is promoted from a candidate preview.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from typing import Any, Mapping


_STOPWORDS = {
    "about", "after", "again", "also", "among", "and", "are", "because",
    "been", "between", "both", "but", "can", "could", "does", "for", "from",
    "have", "into", "its", "more", "not", "paper", "papers", "review",
    "should", "study", "that", "the", "their", "these", "this", "through",
    "what", "when", "where", "which", "with", "within", "would", "using",
    "evidence", "claim", "claims", "section", "source", "reported", "reports",
}

_FACTUAL = "factual_support"
_QUALIFIED = "contextual_or_qualified_support"
_BACKGROUND = "background_only"


def _text(value: Any, limit: int = 420) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()[:limit]


def _tokens(value: Any) -> set[str]:
    out: set[str] = set()
    for raw in re.findall(r"[a-z][a-z0-9_-]{2,}", str(value or "").lower()):
        token = raw.strip("-_")
        if token and token not in _STOPWORDS:
            out.add(token)
    return out


def _overlap(left: Any, right: Any) -> float:
    a, b = _tokens(left), _tokens(right)
    if not a or not b:
        return 0.0
    return len(a & b) / math.sqrt(len(a) * len(b))


def _permission(row: Mapping[str, Any], units: Mapping[str, Mapping[str, Any]]) -> str:
    explicit = str(row.get("use_permission") or "").strip()
    if explicit:
        return explicit
    unit = units.get(str(row.get("chunk_id") or "")) or {}
    quality = ((unit.get("durable_content_card") or {}).get("content_quality") or {})
    provenance = ((unit.get("audit") or {}).get("source_provenance") or {})
    return str(quality.get("evidence_ceiling") or provenance.get("use_permission") or _BACKGROUND)


def _source_kind(row: Mapping[str, Any], units: Mapping[str, Mapping[str, Any]]) -> str:
    unit = units.get(str(row.get("chunk_id") or "")) or {}
    quality = ((unit.get("durable_content_card") or {}).get("content_quality") or {})
    return str(row.get("source_kind") or quality.get("source_kind") or "unknown").lower()


def _paper_id(row: Mapping[str, Any], units: Mapping[str, Mapping[str, Any]]) -> str:
    paper = str(row.get("paper_id") or row.get("source_paper_id") or row.get("doi") or "").strip()
    if paper:
        return paper
    unit = units.get(str(row.get("chunk_id") or "")) or {}
    return str((unit.get("identity") or {}).get("paper_id") or "").strip()


def _propositions(row: Mapping[str, Any], units: Mapping[str, Mapping[str, Any]]) -> list[dict[str, Any]]:
    binding = row.get("material_card_binding")
    if isinstance(binding, Mapping) and isinstance(binding.get("propositions"), list):
        return [dict(item) for item in binding["propositions"] if isinstance(item, Mapping)]
    unit = units.get(str(row.get("chunk_id") or "")) or {}
    found: list[dict[str, Any]] = []
    for annotation in unit.get("query_annotations") or []:
        if not isinstance(annotation, Mapping):
            continue
        for item in annotation.get("propositions") or []:
            if isinstance(item, Mapping):
                found.append(dict(item))
    return found


def _candidate_record(row: Mapping[str, Any], units: Mapping[str, Mapping[str, Any]], statement: str) -> dict[str, Any]:
    props = _propositions(row, units)
    proposition_text = " ".join(_text(item.get("statement"), 420) for item in props)
    preview = " ".join([str(row.get("title") or ""), str(row.get("text_preview") or ""), proposition_text])
    permission = _permission(row, units)
    score = _overlap(statement, preview)
    if props:
        score += 0.30
    if permission == _FACTUAL:
        score += 0.18
    elif permission == _QUALIFIED:
        score += 0.08
    return {
        "chunk_id": str(row.get("chunk_id") or ""),
        "paper_id": _paper_id(row, units),
        "source_kind": _source_kind(row, units),
        "use_permission": permission,
        "proposition_ids": [str(item.get("proposition_id") or "") for item in props if item.get("proposition_id")],
        "match_score": round(score, 4),
        "content_depth": str(row.get("content_depth") or ((units.get(str(row.get("chunk_id") or "")) or {}).get("durable_content") or {}).get("content_depth") or "unknown"),
    }


def _requirement(claim: Mapping[str, Any]) -> tuple[str, str]:
    kind = str(claim.get("claim_kind") or "direct_fact").lower()
    requirement = str(claim.get("evidence_requirement") or "factual").lower()
    if requirement in {"normative", "open_question", "none"} or kind in {"normative_recommendation", "frontier_uncertainty", "absence_or_neglect"}:
        return requirement if requirement in {"normative", "open_question", "none"} else "open_question", _QUALIFIED
    return "factual", _FACTUAL


def _counter_query(statement: str) -> str:
    terms = sorted(_tokens(statement))[:10]
    suffix = " ".join(terms)
    return _text(f"{suffix} limitations contradictory findings boundary conditions alternative explanation", 260)


def build_claim_evidence_contracts(
    sections: list[dict[str, Any]],
    material_units_by_chunk_id: Mapping[str, Mapping[str, Any]] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Attach auditable candidate contracts to every planning claim.

    Contracts are recommendations for the later verifier, not evidence
    bindings.  A factual contract is considered ready only when a candidate
    with ``factual_support`` permission exists.
    """
    units = {str(k): v for k, v in (material_units_by_chunk_id or {}).items() if isinstance(v, Mapping)}
    total = factual = candidate_ready = proposition_ready = 0
    section_reports: list[dict[str, Any]] = []
    for section in sections:
        rows = [row for row in section.get("candidate_text_chunks") or [] if isinstance(row, Mapping) and row.get("chunk_id")]
        section_claims = [claim for claim in section.get("claims") or [] if isinstance(claim, dict)]
        section_papers = {paper for paper in (_paper_id(row, units) for row in rows) if paper}
        section_factual = 0
        section_ready = 0
        section_contract_ids: list[str] = []
        for claim in section_claims:
            total += 1
            statement = _text(claim.get("statement") or claim.get("claim_seed"), 900)
            requirement, minimum_permission = _requirement(claim)
            ranked = sorted((_candidate_record(row, units, statement) for row in rows), key=lambda item: (item["match_score"], item["paper_id"], item["chunk_id"]), reverse=True)
            selected: list[dict[str, Any]] = []
            seen_papers: set[str] = set()
            for item in ranked:
                paper = item["paper_id"]
                if paper and paper in seen_papers and len(selected) < 2:
                    continue
                selected.append(item)
                if paper:
                    seen_papers.add(paper)
                if len(selected) >= 5:
                    break
            factual_candidates = [item for item in ranked if item["use_permission"] == _FACTUAL]
            proposition_candidates = [item for item in ranked if item["proposition_ids"]]
            if requirement == "factual":
                factual += 1
                section_factual += 1
            if selected:
                candidate_ready += 1
                section_ready += 1
            if proposition_candidates:
                proposition_ready += 1
            if requirement == "factual" and factual_candidates:
                section_ready += 0
            missing: list[str] = []
            if not selected:
                missing.append("a relevant material unit")
            if requirement == "factual" and not factual_candidates:
                missing.append("a factual_support passage; contextual or abstract material cannot close this claim")
            if not proposition_candidates:
                missing.append("a proposition-bound material card")
            contract_id = f"{claim.get('claim_id')}:contract"
            claim["evidence_contract"] = {
                "contract_id": contract_id,
                "claim_id": str(claim.get("claim_id") or ""),
                "evidence_requirement": requirement,
                "minimum_use_permission": minimum_permission,
                "candidate_chunk_ids": [item["chunk_id"] for item in selected],
                "candidate_proposition_ids": sorted({pid for item in proposition_candidates[:5] for pid in item["proposition_ids"]}),
                "candidate_source_kinds": dict(Counter(item["source_kind"] for item in selected)),
                "candidate_permission_counts": dict(Counter(item["use_permission"] for item in selected)),
                "candidate_paper_count": len({item["paper_id"] for item in selected if item["paper_id"]}),
                "factual_candidate_count": len(factual_candidates),
                "status": "candidate_ready" if selected and (requirement != "factual" or factual_candidates) else "gap",
                "missing_components": missing,
                "counterevidence_query": _counter_query(statement),
                "boundary_conditions": [
                    "Check the operating regime, geometry, solver assumptions, and measurement or fabrication conditions before generalizing.",
                ],
                "distinguishing_test": "State which observable, benchmark, or failure mode would distinguish this claim from its nearest alternative.",
                "later_binding_rule": "The verifier must bind the final prose to exact source spans; these are ranked candidates only.",
            }
            section_contract_ids.append(contract_id)
        section["argument_contract"] = {
            "load_bearing_claim_ids": [str(c.get("claim_id") or "") for c in section_claims if c.get("load_bearing")],
            "claim_contract_ids": section_contract_ids,
            "independent_paper_count": len(section_papers),
            "triangulation_target": "At least two independent papers for a load-bearing comparison or mechanism claim when the corpus permits it.",
            "counterevidence_required": bool(section_claims),
            "boundary_condition_required": bool(section_claims),
            "visual_status": "candidate_visuals_available" if section.get("candidate_visual_chunks") else "visual_evidence_pending",
        }
        section_reports.append({
            "section_id": str(section.get("section_id") or ""),
            "claim_count": len(section_claims),
            "candidate_paper_count": len(section_papers),
            "candidate_ready_claim_count": section_ready,
            "factual_claim_count": section_factual,
        })
    report = {
        "schema_version": "optomind.blueprint_argument_quality.v1",
        "mode": "deterministic_candidate_contracts",
        "claims_total": total,
        "factual_claims_total": factual,
        "claims_with_candidates": candidate_ready,
        "claims_with_proposition_candidates": proposition_ready,
        "candidate_coverage_ratio": round(candidate_ready / max(1, total), 4),
        "proposition_candidate_ratio": round(proposition_ready / max(1, total), 4),
        "factual_candidate_ready_ratio": round(
            sum(1 for section in sections for claim in section.get("claims") or [] if (claim.get("evidence_contract") or {}).get("status") == "candidate_ready" and (claim.get("evidence_contract") or {}).get("evidence_requirement") == "factual")
            / max(1, factual), 4
        ),
        "section_reports": section_reports,
        "interpretation": "Candidate contracts guide later evidence binding; they do not make the blueprint writing-ready.",
    }
    return sections, report

