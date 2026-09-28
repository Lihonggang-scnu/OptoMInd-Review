"""T11: 4-layer evaluation framework with ablation scaffolding (spec T11).

Layers:
1. Asset layer:     full-text rate, parse quality, figure quality, traceability
2. Reasoning layer: blueprint, claims, evidence relations, DAG, gap resolution
3. Output layer:    review structure, depth, citations, figures, research plan
4. Engineering layer: success rate, retries, cost, latency, cache hits, human burden

Ablation dimensions (spec T11):
- no_mentor vs with_mentor
- no_visual vs with_visual
- single_retrieval vs m3_multi_round
- bm25_only vs hybrid_retrieval
- single_model vs three_party_convergence
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _compact(text: Any, limit: int = 300) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip()[:limit]


# --------------------------------------------------------------------------- #
# Layer 1: Asset quality
# --------------------------------------------------------------------------- #

@dataclass
class AssetQualityReport:
    total_papers: int = 0
    fulltext_available: int = 0
    fulltext_rate: float = 0.0
    total_text_chunks: int = 0
    total_visual_chunks: int = 0
    visual_chunks_with_type: int = 0
    figures_with_caption: int = 0
    traceable_chunks: int = 0          # chunk_id → paper_id resolvable
    parse_error_rate: float = 0.0

    def compute(self) -> None:
        if self.total_papers > 0:
            self.fulltext_rate = round(self.fulltext_available / self.total_papers, 3)
        if self.total_visual_chunks > 0:
            self.visual_coverage = round(self.visual_chunks_with_type / self.total_visual_chunks, 3)
        if self.total_text_chunks > 0:
            self.traceability_rate = round(self.traceable_chunks / self.total_text_chunks, 3)

    def to_dict(self) -> dict[str, Any]:
        return {
            "layer": "asset",
            "total_papers": self.total_papers,
            "fulltext_available": self.fulltext_available,
            "fulltext_rate": self.fulltext_rate,
            "total_text_chunks": self.total_text_chunks,
            "total_visual_chunks": self.total_visual_chunks,
            "visual_chunks_with_type": self.visual_chunks_with_type,
            "figures_with_caption": self.figures_with_caption,
            "traceable_chunks": self.traceable_chunks,
            "parse_error_rate": self.parse_error_rate,
        }


def evaluate_asset_layer(
    corpus_manifest: dict[str, Any],
    knowledge_base_stats: dict[str, Any],
) -> AssetQualityReport:
    """Compute asset layer metrics from corpus manifest and KB stats."""
    report = AssetQualityReport(
        total_papers=int(corpus_manifest.get("total_papers") or 0),
        fulltext_available=int(corpus_manifest.get("fulltext_count") or 0),
        total_text_chunks=int(knowledge_base_stats.get("total_text_chunks") or 0),
        total_visual_chunks=int(knowledge_base_stats.get("total_visual_chunks") or 0),
        visual_chunks_with_type=int(knowledge_base_stats.get("visual_chunks_with_type") or 0),
        figures_with_caption=int(knowledge_base_stats.get("figures_with_caption") or 0),
        traceable_chunks=int(knowledge_base_stats.get("traceable_chunks") or 0),
        parse_error_rate=float(knowledge_base_stats.get("parse_error_rate") or 0.0),
    )
    report.compute()
    return report


# --------------------------------------------------------------------------- #
# Layer 2: Reasoning quality
# --------------------------------------------------------------------------- #

@dataclass
class ReasoningQualityReport:
    total_sections: int = 0
    total_claims: int = 0
    grounded_claims: int = 0
    partially_grounded_claims: int = 0
    open_question_claims: int = 0
    dropped_claims: int = 0
    claims_with_evidence_relations: int = 0
    dag_edge_count: int = 0
    dag_cross_section_edges: int = 0
    dag_backbone_edges: int = 0
    m3_gaps_resolved: int = 0
    m3_gaps_total: int = 0
    seed_coverage_rate: float = 0.0
    grounding_rate: float = 0.0

    def compute(self) -> None:
        if self.total_claims > 0:
            self.grounding_rate = round(
                (self.grounded_claims + self.partially_grounded_claims) / self.total_claims, 3
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "layer": "reasoning",
            "total_sections": self.total_sections,
            "total_claims": self.total_claims,
            "grounded_claims": self.grounded_claims,
            "partially_grounded_claims": self.partially_grounded_claims,
            "open_question_claims": self.open_question_claims,
            "dropped_claims": self.dropped_claims,
            "claims_with_evidence_relations": self.claims_with_evidence_relations,
            "grounding_rate": self.grounding_rate,
            "dag_edge_count": self.dag_edge_count,
            "dag_cross_section_edges": self.dag_cross_section_edges,
            "dag_backbone_edges": self.dag_backbone_edges,
            "m3_gaps_resolved": self.m3_gaps_resolved,
            "m3_gaps_total": self.m3_gaps_total,
            "m3_resolution_rate": round(self.m3_gaps_resolved / max(1, self.m3_gaps_total), 3),
            "seed_coverage_rate": self.seed_coverage_rate,
        }


def evaluate_reasoning_layer(
    blueprint: dict[str, Any],
    gap_report: dict[str, Any] | None = None,
) -> ReasoningQualityReport:
    """Compute reasoning layer metrics from blueprint and gap report."""
    report = ReasoningQualityReport()
    sections = list(blueprint.get("sections") or [])
    report.total_sections = len(sections)

    for section in sections:
        for claim in (section.get("claims") or []):
            report.total_claims += 1
            state = str(claim.get("claim_state") or "planned")
            if state == "grounded":
                report.grounded_claims += 1
            elif state == "partially_grounded":
                report.partially_grounded_claims += 1
            elif state == "open_question":
                report.open_question_claims += 1
            elif state == "dropped":
                report.dropped_claims += 1
            if claim.get("evidence_relations"):
                report.claims_with_evidence_relations += 1

    dag = blueprint.get("argument_dag") or {}
    report.dag_edge_count = int(dag.get("edge_count") or 0)
    report.dag_cross_section_edges = int(dag.get("cross_section_edge_count") or 0)
    report.dag_backbone_edges = sum(
        1 for e in (dag.get("edges") or []) if e.get("is_dag_backbone")
    )

    if gap_report:
        gaps = list(gap_report.get("gaps") or gap_report.get("resolved_gaps") or [])
        report.m3_gaps_total = int(gap_report.get("total_gaps") or len(gaps))
        report.m3_gaps_resolved = int(gap_report.get("resolved_count") or sum(
            1 for g in gaps if g.get("resolved")
        ))

    report.compute()
    return report


# --------------------------------------------------------------------------- #
# Layer 3: Output quality
# --------------------------------------------------------------------------- #

@dataclass
class OutputQualityReport:
    section_count: int = 0
    sections_with_transitions: int = 0
    sections_with_figures: int = 0
    chinese_text_length: int = 0
    citation_sentence_coverage: float = 0.0   # fraction of sentences with ≥1 citation
    overclaim_flags_total: int = 0
    contradiction_notes_total: int = 0
    research_plan_has_falsification_contract: bool = False
    hypothesis_count: int = 0
    hypotheses_with_collision_check: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "layer": "output",
            "section_count": self.section_count,
            "sections_with_transitions": self.sections_with_transitions,
            "sections_with_figures": self.sections_with_figures,
            "chinese_text_length": self.chinese_text_length,
            "citation_sentence_coverage": self.citation_sentence_coverage,
            "overclaim_flags_total": self.overclaim_flags_total,
            "contradiction_notes_total": self.contradiction_notes_total,
            "research_plan_has_falsification_contract": self.research_plan_has_falsification_contract,
            "hypothesis_count": self.hypothesis_count,
            "hypotheses_with_collision_check": self.hypotheses_with_collision_check,
        }


def evaluate_output_layer(
    section_drafts: list[dict[str, Any]],
    hypothesis_portfolio: dict[str, Any] | None = None,
) -> OutputQualityReport:
    """Compute output layer metrics."""
    report = OutputQualityReport()
    report.section_count = len(section_drafts)
    total_sentences = 0
    cited_sentences = 0

    for draft in section_drafts:
        if draft.get("chinese_text"):
            report.chinese_text_length += len(str(draft["chinese_text"]))
        if draft.get("figure_placements"):
            report.sections_with_figures += 1
        report.overclaim_flags_total += len(draft.get("overclaim_flags") or [])
        report.contradiction_notes_total += len(draft.get("contradiction_notes") or [])
        # Estimate citation coverage from citation_map
        cmap = draft.get("citation_map") or {}
        text = str(draft.get("english_text") or "")
        sentences = [s.strip() for s in re.split(r"[.!?]+", text) if len(s.strip()) > 20]
        total_sentences += len(sentences)
        cited_sentences += len(cmap)

    if total_sentences > 0:
        report.citation_sentence_coverage = round(cited_sentences / total_sentences, 3)

    if hypothesis_portfolio:
        candidates = list(hypothesis_portfolio.get("candidates") or [])
        report.hypothesis_count = len(candidates)
        report.hypotheses_with_collision_check = sum(
            1 for c in candidates if c.get("collision_check")
        )
        report.research_plan_has_falsification_contract = any(
            c.get("falsification_contract") for c in candidates
        )

    return report


# --------------------------------------------------------------------------- #
# Layer 4: Engineering metrics
# --------------------------------------------------------------------------- #

@dataclass
class EngineeringReport:
    pipeline_stages: int = 0
    stages_completed: int = 0
    stages_failed: int = 0
    success_rate: float = 0.0
    total_llm_calls: int = 0
    total_retries: int = 0
    total_tokens_input: int = 0
    total_tokens_output: int = 0
    estimated_cost_usd: float = 0.0
    total_latency_ms: float = 0.0
    cache_hit_rate: float = 0.0
    human_events: int = 0
    human_intervention_stages: list[str] = field(default_factory=list)

    def compute(self) -> None:
        if self.pipeline_stages > 0:
            self.success_rate = round(self.stages_completed / self.pipeline_stages, 3)

    def to_dict(self) -> dict[str, Any]:
        return {
            "layer": "engineering",
            "pipeline_stages": self.pipeline_stages,
            "stages_completed": self.stages_completed,
            "stages_failed": self.stages_failed,
            "success_rate": self.success_rate,
            "total_llm_calls": self.total_llm_calls,
            "total_retries": self.total_retries,
            "total_tokens_input": self.total_tokens_input,
            "total_tokens_output": self.total_tokens_output,
            "estimated_cost_usd": round(self.estimated_cost_usd, 4),
            "total_latency_ms": round(self.total_latency_ms, 1),
            "cache_hit_rate": self.cache_hit_rate,
            "human_events": self.human_events,
            "human_intervention_stages": self.human_intervention_stages,
        }


def evaluate_engineering_layer(run_manifest: dict[str, Any]) -> EngineeringReport:
    """Compute engineering metrics from run manifest."""
    report = EngineeringReport()
    stage_statuses = run_manifest.get("stage_status") or {}
    report.pipeline_stages = len(stage_statuses)
    report.stages_completed = sum(
        1 for s in stage_statuses.values() if s.get("status") == "completed"
    )
    report.stages_failed = sum(
        1 for s in stage_statuses.values() if s.get("status") == "failed"
    )

    token_summary = run_manifest.get("token_summary") or {}
    report.total_tokens_input = int(token_summary.get("input") or 0)
    report.total_tokens_output = int(token_summary.get("output") or 0)
    report.estimated_cost_usd = float(run_manifest.get("cost_summary_usd") or 0.0)

    for usage in (run_manifest.get("model_usage") or []):
        if isinstance(usage, dict):
            report.total_llm_calls += 1
            report.total_retries += int(usage.get("retries") or 0)
            report.total_latency_ms += float(usage.get("latency_ms") or 0.0)

    human_events = list(run_manifest.get("human_events") or [])
    report.human_events = len(human_events)
    report.human_intervention_stages = list({
        str(e.get("stage")) for e in human_events if isinstance(e, dict) and e.get("stage")
    })

    report.compute()
    return report


# --------------------------------------------------------------------------- #
# Ablation scaffolding
# --------------------------------------------------------------------------- #

ABLATION_DIMENSIONS = (
    "no_mentor_vs_with_mentor",
    "no_visual_vs_with_visual",
    "single_retrieval_vs_m3_multi_round",
    "bm25_only_vs_hybrid_retrieval",
    "single_model_vs_three_party_convergence",
)


@dataclass
class AblationRun:
    ablation_id: str
    dimension: str                       # member of ABLATION_DIMENSIONS
    condition: str                       # e.g. "no_mentor" or "with_mentor"
    run_manifest_path: str = ""
    reasoning_report: ReasoningQualityReport | None = None
    output_report: OutputQualityReport | None = None
    engineering_report: EngineeringReport | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "ablation_id": self.ablation_id,
            "dimension": self.dimension,
            "condition": self.condition,
            "run_manifest_path": self.run_manifest_path,
            "reasoning": self.reasoning_report.to_dict() if self.reasoning_report else None,
            "output": self.output_report.to_dict() if self.output_report else None,
            "engineering": self.engineering_report.to_dict() if self.engineering_report else None,
        }


class AblationScaffold:
    """Scaffolding for ablation experiments (spec T11).

    Usage:
        scaffold = AblationScaffold()
        run = scaffold.register_run("no_mentor_vs_with_mentor", "no_mentor")
        # ... execute pipeline with condition ...
        scaffold.record_results(run.ablation_id, blueprint, run_manifest)
        scaffold.compare("no_mentor_vs_with_mentor")
    """

    def __init__(self) -> None:
        self.runs: dict[str, AblationRun] = {}

    def register_run(
        self, dimension: str, condition: str, run_manifest_path: str = ""
    ) -> AblationRun:
        if dimension not in ABLATION_DIMENSIONS:
            raise ValueError(
                f"Unknown ablation dimension {dimension!r}. Valid: {ABLATION_DIMENSIONS}"
            )
        ablation_id = f"{dimension}__{condition}__{int(time.time())}"
        run = AblationRun(
            ablation_id=ablation_id,
            dimension=dimension,
            condition=condition,
            run_manifest_path=run_manifest_path,
        )
        self.runs[ablation_id] = run
        return run

    def record_results(
        self,
        ablation_id: str,
        blueprint: dict[str, Any],
        run_manifest: dict[str, Any],
        section_drafts: list[dict[str, Any]] | None = None,
        hypothesis_portfolio: dict[str, Any] | None = None,
        gap_report: dict[str, Any] | None = None,
    ) -> None:
        run = self.runs.get(ablation_id)
        if not run:
            raise ValueError(f"Ablation run {ablation_id!r} not found")
        run.reasoning_report = evaluate_reasoning_layer(blueprint, gap_report)
        run.output_report = evaluate_output_layer(section_drafts or [], hypothesis_portfolio)
        run.engineering_report = evaluate_engineering_layer(run_manifest)

    def compare(self, dimension: str) -> dict[str, Any]:
        """Compare all runs for a given dimension."""
        runs = [r for r in self.runs.values() if r.dimension == dimension]
        return {
            "dimension": dimension,
            "runs": [r.to_dict() for r in runs],
            "delta_summary": self._compute_delta(runs),
        }

    def _compute_delta(self, runs: list[AblationRun]) -> dict[str, Any]:
        if len(runs) < 2:
            return {"status": "insufficient_runs"}
        base = runs[0]
        compare = runs[1]
        delta: dict[str, Any] = {}
        if base.reasoning_report and compare.reasoning_report:
            delta["grounding_rate_delta"] = round(
                compare.reasoning_report.grounding_rate - base.reasoning_report.grounding_rate, 3
            )
            delta["cross_section_edges_delta"] = (
                compare.reasoning_report.dag_cross_section_edges
                - base.reasoning_report.dag_cross_section_edges
            )
        if base.engineering_report and compare.engineering_report:
            delta["cost_delta_usd"] = round(
                compare.engineering_report.estimated_cost_usd
                - base.engineering_report.estimated_cost_usd, 4
            )
        return delta

    def to_dict(self) -> dict[str, Any]:
        by_dimension: dict[str, list[dict]] = {}
        for run in self.runs.values():
            by_dimension.setdefault(run.dimension, []).append(run.to_dict())
        return {
            "schema_version": "ablation_scaffold.v1",
            "total_runs": len(self.runs),
            "by_dimension": by_dimension,
        }

    def save(self, path: Path) -> None:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(self.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8"
        )


# --------------------------------------------------------------------------- #
# EvaluationFramework — top-level entry point
# --------------------------------------------------------------------------- #

@dataclass
class QualityReport:
    run_id: str = ""
    asset: AssetQualityReport | None = None
    reasoning: ReasoningQualityReport | None = None
    output: OutputQualityReport | None = None
    engineering: EngineeringReport | None = None
    evaluated_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": "quality_report.v1",
            "run_id": self.run_id,
            "evaluated_at": self.evaluated_at,
            "asset": self.asset.to_dict() if self.asset else None,
            "reasoning": self.reasoning.to_dict() if self.reasoning else None,
            "output": self.output.to_dict() if self.output else None,
            "engineering": self.engineering.to_dict() if self.engineering else None,
        }

    def save(self, path: Path) -> None:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(self.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8"
        )


class EvaluationFramework:
    """4-layer evaluation framework (spec T11)."""

    def __init__(self) -> None:
        self.ablation = AblationScaffold()

    def evaluate(
        self,
        *,
        run_manifest: dict[str, Any],
        blueprint: dict[str, Any],
        corpus_manifest: dict[str, Any] | None = None,
        knowledge_base_stats: dict[str, Any] | None = None,
        section_drafts: list[dict[str, Any]] | None = None,
        hypothesis_portfolio: dict[str, Any] | None = None,
        gap_report: dict[str, Any] | None = None,
    ) -> QualityReport:
        import time as _time

        report = QualityReport(
            run_id=str(run_manifest.get("run_id") or ""),
            evaluated_at=str(
                __import__("datetime").datetime.now(__import__("datetime").timezone.utc)
                .replace(microsecond=0).isoformat().replace("+00:00", "Z")
            ),
        )
        if corpus_manifest or knowledge_base_stats:
            report.asset = evaluate_asset_layer(
                corpus_manifest or {}, knowledge_base_stats or {}
            )
        report.reasoning = evaluate_reasoning_layer(blueprint, gap_report)
        report.output = evaluate_output_layer(section_drafts or [], hypothesis_portfolio)
        report.engineering = evaluate_engineering_layer(run_manifest)
        return report
