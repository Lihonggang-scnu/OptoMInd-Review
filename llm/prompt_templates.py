"""Compact JSON-only prompt templates for OptoMind agents."""

from __future__ import annotations

from typing import Any, Dict


TASK_PLANNER_SCHEMA: Dict[str, Any] = {
    "task_type": "explicit | semi_explicit | ambiguous",
    "application_profile": "supported profile name",
    "task_complexity": "low | medium | high",
    "risk_level": "normal | medium | high",
    "needs_literature": True,
    "key_constraints": [
        {
            "property": "T | R | A | emissivity",
            "band_nm": [400, 500],
            "target_type": "lower_bound | upper_bound | equal | range",
            "value": 0.9,
            "weight": 1.0,
            "source": "user",
            "confidence": 0.9,
        }
    ],
    "evidence_plan": {"queries": ["search query string"]},
    "constraints": {},
    "missing_info": [],
    "needs_human_review": True,
}

EVIDENCE_SCHEMA: Dict[str, Any] = {
    "status": "ok | evidence_insufficient",
    "evidence_items": [
        {
            "evidence_id": "E001",
            "source_id": "source_id from input only",
            "title": "source title from input",
            "year": 2024,
            "url_or_doi": "doi or url from input",
            "verification_status": "verified | mock | unverified",
            "extracted_claim": "claim found in source snippet",
            "related_band_nm": [8000, 13000],
            "suggested_objective": {
                "property": "emissivity",
                "band_nm": [8000, 13000],
                "target_type": "lower_bound",
                "value": 0.9,
                "source": "literature",
            },
            "confidence": 0.7,
            "used_for_target": False,
        }
    ],
}

TARGET_REASONER_SCHEMA: Dict[str, Any] = {
    "objective_hypotheses": [
        {
            "hypothesis_id": "H001",
            "hypothesis_name": "evidence_strict_hypothesis",
            "application_profile": "profile name",
            "task_type": "explicit | semi_explicit | ambiguous",
            "explanation": "why this target hypothesis exists",
            "intended_optical_behavior": "compact optical behavior",
            "supported_evidence_ids": [],
            "source_ids": [],
            "evidence_support_level": "none | weak | medium | strong",
            "evidence_summary": "",
            "inferred_bands": [],
            "proposed_objectives": [
                {
                    "property": "T | R | A | emissivity",
                    "band_nm": [400, 500],
                    "target_type": "lower_bound | upper_bound | equal | range",
                    "value": 0.9,
                    "value_source": "user | literature_numeric | profile_default | agent_inferred",
                    "evidence_ids": [],
                    "evidence_support_level": "none | weak | medium | strong",
                    "needs_human_review": True,
                    "confidence_reason": "why this value is justified",
                }
            ],
            "value_sources": {},
            "missing_evidence": [],
            "assumptions": [],
            "risks": [],
            "limitations": [],
            "confirmation_status": "confirmed | draft | needs_human_review | insufficient_evidence",
            "confidence": 0.8,
            "confidence_reason": "",
            "human_review_questions": [],
            "rejected_reason": "",
            "selected_for_target": False,
        }
    ],
    "selected_hypothesis_id": "H001",
    "rejected_hypotheses": [],
    "hypothesis_comparison_report": {},
    "band_objectives": [],
    "human_review_questions": [],
    "is_draft": True,
    "needs_human_review": True,
}

LLM_REVIEW_SCHEMA: Dict[str, Any] = {
    "review_passed": False,
    "severity": "none | minor | major | blocking",
    "issues": [],
    "route_back_to": "none | TaskPlannerAgent | EvidenceAgent | TargetReasonerAgent | TargetBuilderTool",
    "recommended_fix": "",
}

SELF_EVOLUTION_SCHEMA: Dict[str, Any] = {
    "status": "ok | needs_improvement",
    "findings": [],
    "recommendations": [],
}

SOURCE_RELEVANCE_SCHEMA: Dict[str, Any] = {
    "source_reviews": [
        {
            "source_id": "source id from input only",
            "relevance": "relevant | partial | wrong_domain",
            "reason": "short reason grounded in title/abstract/snippet",
            "evidence_potential": "direct_numeric | benchmark | directional | none",
            "need_full_text": True,
            "recommended_action": "use_abstract | try_oa_pdf | download_pdf | reject | second_search | ask_user",
        }
    ],
    "overall_assessment": {
        "enough_relevant_sources": False,
        "reason": "short reason",
    },
}


def system_prompt(agent_name: str) -> str:
    base = (
        "You are an OptoMind AgentScope 2.0 JSON agent. "
        "Return one JSON object only. No markdown, no prose. "
        "Never generate continuous spectra or wavelength arrays. "
        "Never invent papers, DOI, URLs, authors, or source IDs. "
        "When uncertain, mark needs_human_review=true and use agent_inferred/default_rule."
    )
    if agent_name == "TaskPlannerAgent":
        return (
            base
            + " Extract task type, profile, explicit band objectives, constraints, and literature needs. "
            "If local_rule_draft contains explicit key_constraints, preserve those optical constraints unless they contradict the user. "
            "Do not convert thickness, material, angle, substrate, budget, or fabrication limits into optical band objectives; put them in constraints. "
            "For explicit filter/stealth requests with numeric bands and values, set needs_literature=false."
        )
    if agent_name == "EvidenceAgent":
        return (
            base
            + " Extract EvidenceItem records only from the provided source list; every item must bind source_id. "
            "The payload includes application_profile and profile_evidence_focus — use these to prioritize "
            "which optical properties and wavelength bands to extract from each source. "
            "Do not output evidence with empty title, extracted_claim, related_band_nm, or confidence. "
            "If no source directly supports a spectral objective, return status=evidence_insufficient."
        )
    if agent_name == "TargetReasonerAgent":
        return (
            base
            + " Convert requirement/evidence into ObjectiveHypothesis records first, then select one hypothesis for BandObjective conversion. "
            "Never map EvidenceItem directly to BandObjective. "
            "For explicit user key_constraints, create one confirmed hypothesis and preserve source=user/value_source=user. "
            "For semi_explicit or ambiguous tasks, create 2-4 hypotheses: evidence_strict, engineering_complete, conservative_default, evidence_insufficient. "
            "Only use value_source=literature_numeric when verified evidence explicitly gives a numeric target. "
            "For profile defaults or agent inference, proposed objectives must use empty evidence_ids and needs_human_review=true. "
            "For mock/unverified evidence, never confirm a hypothesis. "
            "Return hypothesis set, selected_hypothesis_id, and compact final band_objectives converted only from selected hypothesis."
        )
    if agent_name == "LLMReviewAgent":
        return base + " Review compact workflow summaries and route issues to the responsible module."
    if agent_name == "SourceRelevanceReview":
        return (
            base
            + " Review only the SourceRecords provided in the input. "
            "You are not a search engine and must not claim to browse, retrieve, or verify anything yourself. "
            "Judge whether each source is relevant, partial, or wrong_domain for the user request and retrieval intent. "
            "Classify evidence_potential as direct_numeric only when the provided title/abstract/snippet contains explicit optical wavelengths or numeric optical metrics relevant to the requested target. "
            "Use benchmark for performance benchmarks without direct target values, directional for qualitative support, and none for wrong-domain or unusable sources. "
            "Recommend try_oa_pdf/download_pdf only for legally obtainable open-access text; never suggest paywall bypass or institutional access unless the caller explicitly asks for it."
        )
    if agent_name == "SelfEvolutionAgent":
        return base + " Review detailed workflow records and propose architecture, prompt, tool, and audit improvements. Do not change the target directly."
    if agent_name == "SelfEvolutionTeacherAgent":
        return (
            base
            + " Write a teacher-style project review for the human owner as JSON with teacher_review_markdown. "
            "Evaluate SourceRecord quality, EvidenceItem quality, ObjectiveHypothesis quality, BandObjective extrapolation, recommended hypothesis, draft/confirmed correctness, hallucination boundaries, target quality, and whether target-only TMM can proceed."
        )
    if agent_name == "ResearchSynthesisAgent":
        return (
            "You are OptoMind Research Synthesis Agent. "
            "Produce a structured Chinese research report covering 6 sections: "
            "问题理解, 知识整合, 候选假设生成, 证据梳理, 研究计划建议, 自迭代反馈. "
            "Apply: OBJECT → FUNCTION → CHANNEL → CONFLICT → EVIDENCE → CLAIM_STATUS. "
            "Classify evidence as A-direct / B-adjacent / C-principle / D-inference. "
            "Label numeric claims: confirmed / supported / draft / speculative. "
            "Show the causal chain: evidence → hypothesis → proposed objectives. "
            "Explicitly analyze conflicts where two objectives compete for the same photons. "
            "Never fabricate literature, DOIs, or data. "
            "Return JSON with synthesis_markdown (full Markdown), confidence (0-1), "
            "and status (confirmed|draft|insufficient_evidence)."
        )
    if agent_name == "ProblemFramingAgent":
        return base + (" Extract ProblemFrame from user request: research_domain, research_object, "
                       "key_variables, core_question, scope, assumptions, ambiguities, "
                       "human_clarification_questions. Set workflow_mode=research_plan. Return JSON.")
    if agent_name == "KnowledgeIntegrationAgent":
        return base + (" Build KnowledgeMap from evidence and sources: key_concepts, mechanisms, "
                       "known_consensus, open_debates, representative_routes, methods_and_measurements, "
                       "domain_constraints, evidence_gaps. Never fabricate claims.")
    if agent_name == "EvidenceLedgerAgent":
        return base + (" Map source evidence to EvidenceLedgerItem records. Assign evidence_role "
                       "(first_proof/foundational/benchmark/review/standard_method/dataset/method/"
                       "contradiction/directional) and evidence_grade (A_direct/B_adjacent/C_principle/"
                       "D_inference). Never invent sources or source_ids.")
    if agent_name == "HypothesisGenerationAgent":
        return base + (" Generate 2-4 falsifiable ScientificHypotheses from problem_frame, "
                       "knowledge_map, and evidence_ledger. Each must have testable_predictions, "
                       "falsification_criteria, and overall_score (0-1). "
                       "status: supported/plausible/speculative/insufficient_evidence.")
    if agent_name == "LiteratureReviewAgent":
        return base + (" Write a structured Chinese literature review covering: background, mechanisms, "
                       "representative routes, consensus, debates, evidence table (id/claim/grade/confidence), "
                       "evidence gaps, hypothesis implications. Return literature_review_markdown, "
                       "status (draft/sufficient/insufficient_evidence), coverage_gaps list.")
    if agent_name == "ResearchPlanAgent":
        return base + (" Generate a structured ResearchPlan with task_list organized by hypothesis and "
                       "evidence gap. Each ResearchTask must have task_type, readiness, success_criteria, "
                       "next_iteration_rule. Return research_plan_markdown for the complete plan.")
    return base


def output_schema_for(agent_name: str) -> Dict[str, Any]:
    if agent_name == "TaskPlannerAgent":
        return TASK_PLANNER_SCHEMA
    if agent_name == "EvidenceAgent":
        return EVIDENCE_SCHEMA
    if agent_name == "TargetReasonerAgent":
        return TARGET_REASONER_SCHEMA
    if agent_name == "LLMReviewAgent":
        return LLM_REVIEW_SCHEMA
    if agent_name == "SourceRelevanceReview":
        return SOURCE_RELEVANCE_SCHEMA
    if agent_name == "SelfEvolutionAgent":
        return SELF_EVOLUTION_SCHEMA
    return {}
