"""T9: Hypothesis portfolio and research plan generation.

Components:
- FalsificationContract: immutable verification commitments (no silent drift)
- PriorArtCollisionCheck: 8-axis novelty check per hypothesis
- HypothesisPortfolio: manages candidate hypotheses with evolution history

Integration: call HypothesisPortfolio.generate(blueprint, gap_report) to produce
hypothesis_portfolio.json and a research plan skeleton.
"""

from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from llm.qwen_chat_client import call_qwen_chat

PROJECT_ROOT = Path(__file__).resolve().parents[1]
_PROMPTS = PROJECT_ROOT / "prompts"

HYPOTHESIS_GENERATOR_PROMPT = _PROMPTS / "Hypothesis Generator.txt"
PRIOR_ART_CHECKER_PROMPT = _PROMPTS / "Prior Art Checker.txt"
FALSIFICATION_CONTRACT_PROMPT = _PROMPTS / "Falsification Contract Builder.txt"

# 8 axes for prior art collision check (from spec T9)
PRIOR_ART_AXES = (
    "scientific_question",
    "core_physical_mechanism",
    "material_system",
    "spectral_spatial_temporal_freedoms",
    "device_structure",
    "fabrication_method",
    "characterization_and_validation_method",
    "application_scenario",
)

OVERLAP_CATEGORIES = (
    "full_overlap",
    "partial_overlap",
    "same_target_different_mechanism",
    "same_mechanism_different_application",
    "background_adjacent",
)


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _compact(text: Any, limit: int = 300) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip()[:limit]


def _read_prompt(path: Path) -> str:
    if path.exists():
        return path.read_text(encoding="utf-8").strip()
    return f"You are a scientific hypothesis expert. Task: {path.stem}."


def _safe_json(text: str) -> dict | list:
    try:
        v = json.loads(text)
        return v if isinstance(v, (dict, list)) else {}
    except Exception:
        m = re.search(r"(\{.*\}|\[.*\])", str(text or ""), re.S)
        if m:
            try:
                return json.loads(m.group(0))
            except Exception:
                pass
    return


# --------------------------------------------------------------------------- #
# FalsificationContract
# --------------------------------------------------------------------------- #

@dataclass
class FalsificationContract:
    """Immutable verification commitments — cannot be silently weakened.

    Once created, the core_hypothesis, key_variables, and minimum_experiment
    are locked. Any modification requires explicit justification and a new version.
    """
    contract_id: str = field(default_factory=lambda: str(uuid.uuid4())[:8])
    hypothesis_id: str = ""
    core_hypothesis: str = ""                # locked — must not drift
    key_physical_variables: list[str] = field(default_factory=list)
    minimum_experiment: str = ""             # locked — specific protocol
    primary_outcome_metrics: list[str] = field(default_factory=list)
    negative_controls: list[str] = field(default_factory=list)
    falsification_conditions: list[str] = field(default_factory=list)  # what would refute this
    resource_budget: str = ""
    readiness_level: str = "conceptual"      # conceptual|data_ready|experiment_ready|in_progress
    human_decision_points: list[str] = field(default_factory=list)
    created_at: str = field(default_factory=_utc_now)
    locked: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "contract_id": self.contract_id,
            "hypothesis_id": self.hypothesis_id,
            "core_hypothesis": self.core_hypothesis,
            "key_physical_variables": self.key_physical_variables,
            "minimum_experiment": self.minimum_experiment,
            "primary_outcome_metrics": self.primary_outcome_metrics,
            "negative_controls": self.negative_controls,
            "falsification_conditions": self.falsification_conditions,
            "resource_budget": self.resource_budget,
            "readiness_level": self.readiness_level,
            "human_decision_points": self.human_decision_points,
            "created_at": self.created_at,
            "locked": self.locked,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "FalsificationContract":
        return cls(
            contract_id=str(d.get("contract_id", str(uuid.uuid4())[:8])),
            hypothesis_id=str(d.get("hypothesis_id", "")),
            core_hypothesis=str(d.get("core_hypothesis", "")),
            key_physical_variables=list(d.get("key_physical_variables") or []),
            minimum_experiment=str(d.get("minimum_experiment", "")),
            primary_outcome_metrics=list(d.get("primary_outcome_metrics") or []),
            negative_controls=list(d.get("negative_controls") or []),
            falsification_conditions=list(d.get("falsification_conditions") or []),
            resource_budget=str(d.get("resource_budget", "")),
            readiness_level=str(d.get("readiness_level", "conceptual")),
            human_decision_points=list(d.get("human_decision_points") or []),
            created_at=str(d.get("created_at", _utc_now())),
            locked=bool(d.get("locked", True)),
        )


# --------------------------------------------------------------------------- #
# PriorArtCollisionCheck
# --------------------------------------------------------------------------- #

@dataclass
class AxisResult:
    axis: str
    overlap_category: str       # member of OVERLAP_CATEGORIES
    closest_paper_id: str = ""
    closest_paper_title: str = ""
    remaining_difference: str = ""
    notes: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "axis": self.axis,
            "overlap_category": self.overlap_category,
            "closest_paper_id": self.closest_paper_id,
            "closest_paper_title": self.closest_paper_title,
            "remaining_difference": self.remaining_difference,
            "notes": self.notes,
        }


@dataclass
class CollisionCheckResult:
    hypothesis_id: str
    axis_results: list[AxisResult] = field(default_factory=list)
    overall_novelty: str = "unknown"    # novel|partial_overlap|full_overlap|unclear
    closest_existing_work: str = ""
    key_differentiators: list[str] = field(default_factory=list)
    checked_at: str = field(default_factory=_utc_now)

    def to_dict(self) -> dict[str, Any]:
        return {
            "hypothesis_id": self.hypothesis_id,
            "axis_results": [a.to_dict() for a in self.axis_results],
            "overall_novelty": self.overall_novelty,
            "closest_existing_work": self.closest_existing_work,
            "key_differentiators": self.key_differentiators,
            "checked_at": self.checked_at,
        }


class PriorArtCollisionCheck:
    """8-axis novelty check per hypothesis (spec T9, PriorArtCollisionCheck)."""

    def __init__(
        self,
        model_tier: str = "advanced_model",
        prompt_path: Path = PRIOR_ART_CHECKER_PROMPT,
        real_llm: bool = False,
    ) -> None:
        self.model_tier = model_tier
        self.prompt_path = prompt_path
        self.real_llm = real_llm

    def check(
        self,
        hypothesis: dict[str, Any],
        corpus_summary: str = "",
    ) -> CollisionCheckResult:
        hid = str(hypothesis.get("hypothesis_id", ""))
        result = CollisionCheckResult(hypothesis_id=hid)

        if not self.real_llm:
            for axis in PRIOR_ART_AXES:
                result.axis_results.append(AxisResult(
                    axis=axis,
                    overlap_category="background_adjacent",
                    notes="mock check",
                ))
            result.overall_novelty = "unclear"
            return result

        system = _read_prompt(self.prompt_path)
        payload = {
            "hypothesis": {
                "hypothesis_id": hid,
                "core_claim": _compact(hypothesis.get("core_claim"), 500),
                "mechanism": _compact(hypothesis.get("mechanism"), 400),
                "material_system": _compact(hypothesis.get("material_system"), 300),
                "application": _compact(hypothesis.get("application"), 300),
            },
            "corpus_summary": _compact(corpus_summary, 800),
            "axes_to_check": list(PRIOR_ART_AXES),
            "overlap_categories": list(OVERLAP_CATEGORIES),
        }
        try:
            llm_result = call_qwen_chat(
                "PriorArtCheckerAgent",
                [
                    {"role": "system", "content": system},
                    {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
                ],
                model_tier=self.model_tier,
                temperature=0,
                max_tokens=2000,
                response_format={"type": "json_object"},
                force_mock=False,
                max_retries=1,
            )
            parsed = _safe_json(str(llm_result.get("content") or ""))
            if isinstance(parsed, dict):
                for ar in (parsed.get("axis_results") or []):
                    if not isinstance(ar, dict):
                        continue
                    axis = str(ar.get("axis", ""))
                    if axis not in PRIOR_ART_AXES:
                        continue
                    overlap = str(ar.get("overlap_category", "background_adjacent"))
                    if overlap not in OVERLAP_CATEGORIES:
                        overlap = "background_adjacent"
                    result.axis_results.append(AxisResult(
                        axis=axis,
                        overlap_category=overlap,
                        closest_paper_id=str(ar.get("closest_paper_id", "")),
                        closest_paper_title=str(ar.get("closest_paper_title", "")),
                        remaining_difference=_compact(ar.get("remaining_difference"), 300),
                        notes=_compact(ar.get("notes"), 200),
                    ))
                result.overall_novelty = str(parsed.get("overall_novelty", "unclear"))
                result.closest_existing_work = _compact(parsed.get("closest_existing_work"), 400)
                result.key_differentiators = list(parsed.get("key_differentiators") or [])
        except Exception as exc:
            result.axis_results = [
                AxisResult(axis=ax, overlap_category="background_adjacent", notes=f"error: {exc}")
                for ax in PRIOR_ART_AXES
            ]
            result.overall_novelty = "unclear"
        return result


# --------------------------------------------------------------------------- #
# HypothesisPortfolio
# --------------------------------------------------------------------------- #

@dataclass
class HypothesisCandidate:
    hypothesis_id: str = field(default_factory=lambda: "H" + str(uuid.uuid4())[:6])
    core_claim: str = ""
    mechanism: str = ""
    material_system: str = ""
    application: str = ""
    source_gap_ids: list[str] = field(default_factory=list)    # which M3 gaps generated this
    supporting_evidence_ids: list[str] = field(default_factory=list)
    opposing_evidence_ids: list[str] = field(default_factory=list)
    physical_plausibility: str = ""      # reviewer note
    falsifiability_score: float = 0.0   # 0-1
    implementation_cost: str = ""
    rank: int = 0
    status: str = "candidate"           # candidate|accepted|rejected|evolved
    collision_check: CollisionCheckResult | None = None
    falsification_contract: FalsificationContract | None = None
    evolution_history: list[dict[str, Any]] = field(default_factory=list)
    created_at: str = field(default_factory=_utc_now)

    def to_dict(self) -> dict[str, Any]:
        return {
            "hypothesis_id": self.hypothesis_id,
            "core_claim": self.core_claim,
            "mechanism": self.mechanism,
            "material_system": self.material_system,
            "application": self.application,
            "source_gap_ids": self.source_gap_ids,
            "supporting_evidence_ids": self.supporting_evidence_ids,
            "opposing_evidence_ids": self.opposing_evidence_ids,
            "physical_plausibility": self.physical_plausibility,
            "falsifiability_score": self.falsifiability_score,
            "implementation_cost": self.implementation_cost,
            "rank": self.rank,
            "status": self.status,
            "collision_check": self.collision_check.to_dict() if self.collision_check else None,
            "falsification_contract": self.falsification_contract.to_dict() if self.falsification_contract else None,
            "evolution_history": self.evolution_history,
            "created_at": self.created_at,
        }


class HypothesisPortfolio:
    """Manages candidate hypotheses with generation, review, evolution (spec T9).

    Evolution pipeline:
    generate → diversity_clustering → novelty_collision → plausibility_review
    → falsifiability_review → cost_review → ranking → patch_revision → re_review
    """

    def __init__(
        self,
        model_tier: str = "advanced_model",
        real_llm: bool = False,
    ) -> None:
        self.model_tier = model_tier
        self.real_llm = real_llm
        self._collision_checker = PriorArtCollisionCheck(
            model_tier=model_tier, real_llm=real_llm
        )
        self.candidates: list[HypothesisCandidate] = []

    def generate(
        self,
        blueprint: dict[str, Any],
        gap_report: dict[str, Any] | None = None,
        corpus_summary: str = "",
        *,
        max_candidates: int = 8,
    ) -> list[HypothesisCandidate]:
        """Generate candidate hypotheses from blueprint gaps and evidence."""
        if not self.real_llm:
            # Mock: create a single placeholder candidate
            cand = HypothesisCandidate(
                core_claim="[Mock hypothesis] Combining mechanism X with material Y may yield improvement Z.",
                mechanism="Speculative mechanism from gap analysis.",
                source_gap_ids=["mock_gap_001"],
            )
            self.candidates = [cand]
            return self.candidates

        system = _read_prompt(HYPOTHESIS_GENERATOR_PROMPT)
        gap_summary = _compact(json.dumps(list((gap_report or {}).get("gaps") or [])[:10]), 1500)
        sections_summary = [
            {
                "section_id": s.get("section_id"),
                "title": _compact(s.get("title"), 120),
                "open_questions": [
                    _compact(c.get("statement"), 200)
                    for c in (s.get("claims") or [])
                    if c.get("claim_state") in {"open_question", "planned"}
                ][:3],
            }
            for s in (blueprint.get("sections") or [])
        ]
        payload = {
            "sections_with_gaps": sections_summary,
            "gap_report_summary": gap_summary,
            "corpus_summary": _compact(corpus_summary, 800),
            "max_candidates": max_candidates,
        }
        try:
            result = call_qwen_chat(
                "HypothesisGeneratorAgent",
                [
                    {"role": "system", "content": system},
                    {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
                ],
                model_tier=self.model_tier,
                temperature=0.5,
                max_tokens=3000,
                response_format={"type": "json_object"},
                force_mock=False,
                max_retries=1,
            )
            parsed = _safe_json(str(result.get("content") or ""))
            hypotheses = []
            if isinstance(parsed, dict):
                hypotheses = list(parsed.get("hypotheses") or [])
            elif isinstance(parsed, list):
                hypotheses = parsed

            for item in hypotheses[:max_candidates]:
                if not isinstance(item, dict):
                    continue
                cand = HypothesisCandidate(
                    core_claim=_compact(item.get("core_claim"), 500),
                    mechanism=_compact(item.get("mechanism"), 400),
                    material_system=_compact(item.get("material_system"), 300),
                    application=_compact(item.get("application"), 300),
                    source_gap_ids=list(item.get("source_gap_ids") or []),
                    physical_plausibility=_compact(item.get("physical_plausibility"), 300),
                    falsifiability_score=float(item.get("falsifiability_score") or 0.0),
                    implementation_cost=_compact(item.get("implementation_cost"), 200),
                )
                self.candidates.append(cand)
        except Exception:
            pass
        return self.candidates

    def run_collision_checks(self, corpus_summary: str = "") -> None:
        """Run PriorArtCollisionCheck on all candidates."""
        for cand in self.candidates:
            cand.collision_check = self._collision_checker.check(
                cand.to_dict(), corpus_summary=corpus_summary
            )

    def build_contracts(self) -> None:
        """Build FalsificationContract for accepted/ranked candidates."""
        if not self.real_llm:
            for cand in self.candidates:
                cand.falsification_contract = FalsificationContract(
                    hypothesis_id=cand.hypothesis_id,
                    core_hypothesis=cand.core_claim,
                    key_physical_variables=["variable_A", "variable_B"],
                    minimum_experiment="[Mock] Standard characterization protocol.",
                    falsification_conditions=["If measurement X is below threshold Y."],
                )
            return

        system = _read_prompt(FALSIFICATION_CONTRACT_PROMPT)
        for cand in self.candidates:
            if cand.status == "rejected":
                continue
            payload = {
                "hypothesis_id": cand.hypothesis_id,
                "core_claim": cand.core_claim,
                "mechanism": cand.mechanism,
                "supporting_evidence_count": len(cand.supporting_evidence_ids),
                "physical_plausibility": cand.physical_plausibility,
            }
            try:
                result = call_qwen_chat(
                    "FalsificationContractBuilderAgent",
                    [
                        {"role": "system", "content": system},
                        {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
                    ],
                    model_tier=self.model_tier,
                    temperature=0,
                    max_tokens=1200,
                    response_format={"type": "json_object"},
                    force_mock=False,
                    max_retries=1,
                )
                parsed = _safe_json(str(result.get("content") or ""))
                if isinstance(parsed, dict):
                    cand.falsification_contract = FalsificationContract.from_dict(
                        {**parsed, "hypothesis_id": cand.hypothesis_id}
                    )
            except Exception:
                cand.falsification_contract = FalsificationContract(
                    hypothesis_id=cand.hypothesis_id,
                    core_hypothesis=cand.core_claim,
                )

    def rank(self) -> None:
        """Rank candidates by falsifiability, novelty, and plausibility."""
        def score(c: HypothesisCandidate) -> float:
            s = c.falsifiability_score or 0.0
            novelty_bonus = 0.3 if (
                c.collision_check and
                c.collision_check.overall_novelty in {"novel", "unclear"}
            ) else 0.0
            return s + novelty_bonus

        ranked = sorted(self.candidates, key=score, reverse=True)
        for idx, cand in enumerate(ranked):
            cand.rank = idx + 1

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": "hypothesis_portfolio.v1",
            "total_candidates": len(self.candidates),
            "candidates": [c.to_dict() for c in self.candidates],
        }

    def save(self, path: Path) -> None:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(self.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8"
        )
