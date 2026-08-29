"""Adaptive closure for M3 claims: keep, narrow, reframe, or drop."""

from __future__ import annotations

import json
import re
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from llm.qwen_chat_client import call_qwen_chat
from optomind_research.claim_schema import Claim

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PROMPT = PROJECT_ROOT / "prompts" / "Claim Evidence Closure.txt"
VALID_DISPOSITIONS = {
    "keep_supported", "narrow_to_supported", "retain_open_question",
    "convert_to_recommendation", "drop",
}


def _safe_json(text: str) -> dict[str, Any]:
    try:
        value = json.loads(text)
        return value if isinstance(value, dict) else {}
    except Exception:
        match = re.search(r"\{.*\}", str(text or ""), re.S)
        if not match:
            return {}
        try:
            value = json.loads(match.group(0))
            return value if isinstance(value, dict) else {}
        except Exception:
            return {}


def _compact(value: Any, limit: int) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()[:limit]


def _iter_claims(blueprint: dict[str, Any]):
    for section in blueprint.get("sections") or []:
        for claim in section.get("claims") or []:
            if isinstance(claim, dict):
                yield section, claim


class ClaimAdaptationAgent:
    def __init__(self, *, real_llm: bool = True, model_tier: str = "premium_model") -> None:
        self.real_llm = real_llm
        self.model_tier = model_tier

    def decide(
        self,
        claim: dict[str, Any],
        section: dict[str, Any],
        gap_classification: dict[str, Any],
        retrieval_history: list[dict[str, Any]],
    ) -> dict[str, Any]:
        payload = {
            "section": {
                "title": section.get("title", ""),
                "argument_role": section.get("argument_role", ""),
            },
            "claim": {
                "claim_id": claim.get("claim_id", ""),
                "statement": claim.get("statement", ""),
                "claim_kind": claim.get("claim_kind", ""),
                "claim_state": claim.get("claim_state", ""),
                "binding_status": claim.get("evidence_binding_status", ""),
                "binding_confidence": claim.get("evidence_binding_confidence", ""),
                "supported_rewrite_from_verifier": claim.get("supported_rewrite", ""),
                "supported_components": claim.get("evidence_component_map") or [],
                "missing_components": claim.get("missing_evidence_components") or [],
                "evidence_spans": claim.get("evidence_spans") or [],
                "supporting_text_chunk_count": len(claim.get("supporting_text_chunk_ids") or []),
            },
            "gap_classification": gap_classification,
            "retrieval_history": retrieval_history,
        }
        if self.real_llm:
            parsed: dict[str, Any] = {}
            usage_attempts: list[dict[str, Any]] = []
            tiers = list(dict.fromkeys([self.model_tier, "advanced_model"]))
            for tier in tiers:
                try:
                    result = call_qwen_chat(
                        "ClaimAdaptationAgent",
                        [
                            {
                                "role": "system",
                                "content": DEFAULT_PROMPT.read_text(encoding="utf-8"),
                            },
                            {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
                        ],
                        model_tier=tier,
                        temperature=0,
                        max_tokens=1200,
                        response_format={"type": "json_object"},
                        timeout_seconds=180,
                        max_transport_key_candidates=2,
                        allow_model_fallback=False,
                        accept_partial_stream=False,
                        enable_thinking=False,
                        force_mock=False,
                        max_retries=0,
                    )
                    usage_attempts.append(result.get("_llm_usage", {}))
                    parsed = _safe_json(str(result.get("content") or ""))
                    if parsed:
                        break
                except Exception as exc:
                    usage_attempts.append({
                        "model_tier": tier,
                        "success": False,
                        "error_type": type(exc).__name__,
                    })
            parsed["_llm_usage"] = {
                "attempts": usage_attempts,
                "explicit_fallback_used": len(usage_attempts) > 1,
            }
        else:
            parsed = {}

        from optomind_research.scientific_text_english_normalizer import ensure_english_strings
        english_fields = ensure_english_strings([
            str(parsed.get("revised_statement") or claim.get("supported_rewrite") or ""),
            str(parsed.get("open_question") or ""),
            str(parsed.get("reason") or "Evidence closure fallback applied."),
            str(parsed.get("remaining_evidence_need") or ""),
        ])

        disposition = str(parsed.get("disposition") or "")
        binding = str(claim.get("evidence_binding_status") or "")
        missing = list(claim.get("missing_evidence_components") or [])
        rewrite = _compact(english_fields[0], 900)
        gap_type = str(gap_classification.get("gap_type") or "")

        if disposition not in VALID_DISPOSITIONS:
            if binding in {"direct", "synthesized"} and not missing:
                disposition = "keep_supported"
            elif rewrite and claim.get("supporting_text_chunk_ids"):
                disposition = "narrow_to_supported"
            elif gap_type == "normative_recommendation":
                disposition = "convert_to_recommendation"
            else:
                disposition = "retain_open_question"
        # Enforce fail-closed boundaries on the model's decision.
        if disposition == "keep_supported" and (binding not in {"direct", "synthesized"} or missing):
            disposition = "narrow_to_supported" if rewrite and claim.get("supporting_text_chunk_ids") else "retain_open_question"
        if disposition == "narrow_to_supported" and (not rewrite or not claim.get("supporting_text_chunk_ids")):
            disposition = "retain_open_question"
        if disposition == "convert_to_recommendation" and not rewrite:
            # A bare status flip would leave the original unsupported factual
            # premise in the statement.  Preserve it as an open question until
            # the model supplies a genuinely recommendation-only rewrite.
            disposition = "retain_open_question"

        return {
            "disposition": disposition,
            "revised_statement": rewrite if disposition in {"narrow_to_supported", "convert_to_recommendation"} else "",
            "open_question": _compact(english_fields[1], 900),
            "reason": _compact(english_fields[2] or "Evidence closure fallback applied.", 800),
            "confidence": str(parsed.get("confidence") or "low"),
            "remaining_evidence_need": _compact(english_fields[3], 700),
            "_llm_usage": parsed.get("_llm_usage", {}),
        }


def adapt_m3_claims(
    blueprint: dict[str, Any],
    *,
    target_claim_ids: list[str],
    gap_classifications: dict[str, dict[str, Any]],
    round_reports: list[dict[str, Any]],
    real_llm: bool = True,
    max_workers: int = 4,
) -> list[dict[str, Any]]:
    """Apply auditable closure decisions in place and return their report."""
    agent = ClaimAdaptationAgent(real_llm=real_llm)
    target_set = set(target_claim_ids)
    results: list[dict[str, Any]] = []
    # Normalize the lifecycle contract for every claim, not only claims that
    # happened to be selected for retrieval in this run.
    for _, claim in _iter_claims(blueprint):
        if not claim.get("evidence_requirement"):
            claim["evidence_requirement"] = "factual"
        if claim.get("adaptation_history") is None:
            claim["adaptation_history"] = []

    decision_jobs: list[tuple[dict[str, Any], dict[str, Any], list[dict[str, Any]]]] = []
    for section, claim in _iter_claims(blueprint):
        claim_id = str(claim.get("claim_id") or "")
        if claim_id not in target_set:
            continue
        history = [
            {
                "round": row.get("round"),
                "status": row.get("status"),
                "selected_dois": row.get("selected_dois") or [],
                "new_chunk_count": len((row.get("kb_ingest") or {}).get("new_chunk_ids") or []),
                "reused_chunk_count": len((row.get("kb_ingest") or {}).get("reused_chunk_ids") or []),
                "novel_support_count": len((row.get("kb_ingest") or {}).get("novel_support_chunk_ids") or []),
            }
            for row in round_reports if row.get("claim_id") == claim_id
        ]
        decision_jobs.append((section, claim, history))

    def decide_one(job: tuple[dict[str, Any], dict[str, Any], list[dict[str, Any]]]):
        section, claim, history = job
        claim_id = str(claim.get("claim_id") or "")
        return claim_id, agent.decide(
            claim, section, gap_classifications.get(claim_id, {}), history
        )

    if len(decision_jobs) > 1 and max_workers > 1:
        with ThreadPoolExecutor(max_workers=min(max_workers, len(decision_jobs))) as pool:
            decisions = dict(pool.map(decide_one, decision_jobs))
    else:
        decisions = dict(decide_one(job) for job in decision_jobs)

    for section, claim in _iter_claims(blueprint):
        claim_id = str(claim.get("claim_id") or "")
        if claim_id not in target_set:
            continue
        decision = decisions[claim_id]
        original = str(claim.get("original_statement") or claim.get("statement") or "")
        claim["original_statement"] = original
        disposition = decision["disposition"]
        if disposition == "keep_supported":
            claim["closure_disposition"] = "keep_supported"
            claim["evidence_requirement"] = "factual"
            claim["claim_state"] = "grounded"
        elif disposition == "narrow_to_supported":
            claim["statement"] = decision["revised_statement"]
            claim["closure_disposition"] = "narrowed"
            claim["evidence_requirement"] = "factual"
            claim["claim_state"] = "reframed"
            # A closure model may propose wording, but only the independent
            # source verifier may promote it back to a factual claim.
            try:
                from optomind_research.claim_evidence_verifier import ClaimEvidenceVerifier
                verified = ClaimEvidenceVerifier(model_tier="premium_model").verify_and_bind(
                    [Claim.from_dict(claim)], section
                )[0]
                claim.update(verified.to_dict())
                if verified.evidence_binding_status not in {"direct", "synthesized", "partial"}:
                    claim["closure_disposition"] = "open_question"
                    claim["evidence_requirement"] = "open_question"
                    claim["claim_kind"] = "frontier_uncertainty"
                    claim["claim_state"] = "open_question"
                    decision["disposition"] = "retain_open_question"
                    decision["reason"] = (
                        decision["reason"]
                        + " Independent re-verification did not support the proposed narrower wording."
                    ).strip()
            except Exception as exc:
                claim["closure_disposition"] = "open_question"
                claim["evidence_requirement"] = "open_question"
                claim["claim_kind"] = "frontier_uncertainty"
                claim["claim_state"] = "open_question"
                decision["disposition"] = "retain_open_question"
                decision["reason"] = (
                    decision["reason"] + f" Re-verification failed: {type(exc).__name__}."
                ).strip()
        elif disposition == "convert_to_recommendation":
            if decision.get("revised_statement"):
                claim["statement"] = decision["revised_statement"]
            claim["closure_disposition"] = "recommendation"
            claim["evidence_requirement"] = "normative"
            claim["claim_kind"] = "normative_recommendation"
            claim["claim_state"] = "reframed"
        elif disposition == "drop":
            claim["closure_disposition"] = "dropped"
            claim["evidence_requirement"] = "none"
            claim["claim_state"] = "dropped"
            claim["load_bearing"] = False
        else:
            claim["closure_disposition"] = "open_question"
            claim["evidence_requirement"] = "open_question"
            gap_type = str((gap_classifications.get(claim_id) or {}).get("gap_type") or "")
            claim["claim_kind"] = (
                "absence_or_neglect" if gap_type == "absence_or_neglect"
                else "normative_recommendation" if gap_type == "normative_recommendation"
                else "frontier_uncertainty"
            )
            claim["claim_state"] = "open_question"
            if decision.get("open_question"):
                claim["open_question"] = decision["open_question"]
        claim["closure_reason"] = decision["reason"]
        claim.setdefault("adaptation_history", []).append({
            "stage": "m3_adaptive_closure",
            "original_statement": original,
            **{k: v for k, v in decision.items() if k != "_llm_usage"},
        })
        results.append({"claim_id": claim_id, **decision})
    # Claims outside the retrieval budget must not remain factual assertions
    # when they have no usable evidence.  Preserve them as explicit research
    # questions instead of silently dropping them or letting writers assert
    # them as established facts.
    for _, claim in _iter_claims(blueprint):
        if (
            claim.get("evidence_requirement") == "factual"
            and str(claim.get("evidence_binding_status") or "") in {"", "insufficient", "unverified"}
            and not claim.get("supporting_text_chunk_ids")
        ):
            claim["original_statement"] = str(claim.get("original_statement") or claim.get("statement") or "")
            claim["evidence_requirement"] = "open_question"
            claim["claim_state"] = "open_question"
            claim["claim_kind"] = "frontier_uncertainty"
            claim["closure_disposition"] = "open_question"
            claim["closure_reason"] = (
                "No usable evidence was available within the retrieval budget; retained as an explicit research question."
            )
    return results
