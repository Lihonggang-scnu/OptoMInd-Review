"""ReviewCharterAgent — S5: convert query plan + concept map into a formal review task contract.

The charter is the authoritative specification governing all downstream stages (S7-S20).
All charter fields are in English. The only allowed non-English field is user_query (pass-through).
M1 knowledge base role is always 'organizational_mentor_only_not_evidence'.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from optomind_research.artifact_registry import utc_now


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CHARTER_PROMPT = PROJECT_ROOT / "prompts" / "Review Charter Agent.txt"

SCHEMA_VERSION = "review_charter.v1"
# Authoritative chapter-count contract for the production comprehensive-review
# pipeline.  Qwen authors WHICH 8-10 chapters the review needs; Python enforces
# the range and rejects anything outside it.  No deterministic provider may
# invent a scientific architecture in production.
COMPREHENSIVE_SECTION_RANGE: list[int] = [8, 10]


def _section_count_policy_note() -> str:
    """Return the machine-readable chapter-count policy shared by all charters."""
    return (
        "python_enforced_8_10; Qwen owns which 8-10 chapters and their "
        "scientific organization, Python rejects any other count"
    )


def _compact(value: Any, limit: int = 400) -> str:
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


def _word_budget_from_cluster_count(n: int) -> int:
    """Return a publication-scale budget for a comprehensive review.

    The former 8k-12k range was useful for engineering acceptance runs but it
    is too small for a review that must establish foundations, compare several
    routes, examine applications and limitations, and close with a synthesis.
    A user may still explicitly request a focused/brief product downstream;
    absent that request the charter describes the full review, not the test
    harness used to validate one section.
    """
    if n <= 10:
        return 16000
    if n <= 20:
        return 22000
    return 28000


def _extract_query_plan_fields(query_plan: dict) -> tuple[str, str]:
    """Return (problem_understanding, scope_statement) from a S1 query plan artifact."""
    problem = _compact(query_plan.get("problem_understanding", ""), 600)
    scope_obj = query_plan.get("scope_definition") or {}
    if isinstance(scope_obj, dict):
        main = _compact(scope_obj.get("main_scope", ""), 400)
        items = scope_obj.get("scope_items") or []
        scope_text = main + (" Covers: " + "; ".join(str(x) for x in items[:5]) if items else "")
    else:
        scope_text = _compact(scope_obj, 600)
    return problem, scope_text


@dataclass
class ReviewCharterAgent:
    """Converts user query + query plan + concept map summary into a formal review charter."""

    prompt_path: Path = field(default_factory=lambda: DEFAULT_CHARTER_PROMPT)
    model_tier: str = "advanced_model"
    real_llm: bool = False

    def build_charter(
        self,
        *,
        user_query: str,
        query_plan: dict | None = None,
        concept_map_summary: dict | None = None,
        domain: str = "",
    ) -> dict[str, Any]:
        """Return a fully populated review_charter.v1 dict.

        In placeholder mode (real_llm=False): deterministic generation from inputs.
        In real_llm mode: calls LLM with the external prompt.
        """
        query_plan = query_plan or {}
        concept_map_summary = concept_map_summary or {}

        problem_understanding, scope_text = _extract_query_plan_fields(query_plan)
        if not problem_understanding:
            problem_understanding = _compact(user_query, 400)

        cluster_count = int(concept_map_summary.get("cluster_count", 0))
        top_labels: list[str] = list(concept_map_summary.get("top_labels", []))[:8]
        word_budget = _word_budget_from_cluster_count(cluster_count)

        if self.real_llm:
            return self._llm_charter(
                user_query=user_query,
                problem_understanding=problem_understanding,
                scope_text=scope_text,
                top_labels=top_labels,
                cluster_count=cluster_count,
                word_budget=word_budget,
                domain=domain,
            )
        charter = self._deterministic_charter(
            user_query=user_query,
            problem_understanding=problem_understanding,
            scope_text=scope_text,
            top_labels=top_labels,
            cluster_count=cluster_count,
            word_budget=word_budget,
            domain=domain,
        )
        from optomind_research.intermediate_language_guard import ensure_english_payload
        return ensure_english_payload(charter)

    # ------------------------------------------------------------------
    # Deterministic / placeholder path
    # ------------------------------------------------------------------

    @staticmethod
    def _deterministic_charter(
        *,
        user_query: str,
        problem_understanding: str,
        scope_text: str,
        top_labels: list[str],
        cluster_count: int,
        word_budget: int,
        domain: str,
    ) -> dict[str, Any]:
        domain_tag = domain.replace("_", " ") if domain else "the target domain"
        label_phrase = (
            ", ".join(top_labels[:4]) if top_labels else "key research themes"
        )

        title_base = problem_understanding[:80] if problem_understanding else _compact(user_query, 80)
        # Strip trailing punctuation to form a title
        title = re.sub(r"[?.!,;]+$", "", title_base).strip()
        if len(title) < 10:
            title = f"A Systematic Review of {domain_tag.title()}"

        central_question = (
            problem_understanding
            if problem_understanding
            else f"What is the current state of research in {domain_tag} and what are the key open challenges?"
        )

        scope_statement = (
            scope_text
            if scope_text
            else (
                f"This review covers recent advances in {domain_tag}, focusing on {label_phrase}. "
                f"It excludes purely engineering implementation details not connected to scientific principles "
                f"and does not address commercially proprietary systems."
            )
        )

        return {
            "schema_version": SCHEMA_VERSION,
            "created_at": utc_now(),
            "mode": "deterministic_non_production",
            "production": False,
            "non_production_fallback": True,
            "non_production_reason": (
                "Deterministic charter generation is a test/offline provider "
                "only; production requires the Qwen charter agent and rejects "
                "any deterministic scientific outline."
            ),
            "title": title,
            "audience": f"Researchers and graduate students in {domain_tag}; practitioners seeking a consolidated scientific perspective.",
            "central_question": central_question,
            "scope_statement": scope_statement,
            # Downstream stages must not infer review scale from prose.  Keep
            # the target as a machine-readable contract and let later stages
            # report shortfalls instead of silently lowering the standard.
            "review_mode": "comprehensive_review",
            "reference_target_range": [100, 180],
            "word_target_range": [word_budget, max(word_budget, 28000)],
            "review_quality_contract": {
                "mode": "comprehensive_review",
                "reference_target_range": [100, 180],
                "word_target_range": [word_budget, max(word_budget, 28000)],
                "section_target_range": list(COMPREHENSIVE_SECTION_RANGE),
                "section_unique_range": [8, 24],
                "section_direct_range": [5, 16],
                "section_count_policy": _section_count_policy_note(),
            },
            "structural_goals": [
                f"Synthesize competing theoretical frameworks and identify where consensus exists vs. where genuine disagreement remains.",
                f"Map the evidence base: characterize what is well-supported, what is preliminary, and what is contested.",
                f"Expose the principal open challenges and suggest criteria for evaluating proposed solutions.",
                f"Provide a conceptual scaffold that helps readers navigate the literature on {label_phrase}.",
            ],
            "out_of_scope": [
                "Detailed fabrication or manufacturing procedures without direct relevance to scientific principles.",
                "Commercial product comparisons and market analysis.",
                "Topics tangential to the central question that would require a separate review to treat adequately.",
            ],
            "success_criteria": [
                "A reader unfamiliar with the field can identify the3 most important open challenges after reading the review.",
                "Every major claim in the review is traceable to at least one piece of evidence in the knowledge base.",
                "The review's argument arc is clear from section titles and opening sentences alone.",
            ],
            "constraints": {
                "venue_tier": "top-tier review journal",
                "review_scale": "comprehensive",
                "review_mode": "comprehensive_review",
                "word_budget_total": word_budget,
                "section_count_range": list(COMPREHENSIVE_SECTION_RANGE),
                "reference_target_range": [80, 150],
                "language_final": "zh-CN",
                "citation_style": "numbered",
                "m1_kb_role": "organizational_mentor_only_not_evidence",
                "section_count_policy": _section_count_policy_note(),
            },
        }

    # ------------------------------------------------------------------
    # Real LLM path
    # ------------------------------------------------------------------

    def _llm_charter(
        self,
        *,
        user_query: str,
        problem_understanding: str,
        scope_text: str,
        top_labels: list[str],
        cluster_count: int,
        word_budget: int,
        domain: str,
    ) -> dict[str, Any]:
        if not Path(self.prompt_path).exists():
            raise FileNotFoundError(
                f"ReviewCharterAgent prompt not found: {self.prompt_path}"
            )
        system_prompt = Path(self.prompt_path).read_text(encoding="utf-8").strip()
        payload = {
            "user_query": _compact(user_query, 600),
            "problem_understanding": problem_understanding,
            "scope_definition": scope_text,
            "concept_map_summary": {
                "cluster_count": cluster_count,
                "top_labels": top_labels,
            },
            "domain": domain,
        }
        try:
            from llm.qwen_chat_client import call_qwen_chat

            result = call_qwen_chat(
                "ReviewCharterAgent",
                [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
                ],
                model_tier=self.model_tier,
                temperature=0.15,
                max_tokens=2000,
                response_format={"type": "json_object"},
                force_mock=False,
                max_retries=1,
            )
            parsed = _safe_json_parse(str(result.get("content") or ""))
            required = {
                "title", "audience", "central_question", "scope_statement",
                "structural_goals", "out_of_scope", "success_criteria", "constraints",
            }
            if parsed and not (required - set(parsed)):
                parsed.setdefault("schema_version", SCHEMA_VERSION)
                parsed.setdefault("created_at", utc_now())
                parsed["mode"] = "real_llm"
                parsed["production"] = True
                parsed["non_production_fallback"] = False
                from optomind_research.runtime.review_quality_contract import (
                    resolve_review_contract,
                )

                contract = resolve_review_contract(parsed)
                if contract.mode != "comprehensive_review":
                    raise RuntimeError(
                        "ReviewCharterAgent: production blueprint pipeline "
                        f"requires comprehensive_review; Qwen returned "
                        f"{contract.mode!r}."
                    )
                # The chapter-count range is authoritative and Python-enforced.
                # Qwen chooses which 8-10 chapters are scientifically necessary;
                # it may not shrink or expand the count.
                contract.section_target_range = list(
                    COMPREHENSIVE_SECTION_RANGE
                )
                parsed["review_mode"] = "comprehensive_review"
                parsed["review_quality_contract"] = {
                    **contract.to_dict(),
                    "section_count_policy": _section_count_policy_note(),
                }
                constraints = dict(parsed.get("constraints") or {})
                constraints["review_scale"] = "comprehensive"
                constraints["section_count_range"] = list(
                    COMPREHENSIVE_SECTION_RANGE
                )
                constraints["section_count_policy"] = _section_count_policy_note()
                parsed["constraints"] = constraints
                parsed.setdefault(
                    "reference_target_range",
                    contract.reference_target_range,
                )
                parsed["_section_count_policy"] = _section_count_policy_note()
                from optomind_research.intermediate_language_guard import ensure_english_payload
                return ensure_english_payload(parsed)
        except Exception as exc:
            raise RuntimeError(f"ReviewCharterAgent LLM call failed: {exc}") from exc

        raise RuntimeError(
            "ReviewCharterAgent: LLM response missing required fields."
        )
