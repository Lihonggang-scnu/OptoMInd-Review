"""OptoMind research package.

Current mainline (P0 → M1 → M2a → M2b → M3 → M4):
  DomainConfigLoader   — P0 domain config
  ReviewMentorAgent    — M1.5 writing-architecture advisor
  ReviewBlueprintPlanner — M2 full pipeline orchestrator
  ClaimDecomposer      — M2a claim decomposition
  ArgumentDAGBuilder   — M2b argument DAG
  GapOAExpander        — M3 gap retrieval
  VisualArgumentAlignment — M4 visual argument

Legacy v2/v2.7 modules (SearchEngine, QueryPlannerAgent, etc.) are kept for
backward compatibility but not part of the active pipeline.
"""

from __future__ import annotations

from typing import Any


__all__ = [
    # Current mainline
    "ReviewBlueprintPlanner",
    "ClaimDecomposer",
    "ArgumentDAGBuilder",
    "ReviewMentorAgent",
    "GapOAExpander",
    "VisualArgumentAlignment",
    # Legacy v2
    "SearchEngine",
    "ResearchSettings",
    "QueryPlannerAgent",
    "LiteratureResourceBuilder",
    "LiteratureResourceLibrary",
]


def __getattr__(name: str) -> Any:
    # Current mainline
    if name == "ReviewBlueprintPlanner":
        from .review_blueprint_planner import DynamicReviewBlueprintPlanner
        return DynamicReviewBlueprintPlanner
    if name == "ClaimDecomposer":
        from .claim_decomposer import ClaimDecomposer
        return ClaimDecomposer
    if name == "ArgumentDAGBuilder":
        from .argument_dag_builder import ArgumentDAGBuilder
        return ArgumentDAGBuilder
    if name == "ReviewMentorAgent":
        from .review_mentor_agent import ReviewMentorAgent
        return ReviewMentorAgent
    if name == "GapOAExpander":
        from .gap_oa_expander import GapOAEvidenceExpander
        return GapOAEvidenceExpander
    if name == "VisualArgumentAlignment":
        from .visual_argument_alignment import VisualArgumentAligner
        return VisualArgumentAligner
    # Legacy v2
    if name == "SearchEngine":
        from .search_engine import SearchEngine
        return SearchEngine
    if name == "ResearchSettings":
        from .config import ResearchSettings
        return ResearchSettings
    if name == "QueryPlannerAgent":
        from .query_planner import QueryPlannerAgent
        return QueryPlannerAgent
    if name == "LiteratureResourceBuilder":
        from .literature_resource_builder import LiteratureResourceBuilder
        return LiteratureResourceBuilder
    if name == "LiteratureResourceLibrary":
        from .literature_resource_builder import LiteratureResourceLibrary
        return LiteratureResourceLibrary
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
