"""BlueprintTournamentJudge — S8: compare three candidates, select and unify a blueprint.

needs_human flow:
  First call  → generate recommendation → write stage-dir/blueprint_recommendation.json
               → return needs_human (pipeline pauses)
  Resume call → check stage-dir/blueprint_override.json (optional human edit)
               → if no override: auto-accept recommendation
               → write attempt_N/selected_blueprint.json → return completed

Human override format (blueprint_override.json):
  {
    "choice_id": "BP-A" | "BP-B" | "BP-C",   (optional; overrides auto-selected)
    "notes": "free text"                        (optional)
  }
  Creating this file is OPTIONAL. If absent, the auto-recommendation is used.

F2 changes:
  - 7-dimension scoring: scope_coverage, argument_arc, m1_positions, section_overlap,
    visual_provisions, budget_distribution, structural_risk
  - Fixed boolean precedence bug in scope_coverage scoring
  - _apply_override rebuilds unified_blueprint from chosen candidate (F2-1)
  - finalize accepts candidates_by_id for blueprint reconstruction
  - candidates_sha256 stored in recommendation for staleness detection (F2-6)
  - Real LLM path wired via call_qwen_chat
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from optomind_research.artifact_registry import utc_now


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_JUDGE_PROMPT = PROJECT_ROOT / "prompts" / "Blueprint Tournament Judge.txt"

SCHEMA_VERSION_REC = "blueprint_recommendation.v1"
SCHEMA_VERSION_SEL = "selected_blueprint.v1"
BLUEPRINT_SCHEMA = "dynamic_review_blueprint.v4"

_VALID_IDS = {"BP-A", "BP-B", "BP-C"}
# Authoritative chapter-count contract: accepted blueprints contain 8-10
# chapters.  Qwen owns which chapters and their scientific organization; this
# module rejects recommendations outside the range and never repairs a count by
# inventing a deterministic outline.
AUTHORITATIVE_SECTION_RANGE: tuple[int, int] = (8, 10)
DETERMINISTIC_NON_PRODUCTION_MODE = "deterministic_non_production"


def _command_knowledge_block() -> dict[str, Any]:
    """Load ONLY the versioned architecture-planning command bundle.

    Authoring, audit, and manuscript-integration manuals belong to their own
    downstream roles and are never injected into the judge payload.
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


def _validate_section_count(candidates: list[dict]) -> None:
    """Reject any candidate/blueprint outside the authoritative 8-10 range."""
    for candidate in candidates:
        sections = candidate.get("sections") or []
        count = len(sections)
        if not AUTHORITATIVE_SECTION_RANGE[0] <= count <= AUTHORITATIVE_SECTION_RANGE[1]:
            raise ValueError(
                f"BlueprintTournamentJudge: accepted blueprints must contain "
                f"{AUTHORITATIVE_SECTION_RANGE[0]}-{AUTHORITATIVE_SECTION_RANGE[1]} "
                f"chapters; candidate {candidate.get('candidate_id', '?')} has {count}."
            )


def _attach_section_workplan_context(sections: list[dict]) -> list[dict]:
    """Package the complete division of labor onto every section (deterministic).

    This mirrors the planner's packaging: each chapter sees every chapter's
    responsibilities, its own boundary contract, and its siblings' summaries,
    must_not_cover boundaries, and previous/next handoffs.
    """
    workplan = [
        {
            "section_id": str(section.get("section_id") or ""),
            "title": _compact(section.get("section_title") or section.get("title"), 180),
            "argument_role": _compact(section.get("argument_role"), 520),
            "unique_contribution": _compact(
                section.get("unique_contribution")
                or section.get("novel_contribution_to_review"),
                600,
            ),
            "must_cover": [
                item for item in (section.get("must_cover") or []) if str(item).strip()
            ],
            "must_not_cover": [
                item for item in (section.get("must_not_cover") or []) if str(item).strip()
            ],
            "assigned_user_axes": [
                item for item in (section.get("assigned_user_axes") or []) if str(item).strip()
            ],
            "key_questions": [
                item for item in (section.get("key_questions") or []) if str(item).strip()
            ],
            "handoff_from_previous": _compact(section.get("handoff_from_previous"), 420),
            "handoff_to_next": _compact(
                section.get("handoff_to_next")
                or section.get("transition_to_next"),
                420,
            ),
        }
        for section in sections
    ]
    for section in sections:
        section_id = str(section.get("section_id") or "")
        section["current_section_boundary_contract"] = {
            "section_id": section_id,
            "title": _compact(section.get("section_title") or section.get("title"), 180),
            "argument_role": _compact(section.get("argument_role"), 520),
            "unique_contribution": _compact(
                section.get("unique_contribution")
                or section.get("novel_contribution_to_review"),
                600,
            ),
            "must_cover": [
                item for item in (section.get("must_cover") or []) if str(item).strip()
            ],
            "must_not_cover": [
                item for item in (section.get("must_not_cover") or []) if str(item).strip()
            ],
            "assigned_user_axes": [
                item for item in (section.get("assigned_user_axes") or []) if str(item).strip()
            ],
            "key_questions": [
                item for item in (section.get("key_questions") or []) if str(item).strip()
            ],
            "handoff_from_previous": _compact(section.get("handoff_from_previous"), 420),
            "handoff_to_next": _compact(
                section.get("handoff_to_next")
                or section.get("transition_to_next"),
                420,
            ),
        }
        section["sibling_section_responsibilities"] = [
            row
            for row in workplan
            if str(row.get("section_id") or "") != section_id
        ]
        section["full_section_workplan"] = list(workplan)
    return sections


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


def _tokens(value: Any) -> set[str]:
    stop = {"the", "and", "for", "with", "from", "that", "this", "into", "review", "section"}
    return {t for t in re.findall(r"[a-z][a-z0-9_-]{2,}", str(value or "").lower()) if t not in stop}


def _claim_text(value: Any) -> str:
    return str(value.get("text", "") if isinstance(value, dict) else value or "")


def _compact_mentor_advice(mentor_advice: dict) -> dict:
    return {
        "mentor_role": "organizational_patterns_only_not_scientific_content",
        "usable_intellectual_moves": [
            {
                "category": _compact(m.get("category"), 80),
                "borrowed_pattern": _compact(m.get("borrowed_pattern"), 500),
            }
            for m in (mentor_advice.get("usable_intellectual_moves") or [])[:28]
            if isinstance(m, dict)
        ],
        "quality_risks": [_compact(x, 350) for x in (mentor_advice.get("quality_risks") or [])[:6]],
    }


# ---------------------------------------------------------------------------
# Scoring helpers  (F2-5: 7 dimensions, max 5 each = 35 total)
# ---------------------------------------------------------------------------

def _score_candidate(candidate: dict, charter: dict, mentor_advice: dict) -> dict[str, int]:
    """7-dimension scoring heuristics."""
    structural_logic = candidate.get("structural_logic", "")
    goals = charter.get("structural_goals") or []
    central_q = _compact(charter.get("central_question", ""), 200).lower()
    sections = candidate.get("sections") or []
    n_sections = len(sections)
    budget_range = (charter.get("constraints") or {}).get(
        "section_count_range", list(AUTHORITATIVE_SECTION_RANGE)
    )
    min_sec, max_sec = (
        int(budget_range[0]),
        int(budget_range[1]),
    )
    moves = mentor_advice.get("usable_intellectual_moves") or []
    goals_text = " ".join(goals).lower()

    # 1. scope_coverage: infer the structure requested by the actual question.
    scope_score = 3
    query_text = f"{central_q} {goals_text}"
    historical = any(k in query_text for k in ("history", "historical", "evolution", "development over time"))
    comparative = any(k in query_text for k in ("compare", "comparison", "types", "classes", "approaches", "taxonomy"))
    explanatory = any(k in query_text for k in ("why", "how", "mechanism", "meaning", "challenge", "gap", "limitation"))
    if structural_logic == "argument_first" and explanatory:
        scope_score = 5
    elif structural_logic == "chronological_synthesis" and historical:
        scope_score = 5
    elif structural_logic == "taxonomic_contrast" and comparative:
        scope_score = 5
    elif (historical, comparative, explanatory).count(True) == 0:
        scope_score = 4

    # 2. argument_arc: clarity plus manuscript-role integrity.  A specialist
    # first chapter cannot silently double as the introduction, and a roadmap
    # cannot hand off to a conclusion that does not exist.
    section_titles = [_compact(s.get("section_title", ""), 100).lower() for s in sections]
    visible_targets = sum(
        bool(_claim_text(s.get("planned_thesis") or s.get("central_claim") or s.get("period_thesis")))
        for s in sections
    )
    transitions = sum(bool(s.get("transition_to_next")) for s in sections[:-1])
    role_sequence = [str(s.get("section_role") or "").lower() for s in sections]
    role_integrity = bool(
        role_sequence
        and role_sequence[0] == "introduction"
        and role_sequence[-1] == "synthesis"
        and all(role == "body" for role in role_sequence[1:-1])
    )
    arc_score = min(
        5,
        1
        + int(visible_targets >= max(1, n_sections - 1))
        + int(transitions == max(0, n_sections - 1))
        + 2 * int(role_integrity),
    )

    # 3. m1_positions: M1 moves distributed across sections
    available_patterns = {
        str(m.get("borrowed_pattern") or m.get("transferable_rule") or m.get("move") or "")
        for m in moves
    } - {""}
    used_patterns = {
        str(p) for s in sections for p in (s.get("m1_patterns_applied") or []) if p
    }
    valid_used = used_patterns & available_patterns if available_patterns else set()
    coverage = len(valid_used) / max(1, min(len(available_patterns), n_sections))
    m1_score = min(5, 2 + round(3 * coverage))

    # 4. section_overlap: penalise redundant section purposes
    purpose_tokens = [_tokens(s.get("purpose", "")) for s in sections]
    similarities = []
    for i, left in enumerate(purpose_tokens):
        for right in purpose_tokens[i + 1:]:
            similarities.append(len(left & right) / max(1, len(left | right)))
    max_overlap = max(similarities, default=0.0)
    overlap_score = 5 if max_overlap < 0.25 else (4 if max_overlap < 0.4 else (3 if max_overlap < 0.6 else 2))

    # 5. visual_provisions: blueprint suggests visual components
    visual_hits = sum(bool(s.get("expected_visual_arguments")) for s in sections)
    visual_score = min(5, 2 + round(3 * visual_hits / max(1, n_sections)))

    # 6. budget_distribution: section count in range; all sections have a budget
    budget_score = 4 if min_sec <= n_sections <= max_sec else 3
    budgets = [int(s.get("estimated_word_budget", 0)) for s in sections]
    if budgets and all(b > 0 for b in budgets):
        budget_score = min(5, budget_score + 1)

    # 7. structural_risk: honest, specific weaknesses are safer than missing risk analysis.
    weaknesses = (candidate.get("tradeoffs") or {}).get("weaknesses") or []
    specific = sum(len(_tokens(w)) >= 5 for w in weaknesses)
    risk_score = min(5, 2 + specific)
    if not role_integrity:
        risk_score = min(risk_score, 2)

    return {
        "scope_coverage": scope_score,
        "argument_arc": arc_score,
        "m1_positions": m1_score,
        "section_overlap": overlap_score,
        "visual_provisions": visual_score,
        "budget_distribution": budget_score,
        "structural_risk": risk_score,
    }


def _total_score(scores: dict[str, int]) -> int:
    return sum(scores.values())


# ---------------------------------------------------------------------------
# Blueprint reconstruction helper (F2-1)
# ---------------------------------------------------------------------------

def _rebuild_unified_blueprint(
    candidate: dict,
    source_id: str,
    *,
    require_concrete_exclusions: bool = False,
) -> dict:
    """Rebuild a unified_blueprint from a specific candidate's sections."""
    _validate_section_count([candidate])
    if require_concrete_exclusions:
        concrete_errors = _concrete_sibling_exclusion_errors(
            candidate.get("sections") or []
        )
        if concrete_errors:
            raise ValueError(
                "BlueprintTournamentJudge: must_not_cover entries must name "
                "concrete sibling-owned responsibilities: "
                + "; ".join(concrete_errors)
            )
    sections = []
    for i, s in enumerate(candidate.get("sections") or []):
        sec = dict(s)
        sec.setdefault("section_id", f"S{i + 1:02d}")
        sec["source_candidate"] = source_id
        sections.append(sec)
    sections = _attach_section_workplan_context(sections)
    return {
        "schema_version": BLUEPRINT_SCHEMA,
        "structural_logic": candidate.get("structural_logic", ""),
        "taxonomy_root": candidate.get("taxonomy_root", ""),
        "sections": sections,
        "authoritative_section_range": list(AUTHORITATIVE_SECTION_RANGE),
        "argument_dag": {"claims": [], "edges": []},
        "research_gaps": [],
    }


# ---------------------------------------------------------------------------
# Recommendation builder
# ---------------------------------------------------------------------------

def _build_recommendation(
    candidates: list[dict],
    charter: dict,
    mentor_advice: dict,
) -> dict[str, Any]:
    """Score all three candidates; select the winner; check for combination opportunities."""
    _validate_section_count(candidates)
    by_id = {c["candidate_id"]: c for c in candidates}
    scores: dict[str, dict[str, int]] = {}
    for c in candidates:
        cid = c["candidate_id"]
        scores[cid] = _score_candidate(c, charter, mentor_advice)

    ranked = sorted(
        by_id.keys(),
        key=lambda cid: (
            _total_score(scores[cid]),
            scores[cid]["scope_coverage"],
            scores[cid]["argument_arc"],
            cid,
        ),
        reverse=True,
    )
    winner_id = ranked[0]
    winner = by_id[winner_id]

    combination_notes = ""
    combined = False
    unified_sections = []
    for i, s in enumerate(winner.get("sections") or []):
        sec = dict(s)
        sec.setdefault("section_id", f"S{i + 1:02d}")
        sec.setdefault("source_candidate", winner_id)
        unified_sections.append(sec)

    # Borrow final synthesis section if winner's arc score < 4
    for other_id in ranked[1:]:
        other = by_id[other_id]
        other_sections = other.get("sections") or []
        if not other_sections:
            continue
        if scores[winner_id]["argument_arc"] < 4 and unified_sections:
            last_other = other_sections[-1]
            last_winner = unified_sections[-1]
            if last_other.get("purpose") and last_other.get("purpose") != last_winner.get("purpose"):
                unified_sections[-1] = {
                    **last_winner,
                    "purpose": last_other.get("purpose", last_winner.get("purpose", "")),
                    "source_candidate": f"combined ({winner_id}+{other_id})",
                }
                combination_notes = (
                    f"Final synthesis section purpose borrowed from {other_id} "
                    f"to strengthen the argument arc closure of {winner_id}."
                )
                combined = True
                break

    max_possible = 35  # 7 dimensions × 5
    rejected_reasons = {
        cid: (
            f"Lower composite score ({_total_score(scores[cid])}/{max_possible}) than "
            f"{winner_id} ({_total_score(scores[winner_id])}/{max_possible}); "
            f"primary deficit: {min(scores[cid], key=scores[cid].get)}"
        )
        for cid in ranked[1:]
    }

    rationale_parts = [
        f"BP-A={_total_score(scores.get('BP-A', {}))}, "
        f"BP-B={_total_score(scores.get('BP-B', {}))}, "
        f"BP-C={_total_score(scores.get('BP-C', {}))}/{max_possible}.",
        f"{winner_id} ({winner.get('structural_logic', '')}) has the strongest composite fit; "
        f"its scope score is {scores[winner_id]['scope_coverage']} and argument-arc score is "
        f"{scores[winner_id]['argument_arc']}.",
        f"Its principal weakness "
        f"({((winner.get('tradeoffs') or {}).get('weaknesses') or ['none'])[0][:80]}) "
        f"is manageable given the review constraints.",
    ]
    if combined:
        rationale_parts.append(combination_notes)

    unified_blueprint = {
        "schema_version": BLUEPRINT_SCHEMA,
        "structural_logic": winner.get("structural_logic", ""),
        "taxonomy_root": winner.get("taxonomy_root", ""),
        "sections": _attach_section_workplan_context(unified_sections),
        "authoritative_section_range": list(AUTHORITATIVE_SECTION_RANGE),
        "argument_dag": {"claims": [], "edges": []},
        "research_gaps": [],
    }

    return {
        "schema_version": SCHEMA_VERSION_REC,
        "created_at": utc_now(),
        "mode": DETERMINISTIC_NON_PRODUCTION_MODE,
        "production": False,
        "non_production_fallback": True,
        "admission_decision": "reject",
        "admission": {
            "decision": "reject",
            "production": False,
            "non_production_deterministic": True,
            "qwen_architecture_present": False,
            "section_count": len(unified_blueprint.get("sections") or []),
            "reason": (
                "Deterministic selection is test/offline only and is never "
                "admitted to the production S7-S9 mainline."
            ),
        },
        "non_production_reason": (
            "Deterministic blueprint selection is a test/offline provider "
            "only; production requires the Qwen judge and rejects any "
            "deterministic scientific outline."
        ),
        "authoritative_section_range": list(AUTHORITATIVE_SECTION_RANGE),
        "selected_candidate_id": winner_id,
        "combined": combined,
        "selection_rationale": " ".join(rationale_parts),
        "criterion_scores": scores,
        "rejected_reasons": rejected_reasons,
        "combination_notes": combination_notes,
        "candidates_sha256": _sha256_candidates(candidates),  # F2-6
        "unified_blueprint": unified_blueprint,
    }


def _apply_override(
    recommendation: dict,
    override: dict,
    candidates_by_id: dict | None = None,
) -> dict:
    """Incorporate a human override into the recommendation.

    F2-1: If candidates_by_id is provided and choice_id differs from auto-winner,
    reconstructs unified_blueprint entirely from the chosen candidate's sections.
    """
    choice_id = override.get("choice_id", "")
    notes = override.get("notes", "")
    result = dict(recommendation)
    winner_id = recommendation.get("selected_candidate_id", "")

    if choice_id and choice_id not in _VALID_IDS:
        raise ValueError(f"Unknown blueprint choice_id: {choice_id!r}")
    if choice_id and choice_id != winner_id:
        if not candidates_by_id or choice_id not in candidates_by_id:
            raise ValueError(
                "Human override requires the matching candidate set; refusing to change only the ID."
            )
        result["selected_candidate_id"] = choice_id
        # F2-1: Rebuild blueprint from the candidate the human chose
        result["unified_blueprint"] = _rebuild_unified_blueprint(
            candidates_by_id[choice_id], choice_id
        )
        result["combination_notes"] = (
            f"Human override: selected {choice_id} instead of auto-recommended "
            f"{winner_id}. Notes: {notes}"
        )

    result["human_confirmed"] = True
    result["human_notes"] = notes
    return result


# ---------------------------------------------------------------------------
# BlueprintTournamentJudge
# ---------------------------------------------------------------------------

@dataclass
class BlueprintTournamentJudge:
    """Compares BP-A/B/C, selects and unifies a blueprint.

    needs_human protocol:
      - evaluate_and_recommend() → recommendation dict (to be saved and returned as needs_human)
      - finalize() → selected_blueprint dict (called on resume; uses override if present)
    """

    prompt_path: Path = field(default_factory=lambda: DEFAULT_JUDGE_PROMPT)
    # Candidate selection/unification is an A-tier decision, with the shared
    # model ladder providing A-/B+ fallback if the preferred model is absent.
    model_tier: str = "premium_model"
    real_llm: bool = False

    def evaluate_and_recommend(
        self,
        *,
        candidates: list[dict],
        charter: dict,
        mentor_advice: dict | None = None,
    ) -> dict[str, Any]:
        """Score candidates and produce a recommendation."""
        if self.real_llm:
            return self._llm_recommend(
                candidates=candidates,
                charter=charter,
                mentor_advice=mentor_advice or {},
            )
        result = _build_recommendation(candidates, charter, mentor_advice or {})
        from optomind_research.intermediate_language_guard import ensure_english_payload
        return ensure_english_payload(result)

    @staticmethod
    def finalize(
        recommendation: dict,
        override: dict | None = None,
        candidates_by_id: dict | None = None,
    ) -> dict[str, Any]:
        """Produce the final selected_blueprint.v1 artifact.

        F2-1: candidates_by_id is required to reconstruct blueprint on human override.
        """
        if override:
            rec = _apply_override(recommendation, override, candidates_by_id=candidates_by_id)
        else:
            rec = dict(recommendation)
            rec.setdefault("human_confirmed", False)
            # Ensure section_ids present on auto-accepted blueprint
            ub = rec.get("unified_blueprint") or {}
            for i, s in enumerate(ub.get("sections") or []):
                s.setdefault("section_id", f"S{i + 1:02d}")
        # The final artifact must satisfy the authoritative 8-10 contract even
        # when the recommendation came from a stale or hand-edited artifact.
        final_blueprint = rec.get("unified_blueprint") or {}
        _validate_section_count([final_blueprint])
        production = bool(rec.get("production", False))
        if production:
            concrete_errors = _concrete_sibling_exclusion_errors(
                final_blueprint.get("sections") or []
            )
            if concrete_errors:
                raise ValueError(
                    "BlueprintTournamentJudge: must_not_cover entries must "
                    "name concrete sibling-owned responsibilities: "
                    + "; ".join(concrete_errors)
                )
        final_blueprint["sections"] = _attach_section_workplan_context(
            final_blueprint.get("sections") or []
        )
        rec["unified_blueprint"] = final_blueprint
        admission_decision = "admit" if production else "reject"

        return {
            "schema_version": SCHEMA_VERSION_SEL,
            "created_at": utc_now(),
            "selected_candidate_id": rec.get("selected_candidate_id", ""),
            "human_confirmed": rec.get("human_confirmed", False),
            "human_notes": rec.get("human_notes", ""),
            "selection_rationale": rec.get("selection_rationale", ""),
            "combination_notes": rec.get("combination_notes", ""),
            "criterion_scores": rec.get("criterion_scores", {}),
            "rejected_reasons": rec.get("rejected_reasons", {}),
            "candidates_sha256": rec.get("candidates_sha256", ""),
            "authoritative_section_range": list(AUTHORITATIVE_SECTION_RANGE),
            "mode": rec.get("mode", DETERMINISTIC_NON_PRODUCTION_MODE),
            "production": bool(rec.get("production", False)),
            "admission_decision": admission_decision,
            "admission": {
                "decision": admission_decision,
                "production": production,
                "non_production_deterministic": not production,
                "qwen_architecture_present": production,
                "section_count": len(final_blueprint.get("sections") or []),
                "reason": (
                    "Qwen judge recommendation passed the 8-10 chapter and "
                    "concrete sibling-exclusion contract."
                    if production
                    else "Deterministic selection is test/offline only and "
                    "never admitted to the production mainline."
                ),
            },
            "blueprint": rec.get("unified_blueprint", {}),
        }

    # ------------------------------------------------------------------
    # Real LLM path
    # ------------------------------------------------------------------

    def _llm_recommend(
        self,
        *,
        candidates: list[dict],
        charter: dict,
        mentor_advice: dict,
    ) -> dict[str, Any]:
        if not Path(self.prompt_path).exists():
            raise FileNotFoundError(
                f"BlueprintTournamentJudge prompt not found: {self.prompt_path}"
            )
        _validate_section_count(candidates)
        system_prompt = Path(self.prompt_path).read_text(encoding="utf-8").strip()
        command_knowledge = _command_knowledge_block()
        system_prompt = (
            system_prompt
            + "\n\n"
            + "[COMMAND_KNOWLEDGE (versioned skill guidance; not scientific "
            "evidence)]\n"
            + command_knowledge["prompt_block"]
        )
        payload = {
            "candidates": candidates,
            "charter": charter,
            "mentor_advice": _compact_mentor_advice(mentor_advice),
            "m1_case_moves": _m1_case_moves_payload(mentor_advice),
            "command_knowledge": command_knowledge,
        }
        try:
            from llm.qwen_chat_client import call_qwen_chat

            result = call_qwen_chat(
                "BlueprintTournamentJudge",
                [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
                ],
                model_tier=self.model_tier,
                temperature=0.15,
                max_tokens=6000,
                response_format={"type": "json_object"},
                stream=True,
                force_mock=False,
                max_retries=1,
            )
            raw = str(result.get("content") or "")
            usage = result.get("_llm_usage") or {}
            if not bool(usage.get("success", True)):
                raise RuntimeError(
                    "model_call_failed: "
                    + str(usage.get("error_type") or raw or "unknown error")
                )
            parsed = _safe_json_parse(raw)
            required = {"selected_candidate_id", "selection_rationale"}
            selected_id = parsed.get("selected_candidate_id")
            by_id = {c.get("candidate_id"): c for c in candidates}
            if parsed and not (required - set(parsed)) and selected_id in by_id:
                parsed.setdefault("schema_version", SCHEMA_VERSION_REC)
                parsed.setdefault("created_at", utc_now())
                parsed["mode"] = "real_llm"
                parsed["production"] = True
                parsed["non_production_fallback"] = False
                parsed["admission_decision"] = "admit"
                parsed["admission"] = {
                    "decision": "admit",
                    "production": True,
                    "non_production_deterministic": False,
                    "qwen_architecture_present": True,
                    "reason": (
                        "Qwen judge recommendation passed the 8-10 chapter "
                        "and concrete sibling-exclusion contract."
                    ),
                }
                parsed["authoritative_section_range"] = list(
                    AUTHORITATIVE_SECTION_RANGE
                )
                parsed["candidates_sha256"] = _sha256_candidates(candidates)
                # The judge may compare plans, but may not silently rewrite scientific content.
                parsed["unified_blueprint"] = _rebuild_unified_blueprint(
                    by_id[selected_id],
                    selected_id,
                    require_concrete_exclusions=True,
                )
                deterministic_scores = {
                    cid: _score_candidate(candidate, charter, mentor_advice)
                    for cid, candidate in by_id.items()
                }
                parsed.setdefault("criterion_scores", deterministic_scores)
                parsed.setdefault(
                    "rejected_reasons",
                    {cid: "Not selected by the independent blueprint judge." for cid in by_id if cid != selected_id},
                )
                parsed.setdefault("combined", False)
                parsed.setdefault("combination_notes", "")
                from optomind_research.intermediate_language_guard import ensure_english_payload
                return ensure_english_payload(parsed)
        except Exception as exc:
            raise RuntimeError(f"BlueprintTournamentJudge LLM call failed: {exc}") from exc

        raise RuntimeError(
            "BlueprintTournamentJudge: LLM response missing required fields "
            f"(raw_chars={len(raw)}, parsed_keys={sorted(parsed) if isinstance(parsed, dict) else []})."
        )
