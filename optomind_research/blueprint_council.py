"""BlueprintCouncil — S7: generate three structurally distinct candidate blueprints.

Structural logics:
  BP-A: argument_first     — sections correspond to load-bearing sub-arguments
  BP-B: chronological_synthesis — sections trace evolution of understanding over time
  BP-C: taxonomic_contrast — sections correspond to taxonomy branches + cross-comparison

F2 changes:
  - All sections carry a stable section_id (S01, S02 …)
  - central_claim / period_thesis are now provisional claim dicts:
      {"text": "...", "claim_status": "provisional", "evidence_required": [...]}
  - Removed overclaiming language (consensus, multiple independent experiments, paradigm shifts)
  - candidates_sha256 added to output for staleness detection (F2-6)
  - Real LLM path wired via call_qwen_chat
"""

from __future__ import annotations

import hashlib
import json
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from optomind_research.artifact_registry import utc_now


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_COUNCIL_PROMPT = PROJECT_ROOT / "prompts" / "Blueprint Council.txt"
DEFAULT_CANDIDATE_PROMPT = PROJECT_ROOT / "prompts" / "Blueprint Candidate Designer.txt"

SCHEMA_VERSION = "blueprint_candidates.v1"
BLUEPRINT_SCHEMA = "dynamic_review_blueprint.v4"
SECTION_ROLES = {"introduction", "body", "synthesis"}
# Authoritative chapter-count contract.  Qwen owns which 8-10 chapters are
# scientifically necessary; Python rejects every candidate outside this range
# and never invents a scientific architecture to fill it.
AUTHORITATIVE_SECTION_RANGE: tuple[int, int] = (8, 10)
DETERMINISTIC_NON_PRODUCTION_MODE = "deterministic_non_production"


def _command_knowledge_block() -> dict[str, Any]:
    """Load ONLY the versioned architecture-planning command bundle.

    Authoring, audit, and manuscript-integration manuals belong to their own
    downstream roles and are never injected into the council payload.
    """
    from optomind_research.runtime.skill_loader import (
        get_command_skill_bundles,
        get_skill_guidance_prompt,
    )

    skills_dir = PROJECT_ROOT / "skills"
    bundles = get_command_skill_bundles(
        skills_dir, skill_ids=["top-review-architecture"]
    )
    valid = [
        bundle
        for bundle in bundles
        if bundle.name == "top-review-architecture"
        and re.match(r"^\d+\.\d+\.\d+$", bundle.version)
        and bundle.evidence_prohibition is True
    ]
    if len(valid) != 1:
        raise RuntimeError(
            "Production Qwen architecture planning requires the versioned "
            "'top-review-architecture' command-knowledge skill bundle; "
            f"received {[bundle.name for bundle in bundles] or 'none'}."
        )
    bundles = valid
    return {
        "source": "skill_guidance_api",
        "label": "command_knowledge_only_not_evidence",
        "role": "architecture_planning",
        "bundles_loaded": ["top-review-architecture"],
        "prompt_block": get_skill_guidance_prompt(
            skills_dir, skill_ids=["top-review-architecture"]
        ),
        "bundles": [
            {
                "skill": bundle.name,
                "skill_version": bundle.version,
                "role": bundle.role,
                "applicability": bundle.applicability,
                "evidence_prohibition": bundle.evidence_prohibition,
                "instructions": bundle.instructions,
            }
            for bundle in bundles
        ],
    }


_M1_FORBIDDEN_KEYS = frozenset(
    {
        "command_knowledge",
        "prompt_block",
        "skills",
        "skill_version",
        "instructions",
        "provenance",
        "bundles",
    }
)


def _m1_case_moves_payload(mentor_advice: dict[str, Any]) -> Any:
    """Return pure case-move knowledge, never command knowledge or the mentor envelope."""
    if not isinstance(mentor_advice, dict):
        return []
    value = mentor_advice.get("m1_case_moves")
    if value is None:
        value = mentor_advice.get("usable_intellectual_moves") or []
    if isinstance(value, dict):
        return {
            key: item
            for key, item in value.items()
            if key not in _M1_FORBIDDEN_KEYS
        }
    if isinstance(value, list):
        return [
            item
            for item in value
            if not (
                isinstance(item, dict)
                and any(key in item for key in _M1_FORBIDDEN_KEYS)
            )
        ]
    return value


def _concrete_sibling_exclusion_errors(
    sections: list[dict[str, Any]],
) -> list[str]:
    """Return errors when must_not_cover does not name a concrete sibling job."""
    stop = {
        "the", "and", "for", "with", "from", "that", "this", "into",
        "review", "section", "chapter", "must", "not", "cover", "owned",
        "responsibility", "responsibilities", "sibling", "do", "take", "over",
    }

    def tokens(value: Any) -> set[str]:
        return {
            token.strip("-")
            for token in re.findall(r"[a-z][a-z0-9\-]{2,}", str(value or "").lower())
            if token.strip("-") not in stop
        }

    by_id = {
        str(section.get("section_id") or ""): section
        for section in sections
    }
    errors: list[str] = []
    for section in sections:
        section_id = str(section.get("section_id") or "")
        for index, exclusion in enumerate(section.get("must_not_cover") or []):
            text = str(exclusion or "").strip()
            if not text:
                continue
            mentioned_ids = set(re.findall(r"S\d{2}", text))
            matched = False
            for sibling_id, sibling in by_id.items():
                if sibling_id == section_id or sibling_id not in mentioned_ids:
                    continue
                sibling_text = " ".join(
                    [
                        str(sibling.get("section_title") or sibling.get("title") or ""),
                        str(sibling.get("argument_role") or ""),
                        str(
                            sibling.get("unique_contribution")
                            or sibling.get("novel_contribution_to_review")
                            or ""
                        ),
                        " ".join(
                            str(item)
                            for item in (sibling.get("must_cover") or [])
                        ),
                    ]
                )
                if tokens(text) & tokens(sibling_text):
                    matched = True
                    break
            if not matched:
                errors.append(
                    f"{section_id} must_not_cover[{index}] must name a concrete "
                    "sibling-owned responsibility (sibling section ID plus a "
                    "distinctive word from that sibling's title, argument_role, "
                    f"or unique_contribution); received {_compact(text, 140)!r}."
                )
    return errors


def _compact(value: Any, limit: int = 360) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()[:limit]


def _safe_json_parse(text: str) -> dict[str, Any]:
    try:
        v = json.loads(text)
        return v if isinstance(v, dict) else {}
    except Exception:
        m = re.search(r"\{.*\}", str(text or ""), re.S)
        if m:
            try:
                v = json.loads(m.group(0))
                return v if isinstance(v, dict) else {}
            except Exception:
                pass
    return {}


def _sha256_candidates(candidates: list) -> str:
    data = json.dumps(candidates, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(data.encode("utf-8")).hexdigest()


def _provisional(text: str, evidence_required: list[str]) -> dict:
    """Represent a writing target without pretending it is already supported."""
    return {
        "text": text,
        "claim_status": "planned",
        "evidence_required": evidence_required,
    }


def _claim_text(value: Any) -> str:
    if isinstance(value, dict):
        return _compact(value.get("text", ""), 300)
    return _compact(value, 300)


def _normalise_candidate(
    candidate: dict,
    charter: dict,
    *,
    require_division_fields: bool = False,
) -> dict:
    """Apply the F2 blueprint contract to deterministic and LLM candidates alike.

    Chapter count is an 8-10 hard contract on every path.  ``require_division_fields``
    is True on the Qwen path: Qwen must author each chapter's unique contribution,
    must_cover/must_not_cover boundaries, and previous/next handoffs.  The
    deterministic non-production path may derive generic defaults instead.
    """
    result = dict(candidate)
    sections = [dict(s) for s in (candidate.get("sections") or [])]
    configured_range = (charter.get("constraints") or {}).get(
        "section_count_range", list(AUTHORITATIVE_SECTION_RANGE)
    )
    try:
        min_sections, max_sections = int(configured_range[0]), int(configured_range[1])
    except (TypeError, ValueError, IndexError):
        min_sections, max_sections = AUTHORITATIVE_SECTION_RANGE
    if (min_sections, max_sections) != AUTHORITATIVE_SECTION_RANGE:
        raise ValueError(
            "BlueprintCouncil: charter constraints.section_count_range must be "
            f"[8, 10] for the production comprehensive-review pipeline; "
            f"received [{min_sections}, {max_sections}]."
        )
    if not min_sections <= len(sections) <= max_sections:
        raise ValueError(
            f"Each blueprint candidate must contain {min_sections}-{max_sections} "
            f"chapters; received {len(sections)}."
        )

    total_budget = int((charter.get("constraints") or {}).get("word_budget_total", 10000))
    budgets = _distribute_budget(total_budget, len(sections))
    for index, section in enumerate(sections):
        section["section_index"] = index
        section["section_id"] = f"S{index + 1:02d}"
        section["estimated_word_budget"] = int(
            section.get("estimated_word_budget") or budgets[index]
        )
        inferred_role = (
            "introduction" if index == 0
            else "synthesis" if index == len(sections) - 1
            else "body"
        )
        declared_role = str(section.get("section_role") or inferred_role).strip().lower()
        if declared_role not in SECTION_ROLES:
            raise ValueError(
                f"{section['section_id']} has invalid section_role={declared_role!r}."
            )
        if declared_role != inferred_role:
            raise ValueError(
                f"{section['section_id']} must have section_role={inferred_role!r}; "
                f"received {declared_role!r}."
            )
        section["section_role"] = inferred_role

        raw_claim = (
            section.get("planned_thesis")
            or section.get("central_claim")
            or section.get("period_thesis")
        )
        if not isinstance(raw_claim, dict):
            raw_claim = _provisional(
                _claim_text(raw_claim) or (
                    "Determine what can be established for this section after evidence mapping."
                ),
                ["Directly relevant text evidence from the review knowledge base"],
            )
        raw_claim["claim_status"] = "planned"
        raw_claim.setdefault(
            "evidence_required",
            ["Directly relevant text evidence from the review knowledge base"],
        )
        section["planned_thesis"] = raw_claim
        # Keep legacy fields for downstream compatibility, but use the same explicit status.
        if "period_thesis" in section:
            section["period_thesis"] = raw_claim
        else:
            section["central_claim"] = raw_claim

        purpose = _compact(section.get("purpose", ""), 240)
        section.setdefault("argument_role", purpose or "Advance one step in the review argument.")
        section.setdefault(
            "unique_contribution",
            (
                section.get("novel_contribution_to_review")
                or section.get("argument_role")
                or purpose
            ),
        )
        section["unique_contribution"] = _compact(
            section["unique_contribution"]
            or "Advance one step in the review argument.",
            600,
        )
        if require_division_fields:
            for field in (
                "unique_contribution",
                "must_cover",
                "must_not_cover",
                "handoff_from_previous",
                "handoff_to_next",
            ):
                value = section.get(field)
                if not value or (
                    isinstance(value, (list, tuple))
                    and not any(str(item).strip() for item in value)
                ):
                    raise ValueError(
                        f"{section['section_id']} is missing non-empty "
                        f"division-of-labor field {field}; Qwen must author it."
                    )
        else:
            section.setdefault(
                "must_cover",
                ["Cover the intellectual job declared by this section's purpose and argument_role."],
            )
            section.setdefault(
                "must_not_cover",
                ["Do not take over responsibilities assigned to sibling chapters in the full section workplan."],
            )
        section.setdefault("assigned_user_axes", [])
        section["assigned_user_axes"] = [
            item for item in (section.get("assigned_user_axes") or [])
            if str(item).strip()
        ]
        section.setdefault(
            "key_questions",
            [
                f"What can the available evidence establish for '{_compact(section.get('section_title'), 100)}'?",
                "Which boundary conditions, disagreements, or missing evidence qualify that answer?",
            ],
        )
        section.setdefault(
            "required_claim_kinds",
            ["mechanism_or_explanation", "evidence_synthesis", "limitation_or_boundary"],
        )
        section.setdefault(
            "expected_visual_arguments",
            ["Use a figure or comparison table only if it performs a clear argumentative role."],
        )
        section.setdefault(
            "novel_contribution_to_review",
            "Clarify a relationship, boundary, or comparison that is not obvious from paper-by-paper summary.",
        )
        section.setdefault(
            "scope_guardrails",
            [
                "Do not turn a planned thesis into a factual conclusion before evidence mapping.",
                "Do not use M1 writing patterns as scientific evidence.",
            ],
        )
        default_from = (
            "Opening section: introduce the review topic, scope, and argument arc once."
            if index == 0 else
            f"Continue from the unresolved finding established in S{index:02d}; "
            "do not redefine the review topic or repeat prior background."
        )
        default_to = (
            "Closing synthesis: integrate the cumulative findings without introducing a new topic."
            if index == len(sections) - 1 else
            f"Expose the analytical need taken up by S{index + 2:02d} without a section-local summary."
        )
        section["transition_from_previous"] = _compact(
            section.get("transition_from_previous") or default_from, 500
        )
        section["transition_to_next"] = _compact(
            section.get("transition_to_next") or default_to, 500
        )
        section.setdefault(
            "handoff_from_previous",
            section.get("transition_from_previous") or default_from,
        )
        section.setdefault(
            "handoff_to_next",
            section.get("transition_to_next") or default_to,
        )
        section["handoff_from_previous"] = _compact(
            section["handoff_from_previous"], 500
        )
        section["handoff_to_next"] = _compact(
            section["handoff_to_next"], 500
        )
        section["continuity_contract"] = {
            "opening_mode": (
                "introduce_review_once" if inferred_role == "introduction"
                else "continue_without_reintroduction" if inferred_role == "body"
                else "integrate_without_recapitulating_introduction"
            ),
            "closing_mode": (
                "open_argument_arc" if inferred_role == "introduction"
                else "substantive_handoff_without_mini_conclusion"
                if inferred_role == "body" else "whole_review_synthesis"
            ),
            "topic_definition_policy": (
                "define_core_topic_and_abbreviations_here"
                if inferred_role == "introduction"
                else "assume_core_topic_and_abbreviations_are_already_defined"
            ),
            "forbidden_meta_narration": (
                [] if inferred_role == "synthesis" else [
                    "In summary",
                    "To summarize",
                    "Taken together",
                    "These questions frame the subsequent analysis",
                    "The following sections will",
                ]
            ),
        }

    if require_division_fields:
        concrete_errors = _concrete_sibling_exclusion_errors(sections)
        if concrete_errors:
            raise ValueError(
                "BlueprintCouncil: must_not_cover entries must name concrete "
                "sibling-owned responsibilities: "
                + "; ".join(concrete_errors)
            )

    # Enforce the global budget without silently dropping a section.
    current_total = sum(int(s.get("estimated_word_budget", 0)) for s in sections)
    if current_total > total_budget:
        budgets = _distribute_budget(total_budget, len(sections))
        for section, budget in zip(sections, budgets):
            section["estimated_word_budget"] = budget
    result["sections"] = sections
    return result


def _unsupported_numeric_claims(candidate: dict, source_context: str) -> list[str]:
    """Reject new quantitative facts before evidence mapping has happened."""
    pattern = re.compile(r"(?<![A-Za-z])\d+(?:\.\d+)?(?:\s*[-–—]\s*\d+(?:\.\d+)?)?\s*%?")
    allowed = {m.group(0).replace(" ", "") for m in pattern.finditer(source_context)}
    errors: list[str] = []
    for section in candidate.get("sections") or []:
        text = " ".join(
            [
                str(section.get("section_title", "")),
                _claim_text(section.get("planned_thesis") or section.get("central_claim") or section.get("period_thesis")),
            ]
        )
        introduced = {
            m.group(0).replace(" ", "") for m in pattern.finditer(text)
            if m.group(0).replace(" ", "") not in allowed
        }
        if introduced:
            errors.append(
                f"{section.get('section_id', '?')} introduces unsupported numeric literals: {sorted(introduced)}"
            )
    return errors


def _distribute_budget(total: int, n_sections: int) -> list[int]:
    """Distribute word_budget_total across sections; intro/conclusion are smaller."""
    if n_sections <= 1:
        return [total]
    base = total // n_sections
    budgets = [base] * n_sections
    intro_budget = max(600, int(base * 0.6))
    outro_budget = max(600, int(base * 0.7))
    body_total = total - intro_budget - outro_budget
    n_body = max(1, n_sections - 2)
    body_base = body_total // n_body
    body_remainder = body_total - body_base * n_body
    budgets[0] = intro_budget
    budgets[-1] = outro_budget
    for i in range(1, n_sections - 1):
        budgets[i] = body_base + (1 if i - 1 < body_remainder else 0)
    return budgets


def _top_labels(charter: dict, concept_map_summary: dict) -> list[str]:
    labels = list(concept_map_summary.get("top_labels", []))[:6]
    if not labels:
        goals = charter.get("structural_goals") or []
        labels = [_compact(g, 60) for g in goals[:4] if g]
    return labels or ["core mechanisms", "experimental approaches", "applications", "open challenges"]


def _compact_mentor_advice(mentor_advice: dict) -> dict:
    """Expose writing guidance, not the mentor's large internal retrieval audit."""
    moves = []
    for move in (mentor_advice.get("usable_intellectual_moves") or [])[:28]:
        if not isinstance(move, dict):
            continue
        moves.append({
            "category": _compact(move.get("category"), 80),
            "borrowed_pattern": _compact(move.get("borrowed_pattern"), 500),
        })
    return {
        "mentor_role": "organizational_patterns_only_not_scientific_content",
        "usable_intellectual_moves": moves,
        "visual_argument_advice": [
            _compact(x, 350) for x in (mentor_advice.get("visual_argument_advice") or [])[:6]
        ],
        "quality_risks": [
            _compact(x, 350) for x in (mentor_advice.get("quality_risks") or [])[:6]
        ],
    }


# ---------------------------------------------------------------------------
# Placeholder blueprint generators
# ---------------------------------------------------------------------------

def _build_bp_a(charter: dict, top_labels: list[str], word_budget: int, mentor_moves: list[dict]) -> dict:
    """Argument-First blueprint."""
    central_q = _compact(charter.get("central_question", ""), 200)
    label_a = top_labels[0] if top_labels else "mechanisms"
    label_b = top_labels[1] if len(top_labels) > 1 else "evidence base"
    label_c = top_labels[2] if len(top_labels) > 2 else "competing approaches"
    label_d = top_labels[3] if len(top_labels) > 3 else "applications and limits"

    sections_raw = [
        {
            "section_title": "Problem Framing: Why This Question Demands a Review",
            "central_claim": _provisional(
                f"The field may lack a unified framework reconciling {label_a} with {label_b},"
                f" motivating this review.",
                [
                    f"Survey literature confirming the {label_a}–{label_b} reconciliation gap",
                    "Quantitative scope indicator (e.g., publication count, active groups)",
                ],
            ),
            "purpose": "Establish the intellectual gap and motivate the review's central question.",
            "m1_patterns_applied": _pick_patterns(mentor_moves, ["problem_reframing", "central_thesis"], 2),
        },
        {
            "section_title": f"Core Thesis: A Candidate Account of {label_a}",
            "central_claim": _provisional(
                f"Available findings point toward a coherent account of {label_a}"
                f" that may integrate {label_b}, but cross-system replication is needed.",
                [
                    f"Primary experimental data directly on {label_a}",
                    "At least one independent replication or cross-system test",
                ],
            ),
            "purpose": "State and defend the review's load-bearing claim.",
            "m1_patterns_applied": _pick_patterns(mentor_moves, ["central_thesis", "synthesis_moves"], 2),
        },
        {
            "section_title": f"Evidence Synthesis: What the {label_b} Supports",
            "central_claim": _provisional(
                f"Current {label_b} offers partial support for the core thesis"
                f" across at least two experimental paradigms; further confirmation is required.",
                [
                    f"At least two independent studies on {label_b} that address the core thesis",
                    "Quantitative or semi-quantitative result enabling cross-study comparison",
                ],
            ),
            "purpose": "Marshal the evidence that bears on the central claim.",
            "m1_patterns_applied": _pick_patterns(mentor_moves, ["evidence_critique", "synthesis_moves"], 2),
        },
        {
            "section_title": f"Competing Accounts: Where {label_c} Faces Challenges",
            "central_claim": _provisional(
                f"Existing accounts of {label_c} face at least one unresolved criterion;"
                f" whether the review thesis explains this is to be assessed.",
                [
                    f"Direct comparison study between the review thesis and {label_c}",
                    "Identification of at least one criterion on which accounts diverge",
                ],
            ),
            "purpose": "Evaluate alternative frameworks and assess relative explanatory power.",
            "m1_patterns_applied": _pick_patterns(mentor_moves, ["disagreement_handling", "evidence_critique"], 2),
        },
        {
            "section_title": "Translation Across Systems and Operating Conditions",
            "central_claim": _provisional(
                "Whether the proposed account transfers across systems and operating conditions "
                "must be established from comparable evidence rather than assumed.",
                [
                    "Cross-system evidence evaluated under explicit boundary conditions",
                    "Examples showing where transfer succeeds, fails, or remains untested",
                ],
            ),
            "purpose": "Test how the developing account changes across systems, scales, and operating conditions.",
            "m1_patterns_applied": _pick_patterns(mentor_moves, ["synthesis_moves", "evidence_critique"], 2),
        },
        {
            "section_title": f"Scope and Limits: {label_d}",
            "central_claim": _provisional(
                f"The core thesis applies within specific boundary conditions;"
                f" {label_d} marks where current evidence runs out.",
                [
                    "Evidence on failure modes or out-of-scope cases",
                    "Studies that explicitly test the limits of the proposed account",
                ],
            ),
            "purpose": "Bound the claims honestly and identify open research questions.",
            "m1_patterns_applied": _pick_patterns(mentor_moves, ["gap_characterization", "top_journal_publishability"], 2),
        },
        {
            "section_title": "Cross-Scale and Cross-System Validation",
            "central_claim": _provisional(
                "Whether findings established at one scale or in one system transfer "
                "to adjacent conditions must be assessed from comparable evidence "
                "rather than assumed.",
                [
                    "Comparative evidence across scales or systems under explicit boundary conditions",
                    "Examples where transfer succeeds, fails, or remains untested",
                ],
            ),
            "purpose": "Examine the strength of cross-scale and cross-system validation evidence, including replication, boundary-condition reporting, and missing comparisons.",
            "m1_patterns_applied": _pick_patterns(mentor_moves, ["evidence_critique", "synthesis_moves"], 2),
        },
        {
            "section_title": "Synthesis and Open Challenges",
            "central_claim": _provisional(
                "Three high-priority open challenges follow from the review's findings;"
                " tractability relative to current methods is to be assessed.",
                [
                    "Evidence that the three challenges are genuinely open (no settled consensus)",
                    "Feasibility indicators for at least one proposed research direction",
                ],
            ),
            "purpose": "Close the review with concrete, evidence-grounded directions.",
            "m1_patterns_applied": _pick_patterns(mentor_moves, ["gap_characterization", "synthesis_moves"], 2),
        },
    ]
    budgets = _distribute_budget(word_budget, len(sections_raw))
    sections = [
        {**s, "section_index": i, "section_id": f"S{i + 1:02d}", "estimated_word_budget": budgets[i]}
        for i, s in enumerate(sections_raw)
    ]
    return {
        "candidate_id": "BP-A",
        "structural_logic": "argument_first",
        "one_sentence_rationale": (
            f"Organises the review around the central question '{central_q[:80]}'"
            f" as a sequence of claims, making the intellectual arc visible from section titles."
        ),
        "sections": sections,
        "tradeoffs": {
            "strengths": [
                "Argument arc is transparent: readers see the thesis before reading the evidence.",
                "Claims can be directly evaluated; weak claims become visible early.",
            ],
            "weaknesses": [
                "Risk of premature closure if the field is genuinely contested.",
                "Chronological context is implicit; readers unfamiliar with field history may miss background.",
            ],
        },
    }


def _build_bp_b(charter: dict, top_labels: list[str], word_budget: int, mentor_moves: list[dict]) -> dict:
    """Chronological-Synthesis blueprint."""
    label_a = top_labels[0] if top_labels else "early approaches"
    label_b = top_labels[1] if len(top_labels) > 1 else "refinements"
    label_c = top_labels[2] if len(top_labels) > 2 else "modern methods"

    sections_raw = [
        {
            "section_title": "Introduction: The Question Through Time",
            "period_or_phase": "Framing (all periods)",
            "period_thesis": _provisional(
                "Understanding of this question appears to have shifted across"
                " at least two distinct phases, each linked to new experimental capabilities.",
                [
                    "Historical survey identifying at least two clearly distinct research phases",
                    "Evidence that transitions between phases were capability-driven",
                ],
            ),
            "purpose": "Frame the review as a developmental narrative and state what the synthesis reveals.",
            "m1_patterns_applied": _pick_patterns(mentor_moves, ["problem_reframing", "section_progression"], 2),
        },
        {
            "section_title": f"Foundations: Early Work on {label_a}",
            "period_or_phase": "Foundational period (dates to be established from sources)",
            "period_thesis": _provisional(
                f"Early work on {label_a} established a phenomenological baseline;"
                f" mechanistic explanation was largely absent.",
                [
                    f"Representative foundational studies on {label_a}",
                    "Documentation of what questions remained open after this period",
                ],
            ),
            "purpose": "Establish what was known and unknown at the start of systematic study.",
            "m1_patterns_applied": _pick_patterns(mentor_moves, ["taxonomy_design", "evidence_critique"], 2),
        },
        {
            "section_title": f"Development: Mechanistic Models and {label_b}",
            "period_or_phase": "Mechanistic-development period (dates to be established from sources)",
            "period_thesis": _provisional(
                f"Mechanistic modelling of {label_b} resolved selected phenomenological puzzles"
                f" while exposing new scalability challenges.",
                [
                    f"Studies from this period on {label_b} that directly addressed open questions",
                    "Evidence of new challenges that emerged alongside mechanistic progress",
                ],
            ),
            "purpose": "Trace how the field moved from description toward explanation.",
            "m1_patterns_applied": _pick_patterns(mentor_moves, ["synthesis_moves", "disagreement_handling"], 2),
        },
        {
            "section_title": f"Maturation: {label_c} and Evidence Convergence",
            "period_or_phase": "Maturation period (dates to be established from sources)",
            "period_thesis": _provisional(
                f"{label_c} enabled cross-group validation; evidence is converging"
                f" though not yet fully settled.",
                [
                    f"Cross-lab or cross-system validation studies involving {label_c}",
                    "Quantitative indicators of convergence (e.g., meta-analysis, benchmark)",
                ],
            ),
            "purpose": "Characterise the current state of the evidence base and where consensus is forming.",
            "m1_patterns_applied": _pick_patterns(mentor_moves, ["evidence_critique", "synthesis_moves"], 2),
        },
        {
            "section_title": "Translation from Controlled Evidence to Operating Contexts",
            "period_or_phase": "Translational period (dates to be established from sources)",
            "period_thesis": _provisional(
                "The extent to which mature findings retained their meaning outside controlled "
                "conditions remains a question to test across applications and scales.",
                [
                    "Studies comparing controlled and operational conditions",
                    "Evidence identifying boundary conditions for successful translation",
                ],
            ),
            "purpose": "Examine when established findings translated beyond their original experimental context.",
            "m1_patterns_applied": _pick_patterns(mentor_moves, ["evidence_critique", "gap_characterization"], 2),
        },
        {
            "section_title": "Persistent Disagreements and Unresolved Questions",
            "period_or_phase": "Current frontier",
            "period_thesis": _provisional(
                "Several disagreements have persisted across periods and reflect"
                " genuine underdetermination by current evidence.",
                [
                    "Studies that document the same disagreement across at least two periods",
                    "Evidence that disagreements are data-limited, not merely terminological",
                ],
            ),
            "purpose": "Distinguish productive open questions from stale debates.",
            "m1_patterns_applied": _pick_patterns(mentor_moves, ["gap_characterization", "disagreement_handling"], 2),
        },
        {
            "section_title": "Frontier Methods and Emerging Capabilities",
            "period_or_phase": "Frontier methods (dates to be established from sources)",
            "period_thesis": _provisional(
                "New measurement, modelling, or fabrication capabilities are expanding "
                "what can be tested; whether they resolve older disagreements is an "
                "open question.",
                [
                    "Studies describing frontier methods and the questions they newly make tractable",
                    "Evidence on whether frontier methods have already changed prior conclusions",
                ],
            ),
            "purpose": "Examine how frontier methods change the questions the field can answer.",
            "m1_patterns_applied": _pick_patterns(mentor_moves, ["evidence_critique", "synthesis_moves"], 2),
        },
        {
            "section_title": "Synthesis: What the Developmental Record Establishes",
            "period_or_phase": "Cross-period synthesis",
            "period_thesis": _provisional(
                "The developmental phases collectively point toward a partial framework"
                " with identified gaps; cross-period integration may reveal structural lessons.",
                [
                    "At least one comparative analysis spanning multiple periods",
                    "Identification of which open questions have become more tractable over time",
                ],
            ),
            "purpose": "Integrate across periods into a unified picture and identify what comes next.",
            "m1_patterns_applied": _pick_patterns(mentor_moves, ["synthesis_moves", "top_journal_publishability"], 2),
        },
    ]
    budgets = _distribute_budget(word_budget, len(sections_raw))
    sections = [
        {**s, "section_index": i, "section_id": f"S{i + 1:02d}", "estimated_word_budget": budgets[i]}
        for i, s in enumerate(sections_raw)
    ]
    return {
        "candidate_id": "BP-B",
        "structural_logic": "chronological_synthesis",
        "one_sentence_rationale": (
            "Traces how understanding evolved across recognisable phases, making it easy to show"
            " what each generation of work contributed and left open."
        ),
        "sections": sections,
        "tradeoffs": {
            "strengths": [
                "Natural narrative flow; context accumulates progressively.",
                "Disagreements are dateable: readers see when and why positions shifted.",
            ],
            "weaknesses": [
                "Risk of narrative fallacy: developmental framing can imply progress where there is only change.",
                "Readers seeking a current-state answer must synthesise across sections themselves.",
            ],
        },
    }


def _build_bp_c(charter: dict, top_labels: list[str], word_budget: int, mentor_moves: list[dict]) -> dict:
    """Taxonomic-Contrast blueprint."""
    taxa = top_labels[:4] if len(top_labels) >= 4 else (
        top_labels + ["Approach D"] * (4 - len(top_labels))
    )

    sections_raw = [
        {
            "section_title": "Introduction and Taxonomy Overview",
            "purpose": "Introduce the classification axis and explain why this taxonomy is informative for the central question.",
            "m1_patterns_applied": _pick_patterns(mentor_moves, ["taxonomy_design", "problem_reframing"], 2),
        },
        {
            "section_title": f"Category I: {taxa[0]}",
            "purpose": f"Characterise {taxa[0]}: proposed mechanisms, supporting evidence, representative work, known limits.",
            "taxon_label": taxa[0],
            "m1_patterns_applied": _pick_patterns(mentor_moves, ["evidence_critique", "paragraph_moves"], 2),
        },
        {
            "section_title": f"Category II: {taxa[1]}",
            "purpose": f"Characterise {taxa[1]} using the same evaluation criteria as Category I.",
            "taxon_label": taxa[1],
            "m1_patterns_applied": _pick_patterns(mentor_moves, ["evidence_critique", "paragraph_moves"], 2),
        },
        {
            "section_title": f"Category III: {taxa[2]}",
            "purpose": f"Characterise {taxa[2]} and introduce cross-taxon complications.",
            "taxon_label": taxa[2],
            "m1_patterns_applied": _pick_patterns(mentor_moves, ["synthesis_moves", "evidence_critique"], 2),
        },
        {
            "section_title": f"Category IV: {taxa[3]}",
            "purpose": f"Characterise {taxa[3]} and show where it overlaps with or challenges earlier categories.",
            "taxon_label": taxa[3],
            "m1_patterns_applied": _pick_patterns(mentor_moves, ["synthesis_moves", "evidence_critique"], 2),
        },
        {
            "section_title": "Hybrid Approaches and Boundary Cases",
            "purpose": "Examine approaches that span category boundaries and cases where the taxonomy's dimensions overlap.",
            "taxon_label": "hybrid / boundary",
            "m1_patterns_applied": _pick_patterns(mentor_moves, ["synthesis_moves", "disagreement_handling"], 2),
        },
        {
            "section_title": "Cross-Category Comparison and Synthesis",
            "purpose": "Compare all categories on a common set of criteria; resolve apparent overlaps; draw a unified conclusion.",
            "m1_patterns_applied": _pick_patterns(mentor_moves, ["synthesis_moves", "top_journal_publishability"], 2),
        },
        {
            "section_title": "Gaps, Hybrids, and Future Directions",
            "purpose": "Identify what the taxonomy misses, where category boundaries are contested, and what new categories may emerge.",
            "m1_patterns_applied": _pick_patterns(mentor_moves, ["gap_characterization", "synthesis_moves"], 2),
        },
    ]
    # Remove None taxon_labels
    for s in sections_raw:
        if s.get("taxon_label") is None:
            s.pop("taxon_label", None)
    budgets = _distribute_budget(word_budget, len(sections_raw))
    sections = [
        {**s, "section_index": i, "section_id": f"S{i + 1:02d}", "estimated_word_budget": budgets[i]}
        for i, s in enumerate(sections_raw)
    ]
    return {
        "candidate_id": "BP-C",
        "structural_logic": "taxonomic_contrast",
        "taxonomy_root": f"Primary classification axis: {taxa[0]} vs. {taxa[1]} vs. {taxa[2]} vs. {taxa[3]}",
        "one_sentence_rationale": (
            "Organises the review around a classification of approaches, making inter-category"
            " comparisons systematic and exposing where the taxonomy breaks down."
        ),
        "sections": sections,
        "tradeoffs": {
            "strengths": [
                "Systematic comparison: each category is evaluated on the same criteria.",
                "Easy for practitioners to navigate to the approach most relevant to them.",
            ],
            "weaknesses": [
                "Taxonomy may reify category boundaries that the literature does not actually observe.",
                "Historical context is lost; readers may not understand why certain categories emerged.",
            ],
        },
    }


def _pick_patterns(mentor_moves: list[dict], preferred_categories: list[str], n: int = 2) -> list[str]:
    """Pick n pattern labels from mentor_moves preferring given categories."""
    out: list[str] = []
    for cat in preferred_categories:
        for m in mentor_moves:
            if m.get("category") == cat:
                rule = m.get("borrowed_pattern") or m.get("transferable_rule") or m.get("move") or ""
                label = _compact(rule, 80)
                if label and label not in out:
                    out.append(label)
                    break
    for m in mentor_moves:
        if len(out) >= n:
            break
        rule = m.get("borrowed_pattern") or m.get("transferable_rule") or m.get("move") or ""
        label = _compact(rule, 80)
        if label and label not in out:
            out.append(label)
    return out[:n]


# ---------------------------------------------------------------------------
# BlueprintCouncil
# ---------------------------------------------------------------------------

@dataclass
class BlueprintCouncil:
    """Generates three structurally distinct candidate blueprints (BP-A, BP-B, BP-C)."""

    prompt_path: Path = field(default_factory=lambda: DEFAULT_COUNCIL_PROMPT)
    # Review architecture is a high-leverage decision: later retrieval,
    # evidence mapping, writing, and revision all inherit its structure.
    model_tier: str = "premium_model"
    real_llm: bool = False

    def generate_candidates(
        self,
        *,
        charter: dict,
        concept_map_summary: dict | None = None,
        mentor_advice: dict | None = None,
        run_id: str = "",
    ) -> dict[str, Any]:
        """Return a blueprint_candidates.v1 dict with three candidates."""
        concept_map_summary = concept_map_summary or {}
        mentor_advice = mentor_advice or {}

        word_budget: int = int(
            (charter.get("constraints") or {}).get("word_budget_total", 10000)
        )
        top = _top_labels(charter, concept_map_summary)
        mentor_moves: list[dict] = list(mentor_advice.get("usable_intellectual_moves") or [])

        if self.real_llm:
            return self._llm_candidates(
                charter=charter,
                concept_map_summary=concept_map_summary,
                mentor_advice=mentor_advice,
                run_id=run_id,
                word_budget=word_budget,
                top_labels=top,
                mentor_moves=mentor_moves,
            )

        bp_a = _build_bp_a(charter, top, word_budget, mentor_moves)
        bp_b = _build_bp_b(charter, top, word_budget, mentor_moves)
        bp_c = _build_bp_c(charter, top, word_budget, mentor_moves)
        candidates = [
            _normalise_candidate(c, charter, require_division_fields=False)
            for c in (bp_a, bp_b, bp_c)
        ]
        source_context = " ".join(
            [
                str(charter.get("central_question", "")),
                str(charter.get("scope_statement", "")),
                " ".join(map(str, top)),
            ]
        )
        validation_errors = [
            error for candidate in candidates
            for error in _unsupported_numeric_claims(candidate, source_context)
        ]
        if validation_errors:
            raise ValueError("Deterministic blueprint introduced unsupported numbers: " + "; ".join(validation_errors))

        output = {
            "schema_version": SCHEMA_VERSION,
            "created_at": utc_now(),
            "run_id": run_id,
            "mode": DETERMINISTIC_NON_PRODUCTION_MODE,
            "production": False,
            "non_production_fallback": True,
            "admission_decision": "reject",
            "admission": {
                "decision": "reject",
                "production": False,
                "non_production_deterministic": True,
                "qwen_architecture_present": False,
                "reason": (
                    "Deterministic candidates are test/offline only and are "
                    "never admitted to the production S7-S9 mainline."
                ),
            },
            "non_production_reason": (
                "Deterministic blueprint generation is a test/offline provider "
                "only; production requires the Qwen council and rejects any "
                "deterministic scientific outline."
            ),
            "candidates_sha256": _sha256_candidates(candidates),
            "candidates": candidates,
        }
        from optomind_research.intermediate_language_guard import ensure_english_payload
        return ensure_english_payload(output)

    # ------------------------------------------------------------------
    # Real LLM path
    # ------------------------------------------------------------------

    def _llm_candidates(
        self,
        *,
        charter: dict,
        concept_map_summary: dict,
        mentor_advice: dict,
        run_id: str,
        word_budget: int,
        top_labels: list[str],
        mentor_moves: list[dict],
    ) -> dict[str, Any]:
        if not Path(self.prompt_path).exists() or not DEFAULT_CANDIDATE_PROMPT.exists():
            raise FileNotFoundError(
                f"BlueprintCouncil prompt not found: {self.prompt_path} or {DEFAULT_CANDIDATE_PROMPT}"
            )
        candidate_prompt_path = DEFAULT_CANDIDATE_PROMPT
        system_prompt = candidate_prompt_path.read_text(encoding="utf-8").strip()
        command_knowledge = _command_knowledge_block()
        system_prompt = (
            system_prompt
            + "\n\n"
            + "[COMMAND_KNOWLEDGE (versioned skill guidance; not scientific "
            "evidence)]\n"
            + command_knowledge["prompt_block"]
        )
        base_payload = {
            "charter": charter,
            "concept_map_summary": concept_map_summary,
            "mentor_advice": _compact_mentor_advice(mentor_advice),
            "m1_case_moves": _m1_case_moves_payload(mentor_advice),
            "command_knowledge": command_knowledge,
            "word_budget": word_budget,
            "top_labels": top_labels,
        }

        logic_by_id = {
            "BP-A": "argument_first",
            "BP-B": "chronological_synthesis",
            "BP-C": "taxonomic_contrast",
        }
        source_context = " ".join(
            [
                str(charter.get("central_question", "")),
                str(charter.get("scope_statement", "")),
                " ".join(map(str, top_labels)),
            ]
        )

        def call_one(candidate_id: str) -> tuple[dict, list[dict[str, Any]]]:
            from llm.qwen_chat_client import call_qwen_chat

            attempts: list[dict[str, Any]] = []
            last_validation_errors: list[str] = []
            last_error = ""
            for attempt in range(3):
                payload = {
                    **base_payload,
                    "requested_candidate_id": candidate_id,
                    "requested_structural_logic": logic_by_id[candidate_id],
                    "single_candidate_mode": True,
                }
                if attempt:
                    payload["repair_instruction"] = (
                        "The previous response was missing or malformed. Return one complete candidate "
                        "under the key 'candidate'; keep each field concise and preserve all required fields. "
                        "Remove every unsupported numerical or historical value. Previous validation: "
                        + (
                            "; ".join(last_validation_errors[:4])
                            if last_validation_errors
                            else last_error
                        )
                    )
                try:
                    response = call_qwen_chat(
                        f"BlueprintCouncil-{candidate_id}",
                        [
                            {"role": "system", "content": system_prompt},
                            {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
                        ],
                        model_tier=self.model_tier,
                        temperature=0.2,
                        # An 8-10-chapter publication blueprint is commonly
                        # longer than the legacy single-chapter 4.2k-token
                        # allowance.  Truncating it creates syntactically
                        # invalid JSON and wastes the complete model call.
                        max_tokens=12000,
                        response_format={"type": "json_object"},
                        # Publication-scale JSON can take longer to finish than
                        # an idle reverse-proxy timeout.  Streaming keeps the
                        # connection active while preserving the same schema.
                        stream=True,
                        accept_partial_stream=False,
                        timeout_seconds=300,
                        force_mock=False,
                        max_retries=1,
                    )
                    raw = str(response.get("content") or "")
                    usage = response.get("_llm_usage") or {}
                    if not bool(usage.get("success", True)):
                        raise RuntimeError(
                            "model_call_failed: "
                            + str(usage.get("error_type") or raw or "unknown error")
                        )
                    parsed = _safe_json_parse(raw)
                    candidate = parsed.get("candidate")
                    if not isinstance(candidate, dict):
                        rows = parsed.get("candidates") or []
                        candidate = rows[0] if len(rows) == 1 and isinstance(rows[0], dict) else None
                    attempts.append({"attempt": attempt + 1, "raw_chars": len(raw), "parsed": bool(parsed)})
                    if (
                        isinstance(candidate, dict)
                        and candidate.get("candidate_id") == candidate_id
                        and candidate.get("structural_logic") == logic_by_id[candidate_id]
                    ):
                        normalised = _normalise_candidate(
                            candidate,
                            charter,
                            require_division_fields=True,
                        )
                        last_validation_errors = _unsupported_numeric_claims(
                            normalised, source_context
                        )
                        if last_validation_errors:
                            raise ValueError("; ".join(last_validation_errors))
                        return normalised, attempts
                except Exception as exc:
                    last_error = f"{type(exc).__name__}: {exc}"
                    attempts.append({"attempt": attempt + 1, "error": last_error})
            raise RuntimeError(f"{candidate_id} generation failed: {attempts}")

        candidates_by_id: dict[str, dict] = {}
        diagnostics: dict[str, list[dict[str, Any]]] = {}
        errors: list[str] = []
        with ThreadPoolExecutor(max_workers=3) as pool:
            futures = {pool.submit(call_one, cid): cid for cid in logic_by_id}
            for future in as_completed(futures):
                cid = futures[future]
                try:
                    candidate, attempts = future.result()
                    candidates_by_id[cid] = candidate
                    diagnostics[cid] = attempts
                except Exception as exc:
                    errors.append(f"{cid}: {exc}")
        if len(candidates_by_id) < 2:
            raise RuntimeError(
                "BlueprintCouncil produced fewer than two valid, structurally distinct candidates; "
                + " | ".join(errors)
            )

        candidates = [candidates_by_id[cid] for cid in ("BP-A", "BP-B", "BP-C") if cid in candidates_by_id]
        missing_ids = [cid for cid in ("BP-A", "BP-B", "BP-C") if cid not in candidates_by_id]
        output = {
            "schema_version": SCHEMA_VERSION,
            "created_at": utc_now(),
            "mode": "real_llm_parallel" if not missing_ids else "real_llm_parallel_degraded",
            "production": True,
            "non_production_fallback": False,
            "admission_decision": "admit",
            "admission": {
                "decision": "admit",
                "production": True,
                "non_production_deterministic": False,
                "qwen_architecture_present": True,
                "reason": (
                    "Qwen council candidates passed the 8-10 chapter and "
                    "division-of-labor contract; final S8 unification must "
                    "re-affirm admission."
                ),
            },
            "authoritative_section_range": list(AUTHORITATIVE_SECTION_RANGE),
            "run_id": run_id,
            "candidates_sha256": _sha256_candidates(candidates),
            "generation_diagnostics": diagnostics,
            "generation_warnings": errors,
            "missing_candidate_ids": missing_ids,
            "candidates": candidates,
        }
        from optomind_research.intermediate_language_guard import ensure_english_payload
        return ensure_english_payload(output)
