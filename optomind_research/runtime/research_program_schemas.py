"""Canonical schemas for review-to-research-program artifacts."""

from __future__ import annotations

from typing import Any, Dict, List, Literal

from pydantic import BaseModel, Field


OpportunityOrigin = Literal[
    "consensus_boundary",
    "controversy",
    "evidence_gap",
    "method_gap",
    "benchmark_gap",
    "deployment_gap",
]
EvidenceStatus = Literal[
    "supported_boundary",
    "partially_supported",
    "open_gap",
]
Readiness = Literal[
    "ready",
    "needs_more_literature",
    "needs_human_choice",
    "future_phase",
]
VerificationStatus = Literal[
    "verification_deferred",
    "evidence_only",
    "not_applicable",
]


class ResearchOpportunity(BaseModel):
    opportunity_id: str
    title: str
    problem: str
    why_it_matters: str
    origin_type: OpportunityOrigin
    source_section_ids: List[str] = Field(default_factory=list)
    supporting_paper_ids: List[str] = Field(default_factory=list)
    supporting_chunk_ids: List[str] = Field(default_factory=list)
    evidence_status: EvidenceStatus
    evidence_basis: str
    author_inference: str
    uncertainty: str
    recommended_next_evidence: List[str] = Field(default_factory=list)


class ResearchOpportunityMap(BaseModel):
    schema_version: str = "research_harness.opportunity_map.v1"
    opportunities: List[ResearchOpportunity]


class ResearchHypothesis(BaseModel):
    hypothesis_id: str
    title: str
    statement: str
    source_opportunity_ids: List[str]
    mechanism_rationale: str
    supporting_paper_ids: List[str] = Field(default_factory=list)
    supporting_chunk_ids: List[str] = Field(default_factory=list)
    inference_chain: List[str]
    assumptions: List[str]
    alternative_explanations: List[str]
    falsification_conditions: List[str]
    novelty_status: Literal[
        "candidate_novelty",
        "partial_overlap",
        "unknown_requires_prior_art_search",
    ]
    confidence: Literal["low", "medium", "high"]
    readiness: Readiness
    quantitative_commitment_status: Literal[
        "none",
        "evidence_anchored",
        "proposed_program_target",
    ] = "none"
    verification_status: VerificationStatus = "verification_deferred"
    verification_rationale: str = (
        "This is a proposed hypothesis; no new experiment, simulation, or "
        "data-analysis result has been executed by this workflow."
    )
    readiness_calibration: Dict[str, Any] = Field(default_factory=dict)


class ResearchHypothesisPortfolio(BaseModel):
    schema_version: str = "research_harness.hypothesis_portfolio.v1"
    hypotheses: List[ResearchHypothesis]


class ResearchWorkPackage(BaseModel):
    work_package_id: str
    title: str
    objective: str
    hypothesis_ids: List[str] = Field(default_factory=list)
    opportunity_ids: List[str] = Field(default_factory=list)
    methods: List[str]
    inputs: List[str]
    expected_outputs: List[str]
    controls_or_baselines: List[str]
    evaluation_metrics: List[str]
    dependencies: List[str] = Field(default_factory=list)
    risks: List[str]
    readiness: Readiness
    stop_or_pivot_criteria: List[str]
    # R5 focus-gate fields.  They are canonicalized by the provider so an
    # individual model turn does not need to repeat the same platform and
    # evaluation vocabulary in every package.
    platform_id: str = ""
    platform_compatibility_key: str = ""
    metric_ids: List[str] = Field(default_factory=list)
    baseline_ids: List[str] = Field(default_factory=list)
    quantitative_target_status: Literal[
        "none",
        "evidence_anchored",
        "proposed_program_target",
    ] = "none"
    quantitative_target_provenance: Literal[
        "not_applicable",
        "source_anchored",
        "proposed_calibration_target",
    ] = "not_applicable"
    verification_status: VerificationStatus = "verification_deferred"
    verification_rationale: str = (
        "The work package is a planned validation route, not an executed "
        "experiment, simulation, or analysis result."
    )


class ResearchPlan(BaseModel):
    schema_version: str = "research_harness.research_plan.v2"
    title: str
    research_question: str
    strategy: str
    objectives: List[str]
    work_packages: List[ResearchWorkPackage]
    milestones: List[str]
    human_decision_points: List[str]
    unresolved_literature_needs: List[str]
    readiness_summary: Dict[str, Any] = Field(default_factory=dict)
    narrative_markdown: str
    # Publication-facing plan fields.  These deliberately coexist with the
    # work-package structure: the first is easy for a reader or evaluator to
    # inspect, the second retains the machine-traceable execution graph.
    paper_abstract: str = ""
    problem_statement: str = ""
    rationale: str = ""
    technical_details: List[str] = Field(default_factory=list)
    dataset_source: List[str] = Field(default_factory=list)
    dataset_target: List[str] = Field(default_factory=list)
    methods_summary: List[str] = Field(default_factory=list)
    experiments: List[str] = Field(default_factory=list)
    expected_results: List[str] = Field(default_factory=list)
    results_status: VerificationStatus = "verification_deferred"
    reference_paper_ids: List[str] = Field(default_factory=list)
    verification_deferred: List[str] = Field(default_factory=list)
    # The focus decision is stored with the plan rather than left as a
    # prompt-only instruction.  This lets later planning and publication
    # stages inspect the same project spine without reinterpreting prose.
    program_focus_gate_id: str = ""
    main_problem: Dict[str, Any] = Field(default_factory=dict)
    project_type: str = ""
    shared_platform: Dict[str, Any] = Field(default_factory=dict)
    boundaries: Dict[str, List[str]] = Field(default_factory=dict)
    unified_evaluation: Dict[str, Any] = Field(default_factory=dict)
    main_hypothesis_ids: List[str] = Field(default_factory=list)
    future_hypothesis_ids: List[str] = Field(default_factory=list)
    hypothesis_dependencies: List[Dict[str, Any]] = Field(default_factory=list)
    future_branches: List[Dict[str, Any]] = Field(default_factory=list)
    traceability_matrix: List[Dict[str, Any]] = Field(default_factory=list)
    source_context: Dict[str, Any] = Field(default_factory=dict)
    source_limitations: List[str] = Field(default_factory=list)
    main_hypothesis_statements: List[Dict[str, str]] = Field(default_factory=list)
    normalization_audit: List[Dict[str, Any]] = Field(default_factory=list)
