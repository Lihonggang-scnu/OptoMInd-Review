"""Claim dataclass and validation utilities for blueprint planning.

T1 upgrade (2026-07-11):
- Added claim_kind (8 types) to enable gap-type routing in M3 and DAG logic
- Added evidence_relation_type (8 types) for precise source-to-claim bindings
- Added sentence completeness check (no hard char truncation)
- claim_kind + evidence_relation_type must be present on every claim after M2a
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import List

VALID_EVIDENCE_TYPES = ("mechanism", "measurement", "comparison", "application")

# Importance is a writing-planning level, not an evidence verdict.  It tells
# the coverage loop which missing claims can block a section and which gaps can
# remain explicitly declared.
VALID_CLAIM_IMPORTANCE = ("load_bearing", "supporting", "optional")

# Lower rank = more foundational; DAG edges only go low-to-high
EVIDENCE_TYPE_RANK: dict[str, int] = {t: i for i, t in enumerate(VALID_EVIDENCE_TYPES)}

# T1: claim kind taxonomy — determines which M3 retrieval strategy to use
VALID_CLAIM_KINDS = (
    "direct_fact",           # A specific empirical fact or measurement claim
    "mechanism_synthesis",   # Explains causal mechanism from multiple sources
    "quantitative_comparison",  # Compares quantities across studies/conditions
    "corpus_prevalence",     # Claims something is widespread / rare in literature
    "absence_or_neglect",    # Claims a gap or neglected direction exists
    "methodological_critique",  # Critiques measurement or experimental methods
    "frontier_uncertainty",  # Acknowledges an unresolved scientific question
    "normative_recommendation",  # Recommends future direction or standard
)

# T1: evidence relation types — finer-grained than evidence_binding_status
VALID_EVIDENCE_RELATION_TYPES = (
    "direct_support",      # Paper directly and fully supports the claim
    "component_support",   # Paper supports one component of a compound claim
    "indirect_support",    # Paper supports through an intermediate mechanism
    "method_transfer",     # Method or technique from paper is applicable
    "contrast",            # Paper provides a contrasting or comparative case
    "background_only",     # Paper only establishes background context
    "contradiction",       # Paper contradicts the claim
    "not_relevant",        # Paper turns out not relevant after inspection
)

# Sentence-completeness heuristic — a claim must end with a period, ?, or !
# and must not be cut off mid-word or mid-phrase.
_INCOMPLETE_ENDINGS = re.compile(
    r"(?:"
    r"[\w,;\-\(]\s*$"           # ends with word char or hanging punctuation
    r"|\b(?:and|or|but|the|a|an|of|in|on|at|to|for|with|that|which|whose)\s*$"  # dangling conjunction/preposition
    r")",
    re.I,
)

_SENTENCE_TERMINATORS = re.compile(r"[.!?。！？]\s*(?:['\"]|\)|])*\s*$")


def check_sentence_completeness(statement: str) -> tuple[bool, str]:
    """Return (is_complete, reason).

    A statement is complete when:
    1. It ends with a sentence-terminating punctuation (., !, ?).
    2. It does not end with a hanging conjunction/preposition.
    3. It is at least 20 characters long.
    """
    s = statement.strip()
    if len(s) < 20:
        return False, "statement too short (< 20 chars)"
    if not _SENTENCE_TERMINATORS.search(s):
        return False, "missing terminal punctuation (., !, ?)"
    if _INCOMPLETE_ENDINGS.search(s):
        return False, "ends with incomplete phrase or dangling word"
    return True, ""


@dataclass
class EvidenceRelation:
    """A single source-to-claim evidence binding with relation type and span."""
    chunk_id: str
    paper_id: str
    relation_type: str          # member of VALID_EVIDENCE_RELATION_TYPES
    exact_span: str = ""        # verbatim text from source supporting this component
    supported_components: List[str] = field(default_factory=list)  # which parts of the claim
    limitations: List[str] = field(default_factory=list)           # scope constraints
    confidence: str = "medium"  # "high" | "medium" | "low"

    def to_dict(self) -> dict:
        return {
            "chunk_id": self.chunk_id,
            "paper_id": self.paper_id,
            "relation_type": self.relation_type,
            "exact_span": self.exact_span,
            "supported_components": list(self.supported_components),
            "limitations": list(self.limitations),
            "confidence": self.confidence,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "EvidenceRelation":
        return cls(
            chunk_id=str(d.get("chunk_id", "")),
            paper_id=str(d.get("paper_id", "")),
            relation_type=str(d.get("relation_type", "direct_support")),
            exact_span=str(d.get("exact_span", "")),
            supported_components=list(d.get("supported_components") or []),
            limitations=list(d.get("limitations") or []),
            confidence=str(d.get("confidence", "medium")),
        )


@dataclass
class Claim:
    claim_id: str
    statement: str
    evidence_type: str
    # T1: claim kind for M3 routing
    claim_kind: str = "direct_fact"       # member of VALID_CLAIM_KINDS
    # T5: claim lifecycle state
    claim_state: str = "planned"          # planned|partially_grounded|grounded|contested|open_question|reframed|dropped
    supporting_concept_node_ids: List[str] = field(default_factory=list)
    supporting_text_chunk_ids: List[str] = field(default_factory=list)
    supporting_visual_chunk_ids: List[str] = field(default_factory=list)
    # T1: structured evidence relations (replaces flat chunk id lists for deep audit)
    evidence_relations: List[EvidenceRelation] = field(default_factory=list)
    saturation_score: float = 0.0
    load_bearing: bool = False
    importance: str = "supporting"
    critic_flags: List[str] = field(default_factory=list)
    # EvidenceTypeArbiter fields
    decomposer_evidence_type: str = ""
    secondary_evidence_types: List[str] = field(default_factory=list)
    evidence_type_arbiter_reason: str = ""
    evidence_type_confidence: str = ""   # "high"|"medium"|"low"|"not_run"
    # Claim-to-source binding audit
    evidence_binding_status: str = ""    # direct|synthesized|partial|insufficient|contradicted|unverified
    evidence_binding_confidence: str = ""  # high|medium|low|not_run
    evidence_binding_reason: str = ""
    evidence_synthesis_rationale: str = ""
    evidence_component_map: List[dict] = field(default_factory=list)
    missing_evidence_components: List[str] = field(default_factory=list)
    evidence_spans: List[dict] = field(default_factory=list)
    section_fit: str = ""               # central|supporting|boundary|off_scope
    section_fit_reason: str = ""
    # Dynamic evidence-closure provenance.  The original wording is never
    # destroyed when evidence requires narrowing or reframing.
    original_statement: str = ""
    supported_rewrite: str = ""
    adaptation_history: List[dict] = field(default_factory=list)
    closure_disposition: str = ""  # keep_supported|narrowed|open_question|recommendation|dropped
    closure_reason: str = ""
    evidence_requirement: str = "factual"  # factual|open_question|normative|none
    # Claim-level argument roles carried from the blueprint contract.  These
    # are deliberately separate from positive support so counterevidence,
    # qualification, and background cannot silently satisfy readiness.
    relation_roles: List[str] = field(default_factory=list)
    counterevidence_query: str = ""
    boundary_conditions: List[str] = field(default_factory=list)
    axis_assignments: List[dict] = field(default_factory=list)
    counterevidence_text_chunk_ids: List[str] = field(default_factory=list)
    boundary_text_chunk_ids: List[str] = field(default_factory=list)
    background_text_chunk_ids: List[str] = field(default_factory=list)
    author_reported_support_chunk_ids: List[str] = field(default_factory=list)
    evidence_role_bindings: List[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "claim_id": self.claim_id,
            "statement": self.statement,
            "evidence_type": self.evidence_type,
            "claim_kind": self.claim_kind,
            "claim_state": self.claim_state,
            "supporting_concept_node_ids": list(self.supporting_concept_node_ids),
            "supporting_text_chunk_ids": list(self.supporting_text_chunk_ids),
            "supporting_visual_chunk_ids": list(self.supporting_visual_chunk_ids),
            "evidence_relations": [r.to_dict() for r in self.evidence_relations],
            "saturation_score": float(self.saturation_score),
            "load_bearing": bool(self.load_bearing),
            "importance": self.importance,
            "critic_flags": list(self.critic_flags),
            "decomposer_evidence_type": self.decomposer_evidence_type,
            "secondary_evidence_types": list(self.secondary_evidence_types),
            "evidence_type_arbiter_reason": self.evidence_type_arbiter_reason,
            "evidence_type_confidence": self.evidence_type_confidence,
            "evidence_binding_status": self.evidence_binding_status,
            "evidence_binding_confidence": self.evidence_binding_confidence,
            "evidence_binding_reason": self.evidence_binding_reason,
            "evidence_synthesis_rationale": self.evidence_synthesis_rationale,
            "evidence_component_map": list(self.evidence_component_map),
            "missing_evidence_components": [str(x) for x in (self.missing_evidence_components or [])],
            "evidence_spans": list(self.evidence_spans),
            "section_fit": self.section_fit,
            "section_fit_reason": self.section_fit_reason,
            "original_statement": self.original_statement,
            "supported_rewrite": self.supported_rewrite,
            "adaptation_history": list(self.adaptation_history),
            "closure_disposition": self.closure_disposition,
            "closure_reason": self.closure_reason,
            "evidence_requirement": self.evidence_requirement,
            "relation_roles": list(self.relation_roles),
            "counterevidence_query": self.counterevidence_query,
            "boundary_conditions": list(self.boundary_conditions),
            "axis_assignments": list(self.axis_assignments),
            "counterevidence_text_chunk_ids": list(self.counterevidence_text_chunk_ids),
            "boundary_text_chunk_ids": list(self.boundary_text_chunk_ids),
            "background_text_chunk_ids": list(self.background_text_chunk_ids),
            "author_reported_support_chunk_ids": list(self.author_reported_support_chunk_ids),
            "evidence_role_bindings": list(self.evidence_role_bindings),
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Claim":
        evidence_relations = [
            EvidenceRelation.from_dict(r) if isinstance(r, dict) else EvidenceRelation(
                chunk_id=str(r), paper_id="", relation_type="direct_support"
            )
            for r in (d.get("evidence_relations") or [])
        ]
        return cls(
            claim_id=str(d["claim_id"]),
            statement=str(d["statement"]),
            evidence_type=str(d.get("evidence_type", "mechanism")),
            claim_kind=str(d.get("claim_kind", "direct_fact")),
            claim_state=str(d.get("claim_state", "planned")),
            supporting_concept_node_ids=list(d.get("supporting_concept_node_ids") or []),
            supporting_text_chunk_ids=list(d.get("supporting_text_chunk_ids") or []),
            supporting_visual_chunk_ids=list(d.get("supporting_visual_chunk_ids") or []),
            evidence_relations=evidence_relations,
            saturation_score=float(d.get("saturation_score", 0.0)),
            load_bearing=bool(d.get("load_bearing", False)),
            importance=str(
                d.get("importance") or d.get("importance_level")
                or ("load_bearing" if d.get("load_bearing") else "supporting")
            ),
            critic_flags=list(d.get("critic_flags") or []),
            decomposer_evidence_type=str(d.get("decomposer_evidence_type", "")),
            secondary_evidence_types=list(d.get("secondary_evidence_types") or []),
            evidence_type_arbiter_reason=str(d.get("evidence_type_arbiter_reason", "")),
            evidence_type_confidence=str(d.get("evidence_type_confidence", "")),
            evidence_binding_status=str(d.get("evidence_binding_status", "")),
            evidence_binding_confidence=str(d.get("evidence_binding_confidence", "")),
            evidence_binding_reason=str(d.get("evidence_binding_reason", "")),
            evidence_synthesis_rationale=str(d.get("evidence_synthesis_rationale", "")),
            evidence_component_map=list(d.get("evidence_component_map") or []),
            missing_evidence_components=[str(x) for x in (d.get("missing_evidence_components") or [])],
            evidence_spans=list(d.get("evidence_spans") or []),
            section_fit=str(d.get("section_fit", "")),
            section_fit_reason=str(d.get("section_fit_reason", "")),
            original_statement=str(d.get("original_statement", "")),
            supported_rewrite=str(d.get("supported_rewrite", "")),
            adaptation_history=list(d.get("adaptation_history") or []),
            closure_disposition=str(d.get("closure_disposition", "")),
            closure_reason=str(d.get("closure_reason", "")),
            evidence_requirement=str(d.get("evidence_requirement", "factual")),
            relation_roles=[str(x) for x in (d.get("relation_roles") or [])],
            counterevidence_query=str(d.get("counterevidence_query", "")),
            boundary_conditions=[str(x) for x in (d.get("boundary_conditions") or [])],
            axis_assignments=[dict(x) for x in (d.get("axis_assignments") or []) if isinstance(x, dict)],
            counterevidence_text_chunk_ids=[str(x) for x in (d.get("counterevidence_text_chunk_ids") or [])],
            boundary_text_chunk_ids=[str(x) for x in (d.get("boundary_text_chunk_ids") or [])],
            background_text_chunk_ids=[str(x) for x in (d.get("background_text_chunk_ids") or [])],
            author_reported_support_chunk_ids=[str(x) for x in (d.get("author_reported_support_chunk_ids") or [])],
            evidence_role_bindings=[dict(x) for x in (d.get("evidence_role_bindings") or []) if isinstance(x, dict)],
        )


def validate_claim(claim: Claim) -> list[str]:
    """Return list of validation error strings; empty list = valid."""
    errors: list[str] = []
    if not claim.claim_id:
        errors.append("claim_id is empty")

    # T1: sentence completeness check — no hard char truncation allowed
    if not claim.statement:
        errors.append("statement is missing")
    else:
        is_complete, reason = check_sentence_completeness(claim.statement)
        if not is_complete:
            errors.append(f"statement may be incomplete: {reason}")

    if claim.evidence_type not in VALID_EVIDENCE_TYPES:
        errors.append(
            f"evidence_type '{claim.evidence_type}' not in {VALID_EVIDENCE_TYPES}"
        )
    if claim.claim_kind not in VALID_CLAIM_KINDS:
        errors.append(
            f"claim_kind '{claim.claim_kind}' not in {VALID_CLAIM_KINDS}"
        )
    if claim.importance not in VALID_CLAIM_IMPORTANCE:
        errors.append(
            f"importance '{claim.importance}' not in {VALID_CLAIM_IMPORTANCE}"
        )
    if not claim.supporting_text_chunk_ids:
        errors.append("supporting_text_chunk_ids is empty; every claim needs text evidence")
    if not (0.0 <= claim.saturation_score <= 3.0):
        errors.append(f"saturation_score {claim.saturation_score} out of [0, 3] range")
    for rel in claim.evidence_relations:
        if rel.relation_type not in VALID_EVIDENCE_RELATION_TYPES:
            errors.append(
                f"evidence_relation relation_type '{rel.relation_type}' not in {VALID_EVIDENCE_RELATION_TYPES}"
            )
    return errors


def infer_claim_kind_from_statement(statement: str) -> str:
    """Deterministic heuristic to infer claim_kind from statement text.

    Used as a fallback when the LLM does not specify claim_kind.
    """
    s = statement.lower()
    if any(w in s for w in ("absent", "neglect", "overlook", "lack", "missing", "gap", "no study")):
        return "absence_or_neglect"
    if any(w in s for w in ("most", "majority", "prevalent", "common", "widespread", "typically", "generally")):
        return "corpus_prevalence"
    if any(w in s for w in ("unknown", "unclear", "unresolved", "uncertain", "open question", "remain")):
        return "frontier_uncertainty"
    if any(w in s for w in ("should", "recommend", "propose", "suggest", "need to", "must be")):
        return "normative_recommendation"
    if any(w in s for w in ("method", "protocol", "measurement", "artifact", "instrument", "calibrate")):
        return "methodological_critique"
    if any(w in s for w in ("higher", "lower", "compare", "versus", "outperform", "better than", "worse than")):
        return "quantitative_comparison"
    if any(w in s for w in ("mechanism", "due to", "because", "therefore", "enables", "causes", "leads to")):
        return "mechanism_synthesis"
    return "direct_fact"
