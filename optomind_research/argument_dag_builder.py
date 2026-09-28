"""M2b — Argument DAG builder for review blueprint claims.

Builds a directed acyclic graph over Claim objects produced by M2a (ClaimDecomposer).
Four progressive layers narrow candidate edges before a final LLM Proposer-Critic pass.

Layer 1: Section-order filter       (deterministic)
Layer 2: Evidence-type hierarchy    (deterministic, EVIDENCE_TYPE_RANK)
Layer 3: Shared-DOI / keyword       (deterministic, concept-relatedness proxy)
Layer 4: LLM Proposer-Critic        (call_qwen_chat, skipped when real_llm=False)
"""

from __future__ import annotations

import json
import math
import re
from collections import Counter, defaultdict, deque
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from optomind_research.claim_schema import EVIDENCE_TYPE_RANK
from optomind_research.claim_scope_checker import check_claim_scope
from optomind_research.evidence_readiness_gate import evaluate_evidence_readiness

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DAG_PROPOSER_PROMPT = PROJECT_ROOT / "prompts" / "DAG Edge Proposer.txt"
DEFAULT_DAG_CRITIC_PROMPT = PROJECT_ROOT / "prompts" / "DAG Edge Critic.txt"
DEFAULT_DAG_GLOBAL_CRITIC_PROMPT = PROJECT_ROOT / "prompts" / "DAG Global Critic.txt"

# Relation types that participate in topological ordering (have clear logical direction)
DAG_BACKBONE_RELATIONS: frozenset[str] = frozenset({
    "depends_on", "supports", "motivates", "extends", "constrains", "limits",
    "applies_to", "qualifies",
})
# Relation types that are non-directional or not part of the DAG backbone
NON_BACKBONE_RELATIONS: frozenset[str] = frozenset({
    "contrasts_with",
})
VALID_RELATION_TYPES: frozenset[str] = DAG_BACKBONE_RELATIONS | NON_BACKBONE_RELATIONS

# Relation hints may be emitted by a planner or carried over from a legacy
# claim seed.  They are deliberately small and conservative: a same-section
# edge is admitted only when one of these hints is explicit, or when the claim
# language contains an unmistakable contrast/limitation marker.
_RELATION_HINTS: dict[str, str] = {
    "contrast": "contrasts_with",
    "contrasts": "contrasts_with",
    "contrasts_with": "contrasts_with",
    "counterevidence": "contrasts_with",
    "counter": "contrasts_with",
    "boundary": "qualifies",
    "boundary_condition": "qualifies",
    "qualifies": "qualifies",
    "qualifying": "qualifies",
    "limit": "limits",
    "limits": "limits",
    "limitation": "limits",
    "constrain": "constrains",
    "constrains": "constrains",
    "support": "supports",
    "supports": "supports",
    "depends_on": "depends_on",
    "extends": "extends",
    "motivates": "motivates",
    "applies_to": "applies_to",
}
_CONTRAST_MARKERS = frozenset(
    {
        "unlike", "whereas", "however", "in contrast", "versus", "vs.",
        "compared with", "compared to", "on the other hand", "despite",
    }
)
_LIMITATION_MARKERS = frozenset(
    {
        "limited", "limitation", "only when", "depends on", "conditioned",
        "fails when", "breaks down", "boundary", "under", "outside",
    }
)

_STOPWORDS = frozenset(
    "a an the and or but in on at to for of with by from is are was were be been being "
    "have has had do does did will would could should may might shall this that these those "
    "it its we our they their what which who when where how all any both each few more most "
    "other some such only than then there through very between into during including among "
    "while because although however therefore thus since both either neither nor not no".split()
)


# --------------------------------------------------------------------------- #
# Data classes
# --------------------------------------------------------------------------- #

@dataclass
class DAGEdge:
    edge_id: str               # "{source_claim_id}->{target_claim_id}"
    source_claim_id: str
    target_claim_id: str
    source_section_id: str
    target_section_id: str
    source_evidence_type: str
    target_evidence_type: str
    confidence: str            # "high" | "medium" | "low"
    proposer_reason: str = ""
    critic_confirmed: bool = True
    relation_type: str = "depends_on"   # member of VALID_RELATION_TYPES
    is_dag_backbone: bool = True        # True if relation participates in topo sort
    edge_readiness: str = "grounded"    # "grounded" | "provisional"
    requires_evidence_followup: bool = False

    def to_dict(self) -> dict:
        return {
            "edge_id": self.edge_id,
            "source_claim_id": self.source_claim_id,
            "target_claim_id": self.target_claim_id,
            "source_section_id": self.source_section_id,
            "target_section_id": self.target_section_id,
            "source_evidence_type": self.source_evidence_type,
            "target_evidence_type": self.target_evidence_type,
            "confidence": self.confidence,
            "proposer_reason": self.proposer_reason,
            "critic_confirmed": self.critic_confirmed,
            "relation_type": self.relation_type,
            "is_dag_backbone": self.is_dag_backbone,
            "edge_readiness": self.edge_readiness,
            "requires_evidence_followup": self.requires_evidence_followup,
        }


@dataclass
class ArgumentDAG:
    edges: list[DAGEdge] = field(default_factory=list)
    topological_order: list[str] = field(default_factory=list)
    confidence_levels: dict[str, str] = field(default_factory=dict)
    pruning_stats: dict[str, Any] = field(default_factory=dict)
    claims_registry: dict[str, dict] = field(default_factory=dict)  # claim_id → claim dict

    # ------------------------------------------------------------------ #
    # Saturation propagation
    # ------------------------------------------------------------------ #

    def propagate_saturation(self) -> None:
        """Propagate prerequisite evidence bottlenecks to dependent claims.

        Only ``depends_on`` and ``constrains`` propagate saturation.  Supports,
        motivates, applies_to, and contrasts_with retain independent evidence strength.
        """
        # Build adjacency: target → list of source claim_ids (backbone edges only)
        predecessors: dict[str, list[str]] = defaultdict(list)
        for edge in self.edges:
            # Only true prerequisite/boundary relations should propagate an
            # evidence bottleneck. Corroboration, motivation, and application
            # links do not make the target no stronger than their source.
            if not edge.is_dag_backbone or edge.relation_type not in {
                "depends_on", "constrains", "limits"
            }:
                continue
            predecessors[edge.target_claim_id].append(edge.source_claim_id)

        # Process in topological order so predecessors are already finalized
        for claim_id in self.topological_order:
            if claim_id not in predecessors:
                continue
            claim = self.claims_registry.get(claim_id)
            if not claim:
                continue
            pred_saturations = [
                self.claims_registry[p]["saturation_score"]
                for p in predecessors[claim_id]
                if p in self.claims_registry
            ]
            if not pred_saturations:
                continue
            min_pred = min(pred_saturations)
            if min_pred < claim.get("saturation_score", 0.0):
                claim["saturation_score"] = min_pred
                claim["saturation_downgraded_by_dag"] = True

    # ------------------------------------------------------------------ #
    # Validation
    # ------------------------------------------------------------------ #

    def validate(self) -> list[str]:
        """Return a list of validation error strings. Empty = valid."""
        errors: list[str] = []
        for edge in self.edges:
            src_rank = EVIDENCE_TYPE_RANK.get(edge.source_evidence_type)
            tgt_rank = EVIDENCE_TYPE_RANK.get(edge.target_evidence_type)
            if src_rank is None:
                errors.append(
                    f"Unknown evidence_type '{edge.source_evidence_type}' in edge {edge.edge_id}"
                )
                continue
            if tgt_rank is None:
                errors.append(
                    f"Unknown evidence_type '{edge.target_evidence_type}' in edge {edge.edge_id}"
                )
                continue
            if src_rank > tgt_rank:
                errors.append(
                    f"Type hierarchy violation: {edge.source_evidence_type}"
                    f" -> {edge.target_evidence_type} in edge {edge.edge_id}"
                )
        # Check acyclicity on backbone edges only (non-backbone like contrasts_with are undirected)
        backbone_edges = [e for e in self.edges if e.is_dag_backbone]
        node_ids = (
            {e.source_claim_id for e in backbone_edges}
            | {e.target_claim_id for e in backbone_edges}
        )
        if node_ids:
            edge_topo = _topological_sort(list(node_ids), backbone_edges)
            if len(edge_topo) < len(node_ids):
                errors.append("DAG contains a cycle (topological sort incomplete)")
        return errors

    # ------------------------------------------------------------------ #
    # Serialisation
    # ------------------------------------------------------------------ #

    def to_dict(self) -> dict:
        return {
            "nodes": list(self.claims_registry.values()),
            "edges": [e.to_dict() for e in self.edges],
            "topological_order": self.topological_order,
            "confidence_levels": self.confidence_levels,
            "pruning_stats": self.pruning_stats,
            "claims": [
                {**c, "saturation_downgraded_by_dag": c.get("saturation_downgraded_by_dag", False)}
                for c in self.claims_registry.values()
            ],
            "edge_count": len(self.edges),
            "cross_section_edge_count": sum(
                1 for e in self.edges if e.source_section_id != e.target_section_id
            ),
            "grounded_edge_count": sum(
                1 for e in self.edges if e.edge_readiness == "grounded"
            ),
            "provisional_edge_count": sum(
                1 for e in self.edges if e.edge_readiness == "provisional"
            ),
        }


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def _doi_from_chunk_id(chunk_id: str) -> str | None:
    """Extract DOI prefix from chunk_id like 'doi-10.1002-app.58094:hybrid:s0007'."""
    m = re.match(r"^(doi-[^:]+)", chunk_id or "")
    return m.group(1) if m else None


def _keyword_tokens(statement: str) -> set[str]:
    """Return lowercase non-stopword tokens (length ≥ 4) from a claim statement."""
    tokens = re.findall(r"[a-zA-Z]{4,}", statement.lower())
    return {t for t in tokens if t not in _STOPWORDS}


def _jaccard(a: set, b: set) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def _compact_text(value: Any, max_chars: int) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    if len(text) <= max_chars:
        return text
    return text[: max(0, max_chars - 3)].rstrip() + "..."


def _compact_evidence_component_map(rows: Any, *, limit: int = 4) -> list[dict[str, Any]]:
    compact: list[dict[str, Any]] = []
    if not isinstance(rows, list):
        return compact
    for row in rows[:limit]:
        if not isinstance(row, dict):
            continue
        chunk_ids = row.get("chunk_ids") or row.get("text_refs") or []
        compact.append({
            "component": _compact_text(row.get("component"), 180),
            "chunk_ids": [str(cid) for cid in chunk_ids[:3]],
        })
    return [row for row in compact if row.get("component") or row.get("chunk_ids")]


def _compact_evidence_spans(rows: Any, *, limit: int = 3) -> list[dict[str, str]]:
    compact: list[dict[str, str]] = []
    if not isinstance(rows, list):
        return compact
    for row in rows[:limit]:
        if not isinstance(row, dict):
            continue
        item = {
            "chunk_id": _compact_text(row.get("chunk_id"), 120),
            "quote": _compact_text(row.get("quote"), 180),
            "quote_translation": _compact_text(row.get("quote_translation"), 180),
        }
        compact.append({k: v for k, v in item.items() if v})
    return compact


def _claim_can_enter_dag(claim: dict[str, Any]) -> bool:
    # The argument graph describes the review's reasoning architecture, not
    # only its already-proven conclusions. Explicit open questions and
    # recommendations may participate as provisional nodes. Dropped,
    # off-scope, and contradicted claims remain excluded.
    requirement = str(claim.get("evidence_requirement") or "factual").lower().strip()
    if requirement not in {"factual", "open_question", "normative"}:
        return False
    if str(claim.get("claim_state") or "").lower().strip() == "dropped":
        return False
    status = str(claim.get("evidence_binding_status") or "").lower().strip()
    if status == "contradicted":
        return False
    if status == "insufficient" and requirement == "factual":
        return False
    if str(claim.get("section_fit") or "").lower().strip() == "off_scope":
        return False
    return True


def _relation_hints(claim: dict[str, Any]) -> list[str]:
    """Normalize planner-provided relation hints without trusting free text."""

    raw: list[Any] = []
    for key in (
        "relation_type",
        "relation_to_section",
        "relation_role",
        "claim_role",
        "structural_role",
    ):
        value = claim.get(key)
        raw.extend(value if isinstance(value, (list, tuple, set)) else [value])
    raw.extend(claim.get("relation_roles") or [])
    raw.extend(claim.get("relation_hints") or [])
    out: list[str] = []
    for value in raw:
        normalized = re.sub(r"\s+", "_", str(value or "").strip().casefold())
        relation = _RELATION_HINTS.get(normalized)
        if relation and relation not in out:
            out.append(relation)
    return out


def _statement_has_marker(statement: Any, markers: frozenset[str]) -> bool:
    text = re.sub(r"\s+", " ", str(statement or "").casefold()).strip()
    return any(marker in text for marker in markers)


def _same_section_relation_candidate(a: dict[str, Any], b: dict[str, Any]) -> bool:
    """Return whether two claims in one section have an explicit local relation.

    Topic overlap is intentionally insufficient.  This gate only admits local
    counterevidence/contrast and boundary/limitation relationships, leaving
    unrelated claims as independent paragraph material.
    """

    hints = set(_relation_hints(a)) | set(_relation_hints(b))
    if hints & {"contrasts_with", "qualifies", "limits", "constrains"}:
        return True
    if str(a.get("section_fit") or "").casefold() == "boundary" or str(
        b.get("section_fit") or ""
    ).casefold() == "boundary":
        return True
    if _statement_has_marker(a.get("statement"), _CONTRAST_MARKERS) or _statement_has_marker(
        b.get("statement"), _CONTRAST_MARKERS
    ):
        return bool(_keyword_tokens(a.get("statement", "")) & _keyword_tokens(b.get("statement", "")))
    if _statement_has_marker(a.get("statement"), _LIMITATION_MARKERS) or _statement_has_marker(
        b.get("statement"), _LIMITATION_MARKERS
    ):
        return bool(_keyword_tokens(a.get("statement", "")) & _keyword_tokens(b.get("statement", "")))
    return False


def _orient_same_section_pair(
    a: dict[str, Any], b: dict[str, Any]
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Orient a local boundary edge from the central claim to its qualifier."""

    a_fit = str(a.get("section_fit") or "").casefold()
    b_fit = str(b.get("section_fit") or "").casefold()
    if a_fit == "boundary" and b_fit != "boundary":
        return b, a
    return a, b


def _infer_relation_type(source: dict[str, Any], target: dict[str, Any]) -> str:
    """Infer a deterministic relation for offline DAG construction."""

    source_hints = _relation_hints(source)
    target_hints = _relation_hints(target)
    if "contrasts_with" in source_hints or "contrasts_with" in target_hints:
        return "contrasts_with"
    if _statement_has_marker(source.get("statement"), _CONTRAST_MARKERS) or _statement_has_marker(
        target.get("statement"), _CONTRAST_MARKERS
    ):
        return "contrasts_with"
    if str(target.get("section_fit") or "").casefold() == "boundary":
        return "qualifies"
    if "limits" in target_hints or "limits" in source_hints:
        return "limits"
    if "constrains" in target_hints or "constrains" in source_hints:
        return "constrains"
    for relation in (*source_hints, *target_hints):
        if relation in VALID_RELATION_TYPES and relation != "contrasts_with":
            return relation
    if _statement_has_marker(target.get("statement"), _LIMITATION_MARKERS):
        return "qualifies"
    return "depends_on"


def _edge_readiness(source: dict[str, Any], target: dict[str, Any]) -> tuple[str, bool]:
    """Classify whether a logical edge is ready for factual prose."""
    grounded_statuses = {"direct", "synthesized"}
    source_status = str(source.get("evidence_binding_status") or "").lower().strip()
    target_status = str(target.get("evidence_binding_status") or "").lower().strip()
    grounded = (
        source_status in grounded_statuses
        and target_status in grounded_statuses
        and bool(source.get("supporting_text_chunk_ids"))
        and bool(target.get("supporting_text_chunk_ids"))
        and not source.get("missing_evidence_components")
        and not target.get("missing_evidence_components")
        and str(source.get("claim_state") or "").lower().strip()
        not in {"open_question", "planned"}
        and str(target.get("claim_state") or "").lower().strip()
        not in {"open_question", "planned"}
    )
    return ("grounded", False) if grounded else ("provisional", True)


def _evidence_tokens(text: str) -> set[str]:
    tokens = re.findall(r"[a-z0-9]+", text.lower().replace("/", " ").replace("-", " "))
    return {token for token in tokens if len(token) >= 2 and token not in _STOPWORDS}


def _text_mentions_component(text: str, component: str) -> bool:
    text_norm = re.sub(r"\s+", " ", text.lower().replace("/", " ").replace("-", " ")).strip()
    comp_norm = re.sub(r"\s+", " ", component.lower().replace("/", " ").replace("-", " ")).strip()
    if not text_norm or not comp_norm:
        return False
    if comp_norm in text_norm:
        return True
    comp_tokens = _evidence_tokens(comp_norm)
    if not comp_tokens:
        return False
    overlap = comp_tokens & _evidence_tokens(text_norm)
    required = 1 if len(comp_tokens) <= 2 else 2
    return len(overlap) >= required


def _missing_component_dependency_reason(
    source: dict[str, Any],
    target: dict[str, Any],
    relation_reason: str,
) -> str:
    """Return a rejection reason if an edge rationale relies on missing evidence."""
    if not _claim_can_enter_dag(source):
        return "source claim is not evidence-eligible for DAG edges"
    if not _claim_can_enter_dag(target):
        return "target claim is not evidence-eligible for DAG edges"
    reason = _compact_text(relation_reason, 1200)
    if not reason:
        return ""
    for side, claim in (("source", source), ("target", target)):
        for component in (claim.get("missing_evidence_components") or [])[:6]:
            component_text = _compact_text(component, 240)
            if component_text and _text_mentions_component(reason, component_text):
                return f"{side} edge rationale relies on missing evidence component: {component_text}"
    return ""


def _downgrade_confidence_for_partial_binding(
    confidence: str,
    source: dict[str, Any],
    target: dict[str, Any],
) -> str:
    status_values = {
        str(source.get("evidence_binding_status") or "").lower().strip(),
        str(target.get("evidence_binding_status") or "").lower().strip(),
    }
    has_missing = bool(source.get("missing_evidence_components")) or bool(target.get("missing_evidence_components"))
    if "partial" not in status_values and not has_missing:
        return confidence
    if confidence == "high":
        return "medium"
    if confidence == "medium":
        return "low"
    return "low"


def _topological_sort(claim_ids: list[str], edges: list[DAGEdge]) -> list[str]:
    """Kahn's algorithm. Returns ordered list; may be shorter than input if a cycle exists."""
    in_degree: dict[str, int] = {cid: 0 for cid in claim_ids}
    children: dict[str, list[str]] = defaultdict(list)
    for e in edges:
        if e.source_claim_id in in_degree and e.target_claim_id in in_degree:
            in_degree[e.target_claim_id] += 1
            children[e.source_claim_id].append(e.target_claim_id)
    queue = deque(cid for cid, deg in in_degree.items() if deg == 0)
    order: list[str] = []
    while queue:
        node = queue.popleft()
        order.append(node)
        for child in children[node]:
            in_degree[child] -= 1
            if in_degree[child] == 0:
                queue.append(child)
    return order


# --------------------------------------------------------------------------- #
# Cross-section claim deduplication
# --------------------------------------------------------------------------- #

def _tokenize_claim(text: str) -> frozenset[str]:
    tokens = set(re.findall(r"[a-z][a-z0-9\-]{2,}", text.lower()))
    return frozenset(t.strip("-") for t in tokens if t.strip("-") not in _STOPWORDS and len(t) >= 3)


def _dedup_cross_section_claims(claims: list[dict], *, sim_threshold: float = 0.65) -> list[dict]:
    """Remove near-duplicate claims that repeat the same point across different sections.

    Jaccard similarity >= sim_threshold on content tokens triggers dedup:
    the weaker claim (lower saturation_score, then fewer chunks) is dropped.
    Only compares claims from DIFFERENT sections, and never drops the last
    remaining claim of any section.
    """
    if len(claims) <= 1:
        return claims

    token_sets = [_tokenize_claim(c.get("claim_text") or c.get("statement", "")) for c in claims]
    drop: set[int] = set()

    # Track which indices survive per section so we never empty a section.
    from collections import defaultdict as _dd
    section_survivors: dict[str, list[int]] = _dd(list)
    for idx, c in enumerate(claims):
        section_survivors[c.get("section_id")].append(idx)

    for i in range(len(claims)):
        if i in drop:
            continue
        for j in range(i + 1, len(claims)):
            if j in drop:
                continue
            if claims[i].get("section_id") == claims[j].get("section_id"):
                continue
            a, b = token_sets[i], token_sets[j]
            union = a | b
            if not union:
                continue
            sim = len(a & b) / len(union)
            if sim < sim_threshold:
                continue
            sat_i = claims[i].get("saturation_score", 0) or 0
            sat_j = claims[j].get("saturation_score", 0) or 0
            chunks_i = len(claims[i].get("supporting_text_chunk_ids") or [])
            chunks_j = len(claims[j].get("supporting_text_chunk_ids") or [])
            loser = j if (sat_i, chunks_i) >= (sat_j, chunks_j) else i
            loser_sid = claims[loser].get("section_id")
            # Guard: never drop the last claim of a section.
            surviving = [k for k in section_survivors[loser_sid] if k not in drop]
            if len(surviving) > 1:
                drop.add(loser)

    return [c for idx, c in enumerate(claims) if idx not in drop]


def _update_claim_states(claims_registry: dict[str, dict]) -> None:
    """Update claim_state for each claim based on evidence binding and saturation (T5)."""
    for claim in claims_registry.values():
        current_state = str(claim.get("claim_state") or "planned")
        if current_state in {"dropped", "reframed"}:
            continue
        status = str(claim.get("evidence_binding_status") or "").lower().strip()
        saturation = float(claim.get("saturation_score") or 0.0)
        has_chunks = bool(claim.get("supporting_text_chunk_ids"))
        if status in {"direct", "synthesized"} and has_chunks and saturation >= 1.0:
            claim["claim_state"] = "grounded"
        elif status in {"direct", "synthesized", "partial"} and has_chunks:
            claim["claim_state"] = "partially_grounded"
        elif status == "contradicted":
            claim["claim_state"] = "contested"
        elif not has_chunks and bool(claim.get("load_bearing")):
            claim["claim_state"] = "open_question"
        elif not has_chunks:
            claim["claim_state"] = "planned"


# --------------------------------------------------------------------------- #
# Main builder
# --------------------------------------------------------------------------- #

class ArgumentDAGBuilder:
    """Build an ArgumentDAG from M2a claims using four progressive layers."""

    def __init__(
        self,
        real_llm: bool = False,
        model_tier: str = "standard_model",
        proposer_prompt_path: Path = DEFAULT_DAG_PROPOSER_PROMPT,
        critic_prompt_path: Path = DEFAULT_DAG_CRITIC_PROMPT,
        layer3_jaccard_threshold: float = 0.1,
        candidate_mode: str | None = None,   # None = auto; "exhaustive" | "ranked_topk"
        topk_per_target: int = 5,
        max_layer4_candidates: int | None = 80,
        global_critic: bool = True,
        global_critic_prompt_path: Path = DEFAULT_DAG_GLOBAL_CRITIC_PROMPT,
        layer4_workers: int = 6,
    ) -> None:
        self.real_llm = real_llm
        self.model_tier = model_tier
        self.proposer_prompt_path = Path(proposer_prompt_path)
        self.critic_prompt_path = Path(critic_prompt_path)
        self.layer3_jaccard_threshold = layer3_jaccard_threshold
        self.candidate_mode = candidate_mode
        self.topk_per_target = topk_per_target
        self.max_layer4_candidates = max_layer4_candidates
        self.global_critic = bool(global_critic)
        self.global_critic_prompt_path = Path(global_critic_prompt_path)
        self.layer4_workers = max(1, int(layer4_workers))
        self._proposer_prompt: str | None = None
        self._critic_prompt: str | None = None

    # ------------------------------------------------------------------ #
    # Public API
    # ------------------------------------------------------------------ #

    def build(
        self,
        claims: list[dict[str, Any]],
        section_order: list[str],
        section_meta: dict[str, dict] | None = None,
        enable_scope_check: bool = False,
        enable_readiness_check: bool = False,
        scope_definition: str = "",
    ) -> ArgumentDAG:
        """Build and return an ArgumentDAG from M2a claim dicts.

        Args:
            enable_scope_check: Enable U1 Claim Scope Checker pre-filtering
            enable_readiness_check: Enable U4 Evidence Readiness Gate evaluation
            scope_definition: User-defined scope for scope checking
        """
        # ── U1: Claim Scope Checker (optional pre-filter) ──
        scope_check_stats = {"enabled": enable_scope_check, "total": len(claims), "rejected": 0, "reasons": {}}
        if enable_scope_check:
            scope_filtered_claims = []
            for claim in claims:
                scope_result = check_claim_scope(
                    claim_text=claim.get("statement") or claim.get("claim_text", ""),
                    supporting_chunk_ids=claim.get("supporting_text_chunk_ids") or [],
                    scope_definition=scope_definition,
                    kb_total_chunks=0,
                    claim_type=claim.get("evidence_type", ""),
                )
                claim["_scope_check"] = scope_result
                if scope_result["action"] == "reject":
                    scope_check_stats["rejected"] += 1
                    reason = scope_result["scope_axis"]
                    scope_check_stats["reasons"][reason] = scope_check_stats["reasons"].get(reason, 0) + 1
                else:
                    scope_filtered_claims.append(claim)
            claims = scope_filtered_claims

        # ── U4: Evidence Readiness Gate (optional evaluation) ──
        readiness_stats = {"enabled": enable_readiness_check, "total": len(claims), "blocked": 0, "supplement": 0, "proceed": 0}
        if enable_readiness_check:
            for claim in claims:
                sec_meta = (section_meta or {}).get(claim.get("section_id", ""), {})
                chunk_meta = sec_meta.get("text_chunk_meta", {})
                supporting_chunks = [
                    chunk_meta[cid]
                    for cid in (claim.get("supporting_text_chunk_ids") or [])
                    if cid in chunk_meta
                ]
                readiness_result = evaluate_evidence_readiness(
                    claim_text=claim.get("statement") or claim.get("claim_text", ""),
                    supporting_chunks=supporting_chunks,
                    claim_type=claim.get("evidence_type", ""),
                    binding_status=claim.get("evidence_binding_status", ""),
                    binding_confidence=claim.get("evidence_binding_confidence", ""),
                    missing_components=claim.get("missing_evidence_components") or [],
                    load_bearing=bool(claim.get("load_bearing")),
                )
                claim["_readiness_check"] = readiness_result
                if readiness_result["action"] == "block":
                    readiness_stats["blocked"] += 1
                elif readiness_result["action"] == "supplement":
                    readiness_stats["supplement"] += 1
                else:
                    readiness_stats["proceed"] += 1

        section_rank = {sid: i for i, sid in enumerate(section_order)}

        # ── Cross-section claim deduplication ──
        # Remove near-identical claims that repeat the same point across sections.
        # Jaccard similarity on content tokens; threshold=0.65 keeps only distinct claims.
        claims = _dedup_cross_section_claims(claims, sim_threshold=0.65)

        claims_registry = {c["claim_id"]: dict(c) for c in claims if c.get("claim_id")}
        _update_claim_states(claims_registry)

        # Auto-select candidate_mode based on claim count if not explicitly set
        effective_mode = self.candidate_mode
        if effective_mode is None:
            effective_mode = "exhaustive" if len(claims) <= 15 else "ranked_topk"

        # Layer 1
        candidates_l1 = self._filter_layer1(claims, section_rank)
        after_l1 = len(candidates_l1)
        same_section_candidates = sum(
            a.get("section_id") == b.get("section_id")
            for a, b in candidates_l1
        )

        # Layer 2
        candidates_l2 = self._filter_layer2(candidates_l1)
        after_l2 = len(candidates_l2)

        # Layer 3 (candidate selection)
        candidates_l3 = self._filter_layer3(
            candidates_l2,
            effective_mode=effective_mode,
            section_meta=section_meta or {},
        )
        after_l3 = len(candidates_l3)

        # Apply max_layer4_candidates cap before sending to LLM
        cap_applied = False
        cap_mode_effective = effective_mode
        candidates_for_l4 = candidates_l3
        if (
            self.max_layer4_candidates is not None
            and len(candidates_l3) > self.max_layer4_candidates
        ):
            cap_applied = True
            cap_mode_effective = "ranked_topk_after_cap"
            # Score and keep top N
            scored = [
                (self._score_candidate_pair(a, b, section_meta or {}), a, b)
                for a, b in candidates_l3
            ]
            scored.sort(key=lambda x: x[0], reverse=True)
            candidates_for_l4 = [(a, b) for _, a, b in scored[: self.max_layer4_candidates]]

        # Layer 4
        if self.real_llm:
            final_edges, l4_stats = self._filter_layer4_llm(candidates_for_l4, section_meta=section_meta or {})
        else:
            l4_stats: dict[str, Any] = {}
            final_edges = [
                DAGEdge(
                    edge_id=f"{src['claim_id']}->{tgt['claim_id']}",
                    source_claim_id=src["claim_id"],
                    target_claim_id=tgt["claim_id"],
                    source_section_id=src.get("section_id", ""),
                    target_section_id=tgt.get("section_id", ""),
                    source_evidence_type=src.get("evidence_type", "mechanism"),
                    target_evidence_type=tgt.get("evidence_type", "mechanism"),
                    confidence="medium",
                    proposer_reason="mock: layer4 skipped",
                    critic_confirmed=True,
                    relation_type=_infer_relation_type(src, tgt),
                    is_dag_backbone=_infer_relation_type(src, tgt)
                    not in NON_BACKBONE_RELATIONS,
                    edge_readiness=_edge_readiness(src, tgt)[0],
                    requires_evidence_followup=_edge_readiness(src, tgt)[1],
                )
                for src, tgt in candidates_for_l4
            ]

        global_stats: dict[str, Any] = {}
        if self.real_llm and self.global_critic and final_edges:
            final_edges, global_stats = self._global_critique_edges(
                final_edges,
                claims_registry,
                section_order,
                section_meta or {},
            )

        all_claim_ids = list(claims_registry.keys())
        backbone_edges = [e for e in final_edges if e.is_dag_backbone]
        topo_order = _topological_sort(all_claim_ids, backbone_edges)
        confidence_levels = {e.edge_id: e.confidence for e in final_edges}

        pruning_stats: dict[str, Any] = {
            "after_layer1": after_l1,
            "after_layer2": after_l2,
            "after_layer3": after_l3,
            "same_section_candidates": same_section_candidates,
            "final": len(final_edges),
            "backbone": len(backbone_edges),
            "same_section_edges": sum(
                e.source_section_id == e.target_section_id for e in final_edges
            ),
            "relation_type_counts": dict(Counter(e.relation_type for e in final_edges)),
            "candidate_mode": effective_mode,
            "scope_check": scope_check_stats,
            "readiness_check": readiness_stats,
        }
        if cap_applied:
            pruning_stats["candidate_cap_applied"] = True
            pruning_stats["max_layer4_candidates"] = self.max_layer4_candidates
            pruning_stats["candidate_mode_effective"] = cap_mode_effective
        if l4_stats:
            pruning_stats.update(l4_stats)
        if global_stats:
            pruning_stats["global_critic"] = global_stats

        return ArgumentDAG(
            edges=final_edges,
            topological_order=topo_order,
            confidence_levels=confidence_levels,
            pruning_stats=pruning_stats,
            claims_registry=claims_registry,
        )

    # ------------------------------------------------------------------ #
    # Layer filters
    # ------------------------------------------------------------------ #

    def _filter_layer1(
        self,
        claims: list[dict],
        section_rank: dict[str, int],
    ) -> list[tuple[dict, dict]]:
        """Cross-section directed pairs only (earlier section → later section)."""
        result: list[tuple[dict, dict]] = []
        for index_a, a in enumerate(claims):
            if not _claim_can_enter_dag(a):
                continue
            for index_b, b in enumerate(claims):
                if a["claim_id"] == b["claim_id"]:
                    continue
                if not _claim_can_enter_dag(b):
                    continue
                rank_a = section_rank.get(a.get("section_id", ""), -1)
                rank_b = section_rank.get(b.get("section_id", ""), -1)
                if rank_a >= 0 and rank_b >= 0 and rank_a < rank_b:
                    result.append((a, b))
                elif (
                    rank_a >= 0
                    and rank_a == rank_b
                    and index_a < index_b
                    and _same_section_relation_candidate(a, b)
                ):
                    result.append(_orient_same_section_pair(a, b))
        return result

    def _filter_layer2(
        self,
        candidates: list[tuple[dict, dict]],
    ) -> list[tuple[dict, dict]]:
        """Keep only evidence_type rank-compatible pairs (src_rank ≤ tgt_rank)."""
        result: list[tuple[dict, dict]] = []
        for a, b in candidates:
            ra = EVIDENCE_TYPE_RANK.get(a.get("evidence_type", "mechanism"), 0)
            rb = EVIDENCE_TYPE_RANK.get(b.get("evidence_type", "mechanism"), 0)
            if ra <= rb:
                result.append((a, b))
        return result

    def _filter_layer3(
        self,
        candidates: list[tuple[dict, dict]],
        effective_mode: str = "exhaustive",
        section_meta: dict[str, dict] | None = None,
    ) -> list[tuple[dict, dict]]:
        """Candidate selection: exhaustive passes all; ranked_topk keeps top-k per target."""
        if effective_mode == "exhaustive":
            return candidates

        # ranked_topk: score each pair, keep top-k sources per target
        from collections import defaultdict
        target_to_scored: dict[str, list[tuple[float, dict, dict]]] = defaultdict(list)
        for a, b in candidates:
            score = self._score_candidate_pair(a, b, section_meta or {})
            target_to_scored[b["claim_id"]].append((score, a, b))

        result: list[tuple[dict, dict]] = []
        for scored in target_to_scored.values():
            scored.sort(key=lambda x: x[0], reverse=True)
            for _, a, b in scored[:self.topk_per_target]:
                result.append((a, b))
        return result

    def _score_candidate_pair(
        self,
        a: dict,
        b: dict,
        section_meta: dict[str, dict],
    ) -> float:
        """Heuristic score for ranked_topk candidate selection."""
        # Evidence type rank gap: larger gap = more complementary
        ra = EVIDENCE_TYPE_RANK.get(a.get("evidence_type", "mechanism"), 0)
        rb = EVIDENCE_TYPE_RANK.get(b.get("evidence_type", "mechanism"), 0)
        rank_gap_score = min(rb - ra, 3) / 3.0 * 0.15

        # Section title keyword overlap
        sec_a = section_meta.get(a.get("section_id", ""), {})
        sec_b = section_meta.get(b.get("section_id", ""), {})
        title_a = _keyword_tokens(sec_a.get("title", ""))
        title_b = _keyword_tokens(sec_b.get("title", ""))
        title_score = _jaccard(title_a, title_b) * 0.20

        # Section argument_role keyword overlap
        role_a = _keyword_tokens(sec_a.get("argument_role", ""))
        role_b = _keyword_tokens(sec_b.get("argument_role", ""))
        role_score = _jaccard(role_a, role_b) * 0.20

        # Claim statement keyword overlap + concept subsumption
        stmt_a = _keyword_tokens(a.get("statement", ""))
        stmt_b = _keyword_tokens(b.get("statement", ""))
        stmt_score = _jaccard(stmt_a, stmt_b) * 0.15
        # Subsumption: one claim's concept set fully contained in the other's
        # signals hierarchical relatedness (specific ↔ general)
        subsumption_score = 0.10 if (stmt_a and stmt_b and (stmt_a <= stmt_b or stmt_b <= stmt_a)) else 0.0

        # Shared DOI bonus
        dois_a = {_doi_from_chunk_id(c) for c in (a.get("supporting_text_chunk_ids") or [])} - {None}
        dois_b = {_doi_from_chunk_id(c) for c in (b.get("supporting_text_chunk_ids") or [])} - {None}
        doi_score = 0.20 if dois_a & dois_b else 0.0

        # Section order closeness (smaller distance = more likely to be related)
        # Not using this for now since we already filter by section_rank in Layer 1
        section_score = 0.10  # small baseline for any cross-section pair

        return rank_gap_score + title_score + role_score + stmt_score + subsumption_score + doi_score + section_score

    def _filter_layer4_llm(
        self,
        candidates: list[tuple[dict, dict]],
        section_meta: dict[str, dict] | None = None,
    ) -> tuple[list[DAGEdge], dict[str, Any]]:
        """LLM Proposer-Critic pass. Returns (confirmed_edges, stats)."""
        from llm.qwen_chat_client import call_qwen_chat

        if self._proposer_prompt is None:
            self._proposer_prompt = self.proposer_prompt_path.read_text(encoding="utf-8").strip()
        if self._critic_prompt is None:
            self._critic_prompt = self.critic_prompt_path.read_text(encoding="utf-8").strip()

        meta = section_meta or {}
        total_pairs = len(candidates)

        def evaluate_pair(
            pair: tuple[dict, dict],
        ) -> tuple[DAGEdge | None, str, list[dict[str, Any]], dict[str, Any]]:
            a, b = pair
            usages: list[dict[str, Any]] = []
            audit: dict[str, Any] = {
                "source_claim_id": str(a.get("claim_id") or ""),
                "target_claim_id": str(b.get("claim_id") or ""),
                "source_section_id": str(a.get("section_id") or ""),
                "target_section_id": str(b.get("section_id") or ""),
            }
            # --- Proposer ---
            proposer_user = json.dumps(
                {
                    "claim_a": self._build_claim_payload(a, meta),
                    "claim_b": self._build_claim_payload(b, meta),
                },
                ensure_ascii=False,
            )
            prop_result = call_qwen_chat(
                "DAGEdgeProposerAgent",
                [
                    {"role": "system", "content": self._proposer_prompt},
                    {"role": "user", "content": proposer_user},
                ],
                model_tier=self.model_tier,
                temperature=0,
                max_tokens=400,
                response_format={"type": "json_object"},
                force_mock=False,
                # Pairwise graph decisions are inexpensive but structurally
                # important.  A transient timeout must not silently thin the
                # review graph, so retry once, rotate across two available
                # keys, and then use the configured B-family fallback chain.
                max_retries=1,
                timeout_seconds=60,
                max_transport_key_candidates=2,
                allow_model_fallback=True,
            )
            usages.append(prop_result.get("_llm_usage", {}))
            prop_text = str(prop_result.get("content") or "")
            prop_m = re.search(r"\{.*\}", prop_text, re.S)
            if not prop_m:
                audit["proposer_raw_preview"] = prop_text[:240]
                return None, "parse_error", usages, audit
            try:
                prop_json = json.loads(prop_m.group(0))
            except Exception:
                audit["proposer_raw_preview"] = prop_text[:240]
                return None, "parse_error", usages, audit
            audit["proposer"] = prop_json
            if not prop_json.get("has_edge", False):
                return None, "proposer_rejected", usages, audit
            prop_relation = str(prop_json.get("relation_type", "depends_on"))
            if prop_relation not in VALID_RELATION_TYPES:
                prop_relation = "depends_on"
            proposer_reason = str(prop_json.get("reason", ""))

            # --- Critic ---
            critic_user = json.dumps(
                {
                    "claim_a": self._build_claim_payload(a, meta),
                    "claim_b": self._build_claim_payload(b, meta),
                    "proposer_output": prop_json,
                },
                ensure_ascii=False,
            )
            crit_result = call_qwen_chat(
                "DAGEdgeCriticAgent",
                [
                    {"role": "system", "content": self._critic_prompt},
                    {"role": "user", "content": critic_user},
                ],
                model_tier=self.model_tier,
                temperature=0,
                max_tokens=300,
                response_format={"type": "json_object"},
                force_mock=False,
                max_retries=1,
                timeout_seconds=60,
                max_transport_key_candidates=2,
                allow_model_fallback=True,
            )
            usages.append(crit_result.get("_llm_usage", {}))
            crit_text = str(crit_result.get("content") or "")
            crit_m = re.search(r"\{.*\}", crit_text, re.S)
            if not crit_m:
                audit["critic_raw_preview"] = crit_text[:240]
                return None, "parse_error", usages, audit
            try:
                crit_json = json.loads(crit_m.group(0))
            except Exception:
                audit["critic_raw_preview"] = crit_text[:240]
                return None, "parse_error", usages, audit
            audit["critic"] = crit_json
            if not crit_json.get("confirmed", False):
                return None, "critic_rejected", usages, audit
            # Critic may override relation_type if it disagrees
            crit_relation = str(crit_json.get("relation_type", prop_relation))
            if crit_relation not in VALID_RELATION_TYPES:
                crit_relation = prop_relation
            confidence = str(crit_json.get("confidence", "medium"))
            if confidence not in ("high", "medium", "low"):
                confidence = "medium"
            critic_reason = str(crit_json.get("reason", ""))
            if crit_relation == "supports":
                source_status = str(a.get("evidence_binding_status") or "").lower().strip()
                if (
                    source_status not in {"direct", "synthesized", "partial"}
                    or not a.get("supporting_text_chunk_ids")
                ):
                    audit["grounding_rejection"] = (
                        "supports requires a source claim with usable bound evidence"
                    )
                    return None, "grounding_rejected", usages, audit
            is_backbone = crit_relation in DAG_BACKBONE_RELATIONS
            missing_dependency_judgment = crit_json.get("depends_on_missing_component")
            if missing_dependency_judgment is True:
                grounding_rejection = "critic confirmed that the edge depends on a missing component"
            elif missing_dependency_judgment is False:
                grounding_rejection = ""
            else:
                # Backward-compatible deterministic fallback for older model
                # responses and tests that predate the explicit judgment.
                grounding_rejection = _missing_component_dependency_reason(
                    a,
                    b,
                    " ".join([crit_relation, proposer_reason, critic_reason]),
                )
            if grounding_rejection:
                audit["grounding_rejection"] = grounding_rejection
                return None, "grounding_rejected", usages, audit
            confidence = _downgrade_confidence_for_partial_binding(confidence, a, b)
            edge_readiness, requires_followup = _edge_readiness(a, b)

            return (
                DAGEdge(
                    edge_id=f"{a['claim_id']}->{b['claim_id']}",
                    source_claim_id=a["claim_id"],
                    target_claim_id=b["claim_id"],
                    source_section_id=a.get("section_id", ""),
                    target_section_id=b.get("section_id", ""),
                    source_evidence_type=a.get("evidence_type", "mechanism"),
                    target_evidence_type=b.get("evidence_type", "mechanism"),
                    confidence=confidence,
                    proposer_reason=proposer_reason,
                    critic_confirmed=True,
                    relation_type=crit_relation,
                    is_dag_backbone=is_backbone,
                    edge_readiness=edge_readiness,
                    requires_evidence_followup=requires_followup,
                ),
                "confirmed",
                usages,
                audit,
            )

        if candidates:
            with ThreadPoolExecutor(max_workers=min(self.layer4_workers, len(candidates))) as pool:
                pair_results = list(pool.map(evaluate_pair, candidates))
        else:
            pair_results = []
        edges = [edge for edge, _, _, _ in pair_results if edge is not None]
        statuses = [status for _, status, _, _ in pair_results]
        proposer_rejected = statuses.count("proposer_rejected")
        critic_rejected = statuses.count("critic_rejected")
        grounding_rejected = statuses.count("grounding_rejected")
        parse_errors = statuses.count("parse_error")
        confirmed = len(edges)
        transition_counts: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
        transition_samples: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for edge, status, _, audit in pair_results:
            transition = (
                f"{audit.get('source_section_id', '')}->{audit.get('target_section_id', '')}"
            )
            transition_counts[transition][status] += 1
            if len(transition_samples[transition]) < 3:
                transition_samples[transition].append({"status": status, **audit})
        return edges, {
            "proposer_rejected": proposer_rejected,
            "critic_rejected": critic_rejected,
            "grounding_rejected": grounding_rejected,
            "parse_errors": parse_errors,
            "l4_confirmed": confirmed,
            "l4_total_pairs": total_pairs,
            "acceptance_rate": round(confirmed / total_pairs, 3) if total_pairs > 0 else None,
            "l4_workers": min(self.layer4_workers, total_pairs) if total_pairs else 0,
            "l4_llm_calls": sum(len(usages) for _, _, usages, _ in pair_results),
            "decision_audit": {
                "accepted_examples": [
                    audit
                    for edge, status, _, audit in pair_results
                    if edge is not None and status == "confirmed"
                ][:12],
                "rejected_examples": [
                    {"status": status, **audit}
                    for edge, status, _, audit in pair_results
                    if edge is None
                ][:20],
                "transition_counts": {
                    key: dict(value) for key, value in transition_counts.items()
                },
                "transition_samples": dict(transition_samples),
            },
        }

    def _global_critique_edges(
        self,
        edges: list[DAGEdge],
        claims_registry: dict[str, dict],
        section_order: list[str],
        section_meta: dict[str, dict],
        *,
        model_tier_override: str | None = None,
        max_tokens_override: int | None = None,
    ) -> tuple[list[DAGEdge], dict[str, Any]]:
        """Globally prune/correct locally accepted edges into a coherent graph."""
        from llm.qwen_chat_client import call_qwen_chat

        if not self.global_critic_prompt_path.exists():
            return edges, {"status": "prompt_missing", "before": len(edges), "after": len(edges)}
        if len(edges) > 18:
            return self._global_critique_edges_batched(
                edges,
                claims_registry,
                section_order,
                section_meta,
            )
        rank = {sid: idx for idx, sid in enumerate(section_order)}
        edge_by_pair: dict[tuple[str, str], DAGEdge] = {
            (edge.source_claim_id, edge.target_claim_id): edge for edge in edges
        }
        allowed_pairs = set(edge_by_pair)
        allowed_edge_ids = {edge.edge_id for edge in edges}
        involved_claim_ids = {
            claim_id
            for edge in edges
            for claim_id in (edge.source_claim_id, edge.target_claim_id)
        }
        eligible_claims = {
            cid: claim
            for cid, claim in claims_registry.items()
            if cid in involved_claim_ids and _claim_can_enter_dag(claim)
        }
        payload = {
            "sections": [
                {
                    "section_id": sid,
                    "title": section_meta.get(sid, {}).get("title", ""),
                    "argument_role": section_meta.get(sid, {}).get("argument_role", ""),
                }
                for sid in section_order
            ],
            "claims": [
                self._build_global_claim_payload(eligible_claims[cid], section_meta)
                for cid in eligible_claims
            ],
            "candidate_edges": [e.to_dict() for e in edges],
        }
        try:
            result = call_qwen_chat(
                "DAGGlobalCriticAgent",
                [
                    {"role": "system", "content": self.global_critic_prompt_path.read_text(encoding="utf-8")},
                    {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
                ],
                model_tier=model_tier_override or "premium_model",
                temperature=0,
                max_tokens=max_tokens_override or 3200,
                response_format={"type": "json_object"},
                stream=True,
                force_mock=False,
                max_retries=1,
                timeout_seconds=120,
                max_transport_key_candidates=2,
                allow_model_fallback=True,
            )
            raw = str(result.get("content") or "")
            try:
                parsed = json.loads(raw)
            except Exception:
                match = re.search(r"\{.*\}", raw, re.S)
                parsed = json.loads(match.group(0)) if match else {}
            accepted = parsed.get("accepted_edges") if isinstance(parsed.get("accepted_edges"), list) else []
            out: list[DAGEdge] = []
            seen_pairs: set[tuple[str, str]] = set()
            invalid_rows = 0
            grounding_rejected = 0
            invented_edges_rejected = 0
            relation_corrections = 0
            duplicate_pairs_dropped = 0
            for row in accepted:
                if not isinstance(row, dict):
                    invalid_rows += 1
                    continue
                src = str(row.get("source_claim_id") or "")
                tgt = str(row.get("target_claim_id") or "")
                relation = str(row.get("relation_type") or "supports")
                row_edge_id = str(row.get("edge_id") or "")
                if src not in claims_registry or tgt not in claims_registry or src == tgt or relation not in VALID_RELATION_TYPES:
                    invalid_rows += 1
                    continue
                pair_key = (src, tgt)
                if pair_key not in allowed_pairs:
                    invented_edges_rejected += 1
                    continue
                if row_edge_id and row_edge_id not in allowed_edge_ids:
                    invented_edges_rejected += 1
                    continue
                original_edge = edge_by_pair[pair_key]
                if row_edge_id and row_edge_id != original_edge.edge_id:
                    invented_edges_rejected += 1
                    continue
                if pair_key in seen_pairs:
                    duplicate_pairs_dropped += 1
                    continue
                seen_pairs.add(pair_key)
                src_claim = claims_registry[src]
                tgt_claim = claims_registry[tgt]
                if not _claim_can_enter_dag(src_claim) or not _claim_can_enter_dag(tgt_claim):
                    invalid_rows += 1
                    continue
                if relation == "supports" and (
                    str(src_claim.get("evidence_binding_status") or "").lower().strip()
                    not in {"direct", "synthesized", "partial"}
                    or not src_claim.get("supporting_text_chunk_ids")
                ):
                    grounding_rejected += 1
                    continue
                if EVIDENCE_TYPE_RANK.get(str(src_claim.get("evidence_type") or "mechanism"), 0) > EVIDENCE_TYPE_RANK.get(str(tgt_claim.get("evidence_type") or "mechanism"), 0):
                    invalid_rows += 1
                    continue
                src_sid = str(src_claim.get("section_id") or "")
                tgt_sid = str(tgt_claim.get("section_id") or "")
                if rank.get(src_sid, -1) >= rank.get(tgt_sid, -1):
                    invalid_rows += 1
                    continue
                missing_dependency_judgment = row.get("depends_on_missing_component")
                if missing_dependency_judgment is True:
                    grounding_rejection = (
                        "global critic confirmed that the edge depends on a missing component"
                    )
                elif missing_dependency_judgment is False:
                    grounding_rejection = ""
                else:
                    grounding_rejection = _missing_component_dependency_reason(
                        src_claim,
                        tgt_claim,
                        " ".join([relation, str(row.get("reason") or "")]),
                    )
                if grounding_rejection:
                    grounding_rejected += 1
                    continue
                confidence = str(row.get("confidence") or "medium").lower().strip()
                if confidence not in {"high", "medium", "low"}:
                    confidence = "medium"
                confidence = _downgrade_confidence_for_partial_binding(confidence, src_claim, tgt_claim)
                if relation != original_edge.relation_type:
                    relation_corrections += 1
                edge_readiness, requires_followup = _edge_readiness(src_claim, tgt_claim)
                out.append(DAGEdge(
                    edge_id=original_edge.edge_id,
                    source_claim_id=src,
                    target_claim_id=tgt,
                    source_section_id=src_sid,
                    target_section_id=tgt_sid,
                    source_evidence_type=str(src_claim.get("evidence_type") or "mechanism"),
                    target_evidence_type=str(tgt_claim.get("evidence_type") or "mechanism"),
                    confidence=confidence,
                    proposer_reason=str(row.get("reason") or ""),
                    critic_confirmed=True,
                    relation_type=relation,
                    is_dag_backbone=relation in DAG_BACKBONE_RELATIONS,
                    edge_readiness=edge_readiness,
                    requires_evidence_followup=requires_followup,
                ))

            if len(out) > len(allowed_pairs):
                out = out[: len(allowed_pairs)]
            if not out:
                return edges, {
                    "status": "rejected_global_output_empty_or_all_invalid",
                    "before": len(edges),
                    "after": len(edges),
                    "proposed_after": len(out),
                    "invalid_rows": invalid_rows,
                    "grounding_rejected": grounding_rejected,
                    "invented_edges_rejected": invented_edges_rejected,
                    "relation_corrections": relation_corrections,
                    "duplicate_pairs_dropped": duplicate_pairs_dropped,
                    "conservative_fallback": "kept_pairwise_edges",
                }
            # The final whole-graph editor is a semantic safety net, not an
            # authority to erase an already pairwise-vetted architecture.  A
            # very sparse answer commonly indicates context overload or that
            # evidence incompleteness was mistaken for relational invalidity.
            # Keep the compact pairwise graph and expose the over-pruning in
            # the audit instead of silently collapsing the review backbone.
            final_convergence = (
                len(edges) >= 8
                and (model_tier_override in {None, "premium_model"})
            )
            minimum_kept = max(6, math.ceil(len(edges) * 0.45)) if final_convergence else 0
            if minimum_kept and len(out) < minimum_kept:
                return edges, {
                    "status": "rejected_global_output_overpruned",
                    "before": len(edges),
                    "after": len(edges),
                    "proposed_after": len(out),
                    "minimum_kept": minimum_kept,
                    "invalid_rows": invalid_rows,
                    "grounding_rejected": grounding_rejected,
                    "invented_edges_rejected": invented_edges_rejected,
                    "relation_corrections": relation_corrections,
                    "duplicate_pairs_dropped": duplicate_pairs_dropped,
                    "conservative_fallback": "kept_compact_pairwise_graph",
                    "graph_assessment": str(parsed.get("graph_assessment") or ""),
                    "remaining_graph_risks": parsed.get("remaining_graph_risks") if isinstance(parsed.get("remaining_graph_risks"), list) else [],
                }
            return out, {
                "status": "ok",
                "before": len(edges),
                "after": len(out),
                "removed": max(0, len(edges) - len(out)),
                "invalid_rows": invalid_rows,
                "grounding_rejected": grounding_rejected,
                "invented_edges_rejected": invented_edges_rejected,
                "relation_corrections": relation_corrections,
                "duplicate_pairs_dropped": duplicate_pairs_dropped,
                "graph_assessment": str(parsed.get("graph_assessment") or ""),
                "remaining_graph_risks": parsed.get("remaining_graph_risks") if isinstance(parsed.get("remaining_graph_risks"), list) else [],
                "llm_usage": result.get("_llm_usage", {}),
            }
        except Exception as exc:
            return edges, {
                "status": "error_fallback_to_pairwise",
                "before": len(edges),
                "after": len(edges),
                "error": f"{type(exc).__name__}: {exc}",
            }

    def _global_critique_edges_batched(
        self,
        edges: list[DAGEdge],
        claims_registry: dict[str, dict],
        section_order: list[str],
        section_meta: dict[str, dict],
    ) -> tuple[list[DAGEdge], dict[str, Any]]:
        """Critique large graphs by target-section groups, then reconverge."""
        rank = {sid: idx for idx, sid in enumerate(section_order)}
        groups: dict[str, list[DAGEdge]] = defaultdict(list)
        for edge in edges:
            groups[edge.target_section_id].append(edge)
        jobs: list[list[DAGEdge]] = []
        for sid in sorted(groups, key=lambda value: rank.get(value, 10**6)):
            rows = groups[sid]
            jobs.extend(rows[i : i + 15] for i in range(0, len(rows), 15))

        def run_group(group: list[DAGEdge]) -> tuple[list[DAGEdge], dict[str, Any]]:
            involved_ids = {
                cid
                for edge in group
                for cid in (edge.source_claim_id, edge.target_claim_id)
            }
            subset_claims = {
                cid: claims_registry[cid]
                for cid in involved_ids
                if cid in claims_registry
            }
            involved_sections = {
                str(claim.get("section_id") or "")
                for claim in subset_claims.values()
            }
            subset_order = [sid for sid in section_order if sid in involved_sections]
            subset_meta = {sid: section_meta.get(sid, {}) for sid in subset_order}
            return self._global_critique_edges(
                group,
                subset_claims,
                subset_order,
                subset_meta,
                model_tier_override="advanced_model",
                max_tokens_override=2200,
            )

        with ThreadPoolExecutor(max_workers=min(4, len(jobs))) as pool:
            batch_results = list(pool.map(run_group, jobs))
        merged: list[DAGEdge] = []
        seen: set[tuple[str, str, str]] = set()
        for rows, _ in batch_results:
            for edge in rows:
                key = (edge.source_claim_id, edge.target_claim_id, edge.relation_type)
                if key not in seen:
                    seen.add(key)
                    merged.append(edge)

        # Keep the final whole-graph request compact while preserving coverage
        # across target sections. First take two strongest incoming edges per
        # target claim, then cap the final review at eighteen edges.
        confidence_rank = {"high": 3, "medium": 2, "low": 1}
        by_target: dict[str, list[DAGEdge]] = defaultdict(list)
        for edge in merged:
            by_target[edge.target_claim_id].append(edge)
        compact_edges: list[DAGEdge] = []
        for target in sorted(by_target):
            rows = sorted(
                by_target[target],
                key=lambda edge: confidence_rank.get(edge.confidence, 0),
                reverse=True,
            )
            compact_edges.extend(rows[:2])
        compact_edges = compact_edges[:18]

        final_edges = compact_edges
        final_stats: dict[str, Any] = {"status": "skipped_empty_after_batches"}
        if compact_edges:
            involved = {
                cid
                for edge in compact_edges
                for cid in (edge.source_claim_id, edge.target_claim_id)
            }
            final_claims = {cid: claims_registry[cid] for cid in involved if cid in claims_registry}
            final_edges, final_stats = self._global_critique_edges(
                compact_edges,
                final_claims,
                section_order,
                section_meta,
                model_tier_override="premium_model",
                max_tokens_override=3200,
            )
        return final_edges, {
            "status": "ok_batched" if final_edges else "empty_batched",
            "before": len(edges),
            "batch_count": len(jobs),
            "after_batches": len(merged),
            "before_final_convergence": len(compact_edges),
            "after": len(final_edges),
            "batch_stats": [stats for _, stats in batch_results],
            "final_convergence": final_stats,
        }

    def _build_claim_payload(self, claim: dict, section_meta: dict) -> dict:
        """Build enriched claim context for Proposer/Critic payload."""
        sec = section_meta.get(claim.get("section_id", ""), {})
        text_chunk_map: dict[str, str] = sec.get("text_chunk_map", {})
        visual_chunk_map: dict[str, dict] = sec.get("visual_chunk_map", {})

        text_previews = [
            {"chunk_id": cid, "text_preview": text_chunk_map[cid][:400]}
            for cid in (claim.get("supporting_text_chunk_ids") or [])[:2]
            if cid in text_chunk_map
        ]
        visual_previews = [
            {
                "chunk_id": vid,
                "visual_argument_type": visual_chunk_map[vid].get("visual_argument_type", ""),
                "caption": visual_chunk_map[vid].get("caption", "")[:200],
            }
            for vid in (claim.get("supporting_visual_chunk_ids") or [])[:2]
            if vid in visual_chunk_map
        ]
        dois = list({
            m.group(1)
            for cid in (claim.get("supporting_text_chunk_ids") or [])
            if (m := re.match(r"^(doi-[^:]+)", cid))
        })
        missing_components = [
            _compact_text(value, 180)
            for value in (claim.get("missing_evidence_components") or [])[:4]
            if _compact_text(value, 180)
        ]
        return {
            "claim_id": claim.get("claim_id", ""),
            "statement": claim.get("statement", ""),
            "primary_evidence_type": claim.get("evidence_type", "mechanism"),
            "secondary_evidence_types": claim.get("secondary_evidence_types", []),
            "evidence_binding_status": str(claim.get("evidence_binding_status") or ""),
            "evidence_binding_confidence": str(claim.get("evidence_binding_confidence") or ""),
            "evidence_binding_reason": _compact_text(claim.get("evidence_binding_reason"), 240),
            "claim_state": str(claim.get("claim_state") or ""),
            "evidence_requirement": str(claim.get("evidence_requirement") or "factual"),
            "closure_disposition": str(claim.get("closure_disposition") or ""),
            "readiness_action": str(
                (claim.get("_readiness_check") or {}).get("action") or ""
            ),
            "evidence_spans": _compact_evidence_spans(claim.get("evidence_spans")),
            "evidence_component_map": _compact_evidence_component_map(claim.get("evidence_component_map")),
            "missing_evidence_components": missing_components,
            "section_title": sec.get("title", ""),
            "section_argument_role": str(sec.get("argument_role", ""))[:300],
            "review_mentor_advice": sec.get("review_mentor_advice", {}),
            "supporting_text_previews": text_previews,
            "supporting_visual_previews": visual_previews,
            "source_doi_prefixes": dois[:3],
        }

    def _build_global_claim_payload(self, claim: dict, section_meta: dict) -> dict:
        """Compact claim context for whole-graph editing.

        Pairwise judges need source previews.  The global editor only decides
        whether an already-vetted relationship adds architectural value.  In
        particular, repeating the complete mentor memory and text previews for
        every claim caused hundred-thousand-token payloads and unstable pruning.
        """
        sec = section_meta.get(claim.get("section_id", ""), {})
        return {
            "claim_id": str(claim.get("claim_id") or ""),
            "section_id": str(claim.get("section_id") or ""),
            "statement": _compact_text(claim.get("statement"), 900),
            "primary_evidence_type": str(claim.get("evidence_type") or "mechanism"),
            "secondary_evidence_types": list(claim.get("secondary_evidence_types") or [])[:3],
            "evidence_binding_status": str(claim.get("evidence_binding_status") or ""),
            "evidence_binding_confidence": str(claim.get("evidence_binding_confidence") or ""),
            "claim_state": str(claim.get("claim_state") or ""),
            "evidence_requirement": str(claim.get("evidence_requirement") or "factual"),
            "closure_disposition": str(claim.get("closure_disposition") or ""),
            "evidence_spans": _compact_evidence_spans(claim.get("evidence_spans"))[:2],
            "evidence_component_map": _compact_evidence_component_map(
                claim.get("evidence_component_map")
            )[:4],
            "missing_evidence_components": [
                _compact_text(value, 160)
                for value in (claim.get("missing_evidence_components") or [])[:4]
                if _compact_text(value, 160)
            ],
            "section_title": _compact_text(sec.get("title"), 180),
            "section_argument_role": _compact_text(sec.get("argument_role"), 260),
        }
