"""Standalone chapter asset enhancement layer.

This layer consumes an accepted section input packet and its existing English
chapter draft and produces an enhanced chapter. It does not modify or rerun
upstream retrieval, claim review, evidence binding, or the existing
SectionWriter. The approved sequence is:

1. establish the chapter thesis and argument order from a compact plan;
2. generate small evidence-grounded explanation blocks from groups of claims;
3. compare against the legacy draft and patch only worthwhile omissions
   locally, using only existing evidence handles.

The old draft is a semantic map and omission checklist, never an evidence
source. Scientific writing uses the accepted claims, exact spans/verbatim
quotes, scope limitations, and source metadata from the packet. Qwen output
refers only to local semantic evidence handles; this module resolves those
handles to the packet's real ``[REF:paper_id]`` markers.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from llm.qwen_chat_client import call_qwen_chat  # noqa: E402
from optomind_research.review_writer import (  # noqa: E402
    EvidencePacket,
    SectionMaterialPacket,
)
from optomind_research.runtime.cost_ledger import (  # noqa: E402
    estimate_call_cost_cny,
)

SCHEMA_VERSION = "chapter_asset_enhancer.v1"
ENHANCEMENT_CONTRACT_VERSION = "chapter_asset_enhancer.contract.v2"

DEFAULT_PLANNER_TIER = "c_model"
DEFAULT_BLOCK_TIER = "c_model"
DEFAULT_PATCH_TIER = "c_model"
DEFAULT_AUDITOR_TIER = "c2_model"
DEFAULT_CONTRACT_REPAIR_TIER = "c2_model"
DEFAULT_EXPLANATORY_TIER = "c2_model"
DEFAULT_BLOCK_REVIEWER_TIER = "c2_model"

PROMPT_DIR = _PROJECT_ROOT / "prompts"
PROMPT_PLANNER = PROMPT_DIR / "Chapter Asset Argument Planner.txt"
PROMPT_BLOCK = PROMPT_DIR / "Chapter Asset Explanation Block Writer.txt"
PROMPT_AUDITOR = PROMPT_DIR / "Chapter Asset Legacy Gap Auditor.txt"
PROMPT_PATCH = PROMPT_DIR / "Chapter Asset Legacy Gap Patch Writer.txt"
PROMPT_REPAIR = PROMPT_DIR / "Chapter Asset Contract Repair.txt"
PROMPT_EXPLANATORY = (
    PROMPT_DIR / "Chapter Asset Explanatory Citation Planner.txt"
)
PROMPT_RERANKER = (
    PROMPT_DIR / "Chapter Asset Explanatory Semantic Reranker.txt"
)
PROMPT_APPLICATION_WRITER = (
    PROMPT_DIR / "Chapter Asset Representative Application Writer.txt"
)
PROMPT_BLOCK_REVIEWER = (
    PROMPT_DIR / "Chapter Asset Block Scientific Reviewer.txt"
)
PROMPT_BLOCK_REVISER = (
    PROMPT_DIR / "Chapter Asset Block Review Reviser.txt"
)


def enhancement_contract_fingerprint() -> dict[str, str]:
    """Hash every prompt that materially shapes enhancer output."""

    prompt_paths = (
        PROMPT_PLANNER,
        PROMPT_BLOCK,
        PROMPT_AUDITOR,
        PROMPT_PATCH,
        PROMPT_REPAIR,
        PROMPT_EXPLANATORY,
        PROMPT_RERANKER,
        PROMPT_APPLICATION_WRITER,
        PROMPT_BLOCK_REVIEWER,
        PROMPT_BLOCK_REVISER,
    )
    return {
        path.name: hashlib.sha256(
            path.read_text(encoding="utf-8").encode("utf-8")
        ).hexdigest()
        for path in prompt_paths
    }

OLD_CHAPTER_MD = "OLD_CHAPTER.md"
ENHANCED_CHAPTER_MD = "ENHANCED_CHAPTER.md"
CHAPTER_ARGUMENT_PLAN_JSON = "CHAPTER_ARGUMENT_PLAN.json"
TERMINOLOGY_REGISTRY_JSON = "TERMINOLOGY_REGISTRY.json"
EXPLANATION_BLOCKS_JSON = "EXPLANATION_BLOCKS.json"
CLAIM_TO_PARAGRAPH_MAP_JSON = "CLAIM_TO_PARAGRAPH_MAP.json"
LEGACY_GAP_AUDIT_JSON = "LEGACY_GAP_AUDIT.json"
ENHANCEMENT_REPORT_JSON = "ENHANCEMENT_REPORT.json"
EXPLANATORY_CITATION_LEDGER_JSON = "EXPLANATORY_CITATION_LEDGER.json"
BLOCK_SCIENTIFIC_REVIEW_JSON = "BLOCK_SCIENTIFIC_REVIEW.json"

_RAW_CITATION_PATTERN = re.compile(r"\[REF:[^\]]+\]|\[\d+(?:\s*[,;\-\u2013]\s*\d+)*\]")
_SLUG_STOPWORDS = {
    "AND",
    "THE",
    "OF",
    "FOR",
    "WITH",
    "IN",
    "ON",
    "TO",
    "A",
    "AN",
    "FROM",
    "BY",
    "AT",
    "AS",
    "IS",
    "ARE",
    "ITS",
    "IT",
    "THIS",
    "THAT",
}
_USABLE_PERMISSIONS = {"factual_assertion", "hedged_factual_assertion"}
_PLANNABLE_PERMISSIONS = {
    "factual_assertion",
    "hedged_factual_assertion",
    "evidence_gap_only",
}
_LOCAL_SUFFICIENCY_THRESHOLD = 0.30
_SEMANTIC_MISMATCH_MAX = 29
_COMMON_SCHOLARLY_TOKENS = {
    "model",
    "method",
    "design",
    "algorithm",
    "neural",
    "network",
    "gradient",
    "adaptive",
    "framework",
    "application",
    "approach",
    "technique",
    "system",
    "learning",
    "training",
    "based",
    "using",
    "review",
    "study",
    "paper",
}

_REPRESENTATIVE_APPLICATION_BENEFIT = "representative_application"
# Representative applications are a low-cost garnish: mildly relevant local
# material may illustrate an example, but clear topic mismatch stays rejected.
_APPLICATION_ACCEPTANCE_THRESHOLD = 0.12
_APPLICATION_MISMATCH_MAX = 0.05
# Conservative per-chapter and per-target caps so the path stays low cost.
DEFAULT_APPLICATION_MAX_TARGETS = 5
DEFAULT_APPLICATION_PER_TARGET_CAP = 6
DEFAULT_APPLICATION_LOCAL_MAX_RESULTS = 6
DEFAULT_APPLICATION_WRITER_TIER = "c2_model"
DEFAULT_APPLICATION_SOFT_MIN_TARGETS = 4
_APPLICATION_EXPANSION_MAX_CALLS = 1
_APPLICATION_WRITER_MAX_SENTENCES = 3
_APPLICATION_WRITER_MAX_CHARS = 700
_APPLICATION_WRITER_BATCH_SIZE = 3
# Only blocks with real explanatory depth are eligible application targets.
_APPLICATION_MIN_SENTENCE_COUNT = 2
_APPLICATION_BLOCK_TYPES = {
    "explanatory_body",
    "scientific_consequence",
    "mechanism",
    "comparison",
    "application",
}
_APPLICATION_METHOD_KEYWORDS = {
    "method",
    "methodology",
    "algorithm",
    "approach",
    "technique",
    "device",
    "design",
    "framework",
    "system",
    "architecture",
    "model",
    "pipeline",
    "workflow",
    "implementation",
    "procedure",
    "protocol",
    "strategy",
    "optimization",
    "platform",
    "tool",
}

QwenCaller = Callable[..., dict[str, Any]]
SearchCallback = Callable[[str, int], list[dict[str, Any]]]


class ChapterAssetModelError(Exception):
    """Core model response was unavailable or malformed."""

    def __init__(
        self,
        message: str,
        *,
        raw_output: str = "",
        payload: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.raw_output = raw_output
        self.payload = payload


class ChapterAssetIntegrityError(Exception):
    """A hard local integrity invariant was violated."""


@dataclass
class ClaimEntry:
    claim_id: str
    handle: str
    statement: str
    statement_for_writing: str
    writing_permission: str
    evidence_binding_status: str
    claim_state: str
    supported_components: list[Any]
    missing_evidence_components: list[str]
    caveats: list[str]
    qualified_support_only: bool
    qualified_wording_present: bool
    usable: bool
    evidence_handles: list[str] = field(default_factory=list)
    original: dict[str, Any] = field(default_factory=dict)

    def to_compact(self) -> dict[str, Any]:
        return {
            "handle": self.handle,
            "claim_id": self.claim_id,
            "statement": self.statement,
            "statement_for_writing": self.statement_for_writing,
            "writing_permission": self.writing_permission,
            "evidence_binding_status": self.evidence_binding_status,
            "claim_state": self.claim_state,
            "supported_components": list(self.supported_components),
            "missing_evidence_components": list(
                self.missing_evidence_components
            ),
            "caveats": list(self.caveats),
            "qualified_support_only": self.qualified_support_only,
            "qualified_wording_present": self.qualified_wording_present,
            "usable": self.usable,
            "evidence_handles": list(self.evidence_handles),
        }


def _claim_requires_core_evidence(entry: ClaimEntry | None) -> bool:
    return (
        entry is not None
        and entry.writing_permission in _USABLE_PERMISSIONS
    )


def _claim_is_evidence_gap_only(entry: ClaimEntry | None) -> bool:
    return (
        entry is not None
        and entry.writing_permission == "evidence_gap_only"
    )


@dataclass
class EvidenceEntry:
    handle: str
    claim_id: str
    claim_handle: str
    paper_id: str
    chunk_id: str
    exact_spans: list[str]
    limitations: list[str]
    source_kind: str
    evidence_level: str
    scope_fit: str
    retrieval_role: str
    source_title: str
    original: EvidencePacket

    def to_compact(self, *, full: bool = False) -> dict[str, Any]:
        data: dict[str, Any] = {
            "handle": self.handle,
            "claim_handle": self.claim_handle,
            "claim_id": self.claim_id,
            "source_kind": self.source_kind,
            "evidence_level": self.evidence_level,
            "scope_fit": self.scope_fit,
            "retrieval_role": self.retrieval_role,
            "source_title": self.source_title,
            "limitations": list(self.limitations),
        }
        if full:
            data["exact_spans"] = list(self.exact_spans)
        return data


@dataclass
class HandleLedger:
    claims: dict[str, ClaimEntry] = field(default_factory=dict)
    evidence: dict[str, EvidenceEntry] = field(default_factory=dict)
    claim_by_id: dict[str, ClaimEntry] = field(default_factory=dict)
    evidence_by_claim: dict[str, list[EvidenceEntry]] = field(
        default_factory=dict
    )

    def claim(self, handle: str) -> ClaimEntry | None:
        return self.claims.get(handle)

    def evidence_entry(self, handle: str) -> EvidenceEntry | None:
        return self.evidence.get(handle)

    def usable_claim_handles(self) -> set[str]:
        return {
            handle
            for handle, entry in self.claims.items()
            if entry.usable
        }


def _read_prompt(path: Path) -> str:
    source = Path(path)
    if not source.exists():
        raise FileNotFoundError(f"Required prompt file does not exist: {source}")
    return source.read_text(encoding="utf-8")


def _load_json_object(path: Path) -> dict[str, Any]:
    source = Path(path)
    if not source.exists():
        raise ValueError(f"Required source file does not exist: {source}")
    try:
        value = json.loads(source.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001 - clear offline failure message
        raise ValueError(f"Source file is not valid JSON: {source}: {exc}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"Source file must be a JSON object: {source}")
    return value


def _safe_json(text: str) -> Any:
    value = str(text or "").strip()
    try:
        return json.loads(value)
    except Exception:
        pass
    start = value.find("{")
    end = value.rfind("}")
    if start != -1 and end > start:
        try:
            return json.loads(value[start : end + 1])
        except Exception:
            pass
    return None


def _word_count(text: str) -> int:
    return len(re.findall(r"\S+", str(text or "")))


def _paragraph_count(text: str) -> int:
    return len([p for p in re.split(r"\n\s*\n", str(text or "")) if p.strip()])


def _refuse_existing_output(output_dir: Path, allow_overwrite: bool) -> None:
    output = Path(output_dir)
    if output.exists() and not allow_overwrite:
        raise FileExistsError(
            f"Output directory already exists: {output}. "
            "Pass --allow-overwrite to reuse it explicitly."
        )


def _slugify(value: str, limit: int = 46) -> str:
    text = re.sub(r"[^A-Za-z0-9]+", "_", str(value or "")).strip("_").upper()
    words = [
        word
        for word in text.split("_")
        if word and word not in _SLUG_STOPWORDS
    ]
    if not words:
        words = ["EVIDENCE"]
    slug = "_".join(words)[:limit].rstrip("_")
    return re.sub(r"_+", "_", slug)


def _compact_text(value: Any, limit: int = 600) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "..."


def _compact_list(values: Any, limit: int = 6, each_limit: int = 240) -> list[str]:
    if not isinstance(values, (list, tuple, set)):
        values = [values] if values else []
    return [
        _compact_text(item, each_limit)
        for item in list(values)[:limit]
    ]


def _compact_sibling_outlines(values: Any, limit: int = 6) -> list[Any]:
    if not isinstance(values, (list, tuple, set)):
        values = [values] if values else []
    outlines: list[Any] = []
    for item in list(values)[:limit]:
        if isinstance(item, dict):
            outlines.append(
                {
                    key: _compact_text(item.get(key), 200)
                    for key in (
                        "section_id",
                        "title",
                        "goal",
                        "scope",
                        "summary",
                        "do_not_cover",
                    )
                    if item.get(key)
                }
            )
        else:
            outlines.append(_compact_text(item, 240))
    return outlines


def _claim_slug(claim: dict[str, Any]) -> str:
    for key in ("statement_for_writing", "statement"):
        value = str(claim.get(key) or "").strip()
        if value:
            return _slugify(value)
    return "CLAIM"


def _compact_supported_components(claim: dict[str, Any]) -> list[Any]:
    """Structurally compact supported/claim components for model payloads."""
    raw = claim.get("supported_components")
    if isinstance(raw, list) and raw:
        return [
            (
                {
                    "component_id": str(item.get("component_id") or ""),
                    "statement": str(item.get("statement") or ""),
                    "support_assessment": str(
                        item.get("support_assessment") or ""
                    ),
                    "reason": str(item.get("reason") or ""),
                }
                if isinstance(item, dict)
                else str(item)
            )
            for item in raw
        ]
    components = claim.get("claim_components") or []
    if not isinstance(components, list):
        return []
    return [
        {
            "component_id": str(item.get("component_id") or ""),
            "statement": str(item.get("statement") or ""),
            "support_assessment": str(item.get("support_assessment") or ""),
            "reason": str(item.get("reason") or ""),
        }
        for item in components
        if isinstance(item, dict)
    ]


def _evidence_slug(entry: EvidencePacket, claim: dict[str, Any]) -> str:
    if str(entry.source_title or "").strip():
        return _slugify(str(entry.source_title))
    return _claim_slug(claim)


def _verbatim_quotes_for_chunk(
    claim: dict[str, Any],
    chunk_id: str,
) -> list[str]:
    """Collect verbatim quotes already verified for a chunk in the packet."""
    quotes: list[str] = []
    for quote in claim.get("verified_quotes") or []:
        if not isinstance(quote, dict):
            continue
        if str(quote.get("chunk_id") or "") != str(chunk_id or ""):
            continue
        text = str(
            quote.get("verbatim_quote")
            or quote.get("quote")
            or ""
        ).strip()
        if text:
            quotes.append(text)
    for component in claim.get("claim_components") or []:
        if not isinstance(component, dict):
            continue
        for binding in component.get("bindings") or []:
            if not isinstance(binding, dict):
                continue
            if str(binding.get("chunk_id") or "") != str(chunk_id or ""):
                continue
            text = str(binding.get("verbatim_quote") or "").strip()
            if text:
                quotes.append(text)
    return quotes


def _rehydrate_evidence_packet(row: Any, index: int) -> EvidencePacket:
    if not isinstance(row, dict):
        raise ValueError(f"evidence_packets[{index}] must be a JSON object")
    missing = [field_name for field_name in ("claim_id", "paper_id") if field_name not in row]
    if missing:
        raise ValueError(
            f"evidence_packets[{index}] is missing required fields: "
            + ", ".join(missing)
        )
    fields = [
        "claim_id",
        "paper_id",
        "chunk_id",
        "exact_spans",
        "visual_refs",
        "support_relation",
        "limitations",
        "evidence_level",
        "source_kind",
        "scope_fit",
        "retrieval_role",
        "source_title",
    ]
    return EvidencePacket(
        **{
            name: copy.deepcopy(row.get(name))
            for name in fields
            if name in row
        }
    )


def _rehydrate_packet(packet_data: dict[str, Any]) -> SectionMaterialPacket:
    section_id = str(packet_data.get("section_id") or "").strip()
    if not section_id:
        raise ValueError("input_packet.section_id must be a non-empty string")
    if not isinstance(packet_data.get("section_contract"), dict):
        raise ValueError("input_packet.section_contract must be a JSON object")
    if not isinstance(packet_data.get("claims"), list):
        raise ValueError("input_packet.claims must be a JSON array")
    raw_evidence = packet_data.get("evidence_packets")
    if not isinstance(raw_evidence, list):
        raise ValueError("input_packet.evidence_packets must be a JSON array")
    evidence_packets = [
        _rehydrate_evidence_packet(row, index)
        for index, row in enumerate(raw_evidence)
    ]
    return SectionMaterialPacket(
        section_id=section_id,
        section_contract=copy.deepcopy(packet_data["section_contract"]),
        claims=copy.deepcopy(packet_data["claims"]),
        evidence_packets=evidence_packets,
        contradictions=copy.deepcopy(packet_data.get("contradictions") or []),
        open_questions=copy.deepcopy(packet_data.get("open_questions") or []),
        transition_contract=copy.deepcopy(
            packet_data.get("transition_contract") or {}
        ),
        uncited_load_bearing_claim_ids=list(
            packet_data.get("uncited_load_bearing_claim_ids") or []
        ),
        visual_evidence=copy.deepcopy(packet_data.get("visual_evidence") or []),
        visual_gap_plan=copy.deepcopy(packet_data.get("visual_gap_plan") or []),
        manuscript_context=copy.deepcopy(
            packet_data.get("manuscript_context") or {}
        ),
        literature_coverage=copy.deepcopy(
            packet_data.get("literature_coverage") or {}
        ),
    )


def _build_handle_ledger(packet: SectionMaterialPacket) -> HandleLedger:
    ledger = HandleLedger()
    claims_by_id = {
        str(claim.get("claim_id") or ""): claim
        for claim in packet.claims
        if claim.get("claim_id")
    }
    claim_entries: list[ClaimEntry] = []
    for index, claim in enumerate(packet.claims, start=1):
        claim_id = str(claim.get("claim_id") or "")
        if not claim_id:
            continue
        handle = f"C{index:02d}_{_claim_slug(claim)}"
        permission = str(claim.get("writing_permission") or "")
        claim_entries.append(
            ClaimEntry(
                claim_id=claim_id,
                handle=handle,
                statement=str(claim.get("statement") or ""),
                statement_for_writing=str(
                    claim.get("statement_for_writing")
                    or claim.get("statement")
                    or ""
                ),
                writing_permission=permission,
                evidence_binding_status=str(
                    claim.get("evidence_binding_status") or ""
                ),
                claim_state=str(claim.get("claim_state") or ""),
                supported_components=_compact_supported_components(claim),
                missing_evidence_components=[
                    str(item)
                    for item in (
                        claim.get("missing_evidence_components") or []
                    )
                ],
                caveats=[
                    str(item) for item in (claim.get("caveats") or [])
                ],
                qualified_support_only=bool(
                    claim.get("qualified_support_only")
                ),
                qualified_wording_present=bool(
                    claim.get("qualified_wording_present")
                ),
                usable=(
                    permission in _PLANNABLE_PERMISSIONS
                    or bool(
                        claim.get("writing_permission")
                        in _PLANNABLE_PERMISSIONS
                    )
                ),
                original=copy.deepcopy(claim),
            )
        )
        ledger.claims[handle] = claim_entries[-1]
        ledger.claim_by_id[claim_id] = claim_entries[-1]

    evidence_by_claim: dict[str, list[EvidenceEntry]] = {}
    for index, entry in enumerate(packet.evidence_packets, start=1):
        claim = claims_by_id.get(str(entry.claim_id or ""))
        if claim is None or not str(entry.paper_id or "").strip():
            continue
        exact_spans = list(entry.exact_spans or [])
        if not exact_spans:
            exact_spans = _verbatim_quotes_for_chunk(claim, entry.chunk_id)
        if not exact_spans:
            continue
        claim_handle = next(
            (
                candidate.handle
                for candidate in claim_entries
                if candidate.claim_id == str(entry.claim_id or "")
            ),
            "",
        )
        if not claim_handle:
            continue
        handle = f"E{index:02d}_{_evidence_slug(entry, claim)}"
        evidence_entry = EvidenceEntry(
            handle=handle,
            claim_id=str(entry.claim_id or ""),
            claim_handle=claim_handle,
            paper_id=str(entry.paper_id or ""),
            chunk_id=str(entry.chunk_id or ""),
            exact_spans=exact_spans,
            limitations=list(entry.limitations),
            source_kind=str(entry.source_kind or ""),
            evidence_level=str(entry.evidence_level or ""),
            scope_fit=str(entry.scope_fit or ""),
            retrieval_role=str(entry.retrieval_role or ""),
            source_title=str(entry.source_title or ""),
            original=entry,
        )
        ledger.evidence[handle] = evidence_entry
        evidence_by_claim.setdefault(str(entry.claim_id or ""), []).append(
            evidence_entry
        )
        if claim_handle in ledger.claims:
            ledger.claims[claim_handle].evidence_handles.append(handle)
    for claim_entry in ledger.claims.values():
        claim_entry.evidence_handles = sorted(
            set(claim_entry.evidence_handles)
        )
    ledger.evidence_by_claim = evidence_by_claim
    return ledger


def _build_compact_context(
    packet: SectionMaterialPacket,
    ledger: HandleLedger,
) -> dict[str, Any]:
    contract = packet.section_contract or {}
    manuscript_context = packet.manuscript_context or {}
    research_context = manuscript_context.get("research_context") or {}
    boundary_contract = (
        manuscript_context.get("current_section_boundary_contract") or {}
    )
    if not isinstance(boundary_contract, dict):
        boundary_contract = {}
    if not isinstance(research_context, dict):
        research_context = {}
    transition_contract = packet.transition_contract or {}
    contract_transition = contract.get("transition_contract") or {}
    user_question = (
        research_context.get("user_question")
        or research_context.get("global_user_question")
        or research_context.get("user_questions")
        or ""
    ) or (
        contract.get("user_question")
        or manuscript_context.get("user_question")
        or ""
    )
    dynamic_axes = (
        contract.get("dynamic_axes")
        or contract.get("assigned_user_axes")
        or boundary_contract.get("assigned_user_axes")
        or manuscript_context.get("dynamic_axes")
        or []
    )
    must_cover = (
        contract.get("must_cover")
        or boundary_contract.get("must_cover")
        or research_context.get("provisional_must_cover")
        or []
    )
    unique_contribution = (
        contract.get("unique_contribution")
        or boundary_contract.get("unique_contribution")
        or research_context.get("provisional_unique_contribution")
        or ""
    )
    argument_role = (
        contract.get("argument_role")
        or boundary_contract.get("argument_role")
        or research_context.get("provisional_argument_role")
        or ""
    )
    sibling_outlines = (
        manuscript_context.get("full_section_workplan")
        or manuscript_context.get("sibling_section_responsibilities")
        or contract.get("sibling_section_outlines")
        or contract.get("sibling_outlines")
        or manuscript_context.get("sibling_section_outlines")
        or manuscript_context.get("sibling_outlines")
        or []
    )
    do_not_cover = (
        contract.get("do_not_cover")
        or contract.get("must_not_cover")
        or boundary_contract.get("must_not_cover")
        or contract.get("explicit_out_of_scope")
        or contract.get("out_of_scope")
        or manuscript_context.get("do_not_cover")
        or []
    )
    handoff_from_previous = (
        contract.get("handoff_from_previous")
        or boundary_contract.get("handoff_from_previous")
        or contract_transition.get("transition_from_previous")
        or contract_transition.get("handoff_from_previous")
        or transition_contract.get("transition_from_previous")
        or transition_contract.get("handoff_from_previous")
        or ""
    )
    handoff_to_next = (
        contract.get("handoff_to_next")
        or boundary_contract.get("handoff_to_next")
        or contract_transition.get("transition_to_next")
        or contract_transition.get("handoff_to_next")
        or transition_contract.get("transition_to_next")
        or transition_contract.get("handoff_to_next")
        or ""
    )
    key_questions = (
        contract.get("key_questions")
        or contract.get("current_section_key_questions")
        or research_context.get("current_section_key_questions")
        or boundary_contract.get("key_questions")
        or research_context.get("provisional_key_questions")
        or []
    )
    global_review_thesis = (
        research_context.get("global_review_thesis")
        or
        research_context.get("global_thesis")
        or research_context.get("global_narrative")
        or research_context.get("thesis")
        or ""
    )
    global_narrative_strategy = (
        research_context.get("global_narrative_strategy")
        or research_context.get("global_narrative")
        or research_context.get("narrative_strategy")
        or ""
    )
    return {
        "schema_version": SCHEMA_VERSION,
        "section_id": packet.section_id,
        "title": contract.get("title") or packet.section_id,
        "section_responsibility": {
            "must_cover": _compact_list(must_cover),
            "unique_contribution": _compact_text(unique_contribution),
            "section_purpose": _compact_text(
                contract.get("section_purpose")
                or boundary_contract.get("section_purpose")
            ),
            "central_thesis": _compact_text(
                contract.get("central_thesis")
                or boundary_contract.get("central_thesis")
            ),
            "argument_role": _compact_text(argument_role),
            "do_not_cover": _compact_list(do_not_cover),
            "must_not_cover": _compact_list(do_not_cover),
            "key_questions": _compact_list(key_questions),
        },
        "transition_handoff": {
            "transition_from_previous": _compact_text(handoff_from_previous),
            "handoff_from_previous": _compact_text(handoff_from_previous),
            "transition_to_next": _compact_text(handoff_to_next),
            "handoff_to_next": _compact_text(handoff_to_next),
            "manuscript_position": _compact_text(
                contract_transition.get("manuscript_position")
                or transition_contract.get("manuscript_position")
            ),
        },
        "user_context": {
            "user_question": _compact_text(user_question),
            "dynamic_axes": _compact_list(dynamic_axes),
            "assigned_user_axes": _compact_list(dynamic_axes),
            "global_review_thesis": _compact_text(global_review_thesis, 400),
            "global_narrative_strategy": _compact_text(
                global_narrative_strategy, 400
            ),
            "manuscript_context": _compact_text(
                manuscript_context.get("summary")
                or manuscript_context.get("current_manuscript_status"),
                400,
            ),
        },
        "sibling_outlines": _compact_sibling_outlines(sibling_outlines),
        "guardrails": {
            "forbidden_overclaims": _compact_list(
                contract.get("forbidden_overclaims")
            ),
            "scope_guardrails": _compact_list(
                contract.get("scope_guardrails")
            ),
            "open_questions": _compact_list(contract.get("open_questions")),
        },
        "claims": [
            entry.to_compact()
            for entry in ledger.claims.values()
        ],
        "evidence_handles": [
            entry.to_compact(full=False)
            for entry in ledger.evidence.values()
        ],
        "pedagogical_principle": (
            "Define a method on first use, explain its intuitive idea, show "
            "how the mechanism works, connect it to representative evidence "
            "or application, contrast it when useful, state boundaries when "
            "needed, and return to the chapter's own thesis."
        ),
    }


def _collect_usage(
    *,
    agent_name: str,
    model_tier: str,
    result: dict[str, Any],
    messages: list[dict[str, Any]],
    usage_records: list[dict[str, Any]],
) -> None:
    usage = result.get("_llm_usage") if isinstance(result, dict) else {}
    if not isinstance(usage, dict):
        usage = {}
    model_name = str(usage.get("model_name") or "unknown_model")
    input_tokens = int(usage.get("input_tokens") or 0) or max(
        1, len(str(messages)) // 4
    )
    output_tokens = int(usage.get("output_tokens") or 0) or max(
        1, len(str(result.get("content") or "")) // 4
    )
    usage_records.append(
        {
            "agent_name": agent_name,
            "model_tier": model_tier,
            "model_name": model_name,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "estimated_cost_cny": estimate_call_cost_cny(
                model_name, input_tokens, output_tokens
            ),
            "usage_available": bool(
                usage.get("input_tokens") or usage.get("output_tokens")
            ),
            "success": bool(usage.get("success")),
            "error": str(usage.get("error_type") or ""),
            "fallback_used": bool(usage.get("fallback_used")),
            "model_fallback_used": bool(usage.get("model_fallback_used")),
            "attempted_models": list(usage.get("attempted_models") or []),
            "request_attempt_count": int(
                usage.get("request_attempt_count") or 0
            ),
            "retry_count": int(usage.get("retry_count") or 0),
        }
    )


def _call_model(
    *,
    agent_name: str,
    prompt_path: Path,
    payload: dict[str, Any],
    model_tier: str,
    qwen_caller: QwenCaller,
    usage_records: list[dict[str, Any]],
    temperature: float = 0.2,
    max_tokens: int = 4096,
) -> dict[str, Any]:
    prompt = _read_prompt(prompt_path)
    messages = [
        {"role": "system", "content": prompt},
        {
            "role": "user",
            "content": json.dumps(payload, ensure_ascii=False),
        },
    ]
    try:
        result = qwen_caller(
            agent_name,
            messages,
            model_tier=model_tier,
            temperature=temperature,
            max_tokens=max_tokens,
            response_format={"type": "json_object"},
            stream=False,
            force_mock=False,
            max_retries=2,
            timeout_seconds=180,
            max_transport_key_candidates=2,
            allow_model_fallback=False,
            accept_partial_stream=False,
            enable_thinking=False,
        )
    except Exception as exc:  # noqa: BLE001 - bounded offline failure
        usage_records.append(
            {
                "agent_name": agent_name,
                "model_tier": model_tier,
                "model_name": "unknown_model",
                "input_tokens": max(1, len(str(messages)) // 4),
                "output_tokens": 0,
                "estimated_cost_cny": estimate_call_cost_cny(
                    "unknown_model",
                    max(1, len(str(messages)) // 4),
                    0,
                ),
                "usage_available": False,
                "error": type(exc).__name__,
            }
        )
        raise ChapterAssetModelError(
            f"Core model response unavailable for {agent_name}: "
            f"{type(exc).__name__}"
        ) from exc
    if not isinstance(result, dict):
        usage_records.append(
            {
                "agent_name": agent_name,
                "model_tier": model_tier,
                "model_name": "unknown_model",
                "input_tokens": max(1, len(str(messages)) // 4),
                "output_tokens": 0,
                "estimated_cost_cny": estimate_call_cost_cny(
                    "unknown_model",
                    max(1, len(str(messages)) // 4),
                    0,
                ),
                "usage_available": False,
                "error": "non_dict_result",
            }
        )
        raise ChapterAssetModelError(
            f"Core model response unavailable for {agent_name}: non-dict result"
        )
    _collect_usage(
        agent_name=agent_name,
        model_tier=model_tier,
        result=result,
        messages=messages,
        usage_records=usage_records,
    )
    content = str(result.get("content") or "").strip()
    if not content or content.startswith("[mock]") or content.startswith(
        "[fallback]"
    ):
        raise ChapterAssetModelError(
            f"Core model response unavailable for {agent_name}: empty or fallback"
        )
    parsed = _safe_json(content)
    if not isinstance(parsed, dict):
        raise ChapterAssetModelError(
            f"Core model response unavailable for {agent_name}: invalid JSON",
            raw_output=content,
            payload=copy.deepcopy(payload),
        )
    return parsed


def _normalize_plan(
    plan: dict[str, Any],
    ledger: HandleLedger,
) -> tuple[dict[str, Any], list[str]]:
    warnings: list[str] = []
    for required in (
        "chapter_thesis",
        "reader_takeaway",
        "argument_sequence",
        "terminology_rows",
        "explanation_block_rows",
    ):
        if required not in plan:
            raise ChapterAssetIntegrityError(
                f"Planner output is missing required field: {required}"
            )
    if not isinstance(plan["argument_sequence"], list):
        raise ChapterAssetIntegrityError("argument_sequence must be a JSON array")
    if not isinstance(plan["terminology_rows"], list):
        raise ChapterAssetIntegrityError("terminology_rows must be a JSON array")
    if not isinstance(plan["explanation_block_rows"], list):
        raise ChapterAssetIntegrityError(
            "explanation_block_rows must be a JSON array"
        )
    if not plan["explanation_block_rows"]:
        raise ChapterAssetIntegrityError(
            "Planner returned no explanation blocks"
        )

    omitted = plan.get("omitted_handle_reasons") or {}
    if not isinstance(omitted, dict):
        raise ChapterAssetIntegrityError(
            "omitted_handle_reasons must be a JSON object"
        )

    normalized_blocks: list[dict[str, Any]] = []
    seen_indexes: set[int] = set()
    for offset, row in enumerate(plan["explanation_block_rows"], start=1):
        if not isinstance(row, dict):
            raise ChapterAssetIntegrityError(
                "explanation_block_rows entries must be JSON objects"
            )
        block_index = row.get("block_index", offset)
        try:
            block_index = int(block_index)
        except (TypeError, ValueError) as exc:
            raise ChapterAssetIntegrityError(
                "explanation_block_rows block_index must be an integer"
            ) from exc
        if block_index in seen_indexes:
            raise ChapterAssetIntegrityError(
                f"Duplicate explanation block index: {block_index}"
            )
        seen_indexes.add(block_index)
        claim_handles = row.get("claim_handles") or []
        evidence_handles = row.get("evidence_handles") or []
        if not isinstance(claim_handles, list) or not isinstance(
            evidence_handles, list
        ):
            raise ChapterAssetIntegrityError(
                "Each explanation block needs claim_handles and "
                "evidence_handles arrays"
            )
        normalized_claim_handles = [str(handle) for handle in claim_handles]
        normalized_evidence_handles = [str(handle) for handle in evidence_handles]
        normalized_blocks.append(
            {
                "block_index": block_index,
                "title": str(row.get("title") or f"Block {block_index}"),
                "block_type": str(
                    row.get("block_type") or "explanatory_body"
                ),
                "goal": str(row.get("goal") or ""),
                "claim_handles": normalized_claim_handles,
                "evidence_handles": normalized_evidence_handles,
                "omitted_handle_reasons": {
                    str(k): str(v)
                    for k, v in (row.get("omitted_handle_reasons") or {}).items()
                },
            }
        )

    for row in plan["terminology_rows"]:
        if not isinstance(row, dict):
            raise ChapterAssetIntegrityError(
                "terminology_rows entries must be JSON objects"
            )
        for key in ("definition_claim_handle", "representative_application_claim_handle"):
            value = row.get(key)
            if value is None:
                continue
            if ledger.claim(str(value)) is None:
                continue

    for step in plan["argument_sequence"]:
        if not isinstance(step, dict):
            raise ChapterAssetIntegrityError(
                "argument_sequence entries must be JSON objects"
            )
        step["claim_handles"] = [
            str(handle) for handle in step.get("claim_handles") or []
        ]
        step["evidence_handles"] = [
            str(handle) for handle in step.get("evidence_handles") or []
        ]

    responsibility_rows = plan.get("responsibility_coverage_rows") or []
    if not isinstance(responsibility_rows, list):
        warnings.append(
            "responsibility_coverage_rows must be a JSON array; ignored"
        )
        responsibility_rows = []
    normalized_responsibility: list[dict[str, Any]] = []
    for row in responsibility_rows:
        if not isinstance(row, dict):
            warnings.append(
                "responsibility_coverage_rows entry is not a JSON object"
            )
            continue
        handles = [str(handle) for handle in (row.get("claim_handles") or [])]
        unknown = [
            handle for handle in handles if ledger.claim(handle) is None
        ]
        if unknown:
            warnings.append(
                "responsibility_coverage_rows references unknown claim "
                "handles: " + ", ".join(unknown)
            )
        normalized_responsibility.append(
            {
                "responsibility": str(row.get("responsibility") or ""),
                "status": str(row.get("status") or "covered"),
                "claim_handles": handles,
                "note": str(row.get("note") or ""),
            }
        )

    covered = {
        handle
        for block in normalized_blocks
        for handle in block["claim_handles"]
    }
    missing = ledger.usable_claim_handles() - covered - set(omitted)
    for handle in sorted(missing):
        warnings.append(f"usable claim not assigned or omitted: {handle}")
    uncovered_gap_handles = {
        handle
        for handle in missing
        if _claim_is_evidence_gap_only(ledger.claim(handle))
    }
    if uncovered_gap_handles:
        empty_carrier_blocks = [
            str(block["block_index"])
            for block in normalized_blocks
            if not block["claim_handles"] and not block["evidence_handles"]
        ]
    normalized = {
        "chapter_thesis": str(plan.get("chapter_thesis") or ""),
        "reader_takeaway": str(plan.get("reader_takeaway") or ""),
        "argument_sequence": [
            {
                "step_index": int(
                    step.get("step_index", index + 1)
                    if isinstance(step, dict)
                    else index + 1
                ),
                "purpose": str(
                    step.get("purpose") if isinstance(step, dict) else ""
                ),
                "claim_handles": [
                    str(h)
                    for h in (
                        step.get("claim_handles")
                        if isinstance(step, dict)
                        else []
                    )
                    or []
                ],
                "evidence_handles": [
                    str(h)
                    for h in (
                        step.get("evidence_handles")
                        if isinstance(step, dict)
                        else []
                    )
                    or []
                ],
            }
            for index, step in enumerate(plan["argument_sequence"])
        ],
        "terminology_rows": copy.deepcopy(plan["terminology_rows"]),
        "explanation_block_rows": normalized_blocks,
        "responsibility_coverage_rows": normalized_responsibility,
        "omitted_handle_reasons": {
            str(k): str(v) for k, v in omitted.items()
        },
    }
    return normalized, warnings


def _call_contract_repair(
    *,
    original_payload: dict[str, Any],
    expected_fields: list[str],
    allowed_handles: set[str],
    invalid_output: str,
    validation_error: str,
    repair_tier: str,
    qwen_caller: QwenCaller,
    usage_records: list[dict[str, Any]],
    recovery_diagnostics: list[dict[str, Any]],
    max_tokens: int = 5000,
) -> dict[str, Any]:
    repair_payload = {
        "task": (
            "Correct the supplied JSON contract output so it satisfies the "
            "expected fields and semantic-handle constraints."
        ),
        "expected_fields": expected_fields,
        "original_payload": original_payload,
        "invalid_output": invalid_output,
        "validation_error": validation_error,
        "allowed_semantic_handles": sorted(allowed_handles),
        "constraints": (
            "Return JSON only. Use only allowed semantic handles. Never emit "
            "paper IDs, chunk IDs, raw citation markers, hashes, or invented "
            "handles."
        ),
    }
    try:
        repaired = _call_model(
            agent_name="ChapterAssetContractRepairAgent",
            prompt_path=PROMPT_REPAIR,
            payload=repair_payload,
            model_tier=repair_tier,
            qwen_caller=qwen_caller,
            usage_records=usage_records,
            temperature=0.1,
            max_tokens=max_tokens,
        )
    except ChapterAssetModelError as exc:
        recovery_diagnostics.append(
            {
                "stage": "contract_repair",
                "repair_tier": repair_tier,
                "validation_error": str(validation_error)[:500],
                "repaired": False,
                "error": str(exc),
            }
        )
        raise
    return repaired


def _record_contract_repair_outcome(
    *,
    recovery_diagnostics: list[dict[str, Any]],
    repair_tier: str,
    validation_error: str,
    repaired: bool,
    error: str = "",
) -> None:
    record: dict[str, Any] = {
        "stage": "contract_repair",
        "repair_tier": repair_tier,
        "validation_error": str(validation_error)[:500],
        "repaired": repaired,
    }
    if error:
        record["error"] = str(error)[:500]
    recovery_diagnostics.append(record)


def _repair_planner_and_validate(
    *,
    repaired: dict[str, Any],
    ledger: HandleLedger,
    repair_tier: str,
    validation_error: str,
    recovery_diagnostics: list[dict[str, Any]],
) -> tuple[dict[str, Any], list[str]]:
    try:
        normalized, warnings = _normalize_plan(repaired, ledger)
    except ChapterAssetIntegrityError as exc:
        _record_contract_repair_outcome(
            recovery_diagnostics=recovery_diagnostics,
            repair_tier=repair_tier,
            validation_error=validation_error,
            repaired=False,
            error=str(exc),
        )
        raise
    _record_contract_repair_outcome(
        recovery_diagnostics=recovery_diagnostics,
        repair_tier=repair_tier,
        validation_error=validation_error,
        repaired=True,
    )
    return normalized, warnings


def _repair_block_output(
    *,
    original_payload: dict[str, Any],
    allowed_handles: set[str],
    ledger: HandleLedger,
    requires_evidence: bool,
    invalid_output: str,
    validation_error: str,
    repair_tier: str,
    qwen_caller: QwenCaller,
    usage_records: list[dict[str, Any]],
    recovery_diagnostics: list[dict[str, Any]],
) -> tuple[str, list[str]]:
    repaired = _call_contract_repair(
        original_payload=original_payload,
        expected_fields=["paragraph_prose", "used_evidence_handles"],
        allowed_handles=allowed_handles,
        invalid_output=invalid_output,
        validation_error=validation_error,
        repair_tier=repair_tier,
        qwen_caller=qwen_caller,
        usage_records=usage_records,
        recovery_diagnostics=recovery_diagnostics,
    )
    try:
        result = _validate_block_output(
            repaired,
            allowed_handles=allowed_handles,
            ledger=ledger,
            requires_evidence=requires_evidence,
        )
    except ChapterAssetIntegrityError as exc:
        _record_contract_repair_outcome(
            recovery_diagnostics=recovery_diagnostics,
            repair_tier=repair_tier,
            validation_error=validation_error,
            repaired=False,
            error=str(exc),
        )
        raise
    _record_contract_repair_outcome(
        recovery_diagnostics=recovery_diagnostics,
        repair_tier=repair_tier,
        validation_error=validation_error,
        repaired=True,
    )
    return result


def _run_planner(
    *,
    compact_context: dict[str, Any],
    ledger: HandleLedger,
    model_tier: str,
    repair_tier: str,
    qwen_caller: QwenCaller,
    usage_records: list[dict[str, Any]],
    recovery_diagnostics: list[dict[str, Any]],
) -> tuple[dict[str, Any], list[str]]:
    allowed_handles = set(ledger.claims) | set(ledger.evidence)
    expected_fields = [
        "chapter_thesis",
        "reader_takeaway",
        "argument_sequence",
        "terminology_rows",
        "explanation_block_rows",
        "omitted_handle_reasons",
    ]
    try:
        raw_plan = _call_model(
            agent_name="ChapterAssetArgumentPlannerAgent",
            prompt_path=PROMPT_PLANNER,
            payload=compact_context,
            model_tier=model_tier,
            qwen_caller=qwen_caller,
            usage_records=usage_records,
            temperature=0.2,
            max_tokens=8000,
        )
    except ChapterAssetModelError as exc:
        repaired = _call_contract_repair(
            original_payload=compact_context,
            expected_fields=expected_fields,
            allowed_handles=allowed_handles,
            invalid_output=exc.raw_output,
            validation_error=str(exc),
            repair_tier=repair_tier,
            qwen_caller=qwen_caller,
            usage_records=usage_records,
            recovery_diagnostics=recovery_diagnostics,
            max_tokens=8000,
        )
        return _repair_planner_and_validate(
            repaired=repaired,
            ledger=ledger,
            repair_tier=repair_tier,
            validation_error=str(exc),
            recovery_diagnostics=recovery_diagnostics,
        )
    try:
        return _normalize_plan(raw_plan, ledger)
    except ChapterAssetIntegrityError as exc:
        repaired = _call_contract_repair(
            original_payload=compact_context,
            expected_fields=expected_fields,
            allowed_handles=allowed_handles,
            invalid_output=json.dumps(raw_plan, ensure_ascii=False),
            validation_error=str(exc),
            repair_tier=repair_tier,
            qwen_caller=qwen_caller,
            usage_records=usage_records,
            recovery_diagnostics=recovery_diagnostics,
            max_tokens=8000,
        )
        return _repair_planner_and_validate(
            repaired=repaired,
            ledger=ledger,
            repair_tier=repair_tier,
            validation_error=str(exc),
            recovery_diagnostics=recovery_diagnostics,
        )


def _block_payload(
    block_row: dict[str, Any],
    plan: dict[str, Any],
    ledger: HandleLedger,
) -> dict[str, Any]:
    claim_handles = block_row["claim_handles"]
    evidence_handles = block_row["evidence_handles"]
    claims = [
        ledger.claim(handle).to_compact()
        for handle in claim_handles
        if ledger.claim(handle) is not None
    ]
    evidence = [
        ledger.evidence_entry(handle).to_compact(full=True)
        for handle in evidence_handles
        if ledger.evidence_entry(handle) is not None
    ]
    terminology = [
        row
        for row in plan["terminology_rows"]
        if isinstance(row, dict)
        and (
            str(row.get("definition_claim_handle") or "") in claim_handles
            or str(row.get("representative_application_claim_handle") or "")
            in claim_handles
        )
    ]
    rows_sorted = sorted(
        plan["explanation_block_rows"],
        key=lambda item: item["block_index"],
    )
    position_index = next(
        (
            index
            for index, row in enumerate(rows_sorted)
            if row["block_index"] == block_row["block_index"]
        ),
        -1,
    )
    previous_block = (
        rows_sorted[position_index - 1]
        if position_index > 0
        else None
    )
    next_block = (
        rows_sorted[position_index + 1]
        if position_index >= 0 and position_index + 1 < len(rows_sorted)
        else None
    )
    return {
        "schema_version": SCHEMA_VERSION,
        "section_identity": {
            "section_id": plan.get("section_id") or "",
            "title": plan.get("title") or "",
            "chapter_thesis": plan.get("chapter_thesis") or "",
            "reader_takeaway": plan.get("reader_takeaway") or "",
            "argument_step": next(
                (
                    step
                    for step in plan["argument_sequence"]
                    if block_row["block_index"] == step.get("step_index")
                ),
                None,
            ),
        },
        "chapter_outline": [
            {
                "block_index": row["block_index"],
                "title": row["title"],
                "block_type": row["block_type"],
                "goal": row["goal"],
                "claim_handles": list(row["claim_handles"]),
            }
            for row in rows_sorted
        ],
        "position": {
            "block_index": block_row["block_index"],
            "sequence_position": position_index + 1 if position_index >= 0 else 0,
            "previous_block": (
                {
                    "block_index": previous_block["block_index"],
                    "title": previous_block["title"],
                    "goal": previous_block["goal"],
                }
                if previous_block is not None
                else None
            ),
            "next_block": (
                {
                    "block_index": next_block["block_index"],
                    "title": next_block["title"],
                    "goal": next_block["goal"],
                }
                if next_block is not None
                else None
            ),
        },
        "block": {
            "block_index": block_row["block_index"],
            "title": block_row["title"],
            "block_type": block_row["block_type"],
            "goal": block_row["goal"],
        },
        "claims": claims,
        "evidence": evidence,
        "terminology_rows": terminology,
        "pedagogical_principle": (
            "Define a method on first use, explain its intuitive idea, show "
            "how the mechanism works, connect it to representative evidence "
            "or application, contrast it when useful, state boundaries when "
            "needed, and return to the chapter's own thesis."
        ),
        "writing_guidance": {
            "target_words_nonbinding": 180,
            "no_padding": True,
            "note": "Write as much as the supplied evidence supports; never "
            "pad, repeat, or invent to reach a length.",
        },
    }


def _validate_block_output(
    output: dict[str, Any],
    *,
    allowed_handles: set[str],
    ledger: HandleLedger,
    requires_evidence: bool = False,
    required_handles: set[str] | None = None,
) -> tuple[str, list[str]]:
    prose = str(output.get("paragraph_prose") or "").strip()
    if not prose:
        raise ChapterAssetIntegrityError("Block writer returned empty prose")
    if _RAW_CITATION_PATTERN.search(prose):
        raise ChapterAssetIntegrityError(
            "Block writer returned raw citation markers; use semantic handles only"
        )
    used = output.get("used_evidence_handles") or []
    if not isinstance(used, list):
        raise ChapterAssetIntegrityError(
            "used_evidence_handles must be a JSON array"
        )
    used = [str(handle) for handle in used]
    deduped: list[str] = []
    for handle in used:
        if handle not in deduped:
            deduped.append(handle)
    return prose, deduped


def _run_block_writers(
    *,
    plan: dict[str, Any],
    ledger: HandleLedger,
    model_tier: str,
    repair_tier: str,
    qwen_caller: QwenCaller,
    usage_records: list[dict[str, Any]],
    recovery_diagnostics: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    blocks: list[dict[str, Any]] = []
    for row in sorted(
        plan["explanation_block_rows"], key=lambda item: item["block_index"]
    ):
        payload = _block_payload(row, plan, ledger)
        allowed_handles = set(row["evidence_handles"])
        requires_evidence = bool(allowed_handles) and any(
            _claim_requires_core_evidence(ledger.claim(handle))
            for handle in row["claim_handles"]
        )
        try:
            output = _call_model(
                agent_name="ChapterAssetExplanationBlockWriterAgent",
                prompt_path=PROMPT_BLOCK,
                payload=payload,
                model_tier=model_tier,
                qwen_caller=qwen_caller,
                usage_records=usage_records,
                temperature=0.2,
                max_tokens=3000,
            )
        except ChapterAssetModelError as exc:
            prose, used_handles = _repair_block_output(
                original_payload=payload,
                allowed_handles=allowed_handles,
                ledger=ledger,
                requires_evidence=requires_evidence,
                invalid_output=exc.raw_output,
                validation_error=str(exc),
                repair_tier=repair_tier,
                qwen_caller=qwen_caller,
                usage_records=usage_records,
                recovery_diagnostics=recovery_diagnostics,
            )
        else:
            try:
                prose, used_handles = _validate_block_output(
                    output,
                    allowed_handles=allowed_handles,
                    ledger=ledger,
                    requires_evidence=requires_evidence,
                )
            except ChapterAssetIntegrityError as exc:
                prose, used_handles = _repair_block_output(
                    original_payload=payload,
                    allowed_handles=allowed_handles,
                    ledger=ledger,
                    requires_evidence=requires_evidence,
                    invalid_output=json.dumps(output, ensure_ascii=False),
                    validation_error=str(exc),
                    repair_tier=repair_tier,
                    qwen_caller=qwen_caller,
                    usage_records=usage_records,
                    recovery_diagnostics=recovery_diagnostics,
                )
        blocks.append(
            {
                "block_index": row["block_index"],
                "title": row["title"],
                "block_type": row["block_type"],
                "goal": row["goal"],
                "prose": prose,
                "planned_claim_handles": list(row["claim_handles"]),
                "claim_handles": list(row["claim_handles"]),
                "evidence_handles": used_handles,
                "covered_claim_handles": [],
                "covered_claim_ids": [],
                "markers": [],
                "explanatory_handles": [],
                "explanatory_markers": [],
                "explanatory_sentence_markers": {},
                "patch_history": [],
            }
        )
    return blocks


_BLOCK_REVIEW_FLAG_TYPES = {
    "material_contradiction",
    "unsupported_new_mechanism",
    "material_overclaim",
    "evidence_permission_violation",
    "advisory",
}


def _block_evidence_scope(
    block: dict[str, Any],
    plan: dict[str, Any],
    ledger: HandleLedger,
) -> set[str]:
    """Evidence bounded to the block's planned claims plus plan/current handles."""
    planned_claims = set(
        block.get("planned_claim_handles", block["claim_handles"])
    )
    scope = {
        handle
        for handle, entry in ledger.evidence.items()
        if entry.claim_handle in planned_claims
    }
    for row in plan.get("explanation_block_rows") or []:
        if isinstance(row, dict) and row.get("block_index") == block["block_index"]:
            scope.update(str(handle) for handle in (row.get("evidence_handles") or []))
    scope.update(str(handle) for handle in (block.get("evidence_handles") or []))
    return scope


def _block_review_payload(
    *,
    blocks: list[dict[str, Any]],
    plan: dict[str, Any],
    ledger: HandleLedger,
    compact_context: dict[str, Any],
) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "section_id": plan.get("section_id") or "",
        "title": plan.get("title") or "",
        "chapter_thesis": plan.get("chapter_thesis") or "",
        "reader_takeaway": plan.get("reader_takeaway") or "",
        "guardrails": compact_context.get("guardrails") or {},
        "blocks": [
            {
                "block_index": block["block_index"],
                "title": block["title"],
                "block_type": block["block_type"],
                "goal": block["goal"],
                "paragraph_prose": block["prose"],
                "claims": [
                    ledger.claim(handle).to_compact()
                    for handle in block.get(
                        "planned_claim_handles", block["claim_handles"]
                    )
                    if ledger.claim(handle) is not None
                ],
                "evidence": [
                    ledger.evidence_entry(handle).to_compact(full=True)
                    for handle in sorted(
                        _block_evidence_scope(block, plan, ledger)
                    )
                    if ledger.evidence_entry(handle) is not None
                ],
            }
            for block in blocks
        ],
        "instruction": (
            "Flag only obvious material contradictions, unsupported new "
            "mechanisms, or material strengthening absent support. Advisory "
            "rows must not be blocking."
        ),
    }


def _normalize_block_review_rows(
    output: dict[str, Any],
    *,
    blocks: list[dict[str, Any]],
    ledger: HandleLedger,
    plan: dict[str, Any],
) -> tuple[list[dict[str, Any]], list[str]]:
    warnings: list[str] = []
    raw_rows = output.get("review_rows") or []
    if not isinstance(raw_rows, list):
        warnings.append("block scientific reviewer returned non-list review_rows")
        return [], warnings
    block_by_index = {block["block_index"]: block for block in blocks}
    normalized: list[dict[str, Any]] = []
    for index, row in enumerate(raw_rows, start=1):
        if not isinstance(row, dict):
            warnings.append(f"review_rows[{index}] is not a JSON object")
            continue
        try:
            block_index = int(row.get("block_index") or 0)
        except (TypeError, ValueError):
            warnings.append(f"review_rows[{index}] has invalid block_index")
            continue
        block = block_by_index.get(block_index)
        if block is None:
            warnings.append(
                f"review_rows[{index}] references unknown block {block_index}"
            )
            continue
        flag_type = str(row.get("flag_type") or "advisory")
        if flag_type not in _BLOCK_REVIEW_FLAG_TYPES:
            warnings.append(
                f"review_rows[{index}] has invalid flag_type {flag_type!r}"
            )
            continue
        claim_handles = [
            str(handle)
            for handle in (row.get("claim_handles") or [])
            if ledger.claim(str(handle)) is not None
            and str(handle)
            in set(
                block.get(
                    "planned_claim_handles", block["claim_handles"]
                )
            )
        ]
        evidence_handles = [
            str(handle)
            for handle in (row.get("evidence_handles") or [])
            if ledger.evidence_entry(str(handle)) is not None
            and str(handle)
            in _block_evidence_scope(block, plan, ledger)
        ]
        if row.get("claim_handles") or row.get("evidence_handles"):
            unknown_claims = [
                str(handle)
                for handle in (row.get("claim_handles") or [])
                if ledger.claim(str(handle)) is None
                or str(handle)
                not in set(
                    block.get(
                        "planned_claim_handles", block["claim_handles"]
                    )
                )
            ]
            unknown_evidence = [
                str(handle)
                for handle in (row.get("evidence_handles") or [])
                if ledger.evidence_entry(str(handle)) is None
                or str(handle)
                not in _block_evidence_scope(block, plan, ledger)
            ]
            if unknown_claims or unknown_evidence:
                warnings.append(
                    f"review_rows[{index}] references handles outside the "
                    f"block: claims={unknown_claims}, "
                    f"evidence={unknown_evidence}"
                )
                continue
        block_gap_handles = {
            handle
            for handle in block.get(
                "planned_claim_handles", block["claim_handles"]
            )
            if _claim_is_evidence_gap_only(ledger.claim(str(handle)))
        }
        row_gap_handles = set(claim_handles) & block_gap_handles
        blocking = bool(row.get("blocking")) and (
            flag_type == "material_contradiction"
            or (
                flag_type == "evidence_permission_violation"
                and bool(row_gap_handles)
            )
        )
        normalized.append(
            {
                "block_index": block_index,
                "sentence": str(row.get("sentence") or ""),
                "flag_type": flag_type,
                "blocking": blocking,
                "issue": str(row.get("issue") or ""),
                "suggested_hedge": str(row.get("suggested_hedge") or ""),
                "claim_handles": claim_handles,
                "evidence_handles": evidence_handles,
            }
        )
    return normalized, warnings


def _reviser_payload(
    *,
    block: dict[str, Any],
    blocking_rows: list[dict[str, Any]],
    ledger: HandleLedger,
    plan: dict[str, Any],
    compact_context: dict[str, Any],
) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "section_identity": {
            "section_id": plan.get("section_id") or "",
            "title": plan.get("title") or "",
            "chapter_thesis": plan.get("chapter_thesis") or "",
            "reader_takeaway": plan.get("reader_takeaway") or "",
        },
        "guardrails": compact_context.get("guardrails") or {},
        "block": {
            "block_index": block["block_index"],
            "title": block["title"],
            "goal": block["goal"],
            "current_paragraph_prose": block["prose"],
        },
        "blocking_review_rows": blocking_rows,
        "claims": [
            ledger.claim(handle).to_compact()
            for handle in block.get(
                "planned_claim_handles", block["claim_handles"]
            )
            if ledger.claim(handle) is not None
        ],
        "evidence": [
            ledger.evidence_entry(handle).to_compact(full=True)
            for handle in sorted(
                _block_evidence_scope(block, plan, ledger)
            )
            if ledger.evidence_entry(handle) is not None
        ],
        "instruction": (
            "Revise only what is needed to resolve the blocking rows. Preserve "
            "the chapter viewpoint, coherent paragraph, and valid evidence "
            "handles."
        ),
    }


def _run_block_review_and_revision(
    *,
    blocks: list[dict[str, Any]],
    plan: dict[str, Any],
    ledger: HandleLedger,
    compact_context: dict[str, Any],
    reviewer_tier: str,
    reviser_tier: str,
    qwen_caller: QwenCaller,
    usage_records: list[dict[str, Any]],
) -> tuple[dict[str, Any], list[str]]:
    diagnostics: list[str] = []
    review_data: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "section_id": plan.get("section_id") or "",
        "attempted": False,
        "available": False,
        "fallback_reason": "",
        "comments": [],
        "blocking_count": 0,
        "advisory_count": 0,
        "per_block_revision_outcomes": {},
    }
    if not blocks:
        return review_data, diagnostics
    review_data["attempted"] = True
    try:
        output = _call_model(
            agent_name="ChapterAssetBlockScientificReviewerAgent",
            prompt_path=PROMPT_BLOCK_REVIEWER,
            payload=_block_review_payload(
                blocks=blocks,
                plan=plan,
                ledger=ledger,
                compact_context=compact_context,
            ),
            model_tier=reviewer_tier,
            qwen_caller=qwen_caller,
            usage_records=usage_records,
            temperature=0.1,
            max_tokens=5000,
        )
    except ChapterAssetModelError as exc:
        review_data["fallback_reason"] = str(exc)
        diagnostics.append(f"block_scientific_review_unavailable: {exc}")
        return review_data, diagnostics
    raw_review_rows = output.get("review_rows")
    if "review_rows" not in output or not isinstance(raw_review_rows, list):
        review_data["fallback_reason"] = "malformed_output"
        diagnostics.append(
            "block_scientific_review returned missing or non-list review_rows"
        )
        return review_data, diagnostics
    comments, warnings = _normalize_block_review_rows(
        output,
        blocks=blocks,
        ledger=ledger,
        plan=plan,
    )
    diagnostics.extend(warnings)
    review_data["available"] = True
    review_data["comments"] = comments
    review_data["blocking_count"] = sum(
        1 for row in comments if row["blocking"]
    )
    review_data["advisory_count"] = sum(
        1 for row in comments if not row["blocking"]
    )

    blocking_by_block: dict[int, list[dict[str, Any]]] = {}
    for row in comments:
        if row["blocking"]:
            blocking_by_block.setdefault(row["block_index"], []).append(row)
    for block in blocks:
        block_index = block["block_index"]
        blocking_rows = blocking_by_block.get(block_index, [])
        if not blocking_rows:
            review_data["per_block_revision_outcomes"][str(block_index)] = {
                "attempted": False,
                "applied": False,
                "reason": "no_blocking_rows",
            }
            continue
        allowed_handles = _block_evidence_scope(block, plan, ledger)
        requires_evidence = bool(allowed_handles) and any(
            _claim_requires_core_evidence(ledger.claim(handle))
            for handle in block.get(
                "planned_claim_handles", block["claim_handles"]
            )
        )
        payload = _reviser_payload(
            block=block,
            blocking_rows=blocking_rows,
            ledger=ledger,
            plan=plan,
            compact_context=compact_context,
        )
        try:
            revised = _call_model(
                agent_name="ChapterAssetBlockReviewReviserAgent",
                prompt_path=PROMPT_BLOCK_REVISER,
                payload=payload,
                model_tier=reviser_tier,
                qwen_caller=qwen_caller,
                usage_records=usage_records,
                temperature=0.1,
                max_tokens=3000,
            )
            prose, used_handles = _validate_block_output(
                revised,
                allowed_handles=allowed_handles,
                ledger=ledger,
                requires_evidence=requires_evidence,
            )
        except (ChapterAssetModelError, ChapterAssetIntegrityError) as exc:
            review_data["per_block_revision_outcomes"][str(block_index)] = {
                "attempted": True,
                "applied": False,
                "reason": (
                    "reviser_unavailable"
                    if isinstance(exc, ChapterAssetModelError)
                    else "reviser_invalid_output"
                ),
                "detail": str(exc),
            }
            diagnostics.append(
                f"block_review_revision_failed for block {block_index}: {exc}"
            )
            continue
        block["prose"] = prose
        block["evidence_handles"] = list(
            dict.fromkeys(
                list(block.get("evidence_handles") or [])
                + list(used_handles)
            )
        )
        review_data["per_block_revision_outcomes"][str(block_index)] = {
            "attempted": True,
            "applied": True,
            "blocking_row_count": len(blocking_rows),
        }
    return review_data, diagnostics


def _sanitize_marker(value: str) -> str:
    return re.sub(r"[^\w.:/+-]+", "_", str(value or "")).strip("_")


def _normalize_doi(value: Any) -> str:
    text = str(value or "").strip().lower()
    text = re.sub(r"^https?://(dx\.)?doi\.org/", "", text)
    text = re.sub(r"^doi:\s*", "", text)
    return re.sub(r"\s+", "", text)


def _normalize_title_key(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(value or "").lower())


def _stable_paper_identity(raw: dict[str, Any]) -> tuple[str, str]:
    """Return (stable identity, bibliography marker) for a metadata source."""
    doi = ""
    for key in ("doi", "url_or_doi"):
        candidate = _normalize_doi(raw.get(key))
        if candidate.startswith("10.") or "doi.org" in str(
            raw.get(key) or ""
        ).lower():
            doi = candidate
            break
    if doi:
        marker = _sanitize_marker(f"doi:{doi}")
        if marker:
            return f"doi:{doi}", marker
    for key in ("semantic_scholar_paper_id", "s2_paper_id"):
        value = str(raw.get(key) or "").strip()
        if value:
            marker = _sanitize_marker(value)
            if marker:
                return value, marker
    for key in ("paper_id", "source_id"):
        value = str(raw.get(key) or "").strip()
        if value:
            marker = _sanitize_marker(value)
            if marker:
                return value, marker
    title_slug = _slugify(str(raw.get("title") or ""), 40)
    if title_slug:
        return f"title:{title_slug}", f"title:{title_slug}"
    return "", ""


def _score_explanatory_candidate(
    query: str,
    candidate: dict[str, Any],
) -> float:
    """Local deterministic relevance score; provider score is a bonus only."""
    query_tokens = [
        token
        for token in re.findall(r"[A-Za-z0-9]+", str(query or ""))
        if len(token) >= 3
    ]
    metadata = candidate.get("metadata") or {}
    haystack = " ".join(
        [
            str(metadata.get("title") or ""),
            str(metadata.get("abstract") or ""),
            str(metadata.get("venue") or ""),
        ]
    ).lower()
    text_terms = set(re.findall(r"[a-z0-9]+", haystack))
    total_weight = sum(_token_weight(token) for token in query_tokens)
    matched_weight = 0.0
    high_info_query_weight = 0.0
    high_info_matched = 0.0
    for token in query_tokens:
        weight = _token_weight(token)
        if weight >= 1.0 and token.lower() not in _COMMON_SCHOLARLY_TOKENS:
            high_info_query_weight += weight
        if token.lower() in text_terms:
            matched_weight += weight
            if (
                weight >= 1.0
                and token.lower() not in _COMMON_SCHOLARLY_TOKENS
            ):
                high_info_matched += weight
    if total_weight <= 0:
        lexical = 0.0
    else:
        high_info_ratio = (
            high_info_matched / max(1.0, high_info_query_weight)
            if high_info_query_weight > 0
            else 0.0
        )
        lexical = (matched_weight / total_weight) * min(
            1.0, high_info_ratio * 2.0
        )
    haystack_flat = re.sub(r"\s+", " ", haystack)
    phrase_bonus = min(
        0.3,
        0.1
        * sum(
            1
            for phrase in _query_phrases(
                [token.lower() for token in query_tokens]
            )
            if phrase in haystack_flat
        ),
    )
    try:
        provider_score = float(candidate.get("relevance_score") or 0.0)
    except (TypeError, ValueError):
        provider_score = 0.0
    provider_score = max(0.0, min(1.0, provider_score))
    combined = min(1.0, lexical + 0.25 * provider_score)
    candidate["selection_score"] = round(combined, 6)
    candidate["provider_score"] = round(provider_score, 6)
    candidate["lexical_score"] = round(lexical, 6)
    return combined


def _token_weight(token: str) -> float:
    lowered = token.lower()
    if (
        lowered in _COMMON_SCHOLARLY_TOKENS
        or lowered.upper() in _SLUG_STOPWORDS
    ):
        return 0.2
    if token.isupper() and 2 <= len(token) <= 6:
        return 2.0
    if any(char.isdigit() for char in token):
        return 2.0
    if len(token) >= 8:
        return 1.5
    return 1.0


def _query_phrases(tokens: list[str], max_n: int = 3) -> list[str]:
    phrases: list[str] = []
    for n in range(2, max_n + 1):
        for index in range(max(0, len(tokens) - n + 1)):
            phrases.append(" ".join(tokens[index : index + n]))
    return phrases


def _select_per_need(
    candidates: list[dict[str, Any]],
    *,
    query: str,
) -> list[dict[str, Any]]:
    """Rank one need's candidates and take at most two close, distinct picks."""
    eligible: list[dict[str, Any]] = []
    for candidate in candidates:
        score = _score_explanatory_candidate(query, candidate)
        if score >= 0.05:
            eligible.append(candidate)
    eligible.sort(
        key=lambda item: (
            item.get("selection_score", 0.0),
            item.get("stable_paper_id", ""),
        ),
        reverse=True,
    )
    if not eligible:
        return []
    selected = [eligible[0]]
    if len(eligible) >= 2:
        second = eligible[1]
        first_score = float(eligible[0].get("selection_score") or 0.0)
        second_score = float(second.get("selection_score") or 0.0)
        if (
            second["stable_paper_id"] != eligible[0]["stable_paper_id"]
            and second_score >= first_score * 0.85
        ):
            selected.append(second)
    return selected


def _score_distribution(scores: list[float]) -> dict[str, Any]:
    bins = {
        "0.0-0.2": 0,
        "0.2-0.4": 0,
        "0.4-0.6": 0,
        "0.6-0.8": 0,
        "0.8-1.0": 0,
    }
    for score in scores:
        if score < 0.2:
            bins["0.0-0.2"] += 1
        elif score < 0.4:
            bins["0.2-0.4"] += 1
        elif score < 0.6:
            bins["0.4-0.6"] += 1
        elif score < 0.8:
            bins["0.6-0.8"] += 1
        else:
            bins["0.8-1.0"] += 1
    return {
        "min": round(min(scores), 4) if scores else None,
        "max": round(max(scores), 4) if scores else None,
        "mean": round(sum(scores) / len(scores), 4) if scores else None,
        "bins": bins,
    }


def _apply_global_selection(
    records: list[dict[str, Any]],
    *,
    raw_unique_count: int,
    eligible_unique_count: int,
    soft_min: int = 10,
    soft_max: int = 20,
    route_counts: dict[str, int] | None = None,
    semantic_primary: bool = False,
    semantic_audit: dict[str, Any] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    if not records:
        audit = {
            "soft_target_range": [soft_min, soft_max],
            "raw_unique_count": raw_unique_count,
            "eligible_unique_count": eligible_unique_count,
            "selected_unique_count": 0,
            "selected_score_floor": None,
            "stop_reason": "no_eligible_candidates",
            "score_distribution": _score_distribution([]),
            "route_counts": route_counts or {},
            "semantic_rerank": semantic_audit
            or {
                "attempted": False,
                "available": False,
                "scored_count": 0,
                "rejected_clear_mismatch_count": 0,
                "score_distribution": None,
                "fallback_reason": "",
            },
        }
        return [], audit
    if semantic_primary:
        records.sort(
            key=lambda item: (
                -float(item.get("helpfulness_score") or 0.0),
                -float(item.get("selection_score") or 0.0),
            )
        )
        scores = [
            float(record.get("helpfulness_score") or 0.0) / 100.0
            for record in records
        ]
    else:
        records.sort(
            key=lambda item: float(item.get("selection_score") or 0.0),
            reverse=True,
        )
        scores = [
            float(record.get("selection_score") or 0.0)
            for record in records
        ]
    if len(records) > soft_max:
        threshold_score = scores[soft_max - 1]
        next_score = scores[soft_max]
        if next_score >= threshold_score * 0.9 or (
            threshold_score - next_score < 0.08
        ):
            kept = records
            stop_reason = "above_soft_max_high_quality_margins"
        else:
            kept = records[:soft_max]
            stop_reason = "soft_max_marginal_cutoff"
    else:
        kept = records
        stop_reason = (
            "within_soft_range"
            if len(kept) >= soft_min
            else "below_soft_min_but_not_padded"
        )
    if semantic_primary:
        kept_scores = [
            float(record.get("helpfulness_score") or 0.0) / 100.0
            for record in kept
        ]
    else:
        kept_scores = [
            float(record.get("selection_score") or 0.0)
            for record in kept
        ]
    audit = {
        "soft_target_range": [soft_min, soft_max],
        "raw_unique_count": raw_unique_count,
        "eligible_unique_count": eligible_unique_count,
        "selected_unique_count": len(kept),
        "selected_score_floor": (
            round(min(kept_scores), 4) if kept_scores else None
        ),
        "stop_reason": stop_reason,
        "score_distribution": _score_distribution(kept_scores),
        "route_counts": route_counts or {},
        "semantic_rerank": semantic_audit
        or {
            "attempted": False,
            "available": False,
            "scored_count": 0,
            "rejected_clear_mismatch_count": 0,
            "score_distribution": None,
            "fallback_reason": "",
        },
    }
    if semantic_primary and kept:
        audit["selected_helpfulness_floor"] = round(
            min(
                float(record.get("helpfulness_score") or 0.0)
                for record in kept
            ),
            2,
        )
    return kept, audit


def _normalize_explanatory_source(
    raw: dict[str, Any],
    *,
    origin: str,
) -> dict[str, Any] | None:
    stable_id, marker_id = _stable_paper_identity(raw)
    title = _compact_text(raw.get("title"), 240)
    if not stable_id or not title:
        return None
    authors = raw.get("authors") or raw.get("author") or []
    if isinstance(authors, str):
        authors = [authors]
    abstract = _compact_text(
        raw.get("abstract")
        or raw.get("abstract_text")
        or raw.get("abstract_or_snippet")
        or raw.get("summary")
        or raw.get("snippet"),
        1200,
    )
    try:
        relevance_score = float(raw.get("relevance_score") or raw.get("score") or 0.0)
    except (TypeError, ValueError):
        relevance_score = 0.0
    return {
        "stable_paper_id": stable_id,
        "marker_id": marker_id,
        "metadata": {
            "title": title,
            "authors": [str(author) for author in authors],
            "year": raw.get("year"),
            "doi": str(raw.get("doi") or ""),
            "paper_id": str(raw.get("paper_id") or raw.get("source_id") or ""),
            "abstract": abstract,
            "venue": str(
                raw.get("venue") or raw.get("journal_or_venue") or ""
            ),
            "url": str(
                raw.get("url")
                or raw.get("source_url")
                or raw.get("landing_page_url")
                or raw.get("pdf_url")
                or ""
            ),
        },
        "relevance_score": relevance_score,
        "relevance_reason": _compact_text(
            raw.get("reason") or raw.get("relevance_reason"), 300
        ),
        "origin": origin,
    }


def _search_explanatory_candidates(
    *,
    query: str,
    max_results: int,
    local_search_callback: SearchCallback | None,
    s2_search_callback: SearchCallback | None,
    diagnostics: list[str],
) -> tuple[list[dict[str, Any]], str, set[str], set[str]]:
    local_candidates: list[dict[str, Any]] = []
    raw_seen: set[str] = set()
    eligible_seen: set[str] = set()
    if local_search_callback is not None:
        try:
            raw_results = local_search_callback(query, max_results)
            for raw in raw_results or []:
                if isinstance(raw, dict):
                    normalized = _normalize_explanatory_source(
                        raw, origin="local_metadata"
                    )
                    if normalized is not None:
                        local_candidates.append(normalized)
                        raw_seen.add(normalized["stable_paper_id"])
        except Exception as exc:  # noqa: BLE001 - side path must fail open
            diagnostics.append(
                f"local_metadata_search_error: {type(exc).__name__}"
            )
    local_eligible = [
        candidate
        for candidate in local_candidates
        if _score_explanatory_candidate(query, candidate) >= 0.05
    ]
    eligible_seen.update(
        candidate["stable_paper_id"] for candidate in local_eligible
    )
    best_local_score = (
        max(
            float(candidate.get("selection_score") or 0.0)
            for candidate in local_eligible
        )
        if local_eligible
        else 0.0
    )
    if (
        local_eligible
        and best_local_score >= _LOCAL_SUFFICIENCY_THRESHOLD
    ):
        return local_eligible, "local_only", raw_seen, eligible_seen
    if s2_search_callback is not None:
        s2_candidates: list[dict[str, Any]] = []
        try:
            raw_results = s2_search_callback(query, max_results)
            for raw in raw_results or []:
                if isinstance(raw, dict):
                    normalized = _normalize_explanatory_source(
                        raw, origin="semantic_scholar"
                    )
                    if normalized is not None:
                        s2_candidates.append(normalized)
                        raw_seen.add(normalized["stable_paper_id"])
        except Exception as exc:  # noqa: BLE001 - side path must fail open
            diagnostics.append(
                f"semantic_scholar_search_error: {type(exc).__name__}"
            )
        s2_eligible = [
            candidate
            for candidate in s2_candidates
            if _score_explanatory_candidate(query, candidate) >= 0.05
        ]
        eligible_seen.update(
            candidate["stable_paper_id"] for candidate in s2_eligible
        )
        combined = _merge_candidate_lists(local_eligible, s2_eligible)
        combined.sort(
            key=lambda item: float(item.get("selection_score") or 0.0),
            reverse=True,
        )
        route = "local_plus_s2" if local_eligible else "s2_only"
        return combined, route, raw_seen, eligible_seen
    if local_eligible:
        return local_eligible, "local_only", raw_seen, eligible_seen
    return [], "none", raw_seen, eligible_seen


def _merge_candidate_lists(
    local_candidates: list[dict[str, Any]],
    s2_candidates: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    merged: dict[str, dict[str, Any]] = {}
    for candidate in local_candidates + s2_candidates:
        stable_id = candidate["stable_paper_id"]
        existing = merged.get(stable_id)
        if existing is None or float(
            candidate.get("selection_score") or 0.0
        ) > float(existing.get("selection_score") or 0.0):
            merged[stable_id] = candidate
    return list(merged.values())


def _explanatory_reranker_payload(
    records: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "candidate_table": [
            {
                "handle": record["handle"],
                "target_sentence": (
                    record.get("target_sentences") or [""]
                )[0],
                "benefit_types": record.get("benefit_types") or [],
                "query": (record.get("queries") or [""])[0],
                "title": (record.get("metadata") or {}).get("title", ""),
                "year": (record.get("metadata") or {}).get("year"),
                "abstract_or_snippet": _compact_text(
                    (record.get("metadata") or {}).get("abstract"), 900
                ),
            }
            for record in records
        ],
        "rubric": {
            "80-100": "direct definition, mechanism, or representative application",
            "55-79": "useful broader explanatory background",
            "30-54": "tangential or shared generic vocabulary",
            "0-29": "clear topic mismatch",
        },
        "instruction": (
            "Score semantic explanatory helpfulness only. This is not "
            "core-evidence verification; do not require verbatim support."
        ),
    }


def _run_semantic_reranker(
    *,
    records: list[dict[str, Any]],
    model_tier: str,
    qwen_caller: QwenCaller,
    usage_records: list[dict[str, Any]],
    diagnostics: list[str],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    empty_audit = {
        "attempted": False,
        "available": False,
        "scored_count": 0,
        "rejected_clear_mismatch_count": 0,
        "score_distribution": None,
        "fallback_reason": "",
    }
    if not records:
        return records, empty_audit
    audit = dict(empty_audit)
    audit["attempted"] = True
    record_by_handle = {record["handle"]: record for record in records}
    try:
        output = _call_model(
            agent_name="ChapterAssetExplanatorySemanticRerankerAgent",
            prompt_path=PROMPT_RERANKER,
            payload=_explanatory_reranker_payload(records),
            model_tier=model_tier,
            qwen_caller=qwen_caller,
            usage_records=usage_records,
            temperature=0.1,
            max_tokens=4000,
        )
    except ChapterAssetModelError as exc:
        audit["fallback_reason"] = str(exc)
        diagnostics.append(f"semantic_reranker_unavailable: {exc}")
        return records, audit
    rows = output.get("semantic_scores") or []
    if not isinstance(rows, list):
        audit["fallback_reason"] = "malformed_output"
        diagnostics.append(
            "semantic_reranker returned non-list semantic_scores"
        )
        return records, audit
    valid_scores: dict[str, int] = {}
    seen_handles: set[str] = set()
    incomplete = False
    for row in rows:
        if not isinstance(row, dict):
            incomplete = True
            continue
        handle = str(row.get("handle") or "")
        if not handle:
            incomplete = True
            continue
        record = record_by_handle.get(handle)
        if record is None:
            incomplete = True
            diagnostics.append(
                f"semantic_reranker returned unknown handle: {handle}"
            )
            continue
        if handle in seen_handles:
            incomplete = True
            diagnostics.append(
                f"semantic_reranker returned duplicate handle: {handle}"
            )
            continue
        seen_handles.add(handle)
        try:
            helpfulness = int(row.get("helpfulness_score"))
        except (TypeError, ValueError):
            incomplete = True
            diagnostics.append(
                f"semantic_reranker returned invalid score: {handle}"
            )
            continue
        helpfulness = max(0, min(100, helpfulness))
        valid_scores[handle] = helpfulness
    if incomplete or len(valid_scores) != len(records):
        audit["available"] = False
        audit["fallback_reason"] = "incomplete_semantic_rerank"
        audit["scored_count"] = 0
        audit["rejected_clear_mismatch_count"] = 0
        audit["score_distribution"] = None
        for record in records:
            record.pop("helpfulness_score", None)
            record.pop("helpfulness_reason", None)
        diagnostics.append(
            "semantic_reranker response was incomplete; deterministic "
            "selection applied"
        )
        return records, audit
    for record in records:
        handle = record["handle"]
        record["helpfulness_score"] = valid_scores[handle]
        record["helpfulness_reason"] = ""
        for row in rows:
            if isinstance(row, dict) and str(row.get("handle") or "") == handle:
                record["helpfulness_reason"] = str(
                    row.get("reason") or ""
                )[:300]
                break
    scores = list(valid_scores.values())
    audit["available"] = bool(scores)
    audit["scored_count"] = len(scores)
    audit["score_distribution"] = (
        _score_distribution([score / 100.0 for score in scores])
        if scores
        else None
    )
    kept = [
        record
        for record in records
        if "helpfulness_score" not in record
        or int(record["helpfulness_score"])
        > _SEMANTIC_MISMATCH_MAX
    ]
    audit["rejected_clear_mismatch_count"] = len(records) - len(kept)
    return kept, audit


def _explanatory_payload(
    *,
    blocks: list[dict[str, Any]],
    plan: dict[str, Any],
    title: str,
    application_max_targets: int = DEFAULT_APPLICATION_MAX_TARGETS,
    application_soft_min_targets: int = DEFAULT_APPLICATION_SOFT_MIN_TARGETS,
    mode: str = "initial",
    sentence_handles: list[str] | None = None,
) -> dict[str, Any]:
    sentence_table, _ = _build_sentence_table(blocks)
    if sentence_handles is not None:
        allowed = set(str(handle) for handle in sentence_handles)
        sentence_table = [
            row
            for row in sentence_table
            if str(row.get("sentence_handle") or "") in allowed
        ]
    max_targets = max(1, int(application_max_targets or 0))
    soft_min_targets = max(0, int(application_soft_min_targets or 0))
    if mode == "supplemental_representative_applications":
        instruction = (
            "Supplemental pass: propose ONLY additional "
            "representative_application rows from the supplied remaining "
            "sentence table. Do not repeat sentences already covered by the "
            "first pass. Keep target sentences and queries distinct; do not "
            "invent examples or pad when no suitable sentence exists."
        )
    else:
        instruction = (
            "Review every block. For method, device, system, or "
            "application blocks with a concrete use, actively propose "
            "distinct representative_application rows up to the requested "
            "maximum. When the chapter is evidence-rich and enough suitable "
            "sentences exist, aim toward the soft minimum/maximum rather "
            "than stopping after one or two. Keep target sentences and "
            "queries distinct; do not invent examples or pad when no "
            "suitable target exists. Definition and mechanism/background "
            "rows remain supported."
        )
    return {
        "schema_version": SCHEMA_VERSION,
        "section_id": plan.get("section_id") or "",
        "title": title,
        "representative_application_policy": {
            "max_targets": max_targets,
            "soft_min_targets": soft_min_targets,
            "mode": mode,
        },
        "chapter_outline": [
            {
                "block_index": block["block_index"],
                "title": block["title"],
                "goal": block["goal"],
            }
            for block in blocks
        ],
        "sentence_table": sentence_table,
        "instruction": instruction,
    }


def _build_sentence_table(
    blocks: list[dict[str, Any]],
    *,
    max_sentences_per_block: int = 12,
    max_total_sentences: int = 200,
) -> tuple[list[dict[str, Any]], dict[str, tuple[int, str]]]:
    """Locally split blocks into stable sentence handles for the planner."""
    table: list[dict[str, Any]] = []
    handle_map: dict[str, tuple[int, str]] = {}
    total = 0
    for block in blocks:
        block_index = block["block_index"]
        sentences = [
            sentence.strip()
            for sentence in re.split(r"(?<=[.!?])\s+", str(block["prose"] or ""))
            if sentence.strip()
        ][:max_sentences_per_block]
        for sentence_index, sentence in enumerate(sentences, start=1):
            if total >= max_total_sentences:
                break
            handle = f"B{block_index:02d}-S{sentence_index:02d}"
            table.append(
                {
                    "block_index": block_index,
                    "sentence_handle": handle,
                    "sentence_text": sentence,
                }
            )
            handle_map[handle] = (block_index, sentence)
            total += 1
    return table, handle_map


def _run_explanatory_citations(
    *,
    blocks: list[dict[str, Any]],
    ledger: HandleLedger,
    plan: dict[str, Any],
    title: str,
    enabled: bool,
    model_tier: str,
    qwen_caller: QwenCaller,
    usage_records: list[dict[str, Any]],
    local_search_callback: SearchCallback | None,
    s2_search_callback: SearchCallback | None,
    max_results: int,
    application_max_targets: int = DEFAULT_APPLICATION_MAX_TARGETS,
    application_soft_min_targets: int = DEFAULT_APPLICATION_SOFT_MIN_TARGETS,
) -> tuple[dict[str, Any], list[str], list[dict[str, Any]]]:
    diagnostics: list[str] = []
    empty = {
        "schema_version": SCHEMA_VERSION,
        "section_id": plan.get("section_id") or "",
        "records": [],
        "block_to_explanatory_handles": {},
        "sentence_to_handles": {},
        "diagnostics": diagnostics,
    }
    if not enabled:
        diagnostics.append("explanatory_citations_disabled")
        return empty, diagnostics, []
    if local_search_callback is None and s2_search_callback is None:
        diagnostics.append(
            "explanatory_search_unavailable: no local or S2 callback configured"
        )
        return empty, diagnostics, []
    try:
        output = _call_model(
            agent_name="ChapterAssetExplanatoryCitationPlannerAgent",
            prompt_path=PROMPT_EXPLANATORY,
            payload=_explanatory_payload(
                blocks=blocks,
                plan=plan,
                title=title,
                application_max_targets=application_max_targets,
                application_soft_min_targets=application_soft_min_targets,
                mode="initial",
            ),
            model_tier=model_tier,
            qwen_caller=qwen_caller,
            usage_records=usage_records,
            temperature=0.2,
            max_tokens=4000,
        )
    except ChapterAssetModelError as exc:
        diagnostics.append(f"explanatory_planner_unavailable: {exc}")
        return empty, diagnostics, []
    rows = output.get("explanatory_rows") or []
    if not isinstance(rows, list):
        diagnostics.append("explanatory_planner returned non-list rows")
        return empty, diagnostics, []

    records_by_stable: dict[str, dict[str, Any]] = {}
    records: list[dict[str, Any]] = []
    application_rows: list[dict[str, Any]] = []
    raw_unique: set[str] = set()
    eligible_unique: set[str] = set()
    route_counts: dict[str, int] = {}
    _sentence_table, sentence_by_handle = _build_sentence_table(blocks)
    handle_by_position = {
        (block_index, sentence): handle
        for handle, (block_index, sentence) in sentence_by_handle.items()
    }
    used_sentence_handles: set[str] = set()
    core_paper_ids = {
        str(entry.paper_id)
        for entry in ledger.evidence.values()
        if str(entry.paper_id)
    }
    core_title_keys = {
        _normalize_title_key(entry.source_title)
        for entry in ledger.evidence.values()
        if str(entry.source_title or "")
    }
    for row in rows:
        if not isinstance(row, dict):
            continue
        sentence_handle = str(row.get("sentence_handle") or "").strip()
        row_handle = ""
        if sentence_handle:
            resolved = sentence_by_handle.get(sentence_handle)
            if resolved is None:
                diagnostics.append(
                    "explanatory row has invalid sentence_handle: "
                    f"{sentence_handle}"
                )
                continue
            block_index, target_sentence = resolved
            row_handle = sentence_handle
        else:
            try:
                block_index = int(row.get("block_index") or 0)
            except (TypeError, ValueError):
                diagnostics.append("explanatory row has invalid block_index")
                continue
            target_sentence = _compact_text(row.get("target_sentence"), 400)
            row_handle = handle_by_position.get(
                (block_index, target_sentence), ""
            )
        if not (1 <= block_index <= len(blocks)):
            diagnostics.append(
                f"explanatory row block_index out of range: {block_index}"
            )
            continue
        if row_handle:
            used_sentence_handles.add(row_handle)
        query = _compact_text(row.get("query"), 300)
        benefit_type = str(row.get("benefit_type") or "mechanism_background")
        if not query:
            diagnostics.append(
                f"explanatory row for block {block_index} has no query"
            )
            continue
        if benefit_type == _REPRESENTATIVE_APPLICATION_BENEFIT:
            # Representative-application rows are delegated to the dedicated
            # application path; they are never searched or attached here.
            application_rows.append(
                {
                    "block_index": block_index,
                    "target_sentence": target_sentence,
                    "sentence_handle": row_handle,
                    "query": query,
                    "benefit_type": benefit_type,
                }
            )
            continue
        candidates, route, raw_ids, eligible_ids = (
            _search_explanatory_candidates(
                query=query,
                max_results=max_results,
                local_search_callback=local_search_callback,
                s2_search_callback=s2_search_callback,
                diagnostics=diagnostics,
            )
        )
        route_counts[route] = route_counts.get(route, 0) + 1
        raw_unique.update(raw_ids)
        eligible_unique.update(eligible_ids)
        selected = _select_per_need(candidates, query=query)
        if not selected:
            diagnostics.append(
                f"no_explanatory_candidates for block {block_index}"
            )
            continue
        for candidate in selected:
            stable_id = candidate["stable_paper_id"]
            title_key = _normalize_title_key(
                (candidate.get("metadata") or {}).get("title")
            )
            overlaps_core = (
                stable_id in core_paper_ids
                or candidate["marker_id"] in core_paper_ids
                or (title_key and title_key in core_title_keys)
            )
            if stable_id in records_by_stable:
                record = records_by_stable[stable_id]
            else:
                handle = (
                    f"X{len(records) + 1:02d}_"
                    f"{_slugify(candidate['metadata']['title'], 36)}"
                )
                record = {
                    "handle": handle,
                    "role": "explanatory_context",
                    "permission": "background_explanation_only",
                    "metadata": candidate["metadata"],
                    "retrieval_origin": candidate["origin"],
                    "relevance_score": candidate.get("relevance_score") or 0.0,
                    "selection_score": candidate.get("selection_score") or 0.0,
                    "relevance_reason": candidate["relevance_reason"],
                    "benefit_types": [benefit_type],
                    "target_blocks": [],
                    "target_sentences": [],
                    "queries": [],
                    "targets": [],
                    "overlaps_core_reference": overlaps_core,
                    "marker_id": candidate["marker_id"],
                }
                records_by_stable[stable_id] = record
                records.append(record)
            if block_index not in record["target_blocks"]:
                record["target_blocks"].append(block_index)
            if target_sentence and target_sentence not in record["target_sentences"]:
                record["target_sentences"].append(target_sentence)
            if benefit_type not in record["benefit_types"]:
                record["benefit_types"].append(benefit_type)
            if query not in record["queries"]:
                record["queries"].append(query)
            record["targets"].append(
                {
                    "block_index": block_index,
                    "sentence": target_sentence,
                    "benefit_type": benefit_type,
                    "query": query,
                }
            )

    soft_min = max(0, int(application_soft_min_targets or 0))
    distinct_keys: set[tuple[int, str]] = set()
    for row in application_rows:
        if not isinstance(row, dict):
            continue
        key = _application_row_key(row, blocks)
        if key is not None:
            distinct_keys.add(key)
    distinct_count = len(distinct_keys)
    expansion = {
        "call_count": 0,
        "reason": "not_configured",
        "initial_application_row_count": len(application_rows),
        "initial_distinct_target_count": distinct_count,
        "supplemental_application_row_count": 0,
        "supplemental_distinct_target_count": 0,
        "remaining_sentence_handle_count": 0,
        "shortfall_vs_soft_min": max(0, soft_min - distinct_count),
    }
    if soft_min > 0 and distinct_count < soft_min:
        expansion["shortfall_vs_soft_min"] = soft_min - distinct_count
        remaining_handles = [
            handle
            for handle in sentence_by_handle
            if handle not in used_sentence_handles
        ]
        expansion["remaining_sentence_handle_count"] = len(remaining_handles)
        if not remaining_handles:
            expansion["reason"] = "no_eligible_remaining_sentences"
            diagnostics.append(
                "application_target_expansion_skipped: "
                "no_eligible_remaining_sentences"
            )
        elif expansion["call_count"] >= _APPLICATION_EXPANSION_MAX_CALLS:
            expansion["reason"] = "expansion_cap_reached"
            diagnostics.append(
                "application_target_expansion_skipped: "
                "expansion_cap_reached"
            )
        else:
            expansion["call_count"] = 1
            try:
                supplemental_output = _call_model(
                    agent_name="ChapterAssetExplanatoryCitationPlannerAgent",
                    prompt_path=PROMPT_EXPLANATORY,
                    payload=_explanatory_payload(
                        blocks=blocks,
                        plan=plan,
                        title=title,
                        application_max_targets=application_max_targets,
                        application_soft_min_targets=application_soft_min_targets,
                        mode="supplemental_representative_applications",
                        sentence_handles=remaining_handles,
                    ),
                    model_tier=model_tier,
                    qwen_caller=qwen_caller,
                    usage_records=usage_records,
                    temperature=0.2,
                    max_tokens=4000,
                )
                supplemental_rows = (
                    supplemental_output.get("explanatory_rows") or []
                )
                expansion["reason"] = "shortfall_reduced"
                if not isinstance(supplemental_rows, list):
                    raise ChapterAssetModelError(
                        "supplemental planner returned non-list rows"
                    )
                for row in supplemental_rows:
                    if not isinstance(row, dict):
                        continue
                    handle = str(row.get("sentence_handle") or "").strip()
                    resolved = sentence_by_handle.get(handle)
                    if resolved is None:
                        diagnostics.append(
                            "supplemental application row has invalid "
                            f"sentence_handle: {handle}"
                        )
                        continue
                    block_index, target_sentence = resolved
                    query = _compact_text(row.get("query"), 300)
                    benefit_type = str(
                        row.get("benefit_type")
                        or _REPRESENTATIVE_APPLICATION_BENEFIT
                    )
                    if benefit_type != _REPRESENTATIVE_APPLICATION_BENEFIT:
                        continue
                    if not query:
                        diagnostics.append(
                            "supplemental application row has no query: "
                            f"{handle}"
                        )
                        continue
                    application_rows.append(
                        {
                            "block_index": block_index,
                            "target_sentence": target_sentence,
                            "sentence_handle": handle,
                            "query": query,
                            "benefit_type": benefit_type,
                            "supplemental": True,
                        }
                    )
                expansion["supplemental_application_row_count"] = (
                    len(application_rows)
                    - expansion["initial_application_row_count"]
                )
                distinct_after: set[tuple[int, str]] = set()
                for row in application_rows:
                    if not isinstance(row, dict):
                        continue
                    key = _application_row_key(row, blocks)
                    if key is not None:
                        distinct_after.add(key)
                expansion["supplemental_distinct_target_count"] = (
                    len(distinct_after) - distinct_count
                )
                expansion["shortfall_vs_soft_min"] = max(
                    0, soft_min - len(distinct_after)
                )
                diagnostics.append(
                    "application_target_expansion: one supplemental planner "
                    "call executed "
                    f"({expansion['supplemental_application_row_count']} "
                    "additional rows, "
                    f"{expansion['supplemental_distinct_target_count']} "
                    "additional distinct targets)"
                )
            except ChapterAssetModelError as exc:
                expansion["reason"] = "supplemental_unavailable"
                diagnostics.append(
                    f"application_target_expansion_unavailable: {exc}"
                )
    elif soft_min > 0:
        expansion["reason"] = "no_shortfall"

    records, semantic_audit = _run_semantic_reranker(
        records=records,
        model_tier=model_tier,
        qwen_caller=qwen_caller,
        usage_records=usage_records,
        diagnostics=diagnostics,
    )
    records, selection_audit = _apply_global_selection(
        records,
        raw_unique_count=len(raw_unique),
        eligible_unique_count=len(eligible_unique),
        route_counts=route_counts,
        semantic_primary=bool(semantic_audit.get("available")),
        semantic_audit=semantic_audit,
    )
    kept_handles = {record["handle"] for record in records}
    block_map: dict[int, list[str]] = {}
    sentence_map: dict[str, list[str]] = {}
    for record in records:
        for target in record["targets"]:
            block_map.setdefault(target["block_index"], [])
            if record["handle"] not in block_map[target["block_index"]]:
                block_map[target["block_index"]].append(record["handle"])
            if target["sentence"]:
                sentence_key = (
                    f"{target['block_index']}:{target['sentence']}"
                )
                sentence_map.setdefault(sentence_key, [])
                if record["handle"] not in sentence_map[sentence_key]:
                    sentence_map[sentence_key].append(record["handle"])

    for block in blocks:
        block["explanatory_handles"] = []
        block["explanatory_markers"] = []
        block["explanatory_sentence_markers"] = {}
    for record in records:
        if record["handle"] not in kept_handles:
            continue
        for target in record["targets"]:
            block = blocks[target["block_index"] - 1]
            handle = record["handle"]
            marker = f"[REF:{record['marker_id']}]"
            if handle not in block["explanatory_handles"]:
                block["explanatory_handles"].append(handle)
            sentence = target["sentence"]
            if sentence and sentence in block["prose"]:
                sentence_markers = block["explanatory_sentence_markers"].setdefault(
                    sentence, []
                )
                if marker not in sentence_markers:
                    sentence_markers.append(marker)
            else:
                if marker not in block["explanatory_markers"]:
                    block["explanatory_markers"].append(marker)
                if sentence:
                    diagnostics.append(
                        "sentence_not_found_block_level_fallback: "
                        f"block {target['block_index']}"
                    )

    return (
        {
            "schema_version": SCHEMA_VERSION,
            "section_id": plan.get("section_id") or "",
            "records": records,
            "block_to_explanatory_handles": block_map,
            "sentence_to_handles": sentence_map,
            "selection_audit": selection_audit,
            "application_target_expansion": expansion,
            "diagnostics": diagnostics,
        },
        diagnostics,
        application_rows,
    )


def _empty_application_metrics() -> dict[str, Any]:
    return {
        "targets_planned": 0,
        "target_source": "",
        "fallback_used": False,
        "raw_planner_application_row_count": 0,
        "accepted_distinct_application_target_count": 0,
        "application_target_shortfall_vs_soft_min": 0,
        "application_target_expansion_call_count": 0,
        "application_target_expansion_reason": "",
        "local_reuse_hits": 0,
        "s2_fallbacks": 0,
        "skipped_targets": 0,
        "local_candidates_inspected": 0,
        "s2_candidates_inspected": 0,
        "examples_attached": 0,
        "references_added_count": 0,
        "application_writer_call_count": 0,
        "per_target_cap_compliance": [],
        "stop_reasons": [],
    }


def _block_sentences(prose: Any) -> list[str]:
    return [
        sentence.strip()
        for sentence in re.split(r"(?<=[.!?])\s+", str(prose or ""))
        if sentence.strip()
    ]


def _application_eligible_block(block: dict[str, Any]) -> bool:
    """A scientifically suitable major method/idea block with real depth."""

    sentences = _block_sentences(block.get("prose"))
    if len(sentences) < _APPLICATION_MIN_SENTENCE_COUNT:
        return False
    block_type = str(block.get("block_type") or "").strip().lower()
    goal = str(block.get("goal") or "").strip().lower()
    title = str(block.get("title") or "").strip().lower()
    text = " ".join((goal, title))
    return block_type in _APPLICATION_BLOCK_TYPES or any(
        keyword in text for keyword in _APPLICATION_METHOD_KEYWORDS
    )


def _application_targets(
    blocks: list[dict[str, Any]],
    *,
    max_targets: int,
) -> list[dict[str, Any]]:
    """Deterministically pick at most ``max_targets`` method/idea blocks."""

    targets: list[dict[str, Any]] = []
    cap = max(1, int(max_targets or 0))
    for block in blocks:
        if len(targets) >= cap:
            break
        if not _application_eligible_block(block):
            continue
        sentences = _block_sentences(block.get("prose"))
        query = " ".join(
            part
            for part in (
                str(block.get("title") or "").strip(),
                str(block.get("goal") or "").strip(),
                "representative application example",
            )
            if part
        )
        targets.append(
            {
                "block_index": int(block.get("block_index") or 0),
                "anchor_sentence": sentences[-1],
                "query": _compact_text(query, 300),
                "source": "deterministic_fallback",
            }
        )
    return targets


def _application_row_key(
    row: dict[str, Any],
    blocks: list[dict[str, Any]],
) -> tuple[int, str] | None:
    """Resolve the distinct (block_index, sentence) key for one planner row."""

    try:
        block_index = int(row.get("block_index") or 0)
    except (TypeError, ValueError):
        return None
    if not (1 <= block_index <= len(blocks)):
        return None
    sentence = str(row.get("target_sentence") or "").strip()
    if not sentence:
        sentences = _block_sentences(blocks[block_index - 1].get("prose"))
        sentence = sentences[-1] if sentences else ""
    return block_index, sentence


def _planned_application_targets(
    rows: list[dict[str, Any]],
    blocks: list[dict[str, Any]],
    *,
    max_targets: int,
    soft_min_targets: int = DEFAULT_APPLICATION_SOFT_MIN_TARGETS,
    diagnostics: list[str],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Convert planner rows into distinct app targets with an audit report.

    The report carries the raw planner application-row count, the accepted
    distinct count after deduplication and capping, and the shortfall against
    the soft minimum. The soft minimum is advisory only and never blocks.
    """

    targets: list[dict[str, Any]] = []
    seen: set[tuple[int, str]] = set()
    raw_count = 0
    invalid_count = 0
    cap = max(1, int(max_targets or 0))
    for row in rows:
        if not isinstance(row, dict):
            invalid_count += 1
            continue
        raw_count += 1
        key = _application_row_key(row, blocks)
        if key is None:
            invalid_count += 1
            diagnostics.append(
                "application planned row has invalid block_index"
            )
            continue
        block_index, sentence = key
        if key in seen:
            continue
        seen.add(key)
        if len(targets) >= cap:
            continue
        targets.append(
            {
                "block_index": block_index,
                "anchor_sentence": sentence,
                "query": _compact_text(row.get("query"), 300),
                "source": "explanatory_planner",
            }
        )
    report = {
        "raw_planner_application_row_count": raw_count,
        "accepted_distinct_target_count": len(targets),
        "shortfall_vs_soft_min": max(
            0, int(soft_min_targets or 0) - len(targets)
        ),
        "capped_at_max": raw_count > cap,
        "invalid_row_count": invalid_count,
    }
    return targets, report


def _application_candidate_accepted(
    candidate: dict[str, Any],
) -> bool:
    # Mildly relevant material is acceptable, but a provider-only hit with
    # zero lexical grounding is closer to a mismatch and stays unusable.
    score = float(candidate.get("selection_score") or 0.0)
    lexical = float(candidate.get("lexical_score") or 0.0)
    return score >= _APPLICATION_ACCEPTANCE_THRESHOLD and lexical > 0.0


def _application_candidate_mismatch(
    candidate: dict[str, Any],
) -> bool:
    score = float(candidate.get("selection_score") or 0.0)
    return score < _APPLICATION_MISMATCH_MAX


def _search_application_candidates(
    *,
    query: str,
    local_search_callback: SearchCallback | None,
    s2_search_callback: SearchCallback | None,
    local_max_results: int,
    per_target_cap: int,
    diagnostics: list[str],
) -> tuple[list[dict[str, Any]], str, int, int, int, bool]:
    """Local-first search with per-backend caps (never more than four).

    Returns (selected, route, local_inspected, s2_inspected, s2_requested,
    trimmed). S2 is only contacted when no usable local example exists and
    its request is capped at the per-target allowance. Local and S2 inspected
    counts are tracked separately; no combined-cap claim is made.
    """

    local_budget = max(1, int(local_max_results))
    s2_budget = max(1, int(per_target_cap))
    selection_cap = max(1, int(per_target_cap))
    local_inspected = 0
    s2_inspected = 0
    s2_requested = 0
    weak_local: list[dict[str, Any]] = []
    if local_search_callback is not None:
        try:
            raw_results = local_search_callback(query, local_budget)
        except Exception as exc:  # noqa: BLE001 - side path must fail open
            diagnostics.append(
                f"application_local_metadata_search_error: {type(exc).__name__}"
            )
            raw_results = []
        raw_results = list(raw_results or [])
        local_inspected += len(raw_results)
        for raw in raw_results:
            if not isinstance(raw, dict):
                continue
            normalized = _normalize_explanatory_source(
                raw, origin="local_metadata"
            )
            if normalized is None:
                continue
            _score_explanatory_candidate(query, normalized)
            if not _application_candidate_mismatch(normalized):
                weak_local.append(normalized)
    local_usable = [
        candidate
        for candidate in weak_local
        if _application_candidate_accepted(candidate)
    ]
    if local_usable:
        local_usable.sort(
            key=lambda item: (
                float(item.get("selection_score") or 0.0),
                item.get("stable_paper_id", ""),
            ),
            reverse=True,
        )
        selected = local_usable[:selection_cap]
        return (
            selected,
            "local_only",
            local_inspected,
            0,
            0,
            len(local_usable) > selection_cap,
        )

    merged: dict[str, dict[str, Any]] = {
        candidate["stable_paper_id"]: candidate for candidate in weak_local
    }
    route = "local_weak_only"
    s2_contributed = False
    if s2_search_callback is not None:
        try:
            s2_requested = s2_budget
            raw_results = s2_search_callback(query, s2_budget)
        except Exception as exc:  # noqa: BLE001 - side path must fail open
            diagnostics.append(
                f"application_semantic_scholar_search_error: "
                f"{type(exc).__name__}"
            )
            raw_results = []
        raw_results = list(raw_results or [])
        s2_inspected += len(raw_results)
        for raw in raw_results:
            if not isinstance(raw, dict):
                continue
            normalized = _normalize_explanatory_source(
                raw, origin="semantic_scholar"
            )
            if normalized is None:
                continue
            _score_explanatory_candidate(query, normalized)
            if not _application_candidate_accepted(normalized):
                continue
            existing = merged.get(normalized["stable_paper_id"])
            if existing is None or float(
                normalized.get("selection_score") or 0.0
            ) > float(existing.get("selection_score") or 0.0):
                merged[normalized["stable_paper_id"]] = normalized
                s2_contributed = True
        if s2_contributed:
            route = "local_plus_s2" if weak_local else "s2_only"
    if not merged:
        return (
            [],
            "none" if route == "local_weak_only" else route,
            local_inspected,
            s2_inspected,
            s2_requested,
            False,
        )
    candidates = sorted(
        merged.values(),
        key=lambda item: (
            float(item.get("selection_score") or 0.0),
            item.get("stable_paper_id", ""),
        ),
        reverse=True,
    )
    trimmed = len(candidates) > selection_cap
    return (
        candidates[:selection_cap],
        route,
        local_inspected,
        s2_inspected,
        s2_requested,
        trimmed,
    )


def _application_writer_payload(
    *,
    blocks: list[dict[str, Any]],
    targets: list[dict[str, Any]],
    section_id: str,
    batch_index: int = 1,
    batch_count: int = 1,
) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    for target in targets:
        block = blocks[target["block_index"] - 1]
        candidate = target["candidate"]
        metadata = candidate.get("metadata") or {}
        rows.append(
            {
                "target_handle": target["handle"],
                "block_title": str(block.get("title") or ""),
                "block_goal": str(block.get("goal") or ""),
                "target_sentence": target["anchor_sentence"],
                "candidate_title": str(metadata.get("title") or ""),
                "candidate_year": str(metadata.get("year") or ""),
                "candidate_abstract_or_snippet": _compact_text(
                    metadata.get("abstract")
                    or metadata.get("snippet")
                    or "",
                    900,
                ),
            }
        )
    return {
        "schema_version": SCHEMA_VERSION,
        "section_id": section_id,
        "batch": {
            "index": batch_index,
            "size": len(rows),
            "total_batches": batch_count,
        },
        "targets": rows,
    }


def _application_prose_numeric_tokens(prose: str) -> list[str]:
    return [
        token
        for token in re.findall(
            r"\b[A-Za-z0-9.%×µ\u00b5\-]*\d[A-Za-z0-9.%×µ\u00b5\-]*\b",
            prose,
        )
        if token.strip()
    ]


def _validate_application_prose(
    prose: Any,
    *,
    target: dict[str, Any],
    candidate: dict[str, Any],
) -> tuple[bool, str]:
    """Reject invented numbers, REF markers, and raw identifiers."""

    text = str(prose or "").strip()
    if not text:
        return False, "empty_prose"
    if len(text) > _APPLICATION_WRITER_MAX_CHARS:
        return False, "prose_too_long"
    if len(_block_sentences(text)) > _APPLICATION_WRITER_MAX_SENTENCES:
        return False, "too_many_sentences"
    if "[REF:" in text:
        return False, "raw_ref_marker"
    metadata = candidate.get("metadata") or {}
    forbidden = {
        str(target.get("handle") or ""),
        str(candidate.get("stable_paper_id") or ""),
        str(candidate.get("marker_id") or ""),
        str(metadata.get("paper_id") or ""),
    }
    lowered = text.casefold()
    for token in forbidden:
        if token and token.casefold() in lowered:
            return False, "raw_identifier_leak"
    source = " ".join(
        [
            str(metadata.get("title") or ""),
            str(metadata.get("year") or ""),
            str(metadata.get("abstract") or ""),
            str(metadata.get("snippet") or ""),
        ]
    ).casefold()
    for token in _application_prose_numeric_tokens(text):
        normalized = token.rstrip(".").casefold()
        if normalized and normalized not in source:
            return False, "invented_number"
    return True, ""


def _merge_application_record(
    explanatory_ledger: dict[str, Any],
    *,
    candidate: dict[str, Any],
    target: dict[str, Any],
    handle: str,
    sentence: str,
    attached: bool,
    local_inspected: int,
    s2_inspected: int,
    cap_compliant: bool,
) -> str:
    """Merge one application into the canonical top-level explanatory records."""

    records = explanatory_ledger.setdefault("records", [])
    stable_id = candidate["stable_paper_id"]
    metadata = candidate["metadata"]
    title_key = _normalize_title_key(metadata.get("title"))
    existing = next(
        (
            record
            for record in records
            if isinstance(record, dict)
            and (
                str(record.get("marker_id") or "")
                == candidate["marker_id"]
                or str(
                    (record.get("metadata") or {}).get("paper_id") or ""
                )
                == str(metadata.get("paper_id") or "")
                or (
                    title_key
                    and _normalize_title_key(
                        (record.get("metadata") or {}).get("title")
                    )
                    == title_key
                )
            )
        ),
        None,
    )
    block_index = target["block_index"]
    query = target["query"]
    if existing is None:
        record_handle = (
            f"X{len(records) + 1:02d}_"
            f"{_slugify(metadata['title'], 36)}"
        )
        existing = {
            "handle": record_handle,
            "role": "explanatory_context",
            "permission": "background_explanation_only",
            "metadata": metadata,
            "retrieval_origin": candidate["origin"],
            "relevance_score": candidate.get("relevance_score") or 0.0,
            "selection_score": candidate.get("selection_score") or 0.0,
            "relevance_reason": candidate.get("relevance_reason") or "",
            "benefit_types": [],
            "target_blocks": [],
            "target_sentences": [],
            "queries": [],
            "targets": [],
            "overlaps_core_reference": bool(
                candidate.get("overlaps_core_reference")
            ),
            "marker_id": candidate["marker_id"],
            "applications": [],
        }
        records.append(existing)
    if _REPRESENTATIVE_APPLICATION_BENEFIT not in existing.setdefault(
        "benefit_types", []
    ):
        existing["benefit_types"].append(
            _REPRESENTATIVE_APPLICATION_BENEFIT
        )
    if block_index not in existing.setdefault("target_blocks", []):
        existing["target_blocks"].append(block_index)
    if sentence and sentence not in existing.setdefault(
        "target_sentences", []
    ):
        existing["target_sentences"].append(sentence)
    if query not in existing.setdefault("queries", []):
        existing["queries"].append(query)
    existing.setdefault("targets", []).append(
        {
            "block_index": block_index,
            "sentence": sentence,
            "benefit_type": _REPRESENTATIVE_APPLICATION_BENEFIT,
            "query": query,
        }
    )
    existing.setdefault("applications", []).append(
        {
            "handle": handle,
            "benefit_type": _REPRESENTATIVE_APPLICATION_BENEFIT,
            "target": {
                "block_index": block_index,
                "anchor_sentence": sentence,
                "query": query,
            },
            "application_sentence": target.get("application_sentence") or "",
            "prose_source": "chapter_asset_application_writer",
            "retrieval_origin": candidate["origin"],
            "attached": attached,
            "local_candidates_inspected": local_inspected,
            "s2_candidates_inspected": s2_inspected,
            "cap_compliant": cap_compliant,
            "marker_id": candidate["marker_id"],
        }
    )
    record_handle = str(existing.get("handle") or "")
    block_map = explanatory_ledger.setdefault(
        "block_to_explanatory_handles", {}
    )
    block_key = str(block_index)
    block_map.setdefault(block_key, [])
    if record_handle not in block_map[block_key]:
        block_map[block_key].append(record_handle)
    if sentence:
        sentence_map = explanatory_ledger.setdefault(
            "sentence_to_handles", {}
        )
        sentence_key = f"{block_index}:{sentence}"
        sentence_map.setdefault(sentence_key, [])
        if record_handle not in sentence_map[sentence_key]:
            sentence_map[sentence_key].append(record_handle)
    return record_handle


def _attach_application_sentence(
    block: dict[str, Any],
    *,
    anchor_sentence: str,
    sentence: str,
    marker: str,
    handle: str,
) -> bool:
    prose = str(block.get("prose") or "").strip()
    if not prose or not sentence:
        return False
    sentence_markers = block.setdefault("explanatory_sentence_markers", {})
    markers = sentence_markers.setdefault(sentence, [])
    if marker not in markers:
        markers.append(marker)
    handles = block.setdefault("explanatory_handles", [])
    if handle not in handles:
        handles.append(handle)
    needle = str(anchor_sentence or "").strip()
    if needle and needle in prose:
        index = prose.index(needle) + len(needle)
        block["prose"] = f"{prose[:index]} {sentence}{prose[index:]}"
    else:
        block["prose"] = f"{prose} {sentence}"
    return True


def _run_application_writer(
    *,
    payload: dict[str, Any],
    writer_tier: str,
    qwen_caller: QwenCaller,
    usage_records: list[dict[str, Any]],
    diagnostics: list[str],
) -> tuple[list[dict[str, Any]], bool]:
    """One bounded Qwen call for a batch of at most three targets."""

    try:
        output = _call_model(
            agent_name="ChapterAssetRepresentativeApplicationWriterAgent",
            prompt_path=PROMPT_APPLICATION_WRITER,
            payload=payload,
            model_tier=writer_tier,
            qwen_caller=qwen_caller,
            usage_records=usage_records,
            temperature=0.2,
            max_tokens=3000,
        )
    except ChapterAssetModelError as exc:
        diagnostics.append(f"application_writer_unavailable: {exc}")
        return [], True
    rows = output.get("application_rows")
    if not isinstance(rows, list):
        diagnostics.append(
            "application_writer returned non-list application_rows"
        )
        return [], True
    return [row for row in rows if isinstance(row, dict)], True


def _run_representative_applications(
    *,
    blocks: list[dict[str, Any]],
    ledger: HandleLedger,
    plan: dict[str, Any],
    explanatory_ledger: dict[str, Any],
    planned_rows: list[dict[str, Any]],
    enabled: bool,
    local_search_callback: SearchCallback | None,
    s2_search_callback: SearchCallback | None,
    max_targets: int,
    soft_min_targets: int = DEFAULT_APPLICATION_SOFT_MIN_TARGETS,
    per_target_cap: int,
    local_max_results: int,
    writer_tier: str,
    qwen_caller: QwenCaller,
    usage_records: list[dict[str, Any]],
) -> tuple[dict[str, Any], list[str]]:
    """Add optional concrete representative-application prose per chapter.

    Planned representative-application rows from the explanatory citation
    planner are delegated here (a deterministic suitable-block fallback is
    used only when the planner produced none). One batched cheap-Qwen writer
    Selected targets are split into deterministic batches of at most three;
    each batch is one bounded Qwen writer call, and a failed or malformed
    batch fails open without discarding the successes of other batches. Every
    target uses the configured bounded local and S2 result budgets, and
    missing or malformed examples always fail open with a diagnostic.
    """

    diagnostics: list[str] = []
    metrics = _empty_application_metrics()
    empty = {
        "schema_version": SCHEMA_VERSION,
        "section_id": plan.get("section_id") or "",
        "records": [],
        "metrics": metrics,
        "diagnostics": diagnostics,
    }
    if not enabled:
        diagnostics.append("representative_applications_disabled")
        return empty, diagnostics
    if local_search_callback is None and s2_search_callback is None:
        diagnostics.append(
            "representative_application_search_unavailable: "
            "no local or S2 callback configured"
        )
        return empty, diagnostics

    planned, planned_report = _planned_application_targets(
        planned_rows,
        blocks,
        max_targets=max_targets,
        soft_min_targets=soft_min_targets,
        diagnostics=diagnostics,
    )
    metrics["raw_planner_application_row_count"] = planned_report[
        "raw_planner_application_row_count"
    ]
    metrics["accepted_distinct_application_target_count"] = planned_report[
        "accepted_distinct_target_count"
    ]
    metrics["application_target_shortfall_vs_soft_min"] = planned_report[
        "shortfall_vs_soft_min"
    ]
    expansion = explanatory_ledger.get("application_target_expansion") or {}
    if isinstance(expansion, dict):
        metrics["application_target_expansion_call_count"] = int(
            expansion.get("call_count") or 0
        )
        metrics["application_target_expansion_reason"] = str(
            expansion.get("reason") or ""
        )
    if planned:
        targets = planned
        metrics["target_source"] = "explanatory_planner"
        metrics["fallback_used"] = False
    else:
        targets = _application_targets(blocks, max_targets=max_targets)
        metrics["target_source"] = "deterministic_fallback"
        metrics["fallback_used"] = True
        if targets:
            diagnostics.append(
                "representative_application_fallback: planner produced no "
                "application targets; deterministic suitable-block fallback "
                "used"
            )
    metrics["targets_planned"] = len(targets)
    records: list[dict[str, Any]] = []
    core_paper_ids = {
        str(entry.paper_id)
        for entry in ledger.evidence.values()
        if str(entry.paper_id)
    }
    core_title_keys = {
        _normalize_title_key(entry.source_title)
        for entry in ledger.evidence.values()
        if str(entry.source_title or "")
    }
    writer_batch: list[dict[str, Any]] = []
    for index, target in enumerate(targets, start=1):
        block_index = target["block_index"]
        if not (1 <= block_index <= len(blocks)):
            metrics["skipped_targets"] += 1
            metrics["stop_reasons"].append(
                f"block {block_index}: invalid_block_index"
            )
            continue
        (
            candidates,
            route,
            local_inspected,
            s2_inspected,
            s2_requested,
            trimmed,
        ) = _search_application_candidates(
            query=target["query"],
            local_search_callback=local_search_callback,
            s2_search_callback=s2_search_callback,
            local_max_results=local_max_results,
            per_target_cap=per_target_cap,
            diagnostics=diagnostics,
        )
        metrics["local_candidates_inspected"] += local_inspected
        metrics["s2_candidates_inspected"] += s2_inspected
        metrics["per_target_cap_compliance"].append(
            {
                "target_block": block_index,
                "local_candidates_inspected": local_inspected,
                "local_requested": local_max_results,
                "s2_candidates_inspected": s2_inspected,
                "s2_requested": s2_requested,
                "s2_cap_compliant": s2_requested <= int(per_target_cap),
                "candidates_selected": len(candidates),
                "cap_trimmed": trimmed,
                "route": route,
            }
        )
        if route in {"s2_only", "local_plus_s2"}:
            metrics["s2_fallbacks"] += 1
        if not candidates:
            metrics["skipped_targets"] += 1
            reason = "no_usable_application_example"
            metrics["stop_reasons"].append(
                f"block {block_index}: {reason} (route={route})"
            )
            diagnostics.append(
                "representative_application_skipped: "
                f"block {block_index} ({reason}, route={route})"
            )
            continue
        if route == "local_only":
            metrics["local_reuse_hits"] += 1
        best = candidates[0]
        best["overlaps_core_reference"] = bool(
            best["stable_paper_id"] in core_paper_ids
            or best["marker_id"] in core_paper_ids
            or (
                _normalize_title_key(
                    (best.get("metadata") or {}).get("title")
                )
                and _normalize_title_key(
                    (best.get("metadata") or {}).get("title")
                )
                in core_title_keys
            )
        )
        target["handle"] = f"A{index:02d}"
        target["candidate"] = best
        target["local_candidates_inspected"] = local_inspected
        target["s2_candidates_inspected"] = s2_inspected
        target["candidates_selected"] = [
            item["stable_paper_id"] for item in candidates
        ]
        writer_batch.append(target)

    if not writer_batch:
        # No writer call when nothing was selected (cost bound).
        return (
            {
                "schema_version": SCHEMA_VERSION,
                "section_id": plan.get("section_id") or "",
                "records": records,
                "metrics": metrics,
                "diagnostics": diagnostics,
            },
            diagnostics,
        )

    chunks = [
        writer_batch[index : index + _APPLICATION_WRITER_BATCH_SIZE]
        for index in range(
            0, len(writer_batch), _APPLICATION_WRITER_BATCH_SIZE
        )
    ]
    writer_rows: list[dict[str, Any]] = []
    writer_call_count = 0
    for batch_index, chunk in enumerate(chunks, start=1):
        writer_call_count += 1
        chunk_rows, _attempted = _run_application_writer(
            payload=_application_writer_payload(
                blocks=blocks,
                targets=chunk,
                section_id=plan.get("section_id") or "",
                batch_index=batch_index,
                batch_count=len(chunks),
            ),
            writer_tier=writer_tier,
            qwen_caller=qwen_caller,
            usage_records=usage_records,
            diagnostics=diagnostics,
        )
        writer_rows.extend(chunk_rows)
    metrics["application_writer_call_count"] = writer_call_count
    prose_by_handle: dict[str, str] = {}
    unknown_handles = 0
    for row in writer_rows:
        handle = str(row.get("target_handle") or "").strip()
        prose = str(row.get("prose") or "").strip()
        if not handle:
            continue
        if any(
            target.get("handle") == handle for target in writer_batch
        ) is False:
            unknown_handles += 1
            diagnostics.append(
                f"application_writer returned unknown target_handle: {handle}"
            )
            continue
        prose_by_handle[handle] = prose
    if unknown_handles:
        diagnostics.append(
            "application_writer returned unknown target handles: "
            f"{unknown_handles}"
        )

    for target in writer_batch:
        block_index = target["block_index"]
        best = target["candidate"]
        marker_id = best["marker_id"]
        handle = target["handle"]
        prose = prose_by_handle.get(handle, "")
        valid, reason = _validate_application_prose(
            prose,
            target=target,
            candidate=best,
        )
        if not valid:
            metrics["skipped_targets"] += 1
            metrics["stop_reasons"].append(
                f"block {block_index}: writer_output_invalid ({reason})"
            )
            diagnostics.append(
                "representative_application_skipped: "
                f"block {block_index} (writer {reason})"
            )
            continue
        target["application_sentence"] = prose
        block = blocks[block_index - 1]
        attached = _attach_application_sentence(
            block,
            anchor_sentence=target["anchor_sentence"],
            sentence=prose,
            marker=f"[REF:{marker_id}]",
            handle=handle,
        )
        record_handle = _merge_application_record(
            explanatory_ledger,
            candidate=best,
            target=target,
            handle=handle,
            sentence=target["anchor_sentence"],
            attached=attached,
            local_inspected=target.get("local_candidates_inspected", 0),
            s2_inspected=target.get("s2_candidates_inspected", 0),
            cap_compliant=True,
        )
        if attached:
            records.append(
                {
                    "handle": handle,
                    "record_handle": record_handle,
                    "benefit_type": _REPRESENTATIVE_APPLICATION_BENEFIT,
                    "target": {
                        "block_index": block_index,
                        "anchor_sentence": target["anchor_sentence"],
                        "query": target["query"],
                    },
                    "marker_id": marker_id,
                    "retrieval_origin": best["origin"],
                    "application_sentence": prose,
                    "prose_source": "chapter_asset_application_writer",
                    "attached": True,
                    "candidates_selected": target["candidates_selected"],
                    "local_candidates_inspected": target.get(
                        "local_candidates_inspected", 0
                    ),
                    "s2_candidates_inspected": target.get(
                        "s2_candidates_inspected", 0
                    ),
                }
            )
            metrics["examples_attached"] += 1
            metrics["references_added_count"] += 1
        else:
            metrics["skipped_targets"] += 1
            metrics["stop_reasons"].append(
                f"block {block_index}: attach_failed"
            )
            diagnostics.append(
                "representative_application_attach_failed: "
                f"block {block_index}"
            )
    return (
        {
            "schema_version": SCHEMA_VERSION,
            "section_id": plan.get("section_id") or "",
            "records": records,
            "metrics": metrics,
            "diagnostics": diagnostics,
        },
        diagnostics,
    )


def _resolve_markers(handles: list[str], ledger: HandleLedger) -> list[str]:
    markers: list[str] = []
    for handle in handles:
        entry = ledger.evidence_entry(handle)
        if entry is None or not str(entry.paper_id or "").strip():
            continue
        marker = f"[REF:{entry.paper_id}]"
        if marker not in markers:
            markers.append(marker)
    return markers


def _render_enhanced_chapter(
    *,
    blocks: list[dict[str, Any]],
    ledger: HandleLedger,
) -> tuple[str, dict[str, Any], dict[str, Any]]:
    rendered_blocks: list[dict[str, Any]] = []
    for block in blocks:
        markers = _resolve_markers(block["evidence_handles"], ledger)
        explanatory_markers = list(
            block.get("explanatory_markers") or []
        )
        all_markers = markers + [
            marker
            for marker in explanatory_markers
            if marker not in markers
        ]
        block["markers"] = markers
        block["explanatory_markers"] = explanatory_markers
        covered_claim_handles: list[str] = []
        for handle in block["evidence_handles"]:
            entry = ledger.evidence_entry(handle)
            if entry is not None and entry.claim_handle not in covered_claim_handles:
                covered_claim_handles.append(entry.claim_handle)
        block["covered_claim_handles"] = covered_claim_handles
        block["covered_claim_ids"] = [
            str(ledger.claim(handle).claim_id)
            for handle in covered_claim_handles
            if ledger.claim(handle) is not None
        ]
        block["planned_claim_ids"] = [
            str(ledger.claim(handle).claim_id)
            for handle in block.get("planned_claim_handles", block["claim_handles"])
            if ledger.claim(handle) is not None
        ]
        prose = block["prose"].strip()
        for sentence, sentence_markers in (
            block.get("explanatory_sentence_markers") or {}
        ).items():
            needle = str(sentence or "").strip()
            if needle and needle in prose:
                insertion = " " + " ".join(sentence_markers)
                prose = prose.replace(needle, needle + insertion, 1)
        block["explanatory_sentence_markers"] = (
            block.get("explanatory_sentence_markers") or {}
        )
        if all_markers:
            prose = f"{prose} {' '.join(all_markers)}"
        rendered_blocks.append(
            {
                **block,
                "paragraph_text": prose,
                "claim_ids": list(block["covered_claim_ids"]),
            }
        )
    enhanced_text = "\n\n".join(
        block["paragraph_text"] for block in rendered_blocks
    ).strip()
    claim_to_paragraph: dict[str, list[int]] = {}
    block_to_claim: list[dict[str, Any]] = []
    for block in rendered_blocks:
        block_to_claim.append(
            {
                "block_index": block["block_index"],
                "title": block["title"],
                "planned_claim_handles": list(
                    block.get("planned_claim_handles", block["claim_handles"])
                ),
                "planned_claim_ids": list(block["planned_claim_ids"]),
                "covered_claim_handles": list(
                    block["covered_claim_handles"]
                ),
                "covered_claim_ids": list(block["covered_claim_ids"]),
                "claim_ids": list(block["covered_claim_ids"]),
            }
        )
        for claim_id in block["covered_claim_ids"]:
            claim_to_paragraph.setdefault(claim_id, []).append(
                block["block_index"]
            )
    maps = {
        "claim_to_paragraph": {
            claim_id: sorted(paragraphs)
            for claim_id, paragraphs in claim_to_paragraph.items()
        },
        "block_to_claim": block_to_claim,
    }
    return enhanced_text, maps, {"blocks": rendered_blocks}


def _block_summaries(blocks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "block_index": block["block_index"],
            "title": block["title"],
            "block_type": block["block_type"],
            "goal": block["goal"],
            "planned_claim_handles": list(
                block.get("planned_claim_handles", block["claim_handles"])
            ),
            "covered_claim_handles": list(
                block.get("covered_claim_handles") or []
            ),
            "evidence_handles": list(block["evidence_handles"]),
            "paragraph_prose": block["prose"],
        }
        for block in blocks
    ]


def _auditor_payload(
    *,
    old_draft: str,
    blocks: list[dict[str, Any]],
    ledger: HandleLedger,
    title: str,
) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "chapter_title": title,
        "old_draft": _sanitize_legacy_draft(old_draft),
        "new_blocks": _block_summaries(blocks),
        "handle_ledger": [
            entry.to_compact(full=True)
            for entry in ledger.evidence.values()
        ],
        "instruction": (
            "Identify only scientifically useful content genuinely lost "
            "relative to the old draft, never wording, style, synonyms, or "
            "target-length differences."
        ),
    }


def _sanitize_legacy_draft(text: str) -> str:
    """Replace citation markers so the auditor never sees paper hashes."""
    return _RAW_CITATION_PATTERN.sub("[CITATION]", str(text or ""))


def _normalize_gap_audit(
    output: dict[str, Any],
    blocks: list[dict[str, Any]],
    ledger: HandleLedger,
) -> tuple[dict[str, Any], list[dict[str, Any]], list[str]]:
    warnings: list[str] = []
    gaps = output.get("gaps") or []
    if not isinstance(gaps, list):
        warnings.append("gap auditor returned a non-list gaps field")
        gaps = []
    normalized: list[dict[str, Any]] = []
    for index, gap in enumerate(gaps, start=1):
        if not isinstance(gap, dict):
            warnings.append(f"gap[{index}] is not a JSON object")
            continue
        affected = gap.get("affected_block_indices") or []
        if not isinstance(affected, list):
            warnings.append(
                f"gap[{index}].affected_block_indices is not a JSON array"
            )
            continue
        affected = [int(value) for value in affected if str(value).isdigit()]
        claim_handles = [
            str(value) for value in (gap.get("claim_handles") or [])
        ]
        evidence_handles = [
            str(value) for value in (gap.get("evidence_handles") or [])
        ]
        normalized.append(
            {
                "gap_id": str(gap.get("gap_id") or f"G{index:02d}"),
                "old_draft_snippet": str(gap.get("old_draft_snippet") or ""),
                "scientific_content": str(
                    gap.get("scientific_content") or ""
                ),
                "claim_handles": claim_handles,
                "evidence_handles": evidence_handles,
                "affected_block_indices": [
                    value
                    for value in affected
                    if 1 <= value <= len(blocks)
                ],
            }
        )
    audit_record = {
        "verdict": (
            "gaps_found" if normalized else "no_actionable_gaps"
        ),
        "gaps": normalized,
        "notes": str(output.get("notes") or ""),
        "warnings": warnings,
    }
    return audit_record, normalized, warnings


def _run_gap_audit(
    *,
    old_draft: str,
    blocks: list[dict[str, Any]],
    ledger: HandleLedger,
    title: str,
    model_tier: str,
    qwen_caller: QwenCaller,
    usage_records: list[dict[str, Any]],
) -> tuple[dict[str, Any], list[dict[str, Any]], list[str]]:
    payload = _auditor_payload(
        old_draft=old_draft,
        blocks=blocks,
        ledger=ledger,
        title=title,
    )
    try:
        output = _call_model(
            agent_name="ChapterAssetLegacyGapAuditorAgent",
            prompt_path=PROMPT_AUDITOR,
            payload=payload,
            model_tier=model_tier,
            qwen_caller=qwen_caller,
            usage_records=usage_records,
            temperature=0.2,
            max_tokens=4000,
        )
    except ChapterAssetModelError as exc:
        audit_record = {
            "verdict": "audit_unavailable",
            "gaps": [],
            "notes": "",
            "warnings": [str(exc)],
        }
        return audit_record, [], [str(exc)]
    return _normalize_gap_audit(output, blocks, ledger)


def _patch_payload(
    *,
    block: dict[str, Any],
    gap: dict[str, Any],
    ledger: HandleLedger,
    plan: dict[str, Any],
) -> dict[str, Any]:
    allowed_handles = sorted(
        set(block["evidence_handles"]) | set(gap["evidence_handles"])
    )
    evidence = [
        ledger.evidence_entry(handle).to_compact(full=True)
        for handle in allowed_handles
        if ledger.evidence_entry(handle) is not None
    ]
    return {
        "schema_version": SCHEMA_VERSION,
        "section_identity": {
            "section_id": plan.get("section_id") or "",
            "title": plan.get("title") or "",
            "chapter_thesis": plan.get("chapter_thesis") or "",
        },
        "block": {
            "block_index": block["block_index"],
            "title": block["title"],
            "current_paragraph_prose": block["prose"],
            "current_evidence_handles": list(block["evidence_handles"]),
        },
        "gap": {
            "gap_id": gap["gap_id"],
            "old_draft_snippet": gap["old_draft_snippet"],
            "scientific_content": gap["scientific_content"],
        },
        "allowed_evidence": evidence,
        "instruction": (
            "Patch only the supplied block by restoring the genuinely useful "
            "scientific content described by the gap. Use only allowed "
            "evidence handles, never raw citation markers or paper IDs."
        ),
    }


def _run_patches(
    *,
    gaps: list[dict[str, Any]],
    blocks: list[dict[str, Any]],
    ledger: HandleLedger,
    plan: dict[str, Any],
    model_tier: str,
    qwen_caller: QwenCaller,
    usage_records: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[str]]:
    patch_records: list[dict[str, Any]] = []
    warnings: list[str] = []
    for gap in gaps:
        gap_evidence_handles = [
            handle
            for handle in gap["evidence_handles"]
            if ledger.evidence_entry(handle) is not None
            and (
                not gap["claim_handles"]
                or ledger.evidence_entry(handle).claim_handle
                in gap["claim_handles"]
            )
        ]
        if not gap_evidence_handles:
            patch_records.append(
                {
                    "gap_id": gap["gap_id"],
                    "block_index": None,
                    "applied": False,
                    "reason": "no_gap_specific_evidence_handles",
                    "detail": (
                        "Gap has no valid claim-aligned evidence handle; "
                        "record-only, no patch applied."
                    ),
                }
            )
            warnings.append(
                f"gap {gap['gap_id']} has no valid evidence handles; "
                "recorded without patching"
            )
            continue
        effective_gap = dict(gap)
        effective_gap["evidence_handles"] = gap_evidence_handles
        for block_index in gap["affected_block_indices"]:
            block = blocks[block_index - 1]
            allowed_handles = sorted(
                set(block["evidence_handles"]) | set(gap_evidence_handles)
            )
            requires_evidence = bool(allowed_handles) and any(
                _claim_requires_core_evidence(ledger.claim(handle))
                for handle in block.get(
                    "planned_claim_handles", block["claim_handles"]
                )
            )
            payload = _patch_payload(
                block=block,
                gap=effective_gap,
                ledger=ledger,
                plan=plan,
            )
            try:
                output = _call_model(
                    agent_name="ChapterAssetLegacyGapPatchWriterAgent",
                    prompt_path=PROMPT_PATCH,
                    payload=payload,
                    model_tier=model_tier,
                    qwen_caller=qwen_caller,
                    usage_records=usage_records,
                    temperature=0.1,
                    max_tokens=3000,
                )
                prose, used_handles = _validate_block_output(
                    output,
                    allowed_handles=set(allowed_handles),
                    ledger=ledger,
                    requires_evidence=requires_evidence,
                    required_handles=set(gap_evidence_handles),
                )
            except (ChapterAssetModelError, ChapterAssetIntegrityError) as exc:
                patch_records.append(
                    {
                        "gap_id": gap["gap_id"],
                        "block_index": block_index,
                        "applied": False,
                        "reason": (
                            "patch_model_unavailable"
                            if isinstance(exc, ChapterAssetModelError)
                            else "patch_integrity_failure"
                        ),
                        "detail": str(exc),
                    }
                )
                warnings.append(
                    f"patch for {gap['gap_id']} block {block_index} skipped: {exc}"
                )
                continue
            block["prose"] = prose
            block["evidence_handles"] = list(
                dict.fromkeys(
                    list(block["evidence_handles"]) + list(used_handles)
                )
            )
            block["patch_history"].append(
                {
                    "gap_id": gap["gap_id"],
                    "block_index": block_index,
                    "applied": True,
                }
            )
            patch_records.append(
                {
                    "gap_id": gap["gap_id"],
                    "block_index": block_index,
                    "applied": True,
                    "used_evidence_handles": list(block["evidence_handles"]),
                    "gap_evidence_handles": list(gap_evidence_handles),
                }
            )
    return patch_records, warnings


def _usage_summary(usage_records: list[dict[str, Any]]) -> dict[str, Any]:
    total_input = sum(int(row.get("input_tokens") or 0) for row in usage_records)
    total_output = sum(
        int(row.get("output_tokens") or 0) for row in usage_records
    )
    total_cost = sum(
        float(row.get("estimated_cost_cny") or 0.0) for row in usage_records
    )
    return {
        "available": bool(usage_records),
        "call_count": len(usage_records),
        "total_input_tokens": total_input,
        "total_output_tokens": total_output,
        "total_estimated_cost_cny": round(total_cost, 6),
        "calls": list(usage_records),
    }


def _write_json(path: Path, data: Any) -> None:
    path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _format_enhanced_markdown(title: str, text: str) -> str:
    """Render enhanced text with exactly one chapter H1."""
    value = str(text or "").strip()
    if not value:
        return f"# {title}\n"
    if value.startswith("# "):
        lines = value.splitlines()
        seen_h1 = False
        for index, line in enumerate(lines):
            if line.startswith("# "):
                if seen_h1:
                    lines[index] = "##" + line[1:]
                else:
                    seen_h1 = True
        return "\n".join(lines) + "\n"
    return f"# {title}\n\n{value}\n"


def _write_text(path: Path, text: str) -> None:
    path.write_text(str(text), encoding="utf-8")


def _write_artifacts(
    *,
    output_dir: Path,
    old_draft: str,
    enhanced_text: str,
    title: str,
    section_id: str,
    plan: dict[str, Any],
    plan_warnings: list[str],
    blocks: list[dict[str, Any]],
    maps: dict[str, Any],
    audit_record: dict[str, Any],
    patch_records: list[dict[str, Any]],
    ledger: HandleLedger,
    recovery_diagnostics: list[dict[str, Any]],
    block_review: dict[str, Any],
    explanatory_ledger: dict[str, Any],
    explanatory_diagnostics: list[str],
    representative_applications: dict[str, Any],
    representative_application_diagnostics: list[str],
    status: str,
    model_tiers: dict[str, str],
    usage_records: list[dict[str, Any]],
    soft_warnings: list[str],
    fail_open_reason: str,
) -> dict[str, Any]:
    output = Path(output_dir)
    output.mkdir(parents=True, exist_ok=True)
    _write_text(output / OLD_CHAPTER_MD, old_draft.rstrip("\n") + "\n")
    _write_text(
        output / ENHANCED_CHAPTER_MD,
        _format_enhanced_markdown(title, enhanced_text),
    )
    _write_json(
        output / CHAPTER_ARGUMENT_PLAN_JSON,
        {
            "schema_version": SCHEMA_VERSION,
            "section_id": section_id,
            "plan": plan,
            "warnings": plan_warnings,
        },
    )
    _write_json(
        output / TERMINOLOGY_REGISTRY_JSON,
        {
            "schema_version": SCHEMA_VERSION,
            "section_id": section_id,
            "terminology_rows": plan.get("terminology_rows") or [],
            "warnings": plan_warnings,
        },
    )
    _write_json(output / EXPLANATION_BLOCKS_JSON, {"blocks": blocks})
    _write_json(
        output / CLAIM_TO_PARAGRAPH_MAP_JSON,
        {
            "schema_version": SCHEMA_VERSION,
            "section_id": section_id,
            **maps,
        },
    )
    _write_json(
        output / LEGACY_GAP_AUDIT_JSON,
        {
            "schema_version": SCHEMA_VERSION,
            "section_id": section_id,
            "audit": audit_record,
            "patch_records": patch_records,
        },
    )
    explanatory_ledger["representative_applications"] = (
        representative_applications
    )
    selection_audit = explanatory_ledger.get("selection_audit")
    if isinstance(selection_audit, dict):
        # The soft 10-20 explanatory-reference guidance includes application
        # garnish; the counts stay advisory and never become a gate.
        selection_audit["representative_application_record_count"] = len(
            representative_applications.get("records") or []
        )
        selection_audit["representative_applications_included"] = True
    _write_json(
        output / EXPLANATORY_CITATION_LEDGER_JSON,
        explanatory_ledger,
    )
    _write_json(output / BLOCK_SCIENTIFIC_REVIEW_JSON, block_review)
    usable_claim_handles = ledger.usable_claim_handles()
    planned_claim_handles = {
        handle
        for block in blocks
        for handle in block.get(
            "planned_claim_handles", block.get("claim_handles") or []
        )
    }
    covered_claim_handles = {
        handle
        for block in blocks
        for handle in block.get("covered_claim_handles") or []
    }
    covered_claim_ids = {
        claim_id
        for block in blocks
        for claim_id in block.get("covered_claim_ids") or []
    }
    usable_claim_ids = {
        str(ledger.claim(handle).claim_id)
        for handle in usable_claim_handles
        if ledger.claim(handle) is not None
    }
    used_evidence_handles = {
        handle
        for block in blocks
        for handle in block.get("evidence_handles") or []
    }
    visible_markers = set(
        re.findall(r"\[REF:[^\]]+\]", str(enhanced_text or ""))
    )
    omitted = plan.get("omitted_handle_reasons") or {}
    if not isinstance(omitted, dict):
        omitted = {}
    coverage_metrics = {
        "usable_claim_count": len(usable_claim_handles),
        "planned_usable_claim_count": len(
            usable_claim_handles & planned_claim_handles
        ),
        "planned_usable_claim_coverage_ratio": round(
            len(usable_claim_handles & planned_claim_handles)
            / max(1, len(usable_claim_handles)),
            3,
        ),
        "actual_covered_claim_count": len(
            covered_claim_ids & usable_claim_ids
        ),
        "actual_covered_claim_coverage_ratio": round(
            len(covered_claim_ids & usable_claim_ids)
            / max(1, len(usable_claim_ids)),
            3,
        ),
        "evidence_handle_count": len(used_evidence_handles),
        "visible_unique_reference_count": len(visible_markers),
        "claims_omitted_with_reasons": {
            str(key): str(value)
            for key, value in omitted.items()
            if ledger.claim(str(key)) is not None
        },
    }
    core_reference_markers = {
        marker
        for block in blocks
        for marker in block.get("markers") or []
    }
    explanatory_reference_markers = {
        marker
        for block in blocks
        for marker in block.get("explanatory_markers") or []
    }
    for block in blocks:
        for markers in (
            block.get("explanatory_sentence_markers") or {}
        ).values():
            explanatory_reference_markers.update(markers)
    explanatory_records = explanatory_ledger.get("records") or []
    representative_records = (
        representative_applications.get("records") or []
    )
    reference_metrics = {
        "core_reference_count": len(core_reference_markers),
        "explanatory_reference_count": len(
            explanatory_reference_markers - core_reference_markers
        ),
        "explanatory_record_count": len(explanatory_records),
        "representative_application_reference_count": sum(
            1
            for record in representative_records
            if bool(record.get("attached"))
        ),
        "explanatory_non_core_count": sum(
            not bool(record.get("overlaps_core_reference"))
            for record in explanatory_records
        ),
        "representative_application_record_count": len(
            representative_records
        ),
        "total_deduplicated_bibliography_count": len(
            core_reference_markers | explanatory_reference_markers
        ),
    }
    report = {
        "schema_version": SCHEMA_VERSION,
        "section_id": section_id,
        "status": status,
        "mode": "live",
        "model_tiers": model_tiers,
        "input_labels": {
            "packet": "input_packet.json",
            "old_draft": "SECTION_DRAFT_EN.md",
        },
        "word_counts": {
            "old": _word_count(old_draft),
            "enhanced": _word_count(enhanced_text),
        },
        "paragraph_counts": {
            "old": _paragraph_count(old_draft),
            "enhanced": _paragraph_count(enhanced_text),
        },
        "block_count": len(blocks),
        "hard_defects": {
            "empty_output": not str(enhanced_text or "").strip(),
            "unknown_evidence_handle": False,
            "unknown_citation": False,
            "core_model_unavailable": status == "fail_open_original",
        },
        "soft_warnings": list(soft_warnings),
        "fail_open_reason": fail_open_reason,
        "coverage_metrics": coverage_metrics,
        "reference_metrics": reference_metrics,
        "recovery_diagnostics": list(recovery_diagnostics),
        "explanatory_diagnostics": list(explanatory_diagnostics),
        "explanatory_selection_audit": explanatory_ledger.get(
            "selection_audit"
        ),
        "representative_application_metrics": (
            representative_applications.get("metrics") or {}
        ),
        "representative_application_diagnostics": list(
            representative_application_diagnostics
        ),
        "block_scientific_review": {
            "attempted": bool(block_review.get("attempted")),
            "available": bool(block_review.get("available")),
            "blocking_count": int(block_review.get("blocking_count") or 0),
            "advisory_count": int(block_review.get("advisory_count") or 0),
            "revision_attempted_count": sum(
                1
                for outcome in (
                    block_review.get("per_block_revision_outcomes") or {}
                ).values()
                if isinstance(outcome, dict) and outcome.get("attempted")
            ),
            "revision_applied_count": sum(
                1
                for outcome in (
                    block_review.get("per_block_revision_outcomes") or {}
                ).values()
                if isinstance(outcome, dict) and outcome.get("applied")
            ),
            "fallback_reason": block_review.get("fallback_reason") or "",
        },
        "model_usage": _usage_summary(usage_records),
        "artifact_paths": {
            "old_chapter": OLD_CHAPTER_MD,
            "enhanced_chapter": ENHANCED_CHAPTER_MD,
            "argument_plan": CHAPTER_ARGUMENT_PLAN_JSON,
            "terminology_registry": TERMINOLOGY_REGISTRY_JSON,
            "explanation_blocks": EXPLANATION_BLOCKS_JSON,
            "claim_to_paragraph_map": CLAIM_TO_PARAGRAPH_MAP_JSON,
            "legacy_gap_audit": LEGACY_GAP_AUDIT_JSON,
            "explanatory_citation_ledger": EXPLANATORY_CITATION_LEDGER_JSON,
            "block_scientific_review": BLOCK_SCIENTIFIC_REVIEW_JSON,
            "enhancement_report": ENHANCEMENT_REPORT_JSON,
        },
    }
    _write_json(output / ENHANCEMENT_REPORT_JSON, report)
    return report


def run_enhancement(
    *,
    packet_path: Path,
    old_draft_path: Path,
    output_dir: Path,
    live: bool = False,
    allow_overwrite: bool = False,
    planner_tier: str = DEFAULT_PLANNER_TIER,
    block_tier: str = DEFAULT_BLOCK_TIER,
    patch_tier: str = DEFAULT_PATCH_TIER,
    auditor_tier: str = DEFAULT_AUDITOR_TIER,
    contract_repair_tier: str = DEFAULT_CONTRACT_REPAIR_TIER,
    explanatory_citations_enabled: bool = True,
    explanatory_tier: str = DEFAULT_EXPLANATORY_TIER,
    explanatory_max_results: int = 10,
    representative_applications_enabled: bool = True,
    application_max_targets: int = DEFAULT_APPLICATION_MAX_TARGETS,
    application_soft_min_targets: int = DEFAULT_APPLICATION_SOFT_MIN_TARGETS,
    application_per_target_cap: int = DEFAULT_APPLICATION_PER_TARGET_CAP,
    application_local_max_results: int = (
        DEFAULT_APPLICATION_LOCAL_MAX_RESULTS
    ),
    application_writer_tier: str = DEFAULT_APPLICATION_WRITER_TIER,
    local_search_callback: SearchCallback | None = None,
    s2_search_callback: SearchCallback | None = None,
    qwen_caller: QwenCaller = call_qwen_chat,
) -> dict[str, Any]:
    """Enhance one accepted chapter packet and write standalone artifacts.

    ``qwen_caller`` is an offline test seam; the CLI uses the real Qwen chat
    client. Without ``live`` this is a dry run and writes nothing. The output
    directory must not exist unless ``allow_overwrite`` is true.
    """
    output = Path(output_dir)
    _refuse_existing_output(output, allow_overwrite)
    packet_data = _load_json_object(Path(packet_path))
    old_draft = Path(old_draft_path).read_text(encoding="utf-8").strip()
    if not old_draft:
        raise ValueError("Old chapter draft must be non-empty")
    if not live:
        return {
            "mode": "dry_run",
            "validated": True,
            "section_id": packet_data.get("section_id"),
            "output_dir": str(output),
            "model_tiers": {
                "planner": planner_tier,
                "block": block_tier,
                "patch": patch_tier,
                "auditor": auditor_tier,
                "contract_repair": contract_repair_tier,
                "explanatory": explanatory_tier,
                "application_writer": application_writer_tier,
                "block_reviewer": DEFAULT_BLOCK_REVIEWER_TIER,
                "block_reviser": block_tier,
            },
            "explanatory_citations_enabled": explanatory_citations_enabled,
            "explanatory_max_results": explanatory_max_results,
            "representative_applications_enabled": (
                representative_applications_enabled
            ),
            "application_max_targets": application_max_targets,
            "application_soft_min_targets": application_soft_min_targets,
            "application_per_target_cap": application_per_target_cap,
            "application_local_max_results": application_local_max_results,
            "application_writer_tier": application_writer_tier,
        }

    output.mkdir(parents=True, exist_ok=True)
    packet = _rehydrate_packet(packet_data)
    ledger = _build_handle_ledger(packet)
    compact_context = _build_compact_context(packet, ledger)
    contract = packet.section_contract or {}
    title = str(contract.get("title") or packet.section_id)
    usage_records: list[dict[str, Any]] = []
    recovery_diagnostics: list[dict[str, Any]] = []
    soft_warnings: list[str] = []
    status = "enhanced"
    fail_open_reason = ""
    plan: dict[str, Any] = {}
    plan_warnings: list[str] = []
    blocks: list[dict[str, Any]] = []
    maps: dict[str, Any] = {
        "claim_to_paragraph": {},
        "block_to_claim": [],
    }
    audit_record: dict[str, Any] = {
        "verdict": "not_run",
        "gaps": [],
        "notes": "",
        "warnings": [],
    }
    patch_records: list[dict[str, Any]] = []
    explanatory_ledger: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "section_id": packet.section_id,
        "records": [],
        "block_to_explanatory_handles": {},
        "sentence_to_handles": {},
        "diagnostics": [],
    }
    explanatory_diagnostics: list[str] = []
    representative_applications: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "section_id": packet.section_id,
        "records": [],
        "metrics": _empty_application_metrics(),
        "diagnostics": [],
    }
    representative_application_diagnostics: list[str] = []
    block_review: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "section_id": packet.section_id,
        "attempted": False,
        "available": False,
        "fallback_reason": "",
        "comments": [],
        "blocking_count": 0,
        "advisory_count": 0,
        "per_block_revision_outcomes": {},
    }
    enhanced_text = old_draft
    try:
        plan, plan_warnings = _run_planner(
            compact_context=compact_context,
            ledger=ledger,
            model_tier=planner_tier,
            repair_tier=contract_repair_tier,
            qwen_caller=qwen_caller,
            usage_records=usage_records,
            recovery_diagnostics=recovery_diagnostics,
        )
        plan["section_id"] = packet.section_id
        plan["title"] = title
        blocks = _run_block_writers(
            plan=plan,
            ledger=ledger,
            model_tier=block_tier,
            repair_tier=contract_repair_tier,
            qwen_caller=qwen_caller,
            usage_records=usage_records,
            recovery_diagnostics=recovery_diagnostics,
        )
        audit_record, gaps, audit_warnings = _run_gap_audit(
            old_draft=old_draft,
            blocks=blocks,
            ledger=ledger,
            title=title,
            model_tier=auditor_tier,
            qwen_caller=qwen_caller,
            usage_records=usage_records,
        )
        soft_warnings.extend(audit_warnings)
        patch_records, patch_warnings = _run_patches(
            gaps=gaps,
            blocks=blocks,
            ledger=ledger,
            plan=plan,
            model_tier=patch_tier,
            qwen_caller=qwen_caller,
            usage_records=usage_records,
        )
        soft_warnings.extend(patch_warnings)
        block_review, block_review_diagnostics = (
            _run_block_review_and_revision(
                blocks=blocks,
                plan=plan,
                ledger=ledger,
                compact_context=compact_context,
                reviewer_tier=DEFAULT_BLOCK_REVIEWER_TIER,
                reviser_tier=block_tier,
                qwen_caller=qwen_caller,
                usage_records=usage_records,
            )
        )
        soft_warnings.extend(block_review_diagnostics)
        explanatory_ledger, explanatory_diagnostics, application_rows = (
            _run_explanatory_citations(
                blocks=blocks,
                ledger=ledger,
                plan=plan,
                title=title,
                enabled=explanatory_citations_enabled,
                model_tier=explanatory_tier,
                qwen_caller=qwen_caller,
                usage_records=usage_records,
                local_search_callback=local_search_callback,
                s2_search_callback=s2_search_callback,
                max_results=explanatory_max_results,
                application_max_targets=application_max_targets,
                application_soft_min_targets=application_soft_min_targets,
            )
        )
        soft_warnings.extend(explanatory_diagnostics)
        representative_applications, (
            representative_application_diagnostics
        ) = _run_representative_applications(
            blocks=blocks,
            ledger=ledger,
            plan=plan,
            explanatory_ledger=explanatory_ledger,
            planned_rows=application_rows,
            enabled=representative_applications_enabled,
            local_search_callback=local_search_callback,
            s2_search_callback=s2_search_callback,
            max_targets=application_max_targets,
            soft_min_targets=application_soft_min_targets,
            per_target_cap=application_per_target_cap,
            local_max_results=application_local_max_results,
            writer_tier=application_writer_tier,
            qwen_caller=qwen_caller,
            usage_records=usage_records,
        )
        soft_warnings.extend(representative_application_diagnostics)
        enhanced_text, maps, _ = _render_enhanced_chapter(
            blocks=blocks,
            ledger=ledger,
        )
    except (ChapterAssetModelError, ChapterAssetIntegrityError) as exc:
        status = "fail_open_original"
        fail_open_reason = str(exc)
        enhanced_text = old_draft
        soft_warnings.append(f"fail_open_original: {exc}")

    soft_warnings.extend(plan_warnings)
    report = _write_artifacts(
        output_dir=output,
        old_draft=old_draft,
        enhanced_text=enhanced_text,
        title=title,
        section_id=packet.section_id,
        plan=plan,
        plan_warnings=plan_warnings,
        blocks=blocks,
        maps=maps,
        audit_record=audit_record,
        patch_records=patch_records,
        ledger=ledger,
        recovery_diagnostics=recovery_diagnostics,
        block_review=block_review,
        explanatory_ledger=explanatory_ledger,
        explanatory_diagnostics=explanatory_diagnostics,
        representative_applications=representative_applications,
        representative_application_diagnostics=(
            representative_application_diagnostics
        ),
        status=status,
        model_tiers={
            "planner": planner_tier,
            "block": block_tier,
            "patch": patch_tier,
            "auditor": auditor_tier,
            "contract_repair": contract_repair_tier,
            "explanatory": explanatory_tier,
            "application_writer": application_writer_tier,
            "block_reviewer": DEFAULT_BLOCK_REVIEWER_TIER,
            "block_reviser": block_tier,
        },
        usage_records=usage_records,
        soft_warnings=soft_warnings,
        fail_open_reason=fail_open_reason,
    )
    report["enhanced_text"] = enhanced_text
    return report
