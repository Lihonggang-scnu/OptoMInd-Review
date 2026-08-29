"""Offline-safe contracts for OptoMind supplementary retrieval.

This module is the stable foundation behind the pre-supplementary-retrieval
checkpoint.  It defines exactly five gap types, a shared immutable-ish context
registry with stable reusable field cells, the per-task contract, stable task
fingerprints, portfolio guardrails, and materialization policy constants.

The context catalog deliberately stores fine-grained, reusable field cells
(for example ``target_claim_or_sentence`` and ``visual_slots``) instead of
coarse containers.  Each gap task references the minimal purposeful subset for
its type.  Portfolio guardrails (max 200 references, background-only fraction
<= 0.25) belong to final review portfolio selection and are provided here as a
downstream selection helper, never as a material-library commit gate.

It deliberately performs no network I/O, model calls, or credential access.
The old ``blueprint_gap_adapter`` terminology (for example
``evidence_permission_deficit``) is not a gap type here; evidence permission is
encoded as material requirements and context cells.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping


SCHEMA_VERSION = "supplementary_retrieval.contract.v1"
CONTEXT_REGISTRY_SCHEMA_VERSION = "supplementary_retrieval.context_registry.v1"
TASK_FINGERPRINT_SCHEMA_VERSION = "supplementary_retrieval.task_fingerprint.v1"
CONTEXT_PROJECTION_SCHEMA_VERSION = "supplementary_retrieval.context_projection.v1"
EXPANSION_POLICY_SCHEMA_VERSION = "supplementary_retrieval.expansion_policy.v1"

# The canonical, exhaustive set of supplementary retrieval gap types.
GAP_TYPES = (
    "claim_evidence_gap",
    "section_argument_gap",
    "review_structure_gap",
    "whole_review_gap",
    "visual_material_gap",
)

GAP_TYPE_LABELS = {
    "claim_evidence_gap": "A specific claim lacks sufficient verified evidence.",
    "section_argument_gap": "A section argument lacks sufficient literature support.",
    "review_structure_gap": "The planned article structure needs new sections or subsections.",
    "whole_review_gap": "The whole review has a coverage or balance deficiency.",
    "visual_material_gap": "A visual/figure material requirement is unmet.",
}

# Canonical graph expansion modes controlled by the task policy.  These are
# request/consumption intents: whether references, citations, cited-by,
# recommendations, or multi-seed expansion may be requested for the gap.
GRAPH_EXPANSION_MODES = (
    "references",
    "citations",
    "cited_by",
    "recommendations",
    "multi_seed",
)

# Old adapter vocabulary that must never be accepted as a gap type.  These are
# material/evidence constraints, not gap families.
NON_GAP_TYPE_ALIASES = {
    "evidence_permission_deficit": (
        "evidence_permission_deficit is not a gap type; encode evidence "
        "permission as material requirements and context cells."
    ),
    "abstract_only": (
        "abstract_only is not a gap type; abstracts are background-only "
        "material and are represented by material requirements."
    ),
    "abstract_claim": (
        "abstract_claim is not a gap type; it is a material class inside the "
        "materialization policy."
    ),
}

# Required context field-cell IDs per gap type.  Required subsets differ by
# type and match the purpose of the gap.
GAP_TYPE_REQUIRED_CONTEXT_FIELDS = {
    "claim_evidence_gap": (
        "topic_scope",
        "user_question",
        "dynamic_axes",
        "target_claim_or_sentence",
        "bound_papers_and_quotes",
        "missing_fact_units",
        "required_material_strength",
        "retrieval_success_criteria",
        "existing_paper_identities",
        "materialization_policy",
    ),
    "section_argument_gap": (
        "topic_scope",
        "user_question",
        "section_task",
        "argument_role",
        "dynamic_axes",
        "bound_papers_and_quotes",
        "missing_fact_units",
        "required_material_strength",
        "retrieval_success_criteria",
        "existing_paper_identities",
        "materialization_policy",
    ),
    "review_structure_gap": (
        "topic_scope",
        "user_question",
        "current_review_structure",
        "paper_introduction_conclusion_excerpts",
        "reviewer_feedback",
        "author_revision_history",
        "retrieval_success_criteria",
        "materialization_policy",
    ),
    "whole_review_gap": (
        "topic_scope",
        "user_question",
        "whole_review_feedback",
        "reviewer_feedback",
        "author_revision_history",
        "existing_paper_identities",
        "portfolio_limits",
        "retrieval_success_criteria",
    ),
    "visual_material_gap": (
        "topic_scope",
        "user_question",
        "visual_slots",
        "visual_gaps",
        "required_material_strength",
        "retrieval_success_criteria",
        "existing_paper_identities",
        "materialization_policy",
    ),
}

# Materialization priority for supplementary retrieval.  The priority is a
# preference, not a quota: any non-empty adequate source may enter downstream.
# If S2 structured body snippets are already adequate, public OA full text is
# unnecessary.  Abstract claims remain background-only.
MATERIALIZATION_PRIORITY = (
    "s2_structured_body",
    "public_oa_fulltext",
    "abstract_claim",
)
ABSTRACT_ONLY_BACKGROUND = True

# Portfolio guardrails for final review portfolio selection.  They are never
# applied at material-library commit time.
DEFAULT_PORTFOLIO_LIMITS = {
    "max_references": 200,
    "max_background_fraction": 0.25,
}

# Review-structure planning guardrails, not forced quotas.  The structure gap
# may plan at most three new sections and at most three new subsections per
# existing section; fewer is always valid.
STRUCTURE_GUARDRAILS = {
    "max_new_sections": 3,
    "max_new_subsections_per_existing_section": 3,
}


def validate_gap_type(value: Any) -> str:
    """Return the canonical gap type or raise ``ValueError``."""

    text = str(value or "").strip()
    alias_message = NON_GAP_TYPE_ALIASES.get(text.casefold())
    if alias_message:
        raise ValueError(alias_message)
    if text not in GAP_TYPES:
        raise ValueError(
            f"unknown gap type {text!r}; expected one of {', '.join(GAP_TYPES)}"
        )
    return text


@dataclass(frozen=True, slots=True)
class ContextFieldSpec:
    """Stable metadata for one shared reusable context field cell."""

    field_id: str
    description: str
    allowed_types: tuple[type, ...] = (object,)


CONTEXT_FIELD_CATALOG: dict[str, ContextFieldSpec] = {
    "user_question": ContextFieldSpec(
        "user_question",
        "Canonical English user/research question.",
        (str,),
    ),
    "dynamic_axes": ContextFieldSpec(
        "dynamic_axes",
        "Seed and material-emergent scientific axes for the question.",
        (list,),
    ),
    "section_task": ContextFieldSpec(
        "section_task",
        "One section task: ID, title, argument task, and required roles.",
        (dict,),
    ),
    "target_claim_or_sentence": ContextFieldSpec(
        "target_claim_or_sentence",
        "The exact claim/sentence needing evidence, with claim ID.",
        (dict,),
    ),
    "argument_role": ContextFieldSpec(
        "argument_role",
        "The section argument role this task must support.",
        (str,),
    ),
    "bound_papers_and_quotes": ContextFieldSpec(
        "bound_papers_and_quotes",
        "Papers and exact quotes already bound to claims/sections.",
        (list,),
    ),
    "reviewer_feedback": ContextFieldSpec(
        "reviewer_feedback",
        "Structured reviewer/mentor feedback driving the gap.",
        (dict,),
    ),
    "author_revision_history": ContextFieldSpec(
        "author_revision_history",
        "Prior revision attempts and their outcomes.",
        (list,),
    ),
    "missing_fact_units": ContextFieldSpec(
        "missing_fact_units",
        "Specific factual units still missing from the argument.",
        (list,),
    ),
    "required_material_strength": ContextFieldSpec(
        "required_material_strength",
        "Minimum material strength/evidence ceiling required by the gap.",
        (dict,),
    ),
    "retrieval_success_criteria": ContextFieldSpec(
        "retrieval_success_criteria",
        "Explicit success criteria for the retrieval task.",
        (list,),
    ),
    "existing_paper_identities": ContextFieldSpec(
        "existing_paper_identities",
        "Known paper identities already in the library.",
        (list,),
    ),
    "historical_queries": ContextFieldSpec(
        "historical_queries",
        "Previously executed queries imported into durable history.",
        (list,),
    ),
    "concurrent_queries": ContextFieldSpec(
        "concurrent_queries",
        "Queries currently queued/running from other tasks.",
        (list,),
    ),
    "current_review_structure": ContextFieldSpec(
        "current_review_structure",
        "Current article structure plus planned new sections/subsections; "
        "planning guardrails are upper bounds, not quotas.",
        (dict,),
    ),
    "paper_introduction_conclusion_excerpts": ContextFieldSpec(
        "paper_introduction_conclusion_excerpts",
        "Current paper introduction and conclusion excerpts.",
        (dict,),
    ),
    "whole_review_feedback": ContextFieldSpec(
        "whole_review_feedback",
        "Whole-article coverage/balance feedback and review-level gaps.",
        (dict,),
    ),
    "visual_slots": ContextFieldSpec(
        "visual_slots",
        "Planned visual slots with argument roles and placement.",
        (list,),
    ),
    "visual_gaps": ContextFieldSpec(
        "visual_gaps",
        "Specific unmet visual material requirements.",
        (list,),
    ),
    "topic_scope": ContextFieldSpec(
        "topic_scope",
        "Topic identity and scope contract (anchors, boundaries, lenses).",
        (dict,),
    ),
    "materialization_policy": ContextFieldSpec(
        "materialization_policy",
        "Materialization priority, abstract-background-only rule, and adequacy policy.",
        (dict,),
    ),
    "portfolio_limits": ContextFieldSpec(
        "portfolio_limits",
        "Final review portfolio guardrails: max references and background fraction.",
        (dict,),
    ),
}


@dataclass(frozen=True, slots=True)
class SupplementaryExpansionPolicy:
    """Task-specific independent expansion controls for one gap type.

    ``result_cap`` bounds discovery result papers per query.  Independent
    per-route caps bound snippet results per query, precise S2 papers, batch
    enrichment papers, OA papers, abstract-claim papers, and graph seeds;
    route caps never consume one another.  ``extra_request_cap`` is a
    backward-compatible emergency hard ceiling/audit field only and never
    shrinks a normal route.  Execution uses the individual booleans:
    role expansion, exact per-paper follow-up, batch enrichment, OA/full-text
    fallback, reference (backward) expansion, citation (forward/cited-by)
    expansion, recommendation expansion, multi-seed graph, and the visual
    route.  ``allow_graph_expansion`` and ``graph_modes`` are derived
    backward-compatible audit fields, never independent execution controls.
    """

    gap_type: str
    result_cap: int = 8
    extra_request_cap: int = 8
    s2_snippet_results_per_query_cap: int = 5
    s2_precise_paper_cap: int = 2
    batch_enrichment_paper_cap: int = 0
    oa_fulltext_paper_cap: int = 6
    abstract_claim_paper_cap: int = 8
    graph_seed_cap: int = 0
    allow_role_expansion: bool = False
    allow_exact_paper_followup: bool = True
    allow_batch_enrichment: bool = False
    allow_oa_fulltext_fallback: bool = True
    allow_reference_expansion: bool = False
    allow_citation_expansion: bool = False
    allow_recommendation_expansion: bool = False
    allow_multi_seed_graph: bool = False
    allow_visual_processing: bool = False

    @property
    def allow_graph_expansion(self) -> bool:
        """Derived audit flag: graph runs when any relation direction is on."""

        return bool(
            self.allow_reference_expansion
            or self.allow_citation_expansion
            or self.allow_recommendation_expansion
        )

    @property
    def graph_modes(self) -> tuple[str, ...]:
        """Derived audit modes from the independent relation switches."""

        modes: list[str] = []
        if self.allow_reference_expansion:
            modes.append("references")
        if self.allow_citation_expansion:
            modes.extend(("citations", "cited_by"))
        if self.allow_recommendation_expansion:
            modes.append("recommendations")
        if self.allow_multi_seed_graph:
            modes.append("multi_seed")
        return tuple(dict.fromkeys(modes))

    def validate(self) -> list[str]:
        """Return contract violations for this policy (empty means valid)."""

        errors: list[str] = []
        try:
            validate_gap_type(self.gap_type)
        except ValueError as exc:
            errors.append(str(exc))
        if not isinstance(self.result_cap, int) or self.result_cap < 1:
            errors.append("result_cap_must_be_positive_integer")
        if not isinstance(self.extra_request_cap, int) or self.extra_request_cap < 0:
            errors.append("extra_request_cap_must_be_non_negative_integer")
        for field, value in (
            ("s2_snippet_results_per_query_cap", self.s2_snippet_results_per_query_cap),
            ("s2_precise_paper_cap", self.s2_precise_paper_cap),
            ("batch_enrichment_paper_cap", self.batch_enrichment_paper_cap),
            ("oa_fulltext_paper_cap", self.oa_fulltext_paper_cap),
            ("abstract_claim_paper_cap", self.abstract_claim_paper_cap),
            ("graph_seed_cap", self.graph_seed_cap),
        ):
            if not isinstance(value, int) or value < 0:
                errors.append(f"{field}_must_be_non_negative_integer")
        if self.gap_type == "visual_material_gap" and not self.allow_visual_processing:
            errors.append("visual_material_gap_requires_visual_processing")
        return errors

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a stable, inspectable policy record."""

        return {
            "schema_version": EXPANSION_POLICY_SCHEMA_VERSION,
            "gap_type": self.gap_type,
            "result_cap": self.result_cap,
            "extra_request_cap": self.extra_request_cap,
            "s2_snippet_results_per_query_cap": self.s2_snippet_results_per_query_cap,
            "s2_precise_paper_cap": self.s2_precise_paper_cap,
            "batch_enrichment_paper_cap": self.batch_enrichment_paper_cap,
            "oa_fulltext_paper_cap": self.oa_fulltext_paper_cap,
            "abstract_claim_paper_cap": self.abstract_claim_paper_cap,
            "graph_seed_cap": self.graph_seed_cap,
            "allow_role_expansion": bool(self.allow_role_expansion),
            "allow_exact_paper_followup": bool(self.allow_exact_paper_followup),
            "allow_batch_enrichment": bool(self.allow_batch_enrichment),
            "allow_oa_fulltext_fallback": bool(self.allow_oa_fulltext_fallback),
            "allow_reference_expansion": bool(self.allow_reference_expansion),
            "allow_citation_expansion": bool(self.allow_citation_expansion),
            "allow_recommendation_expansion": bool(
                self.allow_recommendation_expansion
            ),
            "allow_multi_seed_graph": bool(self.allow_multi_seed_graph),
            "allow_visual_processing": bool(self.allow_visual_processing),
            "allow_graph_expansion": self.allow_graph_expansion,
            "graph_modes": sorted(set(self.graph_modes)),
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "SupplementaryExpansionPolicy":
        """Rehydrate from a mapping, failing closed on invalid values."""

        gap_type = validate_gap_type(payload.get("gap_type"))
        raw_modes = [
            str(item)
            for item in (payload.get("graph_modes") or [])
            if str(item).strip()
        ]
        unknown_modes = sorted(set(raw_modes) - set(GRAPH_EXPANSION_MODES))
        if unknown_modes:
            raise ValueError(
                "unknown_graph_expansion_mode:"
                + ",".join(unknown_modes)
            )
        # Backward-compatible legacy mapping: when only the coarse graph flag
        # is supplied, treat it as enabling every relation direction and
        # multi-seed.  Explicit independent switches always win.
        legacy_graph = bool(payload.get("allow_graph_expansion", False))
        has_individual = any(
            key in payload
            for key in (
                "allow_reference_expansion",
                "allow_citation_expansion",
                "allow_recommendation_expansion",
                "allow_multi_seed_graph",
            )
        )
        legacy_modes = set(raw_modes)
        if "multi_seed" in legacy_modes:
            legacy_graph = True
        raw_result_cap = payload.get("result_cap", 8)
        raw_extra_request_cap = payload.get("extra_request_cap", 8)
        policy = cls(
            gap_type=gap_type,
            result_cap=int(8 if raw_result_cap is None else raw_result_cap),
            extra_request_cap=int(
                8
                if raw_extra_request_cap is None
                else raw_extra_request_cap
            ),
            s2_snippet_results_per_query_cap=int(
                5
                if payload.get("s2_snippet_results_per_query_cap") is None
                else payload.get("s2_snippet_results_per_query_cap")
            ),
            s2_precise_paper_cap=int(
                2
                if payload.get("s2_precise_paper_cap") is None
                else payload.get("s2_precise_paper_cap")
            ),
            batch_enrichment_paper_cap=int(
                0
                if payload.get("batch_enrichment_paper_cap") is None
                else payload.get("batch_enrichment_paper_cap")
            ),
            oa_fulltext_paper_cap=int(
                6
                if payload.get("oa_fulltext_paper_cap") is None
                else payload.get("oa_fulltext_paper_cap")
            ),
            abstract_claim_paper_cap=int(
                8
                if payload.get("abstract_claim_paper_cap") is None
                else payload.get("abstract_claim_paper_cap")
            ),
            graph_seed_cap=int(
                0
                if payload.get("graph_seed_cap") is None
                else payload.get("graph_seed_cap")
            ),
            allow_role_expansion=bool(payload.get("allow_role_expansion", False)),
            allow_exact_paper_followup=bool(
                payload.get("allow_exact_paper_followup", True)
            ),
            allow_batch_enrichment=bool(
                payload.get("allow_batch_enrichment", False)
            ),
            allow_oa_fulltext_fallback=bool(
                payload.get("allow_oa_fulltext_fallback", True)
            ),
            allow_reference_expansion=(
                bool(payload.get("allow_reference_expansion", False))
                if has_individual
                else legacy_graph or "references" in legacy_modes
            ),
            allow_citation_expansion=(
                bool(payload.get("allow_citation_expansion", False))
                if has_individual
                else legacy_graph
                or "citations" in legacy_modes
                or "cited_by" in legacy_modes
            ),
            allow_recommendation_expansion=(
                bool(payload.get("allow_recommendation_expansion", False))
                if has_individual
                else legacy_graph or "recommendations" in legacy_modes
            ),
            allow_multi_seed_graph=(
                bool(payload.get("allow_multi_seed_graph", False))
                if has_individual
                else legacy_graph or "multi_seed" in legacy_modes
            ),
            allow_visual_processing=bool(
                payload.get("allow_visual_processing", False)
            ),
        )
        errors = policy.validate()
        if errors:
            raise ValueError("invalid expansion policy: " + "; ".join(errors))
        return policy


# Approved per-gap-type expansion defaults.  Per-route caps (snippet results
# per query, precise papers, batch papers, OA papers, abstract papers, graph
# seeds) are independent and never consume one another.  ``result_cap`` is
# discovery result papers per query.  ``extra_request_cap`` is a
# backward-compatible emergency hard ceiling/audit field only and never
# shrinks a normal route.
# These controls never change ordinary first-round retrieval behavior.
DEFAULT_EXPANSION_POLICIES: dict[str, SupplementaryExpansionPolicy] = {
    "claim_evidence_gap": SupplementaryExpansionPolicy(
        gap_type="claim_evidence_gap",
        result_cap=16,
        extra_request_cap=8,
        s2_snippet_results_per_query_cap=10,
        s2_precise_paper_cap=8,
        batch_enrichment_paper_cap=0,
        oa_fulltext_paper_cap=16,
        abstract_claim_paper_cap=8,
        graph_seed_cap=0,
        allow_role_expansion=False,
        allow_exact_paper_followup=True,
        allow_batch_enrichment=False,
        allow_oa_fulltext_fallback=True,
        allow_reference_expansion=False,
        allow_citation_expansion=False,
        allow_recommendation_expansion=False,
        allow_multi_seed_graph=False,
        allow_visual_processing=False,
    ),
    "section_argument_gap": SupplementaryExpansionPolicy(
        gap_type="section_argument_gap",
        result_cap=16,
        extra_request_cap=12,
        s2_snippet_results_per_query_cap=10,
        s2_precise_paper_cap=12,
        batch_enrichment_paper_cap=24,
        oa_fulltext_paper_cap=16,
        abstract_claim_paper_cap=12,
        graph_seed_cap=2,
        allow_role_expansion=True,
        allow_exact_paper_followup=True,
        allow_batch_enrichment=True,
        allow_oa_fulltext_fallback=True,
        allow_reference_expansion=True,
        allow_citation_expansion=True,
        allow_recommendation_expansion=False,
        allow_multi_seed_graph=False,
        allow_visual_processing=False,
    ),
    "review_structure_gap": SupplementaryExpansionPolicy(
        gap_type="review_structure_gap",
        result_cap=20,
        extra_request_cap=20,
        s2_snippet_results_per_query_cap=10,
        s2_precise_paper_cap=24,
        batch_enrichment_paper_cap=40,
        oa_fulltext_paper_cap=32,
        abstract_claim_paper_cap=24,
        graph_seed_cap=3,
        allow_role_expansion=True,
        allow_exact_paper_followup=True,
        allow_batch_enrichment=True,
        allow_oa_fulltext_fallback=True,
        allow_reference_expansion=True,
        allow_citation_expansion=True,
        allow_recommendation_expansion=True,
        allow_multi_seed_graph=True,
        allow_visual_processing=False,
    ),
    "whole_review_gap": SupplementaryExpansionPolicy(
        gap_type="whole_review_gap",
        result_cap=20,
        extra_request_cap=14,
        s2_snippet_results_per_query_cap=10,
        s2_precise_paper_cap=16,
        batch_enrichment_paper_cap=32,
        oa_fulltext_paper_cap=24,
        abstract_claim_paper_cap=16,
        graph_seed_cap=1,
        allow_role_expansion=True,
        allow_exact_paper_followup=True,
        allow_batch_enrichment=True,
        allow_oa_fulltext_fallback=True,
        allow_reference_expansion=True,
        allow_citation_expansion=True,
        allow_recommendation_expansion=False,
        allow_multi_seed_graph=False,
        allow_visual_processing=False,
    ),
    "visual_material_gap": SupplementaryExpansionPolicy(
        gap_type="visual_material_gap",
        result_cap=6,
        extra_request_cap=8,
        s2_snippet_results_per_query_cap=5,
        s2_precise_paper_cap=2,
        batch_enrichment_paper_cap=0,
        oa_fulltext_paper_cap=6,
        abstract_claim_paper_cap=6,
        graph_seed_cap=0,
        allow_role_expansion=False,
        allow_exact_paper_followup=True,
        allow_batch_enrichment=False,
        allow_oa_fulltext_fallback=True,
        allow_reference_expansion=False,
        allow_citation_expansion=False,
        allow_recommendation_expansion=False,
        allow_multi_seed_graph=False,
        allow_visual_processing=True,
    ),
}


def resolve_expansion_policy(
    task: SupplementaryRetrievalTask,
    overrides: Mapping[str, Any] | None = None,
) -> SupplementaryExpansionPolicy:
    """Resolve the effective expansion policy for a task.

    ``task.metadata["expansion_policy"]`` and the optional ``overrides``
    mapping may override individual controls while keeping the gap-type
    defaults as the base.  Unknown override keys are ignored so forward
    compatibility is preserved.
    """

    gap_type = validate_gap_type(task.gap_type)
    base = DEFAULT_EXPANSION_POLICIES[gap_type]
    merged: dict[str, Any] = base.to_dict()
    sources: list[Mapping[str, Any]] = []
    metadata_policy = task.metadata.get("expansion_policy")
    if isinstance(metadata_policy, Mapping):
        sources.append(metadata_policy)
    if isinstance(overrides, Mapping):
        sources.append(overrides)
    individual_keys = {
        "allow_role_expansion",
        "allow_exact_paper_followup",
        "allow_batch_enrichment",
        "allow_oa_fulltext_fallback",
        "allow_reference_expansion",
        "allow_citation_expansion",
        "allow_recommendation_expansion",
        "allow_multi_seed_graph",
        "allow_visual_processing",
    }
    for source in sources:
        has_individual = bool(individual_keys & set(source))
        if (
            not has_individual
            and "allow_graph_expansion" in source
        ):
            legacy_value = bool(source["allow_graph_expansion"])
            merged["allow_reference_expansion"] = legacy_value
            merged["allow_citation_expansion"] = legacy_value
            merged["allow_recommendation_expansion"] = legacy_value
            merged["allow_multi_seed_graph"] = legacy_value
        if not has_individual and "graph_modes" in source:
            modes = {
                str(item)
                for item in (source.get("graph_modes") or [])
                if str(item).strip()
            }
            unknown_modes = sorted(set(modes) - set(GRAPH_EXPANSION_MODES))
            if unknown_modes:
                raise ValueError(
                    "unknown_graph_expansion_mode:"
                    + ",".join(unknown_modes)
                )
            merged["allow_reference_expansion"] = "references" in modes
            merged["allow_citation_expansion"] = (
                "citations" in modes or "cited_by" in modes
            )
            merged["allow_recommendation_expansion"] = (
                "recommendations" in modes
            )
            merged["allow_multi_seed_graph"] = "multi_seed" in modes
        for key, value in source.items():
            if key in individual_keys or key in {
                "result_cap",
                "extra_request_cap",
                "s2_snippet_results_per_query_cap",
                "s2_precise_paper_cap",
                "batch_enrichment_paper_cap",
                "oa_fulltext_paper_cap",
                "abstract_claim_paper_cap",
                "graph_seed_cap",
            }:
                merged[key] = value
    policy = SupplementaryExpansionPolicy.from_dict(merged)
    errors = policy.validate()
    if errors:
        raise ValueError("invalid expansion policy: " + "; ".join(errors))
    return policy


def project_context_for_task(
    task: SupplementaryRetrievalTask,
    registry: ContextRegistry,
) -> dict[str, Any]:
    """Project only the task's declared context subset plus task metadata.

    This is the exact mapping handed to the query generator.  It contains the
    minimal reusable field cells referenced by the task (never a whole-registry
    dump) plus an inspectable ``task_metadata`` block with the task identity
    and the resolved expansion policy.
    """

    errors = list(task.validate())
    errors.extend(validate_task_context(task, registry))
    if errors:
        raise ValueError("; ".join(errors))
    resolved = registry.resolve(task.context_refs)
    projected = {
        field_id: copy.deepcopy(value)
        for field_id, value in resolved.items()
    }
    projected["task_metadata"] = {
        "schema_version": CONTEXT_PROJECTION_SCHEMA_VERSION,
        "task_id": task.task_id,
        "gap_type": task.gap_type,
        "context_field_ids": sorted(set(task.context_refs)),
        "priority": task.priority,
        "success_criteria": sorted(set(task.success_criteria)),
        "material_requirements": sorted(set(task.material_requirements)),
        "expansion_policy": resolve_expansion_policy(task).to_dict(),
    }
    return projected


class ContextValidationError(ValueError):
    """Raised when a context registry cannot satisfy a task's references."""


def _json_default(value: Any) -> Any:
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, (set, frozenset)):
        return sorted(str(item) for item in value)
    if hasattr(value, "to_dict"):
        return value.to_dict()
    if hasattr(value, "__dataclass_fields__"):
        return asdict(value)
    return str(value)


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=_json_default,
    )


@dataclass(slots=True)
class ContextRegistry:
    """Shared, reusable context storage with stable field-cell IDs.

    Each comprehensive reusable field is stored exactly once and referenced by
    stable ID.  ``freeze()`` returns a structurally deep copy that rejects
    further mutation, so nested mutation of the original cannot change a frozen
    registry or its task fingerprints.
    """

    fields: dict[str, Any] = field(default_factory=dict)
    schema_version: str = CONTEXT_REGISTRY_SCHEMA_VERSION
    _frozen: bool = False

    def set(self, field_id: str, value: Any) -> "ContextRegistry":
        """Store one cataloged field value; validates ID/type; raises after freeze."""

        if self._frozen:
            raise RuntimeError("ContextRegistry is frozen and cannot be mutated")
        spec = CONTEXT_FIELD_CATALOG.get(str(field_id or ""))
        if spec is None:
            raise ContextValidationError(f"unknown_context_field:{field_id}")
        if spec.allowed_types != (object,) and not isinstance(value, spec.allowed_types):
            expected = " or ".join(t.__name__ for t in spec.allowed_types)
            raise TypeError(
                f"context field {field_id!r} must be {expected}, got {type(value).__name__}"
            )
        self.fields[str(field_id)] = value
        return self

    def freeze(self) -> "ContextRegistry":
        """Return a deeply copied registry that rejects further mutation."""

        frozen = ContextRegistry(
            fields=copy.deepcopy(self.fields),
            schema_version=self.schema_version,
        )
        frozen._frozen = True
        return frozen

    def resolve(self, refs: Iterable[str]) -> dict[str, Any]:
        """Resolve referenced fields to values; raises on unknown/missing."""

        errors: list[str] = []
        resolved: dict[str, Any] = {}
        for raw in refs:
            field_id = str(raw or "").strip()
            if field_id not in CONTEXT_FIELD_CATALOG:
                errors.append(f"unknown_context_field:{field_id}")
            elif field_id not in self.fields:
                errors.append(f"missing_context_field:{field_id}")
            else:
                resolved[field_id] = self.fields[field_id]
        if errors:
            raise ContextValidationError("; ".join(errors))
        return resolved

    def to_dict(self) -> dict[str, Any]:
        """Serialize the registry for durable storage."""

        return {
            "schema_version": self.schema_version,
            "frozen": self._frozen,
            "fields": dict(self.fields),
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "ContextRegistry":
        """Rehydrate a registry through ``set()`` so IDs/types are validated."""

        raw_fields = payload.get("fields")
        if not isinstance(raw_fields, Mapping):
            raise ContextValidationError("context_registry.fields_must_be_mapping")
        registry = cls(
            schema_version=str(
                payload.get("schema_version") or CONTEXT_REGISTRY_SCHEMA_VERSION
            )
        )
        for field_id, value in raw_fields.items():
            registry.set(field_id, value)
        if bool(payload.get("frozen")):
            return registry.freeze()
        return registry


_SAFE_SLUG = re.compile(r"^[a-zA-Z0-9_\-]{1,160}$")


@dataclass(slots=True)
class SupplementaryRetrievalTask:
    """One durable supplementary retrieval gap task.

    The task references a required subset of shared context field cells,
    carries explicit success criteria and material requirements, records
    history and source provenance, and computes a stable fingerprint.
    """

    task_id: str
    gap_type: str
    context_refs: tuple[str, ...]
    priority: int = 0
    source_provenance: dict[str, Any] = field(default_factory=dict)
    history_refs: tuple[str, ...] = ()
    success_criteria: tuple[str, ...] = ()
    material_requirements: tuple[str, ...] = ()
    retrieval_queries: tuple[str, ...] = ()
    visual_route: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)

    def validate(self) -> list[str]:
        """Return a list of contract violations (empty means valid)."""

        errors: list[str] = []
        if not _SAFE_SLUG.match(str(self.task_id or "")):
            errors.append(f"invalid_task_id:{self.task_id!r}")
        try:
            validate_gap_type(self.gap_type)
        except ValueError as exc:
            errors.append(str(exc))
        if not self.context_refs:
            errors.append("context_refs_must_not_be_empty")
        if not isinstance(self.priority, int):
            errors.append("priority_must_be_integer")
        if not self.success_criteria:
            errors.append("success_criteria_must_not_be_empty")
        if not self.material_requirements:
            errors.append("material_requirements_must_not_be_empty")
        if not isinstance(self.source_provenance, dict) or not self.source_provenance:
            errors.append("source_provenance_must_be_non_empty_dict")
        if self.gap_type == "visual_material_gap" and not self.visual_route:
            errors.append("visual_material_gap_requires_visual_route")
        if self.gap_type != "visual_material_gap" and self.visual_route:
            errors.append("visual_route_only_for_visual_material_gap")
        for query in self.retrieval_queries:
            if not isinstance(query, str) or not query.strip():
                errors.append("retrieval_queries_must_be_non_empty_strings")
        return errors

    def is_visual(self) -> bool:
        """Return True when this task must use the visual callback route."""

        return bool(self.visual_route) or self.gap_type == "visual_material_gap"

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "gap_type": self.gap_type,
            "context_refs": sorted(set(self.context_refs)),
            "priority": self.priority,
            "source_provenance": dict(self.source_provenance),
            "history_refs": sorted(set(self.history_refs)),
            "success_criteria": sorted(set(self.success_criteria)),
            "material_requirements": sorted(set(self.material_requirements)),
            "retrieval_queries": list(self.retrieval_queries),
            "visual_route": bool(self.is_visual()),
            "metadata": dict(self.metadata),
        }


def validate_task_context(
    task: SupplementaryRetrievalTask,
    registry: ContextRegistry,
) -> list[str]:
    """Validate a task's context refs against the registry and its gap type."""

    errors: list[str] = []
    seen: set[str] = set()
    for raw in task.context_refs:
        field_id = str(raw or "").strip()
        if not field_id or field_id in seen:
            continue
        seen.add(field_id)
        if field_id not in CONTEXT_FIELD_CATALOG:
            errors.append(f"unknown_context_field:{field_id}")
        elif field_id not in registry.fields:
            errors.append(f"missing_context_field:{field_id}")
    required = GAP_TYPE_REQUIRED_CONTEXT_FIELDS.get(task.gap_type, ())
    for field_id in required:
        if field_id not in seen:
            errors.append(f"missing_required_context:{field_id}")
    if "current_review_structure" in seen and "current_review_structure" in registry.fields:
        errors.extend(
            validate_current_review_structure(registry.fields["current_review_structure"])
        )
    if (
        "paper_introduction_conclusion_excerpts" in seen
        and "paper_introduction_conclusion_excerpts" in registry.fields
    ):
        errors.extend(
            validate_paper_introduction_conclusion_excerpts(
                registry.fields["paper_introduction_conclusion_excerpts"]
            )
        )
    return errors


def task_fingerprint(
    task: SupplementaryRetrievalTask,
    registry: ContextRegistry,
) -> str:
    """Compute a stable identity fingerprint for a task and its context.

    The fingerprint intentionally excludes the task ID, priority, and volatile
    source-provenance timestamps so that identical work submitted by different
    callers maps to the same durable task.
    """

    resolved = registry.resolve(task.context_refs)
    payload = {
        "schema_version": TASK_FINGERPRINT_SCHEMA_VERSION,
        "gap_type": task.gap_type,
        "context_refs": sorted(set(task.context_refs)),
        "context_values": {
            field_id: resolved[field_id] for field_id in sorted(task.context_refs)
        },
        "retrieval_queries": sorted(
            {str(q).strip() for q in task.retrieval_queries if str(q).strip()}
        ),
        "material_requirements": sorted(set(task.material_requirements)),
        "success_criteria": sorted(set(task.success_criteria)),
        "history_refs": sorted(set(task.history_refs)),
        "visual_route": bool(task.is_visual()),
        "expansion_policy": resolve_expansion_policy(task).to_dict(),
    }
    raw = _canonical_json(payload).encode("utf-8")
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def validate_portfolio_limits(
    total_references: int,
    background_only_references: int,
    limits: Mapping[str, Any] | None = None,
) -> tuple[bool, list[str]]:
    """Check final-review-portfolio guardrails; returns (ok, violations).

    This is a downstream portfolio-selection helper.  It must never gate
    material-library commit.
    """

    merged = dict(DEFAULT_PORTFOLIO_LIMITS)
    if isinstance(limits, Mapping):
        merged.update({k: v for k, v in limits.items() if v is not None})
    max_references = max(0, int(merged.get("max_references", 200) or 200))
    max_background_fraction = float(
        merged.get("max_background_fraction", 0.25) or 0.25
    )
    total = max(0, int(total_references or 0))
    background = max(0, int(background_only_references or 0))
    violations: list[str] = []
    if total > max_references:
        violations.append(
            f"portfolio_max_references_exceeded:{total}>{max_references}"
        )
    fraction = background / total if total > 0 else 0.0
    if fraction > max_background_fraction + 1e-9:
        violations.append(
            f"portfolio_background_fraction_exceeded:{fraction:.4f}>{max_background_fraction}"
        )
    return not violations, violations


def validate_current_review_structure(value: Any) -> list[str]:
    """Validate structure-planning guardrails (upper bounds, not quotas)."""

    errors: list[str] = []
    if not isinstance(value, Mapping):
        return ["current_review_structure_must_be_mapping"]
    existing = value.get("existing_sections")
    if existing is not None and not isinstance(existing, (list, tuple)):
        errors.append("existing_sections_must_be_list")
    new_sections = value.get("new_sections") or []
    if isinstance(new_sections, (list, tuple)):
        if len(new_sections) > STRUCTURE_GUARDRAILS["max_new_sections"]:
            errors.append(
                "structure_guardrail_max_new_sections:"
                f"{len(new_sections)}>{STRUCTURE_GUARDRAILS['max_new_sections']}"
            )
    else:
        errors.append("new_sections_must_be_list")
    subsections = value.get("new_subsections_per_existing_section") or {}
    if isinstance(subsections, Mapping):
        for section_id, count in subsections.items():
            try:
                number = int(count)
            except (TypeError, ValueError):
                errors.append(f"subsection_count_not_integer:{section_id}")
                continue
            if number > STRUCTURE_GUARDRAILS["max_new_subsections_per_existing_section"]:
                errors.append(
                    "structure_guardrail_max_new_subsections:"
                    f"{section_id}:{number}>"
                    f"{STRUCTURE_GUARDRAILS['max_new_subsections_per_existing_section']}"
                )
    else:
        errors.append("new_subsections_per_existing_section_must_be_mapping")
    return errors


def validate_paper_introduction_conclusion_excerpts(value: Any) -> list[str]:
    """Validate that both paper excerpt cells are present and non-empty."""

    errors: list[str] = []
    if not isinstance(value, Mapping):
        return ["paper_introduction_conclusion_excerpts_must_be_mapping"]
    intro = value.get("current_paper_introduction_excerpt")
    conclusion = value.get("current_paper_conclusion_excerpt")
    if not isinstance(intro, str) or not intro.strip():
        errors.append("missing_current_paper_introduction_excerpt")
    if not isinstance(conclusion, str) or not conclusion.strip():
        errors.append("missing_current_paper_conclusion_excerpt")
    return errors


def validate_materialization_policy(value: Any) -> list[str]:
    """Validate the materialization policy shape; routes are preferences."""

    errors: list[str] = []
    if not isinstance(value, Mapping):
        return ["materialization_policy_must_be_mapping"]
    priority = value.get("priority")
    if priority is not None:
        if not isinstance(priority, (list, tuple)):
            errors.append("materialization_policy.priority_must_be_sequence")
        else:
            normalized = tuple(str(item) for item in priority)
            if any(item not in MATERIALIZATION_PRIORITY for item in normalized):
                errors.append(
                    "materialization_policy.unknown_route:"
                    + ",".join(
                        item
                        for item in normalized
                        if item not in MATERIALIZATION_PRIORITY
                    )
                )
    abstract_rule = value.get("abstract_background_only", ABSTRACT_ONLY_BACKGROUND)
    if not isinstance(abstract_rule, bool):
        errors.append("materialization_policy.abstract_background_only_must_be_bool")
    return errors


__all__ = [
    "ABSTRACT_ONLY_BACKGROUND",
    "CONTEXT_FIELD_CATALOG",
    "CONTEXT_PROJECTION_SCHEMA_VERSION",
    "CONTEXT_REGISTRY_SCHEMA_VERSION",
    "ContextFieldSpec",
    "ContextRegistry",
    "ContextValidationError",
    "DEFAULT_EXPANSION_POLICIES",
    "DEFAULT_PORTFOLIO_LIMITS",
    "EXPANSION_POLICY_SCHEMA_VERSION",
    "GRAPH_EXPANSION_MODES",
    "GAP_TYPE_LABELS",
    "GAP_TYPE_REQUIRED_CONTEXT_FIELDS",
    "GAP_TYPES",
    "MATERIALIZATION_PRIORITY",
    "NON_GAP_TYPE_ALIASES",
    "SCHEMA_VERSION",
    "STRUCTURE_GUARDRAILS",
    "SupplementaryExpansionPolicy",
    "SupplementaryRetrievalTask",
    "TASK_FINGERPRINT_SCHEMA_VERSION",
    "task_fingerprint",
    "project_context_for_task",
    "resolve_expansion_policy",
    "validate_current_review_structure",
    "validate_gap_type",
    "validate_materialization_policy",
    "validate_paper_introduction_conclusion_excerpts",
    "validate_portfolio_limits",
    "validate_task_context",
]
