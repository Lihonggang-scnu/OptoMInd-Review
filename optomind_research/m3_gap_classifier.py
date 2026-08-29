"""M3 Gap Classifier — 9-type gap routing to prevent infinite M3 loops (T6).

Gap Types (extended from 4 to 9):
- direct_retrievable:       Single-paper evidence exists → Semantic Scholar + OpenAlex
- mechanism_component:      Mechanistic explanation needed → synonym expansion + citation tracing
- corpus_prevalence:        Widespread phenomenon claim → statistical corpus analysis
- absence_or_neglect:       Claims gap/neglect exists → systematic search + coverage analysis
- quantitative_benchmark:   Comparative numbers needed → structured data extraction + matrix
- methodological_critique:  Experimental method issues → conditions/limitations comparison
- contradiction:            Conflicting evidence → paired retrieval with condition differences
- frontier_unknown:         Open science question → flag, do not force resolution
- normative_recommendation: Recommendation/suggestion → multi-evidence synthesis

Design constraints:
- Domain-agnostic: works for any research field
- LLM call: standard_model, JSON output
- Fast: single LLM call per claim, <3s overhead
- Conservative: uncertain → 'direct_retrievable' to avoid blocking valid gaps
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from llm.qwen_chat_client import call_qwen_chat

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PROMPT = PROJECT_ROOT / "prompts" / "M3 Gap Classifier.txt"

VALID_GAP_TYPES: frozenset[str] = frozenset({
    "direct_retrievable",
    "mechanism_component",
    "corpus_prevalence",
    "absence_or_neglect",
    "quantitative_benchmark",
    "methodological_critique",
    "contradiction",
    "frontier_unknown",
    "normative_recommendation",
    "structural_or_writing",
})

# Maps gap_type → {action, tools, priority_floor, retrieval_ready, implementation_status}
# retrieval_ready=True  → paper retrieval can proceed now.  A later analysis
#                        step may still be pending; that must not suppress useful
#                        retrieval candidates.
# implementation_status → audit label for the complete route, not a second
#                         retrieval gate.
GAP_ROUTING_TABLE: dict[str, dict[str, Any]] = {
    "direct_retrievable": {
        "action": "retrieve",
        "tools": ["semantic_scholar", "openalex"],
        "priority_floor": 5,
        "retrieval_ready": True,
        "implementation_status": "implemented",
        "description": "Direct point retrieval via Semantic Scholar + OpenAlex",
    },
    "mechanism_component": {
        "action": "retrieve_mechanism",
        "tools": ["semantic_scholar", "openalex", "citation_tracing"],
        "priority_floor": 6,
        "retrieval_ready": True,
        "implementation_status": "implemented",
        "description": "Mechanism synonym expansion + citation backward tracing",
    },
    "corpus_prevalence": {
        "action": "corpus_analysis",
        "tools": ["kb_matrix_analysis", "citation_statistics"],
        "priority_floor": 5,
        "retrieval_ready": False,
        "implementation_status": "planned_action",
        "description": "Statistical analysis of existing corpus; kb_matrix_analysis tool not yet implemented",
    },
    "absence_or_neglect": {
        "action": "systematic_search",
        "tools": ["semantic_scholar", "openalex", "coverage_analysis"],
        "priority_floor": 6,
        "retrieval_ready": False,
        "implementation_status": "planned_action",
        "description": "Systematic search protocol + coverage analysis with cautious wording",
    },
    "quantitative_benchmark": {
        "action": "retrieve_then_extract",
        "tools": ["semantic_scholar", "openalex", "structured_extraction", "benchmark_matrix"],
        "priority_floor": 7,
        "retrieval_ready": True,
        "implementation_status": "retrieval_implemented_postprocessing_pending",
        "description": "Retrieve papers containing the requested benchmark first; structured extraction and matrix comparison remain an audited downstream step",
    },
    "methodological_critique": {
        "action": "retrieve_then_compare",
        "tools": ["semantic_scholar", "openalex", "limitations_comparison"],
        "priority_floor": 5,
        "retrieval_ready": True,
        "implementation_status": "retrieval_implemented_postprocessing_pending",
        "description": "Retrieve method and limitations evidence now; condition-aware comparison remains an audited downstream step",
    },
    "contradiction": {
        "action": "retrieve_opposing_evidence",
        "tools": ["semantic_scholar", "openalex", "condition_diff_analysis"],
        "priority_floor": 7,
        "retrieval_ready": True,
        "implementation_status": "retrieval_implemented_postprocessing_pending",
        "description": "Retrieve potentially opposing evidence now; pair validation and condition-difference analysis remain audited downstream steps",
    },
    "frontier_unknown": {
        "action": "flag",
        "tools": [],
        "priority_floor": 4,
        "retrieval_ready": False,
        "implementation_status": "implemented",
        "description": "Preserve as open question; do not force resolution into consensus",
    },
    "normative_recommendation": {
        "action": "retrieve_then_synthesize",
        "tools": ["semantic_scholar", "openalex"],
        "priority_floor": 4,
        "retrieval_ready": True,
        "implementation_status": "retrieval_implemented_postprocessing_pending",
        "description": "Retrieve several relevant evidence lines now; synthesis must remain explicitly labelled as a recommendation",
    },
    "structural_or_writing": {
        "action": "return_to_m2a",
        "tools": [],
        "priority_floor": 8,
        "retrieval_ready": False,
        "implementation_status": "implemented",
        "description": "Structural or writing issue — must return to M2a/M2b for revision; paper retrieval cannot fix this",
    },
}

# Legacy 4-type → new 9-type mapping for backward compatibility
_LEGACY_TYPE_MAP: dict[str, str] = {
    "retrievable": "direct_retrievable",
    "structural": "structural_or_writing",   # must return to M2a, not retrieval
    "writing_issue": "structural_or_writing",
    "frontier": "frontier_unknown",
    "methodology": "methodological_critique",
}


def get_gap_routing(gap_type: str) -> dict[str, Any]:
    """Return the routing entry for a gap type (with fallback)."""
    return GAP_ROUTING_TABLE.get(gap_type, GAP_ROUTING_TABLE["direct_retrievable"])


def classify_gap(
    *,
    claim_text: str,
    supporting_chunk_ids: list[str],
    claim_context: str = "",
    existing_chunks_summary: str = "",
    claim_kind: str = "",
    model_tier: str = "standard_model",
    force_mock: bool = False,
    prompt_path: Path = DEFAULT_PROMPT,
) -> dict[str, Any]:
    """Classify gap type for a low-saturation claim.

    Args:
        claim_text:             The claim statement (required)
        supporting_chunk_ids:   Current supporting chunk IDs (may be empty)
        claim_context:          Section title, adjacent claims, or other context
        existing_chunks_summary: Brief summary of what's already in KB
        claim_kind:             T1 claim_kind if already inferred (used as hint)
        model_tier:             LLM model tier (default: standard_model)
        force_mock:             If True, return mock classification without LLM call

    Returns:
        {
            "gap_type":           one of VALID_GAP_TYPES,
            "reasoning":          why this gap type was chosen,
            "retrieval_priority": int 0-10 (higher = more urgent),
            "action":             action code from GAP_ROUTING_TABLE,
            "tools":              list of suggested tools,
            "recommendation":     specific next-step guidance,
            "stop_signals":       conditions that should halt retrieval for this gap,
        }
    """
    if force_mock:
        routing = GAP_ROUTING_TABLE["direct_retrievable"]
        return {
            "gap_type": "direct_retrievable",
            "reasoning": "Mock classification — defaulting to direct_retrievable",
            "retrieval_priority": 7,
            "action": routing["action"],
            "tools": routing["tools"],
            "retrieval_ready": routing["retrieval_ready"],
            "implementation_status": routing["implementation_status"],
            "recommendation": "Trigger M3 OA expansion with default settings",
            "stop_signals": [],
        }

    system_prompt = Path(prompt_path).read_text(encoding="utf-8")
    payload = {
        "claim": claim_text,
        "context": claim_context,
        "claim_kind_hint": claim_kind,
        "current_supporting_chunk_count": len(supporting_chunk_ids),
        "existing_kb_summary": existing_chunks_summary,
        "valid_gap_types": sorted(VALID_GAP_TYPES),
    }

    try:
        result = call_qwen_chat(
            "GapClassifier",
            [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
            ],
            model_tier=model_tier,
            temperature=0.1,
            max_tokens=500,
            response_format={"type": "json_object"},
            force_mock=False,
            max_retries=0,
        )
        content = str(result.get("content") or "")
        parsed = json.loads(content) if content else {}

        gap_type = str(parsed.get("gap_type", "direct_retrievable"))
        # Normalise legacy 4-type output from older prompt versions
        if gap_type in _LEGACY_TYPE_MAP:
            gap_type = _LEGACY_TYPE_MAP[gap_type]
        if gap_type not in VALID_GAP_TYPES:
            gap_type = "direct_retrievable"

        priority = int(parsed.get("retrieval_priority", 5))
        priority = max(0, min(10, priority))
        routing = GAP_ROUTING_TABLE[gap_type]
        if priority < routing["priority_floor"]:
            priority = routing["priority_floor"]

        action = routing["action"]
        tools = routing["tools"]

        stop_signals = list(parsed.get("stop_signals") or [])

        return {
            "gap_type": gap_type,
            "reasoning": str(parsed.get("reasoning") or "No reasoning provided"),
            "retrieval_priority": priority,
            "action": action,
            "tools": tools,
            "retrieval_ready": routing["retrieval_ready"],
            "implementation_status": routing["implementation_status"],
            "recommendation": str(parsed.get("recommendation") or routing["description"]),
            "stop_signals": stop_signals,
        }

    except Exception as exc:
        routing = GAP_ROUTING_TABLE["direct_retrievable"]
        return {
            "gap_type": "direct_retrievable",
            "reasoning": f"Classifier error: {type(exc).__name__}, defaulting to direct_retrievable",
            "retrieval_priority": 5,
            "action": routing["action"],
            "tools": routing["tools"],
            "retrieval_ready": routing["retrieval_ready"],
            "implementation_status": routing["implementation_status"],
            "recommendation": "LLM classification failed, proceeding with retrieval",
            "stop_signals": [],
        }
