"""Typed contracts for completing a review article around its body sections.

These schemas deliberately separate article-level rhetoric from the body
section list.  Coverage research and section authoring therefore continue to
operate on scientific body sections only, while the introduction, outlook,
conclusion, abstract, and global figures receive explicit contracts.
"""

from __future__ import annotations

from typing import Dict, List, Literal

from pydantic import BaseModel, Field


MethodologyIdentity = Literal[
    "critical_narrative_review",
    "scoping_review",
    "systematic_review",
    "perspective_review",
]
EvidenceState = Literal["established", "conditional", "open", "speculative"]


class WordRange(BaseModel):
    min: int = 180
    max: int = 300


class IntroductionContract(BaseModel):
    why_now: str
    reader_prerequisites: List[str] = Field(default_factory=list)
    problem_reframing: str
    review_gap: str
    scope_and_method_disclosure: str
    roadmap_function: str


class BodyContract(BaseModel):
    primary_taxonomy: str
    cross_cutting_dimensions: List[str] = Field(default_factory=list)
    progression_logic: str
    must_resolve: List[str] = Field(default_factory=list)


class OutlookContract(BaseModel):
    challenge_axes: List[str] = Field(default_factory=list)
    required_reasoning: List[str] = Field(default_factory=list)
    speculation_policy: str


class ConclusionContract(BaseModel):
    required_takeaways: int = 4
    must_answer_central_question: bool = True
    no_new_topics: bool = True
    no_new_evidence: bool = True


class AbstractContract(BaseModel):
    target_word_range: WordRange = Field(default_factory=WordRange)
    required_moves: List[str] = Field(default_factory=list)


class GlobalFigureContract(BaseModel):
    candidate_templates: List[str] = Field(default_factory=list)
    selection_rule: str


class ArticleRhetoricalContract(BaseModel):
    schema_version: str = "article_rhetorical_contract.v1"
    methodology_identity: MethodologyIdentity = "critical_narrative_review"
    target_article_type: str = "comprehensive_review"
    target_audience: List[str] = Field(default_factory=list)
    provisional_title: str
    central_question: str
    review_thesis: str
    distinctive_angle: str
    scope_inclusions: List[str] = Field(default_factory=list)
    scope_exclusions: List[str] = Field(default_factory=list)
    introduction_contract: IntroductionContract
    body_contract: BodyContract
    outlook_contract: OutlookContract
    conclusion_contract: ConclusionContract = Field(
        default_factory=ConclusionContract
    )
    abstract_contract: AbstractContract = Field(default_factory=AbstractContract)
    global_figure_contract: GlobalFigureContract
    provenance: Literal["native", "legacy_compatibility"] = "native"


class VisualTakeaway(BaseModel):
    visual_chunk_id: str
    argumentative_function: str


class SectionHandoffCard(BaseModel):
    schema_version: str = "section_handoff_card.v1"
    section_id: str
    section_title: str
    section_argument_completed: bool = False
    established_takeaways: List[str] = Field(default_factory=list)
    conditional_judgments: List[str] = Field(default_factory=list)
    unresolved_tensions: List[str] = Field(default_factory=list)
    terms_defined: List[str] = Field(default_factory=list)
    avoid_repeating: List[str] = Field(default_factory=list)
    forward_question: str = ""
    why_next_section_is_needed: str = ""
    visual_takeaways: List[VisualTakeaway] = Field(default_factory=list)
    used_paper_ids: List[str] = Field(default_factory=list)
    used_chunk_ids: List[str] = Field(default_factory=list)


class SourceDiversity(BaseModel):
    unique_papers: int = 0
    direct_papers: int = 0


class SectionContribution(BaseModel):
    section_id: str
    argument_role: str = ""
    established_takeaways: List[str] = Field(default_factory=list)
    conditional_judgments: List[str] = Field(default_factory=list)
    unresolved_tensions: List[str] = Field(default_factory=list)
    source_diversity: SourceDiversity = Field(default_factory=SourceDiversity)


class ChallengeCandidate(BaseModel):
    challenge_id: str
    statement: str
    linked_section_ids: List[str] = Field(default_factory=list)
    evidence_state: Literal["established", "conditional", "open"] = "open"
    root_cause: str
    current_responses: List[str] = Field(default_factory=list)
    remaining_boundary: str


class OutlookCandidate(BaseModel):
    opportunity_id: str
    linked_challenge_ids: List[str] = Field(default_factory=list)
    direction: str
    actionable_milestones: List[str] = Field(default_factory=list)
    success_indicators: List[str] = Field(default_factory=list)
    confidence: Literal["high", "medium", "low"] = "medium"
    downstream_research_plan_ready: bool = False


class ReferenceInventory(BaseModel):
    unique_paper_ids: List[str] = Field(default_factory=list)
    landmark_paper_ids: List[str] = Field(default_factory=list)
    frontier_paper_ids: List[str] = Field(default_factory=list)


class VisualInventory(BaseModel):
    existing_visual_ids: List[str] = Field(default_factory=list)
    global_figure_opportunities: List[str] = Field(default_factory=list)


class ArticleSynthesisMap(BaseModel):
    schema_version: str = "article_synthesis_map.v1"
    article_question: str
    review_thesis: str
    methodology_identity: MethodologyIdentity = "critical_narrative_review"
    section_contributions: List[SectionContribution] = Field(default_factory=list)
    review_wide_consensus: List[str] = Field(default_factory=list)
    review_wide_disagreements: List[str] = Field(default_factory=list)
    cross_section_tradeoffs: List[str] = Field(default_factory=list)
    challenge_candidates: List[ChallengeCandidate] = Field(default_factory=list)
    outlook_candidates: List[OutlookCandidate] = Field(default_factory=list)
    conclusion_candidates: List[str] = Field(default_factory=list)
    intro_promise_candidates: List[str] = Field(default_factory=list)
    reference_inventory: ReferenceInventory = Field(
        default_factory=ReferenceInventory
    )
    visual_inventory: VisualInventory = Field(default_factory=VisualInventory)


class OutlookItem(BaseModel):
    opportunity_id: str
    text_span: str
    linked_section_ids: List[str] = Field(default_factory=list)
    linked_challenge_ids: List[str] = Field(default_factory=list)
    evidence_state: EvidenceState = "conditional"
    downstream_research_plan_ready: bool = False


class CompletionQualitySelfCheck(BaseModel):
    introduction_promises: List[str] = Field(default_factory=list)
    conclusion_takeaways: List[str] = Field(default_factory=list)
    abstract_major_messages: List[str] = Field(default_factory=list)
    new_topic_declared: bool = False


class ArticleCompletionPackage(BaseModel):
    schema_version: str = "article_completion_package.v1"
    title: str
    abstract: str
    introduction: str
    challenge_and_outlook: str
    conclusion: str
    methodology_identity: MethodologyIdentity = "critical_narrative_review"
    outlook_items: List[OutlookItem] = Field(default_factory=list)
    quality_self_check: CompletionQualitySelfCheck = Field(
        default_factory=CompletionQualitySelfCheck
    )


class GlobalFigureItem(BaseModel):
    figure_id: str
    template_kind: Literal[
        "field_map",
        "timeline",
        "benchmark_landscape",
        "challenge_roadmap",
        "mechanism_synthesis",
        "taxonomy_map",
    ]
    argumentative_purpose: str
    placement: str
    eligibility_status: Literal["eligible", "ineligible", "needs_data"]
    eligibility_reasons: List[str] = Field(default_factory=list)
    source_route: Literal[
        "existing",
        "composite",
        "deterministic_plot",
        "generated",
        "unfilled",
    ] = "unfilled"
    visual_chunk_ids: List[str] = Field(default_factory=list)
    data_provenance_level: Literal["exact", "approximate", "schematic"] = (
        "schematic"
    )
    generation_brief: str = ""
    human_review_required: bool = True


class GlobalFigurePlan(BaseModel):
    schema_version: str = "global_figure_plan.v1"
    article_level_figures: List[GlobalFigureItem] = Field(default_factory=list)
    section_level_figures: List[Dict] = Field(default_factory=list)
    intentionally_unfilled: List[Dict] = Field(default_factory=list)


# Additive staged full-manuscript completion contracts (see
# staged_article_completion.py).  Re-exported here so the schema hub stays
# importable from one place without changing existing contracts.
from .staged_article_completion import (  # noqa: E402
    AbstractDraft,
    AbstractWorkplan,
    BoundedPatchProposal,
    CommanderStructuralAuthority,
    ConclusionDraft,
    ConclusionWorkplan,
    IntroductionDraft,
    IntroductionWorkplan,
    ManuscriptReviewFinding,
    ManuscriptReviewReport,
    MultiReviewerReport,
    PatchProposalSet,
    QwenMultiReviewerProvider,
    QwenStagedProvider,
    ReviewerRole,
    SCHEMA_VERSION as STAGED_SCHEMA_VERSION,
    STAGE_ORDER,
    StagedArticleCompletionState,
    StagedStageState,
    aggregate_multi_reviewer_report,
    make_multi_reviewer_qwen_provider,
    make_qwen_stage_provider,
)
