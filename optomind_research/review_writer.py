"""T8: Review writing pipeline — section-level writing with evidence grounding.

Writing order (per spec):
1. English section draft (SectionWriter)
2. Sentence-level claim identification
3. Sentence-level citation binding (CitationBinder)
4. Overclaim and numeric audit (OverclaimAuditor)
5. Contradiction editing (ContradictionEditor)
6. Cross-section dedup and transitions (CrossSectionEditor)
7. Figure insertion and text-figure alignment (FigurePlanner)
8. Final Chinese translation and academic polish (FinalTranslator)

Integration: call ReviewWritingPipeline.run(blueprint, kb_path) from run_review.py.
"""

from __future__ import annotations

import copy
import json
import math
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from llm.qwen_chat_client import call_qwen_chat
from optomind_research.section_claim_router import (
    ClaimRoutingResult,
    route_section_claims,
)
from optomind_research.section_evidence_handles import EvidenceHandleRegistry

PROJECT_ROOT = Path(__file__).resolve().parents[1]
_PROMPTS = PROJECT_ROOT / "prompts"

SECTION_WRITER_PROMPT = _PROMPTS / "Section Writer.txt"
SECTION_PARAGRAPH_RECOVERY_PROMPT = _PROMPTS / "Section Paragraph Compact Recovery.txt"
# qwen3.5-plus compact paragraph generation needs a longer bounded timeout
# than the 120-second default used for short audit calls.
PARAGRAPH_RECOVERY_TIMEOUT_SECONDS = 300
COMPACT_EVIDENCE_HANDLES_MODE = "compact_evidence_handles"
COMPACT_EVIDENCE_HANDLES_SYSTEM_SUFFIX = (
    "\n\nCOMPACT EVIDENCE-HANDLE MODE: This paragraph uses local evidence "
    "handles. The payload field evidence_handles replaces evidence_packets in "
    "this mode. Emit evidence references as [REF:E01] or [E01] (both forms "
    "are accepted and resolved locally), exactly matching handles listed in "
    "allowed_reference_markers. Never emit a paper_id, chunk_id, claim_id, or "
    "any other identifier inside a REF bracket or bare bracket. A handle is "
    "valid only when it appears in this paragraph's allowed_reference_markers."
)
CITATION_BINDER_PROMPT = _PROMPTS / "Citation Binder.txt"
CITATION_ENTAILMENT_PROMPT = _PROMPTS / "Citation Entailment Judge.txt"
OVERCLAIM_AUDITOR_PROMPT = _PROMPTS / "Overclaim Auditor.txt"
POSITIVE_ASSERTION_REFINER_PROMPT = _PROMPTS / "Positive Assertion Style Refiner.txt"
CONTRADICTION_EDITOR_PROMPT = _PROMPTS / "Contradiction Editor.txt"
CROSS_SECTION_EDITOR_PROMPT = _PROMPTS / "Cross Section Editor.txt"
FIGURE_PLANNER_PROMPT = _PROMPTS / "Figure Planner.txt"
FINAL_TRANSLATOR_PROMPT = _PROMPTS / "Final Translator.txt"
EVIDENCE_AWARE_REVISER_PROMPT = _PROMPTS / "Evidence-Aware Review Reviser.txt"


def _compact(text: Any, limit: int = 400) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip()[:limit]


def _sentence_safe_excerpt(text: Any, limit: int = 1200) -> str:
    value = re.sub(r"\s+", " ", str(text or "")).strip()
    if len(value) <= limit and re.search(r"[.!?;:)\]\"']$", value):
        return value
    prefix = value[:limit] if len(value) > limit else value
    boundaries = [prefix.rfind(mark) for mark in (". ", "? ", "! ", "; ")]
    boundary = max(boundaries)
    if boundary >= int(limit * 0.55):
        return prefix[: boundary + 1].strip()
    word_boundary = prefix.rfind(" ")
    return prefix[:word_boundary].strip() if word_boundary > 0 else prefix.strip()


def _complete_evidence_excerpt(exact: Any, record_text: Any, limit: int = 1200) -> str:
    """Prefer the KB chunk when a verifier quote is visibly transport-truncated."""
    quote = re.sub(r"\s+", " ", str(exact or "")).strip()
    source = re.sub(r"\s+", " ", str(record_text or "")).strip()
    looks_incomplete = bool(
        quote
        and source
        and len(source) > len(quote)
        and not re.search(r"[.!?;:)\]\"']$", quote)
    )
    return _sentence_safe_excerpt(source if looks_incomplete else quote or source, limit)


def _read_prompt(path: Path) -> str:
    if path.exists():
        return path.read_text(encoding="utf-8").strip()
    return f"You are a scientific review writing assistant. Task: {path.stem}."


def _safe_json(text: str) -> dict | list:
    """Recover one root JSON value without inventing scientific fields.

    Strict JSON wins.  Otherwise the first balanced root object is extracted
    and, when the model appended a small trailing root-field fragment (for
    example `, "submission_metadata": {...}}`), valid new root fields are
    reattached exactly.  Arbitrary prose and additional complete JSON
    documents are never merged into the root.
    """

    value = str(text or "").strip()
    try:
        v = json.loads(value)
        return v if isinstance(v, (dict, list)) else {}
    except Exception:
        pass
    root, remainder = _balanced_root(value)
    if isinstance(root, dict):
        reattached = _reattach_trailing_root_fields(root, remainder)
        if reattached is not None:
            return reattached
    if isinstance(root, (dict, list)):
        return root
    return {}


def _balanced_root(text: str) -> tuple[dict | list | None, str]:
    """Extract the first balanced JSON root and the remaining suffix."""

    start: int | None = None
    opener = ""
    closer = ""
    for index, char in enumerate(text):
        if char in "{[":
            start, opener, closer = index, char, "}" if char == "{" else "]"
            break
    if start is None:
        return None, text
    depth = 0
    in_string = False
    escaped = False
    for index in range(start, len(text)):
        char = text[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == opener:
            depth += 1
        elif char == closer:
            depth -= 1
            if depth == 0:
                raw = text[start : index + 1]
                try:
                    parsed = json.loads(raw)
                except Exception:
                    return None, text
                if not isinstance(parsed, (dict, list)):
                    return None, text
                return parsed, text[index + 1 :]
    return None, text


def _reattach_trailing_root_fields(
    root: dict, remainder: str
) -> dict | None:
    """Reattach valid trailing root fields bounded to one repair pattern.

    The suffix must start with a comma followed by one root key/value pair
    (with up to two stray trailing closing braces from a duplicated root
    close).  Existing root fields are never overwritten.
    """

    fragment = str(remainder or "").strip()
    if not fragment.startswith(","):
        return None
    fragment = fragment[1:].strip()
    if not fragment:
        return None
    # A second complete JSON document is not a root-field fragment.
    if fragment.startswith(("{", "[")):
        return None
    for stray_braces in range(3):
        candidate = fragment
        for _ in range(stray_braces):
            if candidate.endswith("}"):
                candidate = candidate[:-1].rstrip()
        try:
            parsed = json.loads("{" + candidate + "}")
        except Exception:
            continue
        if not isinstance(parsed, dict) or not parsed:
            continue
        merged = dict(root)
        changed = False
        for key, field_value in parsed.items():
            if key in merged:
                continue
            merged[key] = field_value
            changed = True
        if changed:
            return merged
        return None
    return None


def _word_count(text: str) -> int:
    return len(re.findall(r"\b[A-Za-z][A-Za-z'-]*\b", str(text or "")))


def _paragraph_count(text: str) -> int:
    return len([row for row in re.split(r"\n\s*\n", str(text or "")) if row.strip()])


def _split_into_paragraphs(text: str, expected_count: int) -> list[str]:
    """Pair the existing draft with the contracted paragraph slots.

    A short first draft may contain fewer paragraphs than the contract
    expects; later slots simply start empty and are rebuilt from the compact
    paragraph calls. Overflow paragraphs are merged into the final slot so the
    contracted paragraph count is preserved.
    """
    paragraphs = [
        row.strip()
        for row in re.split(r"\n\s*\n", str(text or ""))
        if row.strip()
    ]
    if expected_count <= 0:
        return []
    if expected_count == 1:
        return ["\n\n".join(paragraphs).strip()] if paragraphs else [""]
    if len(paragraphs) <= expected_count:
        return paragraphs + [""] * (expected_count - len(paragraphs))
    return paragraphs[: expected_count - 1] + [
        "\n\n".join(paragraphs[expected_count - 1:]).strip()
    ]


def _section_word_guidance(
    word_budget: int,
    min_word_count: Any = None,
    max_word_count: Any = None,
) -> tuple[int, int]:
    """Resolve (reference_target_words, hard_max_words) section guidance.

    An explicit min_word_count is the preferred/reference lower target for the
    drafting pass (e.g. 1200 for S04), not a hard acceptance gate. An explicit
    max_word_count is a hard runaway cap; without one, 115% of word_budget
    caps runaway length. Sanitized so the hard cap never falls below the
    reference target.
    """
    budget = max(0, int(word_budget))

    def _positive(value: Any) -> int:
        if value in (None, ""):
            return 0
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            return 0
        return parsed if parsed > 0 else 0

    reference_target = _positive(min_word_count) or max(
        1, int(budget * 0.80)
    )
    hard_max = _positive(max_word_count) or max(
        reference_target, int(budget * 1.15)
    )
    if hard_max < reference_target:
        hard_max = reference_target
    return reference_target, hard_max


def _paragraph_word_targets(
    word_budget: int,
    paragraph_count: int,
    reference_target_words: int | None = None,
    hard_max_words: int | None = None,
) -> list[tuple[int, int, int]]:
    """Allocate (reference, target, hard_max) words per contracted paragraph.

    Targets sum to the section word budget (the preferred prose length). The
    per-paragraph reference is a soft aim near the section reference target
    with a modest counting cushion; prose may stop shorter when the supplied
    evidence is exhausted. The local hard max is deliberately generous: it
    only catches abnormal runaway prose (a single paragraph exceeding the
    whole section cap) and must not reject a rich paragraph merely because
    equal division of the section cap gives a smaller average. The
    section-wide assembled hard maximum remains authoritative for total
    length, so uneven evidence density between paragraphs is allowed.
    """
    count = max(1, int(paragraph_count))
    budget = max(0, int(word_budget))
    reference_target, hard_max = _section_word_guidance(
        budget, reference_target_words, hard_max_words
    )
    base, remainder = divmod(budget, count)
    targets = [base + (1 if index < remainder else 0) for index in range(count)]
    reference = max(
        20,
        min(base, math.ceil(reference_target * 1.05 / count)),
    )
    return [
        (
            reference,
            target,
            hard_max,
        )
        for target in targets
    ]


def _build_length_plan(
    packet: SectionMaterialPacket,
    *,
    word_budget: int,
    expected_paragraphs: int,
    reference_target_words: int,
    hard_max_words: int,
) -> dict[str, Any]:
    """Model-facing length plan derived locally from the section contract.

    The plan is attached to the model payload without mutating the source
    packet. Paragraph targets use the existing allocation helper and sum to
    ``word_budget``; ``unit`` explicitly defines a word as whitespace-delimited
    English prose, not a token or character.
    """
    word_targets = _paragraph_word_targets(
        word_budget,
        expected_paragraphs,
        reference_target_words,
        hard_max_words,
    )
    return {
        "unit": "whitespace-delimited English prose words",
        "requested_final_word_count": max(0, int(word_budget)),
        "soft_reference_word_count": max(0, int(reference_target_words)),
        "hard_maximum_word_count": max(0, int(hard_max_words)),
        "expected_paragraph_count": max(0, int(expected_paragraphs)),
        "recommended_words_by_paragraph": [
            {
                "paragraph_index": index + 1,
                "soft_reference_words": reference,
                "recommended_words": target,
                "hard_maximum_words": max_words,
            }
            for index, (reference, target, max_words) in enumerate(word_targets)
        ],
    }


def _paragraph_tail(text: str, limit: int = 240) -> str:
    value = re.sub(r"\s+", " ", str(text or "")).strip()
    return value[-limit:].strip() if value else ""


def _markers_for_evidence(evidence_packets: list[EvidencePacket]) -> set[str]:
    """REF markers authorized by a specific set of evidence packets."""
    return {
        marker
        for ep in evidence_packets
        if ep.paper_id
        for marker in {
            ep.paper_id,
            f"{ep.paper_id}:{ep.claim_id}" if ep.claim_id else "",
        }
        if marker
    }


def _allowed_reference_markers(packet: SectionMaterialPacket) -> set[str]:
    """Section-global REF allowlist: evidence packets plus coverage sources."""
    allowed = _markers_for_evidence(packet.evidence_packets)
    allowed.update(
        str(source.get("paper_id") or "")
        for source in (packet.literature_coverage.get("sources") or [])
        if isinstance(source, dict) and source.get("paper_id")
    )
    return allowed


def _compact_evidence_handles_enabled(packet: SectionMaterialPacket) -> bool:
    """Opt-in switch; the default legacy mode stays unchanged."""
    return (
        packet.section_contract.get("writing_mode") == COMPACT_EVIDENCE_HANDLES_MODE
    )


def _packet_level_evidence_items(
    packet: SectionMaterialPacket,
) -> list[EvidencePacket]:
    """Evidence plus chapter-level coverage sources as registry items."""
    items = list(packet.evidence_packets)
    for source in (packet.literature_coverage.get("sources") or []):
        if not isinstance(source, dict) or not source.get("paper_id"):
            continue
        for chunk in (source.get("representative_chunks") or [])[:2]:
            if not isinstance(chunk, dict) or not chunk.get("chunk_id"):
                continue
            items.append(EvidencePacket(
                claim_id="",
                paper_id=str(source.get("paper_id") or ""),
                chunk_id=str(chunk.get("chunk_id") or ""),
                exact_spans=[str(chunk.get("text_preview") or "")],
                support_relation="chapter_literature_context",
                limitations=[
                    "Supports chapter-level context or synthesis; "
                    "precise factual claims require direct verification."
                ],
                evidence_level="fulltext",
                source_kind="fulltext",
                scope_fit="in_domain",
                retrieval_role="literature_context",
                source_title=str(source.get("title") or chunk.get("title") or ""),
            ))
    return items


def _compact_packet_payload(
    packet: SectionMaterialPacket,
) -> tuple[dict[str, Any], EvidenceHandleRegistry]:
    """Build a compact, handle-only model payload for the full section call."""
    registry = EvidenceHandleRegistry(_packet_level_evidence_items(packet))
    payload = _sanitize_compact_payload(packet.to_dict(), registry)
    payload["writing_mode"] = COMPACT_EVIDENCE_HANDLES_MODE
    payload["evidence_handles"] = registry.compact_rows()
    payload["allowed_reference_markers"] = sorted(registry.allowed_markers())
    payload.pop("evidence_packets", None)
    coverage_sources = []
    for source in (packet.literature_coverage.get("sources") or []):
        if not isinstance(source, dict):
            continue
        paper_id = str(source.get("paper_id") or "")
        sanitized_source = _sanitize_compact_payload(source, registry)
        if isinstance(sanitized_source, dict):
            sanitized_source["evidence_handles"] = (
                registry.handles_for_paper(paper_id) if paper_id else []
            )
            coverage_sources.append(sanitized_source)
    payload["literature_coverage"] = {
        "mode": COMPACT_EVIDENCE_HANDLES_MODE,
        "sources": coverage_sources,
    }
    return payload, registry


def _router_result_for_packet(
    packet: SectionMaterialPacket,
    expected_paragraphs: int,
) -> ClaimRoutingResult:
    """Deterministic primary/secondary claim routing for compact writing."""
    evidence_text_by_claim: dict[str, str] = {}
    for ep in packet.evidence_packets:
        claim_id = str(ep.claim_id or "")
        if not claim_id:
            continue
        text = " ".join(
            str(span) for span in (ep.exact_spans or []) if str(span).strip()
        )
        if text:
            evidence_text_by_claim[claim_id] = (
                evidence_text_by_claim.get(claim_id, "") + " " + text
            ).strip()
    return route_section_claims(
        paragraph_functions=list(
            packet.section_contract.get("paragraph_functions") or []
        ),
        argument_sequence=list(
            packet.section_contract.get("argument_sequence") or []
        ),
        claims=packet.claims,
        ready_claim_ids={
            str(claim.get("claim_id") or "")
            for claim in _ready_claims(packet)
        },
        evidence_text_by_claim=evidence_text_by_claim,
        expected_paragraphs=expected_paragraphs,
        section_key_questions=list(
            packet.section_contract.get("key_questions") or []
        ),
    )


_COMPACT_SOURCE_IDENTIFIER_KEYS = {
    "_rejected_chunk_ids",
    "canonical_marker",
    "chunk_id",
    "chunk_ids",
    "doi",
    "evidence_chunk_ids",
    "evidence_provenance",
    "paper_id",
    "paper_ids",
    "provenance",
    "provenance_source",
    "provenance_type",
    "source_id",
    "source_ids",
}


def _is_compact_source_identifier_key(key: Any) -> bool:
    """True for source-identity fields that must never reach the model."""
    normalized = str(key or "").lower().replace("-", "_")
    if normalized in _COMPACT_SOURCE_IDENTIFIER_KEYS:
        return True
    return any(
        normalized.endswith(suffix)
        for suffix in (
            "chunk_id",
            "chunk_ids",
            "paper_id",
            "paper_ids",
            "source_id",
            "source_ids",
            "evidence_provenance",
        )
    )


_SOURCE_LOCATOR_CHUNK_PATTERN = re.compile(
    r"\bChunk\s+[A-Za-z0-9_.-]+:[0-9a-fA-F]{20,}(?::[0-9]{1,6})?\b",
    re.I,
)
_SOURCE_LOCATOR_KIND_PATTERN = re.compile(
    r"\bChunk\s+(?:s2-body|abstract|m3gap|oa-fulltext|openalex|oa|arxiv)"
    r"[: -][A-Za-z0-9_.:-]{6,}\b",
    re.I,
)
_SOURCE_LOCATOR_HEX_PATTERN = re.compile(r"\b[0-9a-fA-F]{20,}\b")


def _redact_source_locators(text: str) -> str:
    """Replace obvious canonical source locators in free text.

    Covers ``Chunk s2-body:<long hex>:0010`` forms, other chunk-kind locators,
    and bare 20+ hex tokens (including misspelled hashes not in the registry).
    Normal scientific numbers and ordinary prose are left untouched.
    """
    value = _SOURCE_LOCATOR_CHUNK_PATTERN.sub(
        "[source locator omitted]", str(text or "")
    )
    value = _SOURCE_LOCATOR_KIND_PATTERN.sub(
        "[source locator omitted]", value
    )
    return _SOURCE_LOCATOR_HEX_PATTERN.sub("[source locator omitted]", value)


def _redact_canonical_identifiers_in_text(
    text: str,
    registry: EvidenceHandleRegistry,
) -> str:
    """Replace known canonical chunk/paper ids inside free text with handles."""
    replacements: list[tuple[str, str]] = []
    for handle in registry.handles:
        entry = registry.entry(handle)
        if entry is None:
            continue
        chunk_id = entry.get("chunk_id") or ""
        paper_id = entry.get("paper_id") or ""
        if chunk_id and chunk_id in text:
            replacements.append((chunk_id, handle))
        if paper_id and paper_id in text:
            replacements.append((paper_id, handle))
    replacements.sort(key=lambda pair: len(pair[0]), reverse=True)
    result = text
    for token, handle in replacements:
        result = result.replace(token, handle)
    return result


def _sanitize_compact_payload(
    value: Any,
    registry: EvidenceHandleRegistry | None = None,
) -> Any:
    """Recursively remove source identifier fields from model-facing data.

    Scientific content (statements, roles, caveats, verified quote/exact text,
    limitations, source titles, evidence levels) is preserved; only canonical
    hash/chunk/source identity fields and identifier-keyed provenance maps are
    dropped, and known canonical ids inside free text are redacted to handles.
    The local registry keeps the exact identity mapping for local resolution
    and diagnostics.
    """
    if isinstance(value, dict):
        return {
            key: _sanitize_compact_payload(item, registry)
            for key, item in value.items()
            if not _is_compact_source_identifier_key(key)
        }
    if isinstance(value, list):
        return [_sanitize_compact_payload(item, registry) for item in value]
    if isinstance(value, str):
        value = _redact_source_locators(value)
        if registry is not None:
            value = _redact_canonical_identifiers_in_text(value, registry)
        return value
    return value


def _resolve_evidence_handle_text(
    text: str,
    registry: EvidenceHandleRegistry | None,
    packet: SectionMaterialPacket,
) -> tuple[str, list[str]]:
    """Resolve compact handles; return (resolved_text, compact_failures).

    In legacy mode the exact deterministic alias normalizer is used and no
    compact failures are produced. In compact mode only registry handles are
    accepted; canonical markers and unknown handles are left unchanged and
    reported as compact failures (fail-closed).
    """
    if registry is None:
        return normalize_reference_markers(text, packet), []
    non_handle = sorted(
        registry.non_handle_reference_markers(text)
        - registry.allowed_markers()
    )
    unknown_handles = sorted(
        registry.handle_markers(text) - registry.allowed_markers()
    )
    malformed_handles = registry.malformed_handle_markers(text)
    compact_failures = []
    if non_handle:
        compact_failures.append(f"non_handle_reference_markers={non_handle}")
    if unknown_handles:
        compact_failures.append(f"unknown_handle_markers={unknown_handles}")
    if malformed_handles:
        compact_failures.append(f"malformed_handle_markers={malformed_handles}")
    resolved, _resolved_handles, _unknown = registry.resolve_text(text)
    residual_brackets = registry.residual_handle_brackets(resolved)
    if residual_brackets:
        compact_failures.append(
            f"unresolved_handle_brackets={residual_brackets}"
        )
    return resolved, compact_failures


def _chunk_tail(chunk_id: str) -> str:
    """Last colon-separated segment of a chunk id when a separator exists."""
    value = str(chunk_id or "").strip()
    if ":" not in value:
        return ""
    tail = value.rsplit(":", 1)[-1].strip()
    return tail


def reference_marker_aliases(
    packet: SectionMaterialPacket,
) -> dict[str, str]:
    """Deterministic alias -> canonical marker map proven by packet evidence.

    Canonical markers remain ``paper_id`` and ``paper_id:claim_id``.  For each
    evidence packet the full ``chunk_id`` and a conservative compact alias
    ``paper_id:<chunk_tail>`` are accepted only when that packet maps the chunk
    to the same paper.  Aliases with more than one canonical target are
    dropped as ambiguous; canonical markers themselves are never remapped.
    """
    aliases: dict[str, str] = {}
    ambiguous: set[str] = set()

    def add(alias: str, canonical: str) -> None:
        if not alias or alias == canonical or alias in ambiguous:
            return
        existing = aliases.get(alias)
        if existing is not None and existing != canonical:
            ambiguous.add(alias)
            aliases.pop(alias, None)
        else:
            aliases[alias] = canonical

    for ep in packet.evidence_packets:
        if not ep.paper_id:
            continue
        canonical = ep.paper_id
        chunk_id = str(ep.chunk_id or "").strip()
        if not chunk_id:
            continue
        add(chunk_id, canonical)
        tail = _chunk_tail(chunk_id)
        if tail:
            add(f"{ep.paper_id}:{tail}", canonical)
    for marker in _allowed_reference_markers(packet):
        aliases.pop(marker, None)
        ambiguous.discard(marker)
    return aliases


def normalize_reference_markers(
    text: str,
    packet: SectionMaterialPacket,
) -> str:
    """Rewrite packet-proven chunk aliases to canonical paper markers.

    Exact deterministic aliases only; approximate or fuzzy paper-id matching
    is intentionally not accepted.
    """
    aliases = reference_marker_aliases(packet)

    def replace(match: re.Match[str]) -> str:
        body = match.group(1)
        canonical = aliases.get(body)
        if canonical is None:
            canonical = body
        return f"[REF:{canonical}]"

    return re.sub(r"\[REF:([^\]]+)\]", replace, str(text or ""))


def _initial_draft_hard_failures(
    text: str,
    *,
    expected_paragraphs: int,
    allowed_markers: set[str],
    hard_max_words: int,
    reference_target_words: int,
) -> list[str]:
    """Hard defects that justify recovery/repair in the initial draft.

    A merely below-reference draft (at least 50% of the soft reference target)
    is deliberately not a failure: the reference is guidance and must never by
    itself trigger extra model calls.  Only empty output, unknown citation
    markers, runaway length over the section-wide hard cap, a gross
    paragraph-structure shortfall, or catastrophic truncation below 50% of the
    soft reference target qualify as hard recovery conditions.
    """
    failures: list[str] = []
    if not str(text or "").strip():
        return ["empty"]
    unknown = set(re.findall(r"\[REF:([^\]]+)\]", text)) - allowed_markers
    if unknown:
        failures.append(f"unknown_reference_markers={sorted(unknown)}")
    words = _word_count(text)
    if words > hard_max_words:
        failures.append(f"word_count={words}>max={hard_max_words}")
    if reference_target_words > 0:
        catastrophic_threshold = max(1, int(reference_target_words * 0.5))
        if words < catastrophic_threshold:
            failures.append(
                f"word_count={words}<50%_reference_target="
                f"{catastrophic_threshold}"
            )
    if expected_paragraphs > 0:
        count = _paragraph_count(text)
        minimum = 1 if expected_paragraphs == 1 else max(
            2, expected_paragraphs - 1
        )
        if count < minimum:
            failures.append(
                f"paragraph_count={count}<expected={expected_paragraphs}"
            )
    return failures


def _ready_claims(packet: SectionMaterialPacket) -> list[dict[str, Any]]:
    """Claims the paragraph writer is permitted to develop."""
    claim_ids_with_evidence = {ep.claim_id for ep in packet.evidence_packets}
    return [
        claim
        for claim in packet.claims
        if claim.get("claim_id")
        and (
            str(claim.get("writing_permission") or "")
            not in {"omit", "evidence_gap_only"}
            or claim.get("claim_id") in claim_ids_with_evidence
        )
    ]


def _assign_claims_to_paragraphs(
    claims: list[dict[str, Any]],
    paragraph_count: int,
) -> list[list[dict[str, Any]]]:
    assigned: list[list[dict[str, Any]]] = [
        [] for _ in range(max(0, paragraph_count))
    ]
    if not assigned:
        return assigned
    for index, claim in enumerate(claims):
        assigned[index % len(assigned)].append(claim)
    return assigned


def _contract_paragraph_claim_assignment(
    packet: SectionMaterialPacket,
    expected_paragraphs: int,
) -> tuple[list[list[dict[str, Any]]], str, list[str]]:
    """Assign ready claims from contract claim_ids, with recorded fallback.

    paragraph_functions entries may be mappings that carry explicit claim_ids;
    argument_sequence entries may carry them as well. Both are honored in the
    exact order they specify, so evidence travels with the subsection that
    actually owns a claim (including claims inserted by supplementary
    revision). Only a fully legacy contract with no usable claim-id mapping
    anywhere falls back to the compatibility round-robin distribution.

    Returns (assigned_claims_by_paragraph, assignment_mode, claim_id_sources).
    """
    functions = list(packet.section_contract.get("paragraph_functions") or [])
    sequence = list(packet.section_contract.get("argument_sequence") or [])
    claims_by_id = {
        str(claim.get("claim_id") or ""): claim
        for claim in packet.claims
        if claim.get("claim_id")
    }
    ready_ids = {
        str(claim.get("claim_id") or "")
        for claim in _ready_claims(packet)
        if claim.get("claim_id")
    }

    def slot_ids(entry: Any) -> list[str]:
        if not isinstance(entry, dict):
            return []
        raw = entry.get("claim_ids") or []
        if not isinstance(raw, (list, tuple, set)):
            return []
        return [str(value) for value in raw if str(value)]

    explicit_ids: list[list[str]] = []
    sources: list[str] = []
    any_mapping = False
    for index in range(expected_paragraphs):
        function = functions[index] if index < len(functions) else None
        ids = list(dict.fromkeys(slot_ids(function)))
        source = "paragraph_functions" if ids else "none"
        if not ids and index < len(sequence):
            ids = list(dict.fromkeys(slot_ids(sequence[index])))
            source = "argument_sequence" if ids else "none"
        explicit_ids.append(ids)
        sources.append(source)
        if ids:
            any_mapping = True

    if any_mapping:
        assigned = [
            [
                claims_by_id[claim_id]
                for claim_id in ids
                if claim_id in claims_by_id and claim_id in ready_ids
            ]
            for ids in explicit_ids
        ]
        return assigned, "contract_claim_ids", sources

    assigned = _assign_claims_to_paragraphs(
        _ready_claims(packet), expected_paragraphs
    )
    return (
        assigned,
        "legacy_round_robin_compat",
        ["compat_round_robin"] * expected_paragraphs,
    )


def _compact_claim_for_paragraph(claim: dict[str, Any]) -> dict[str, Any]:
    return {
        "claim_id": claim.get("claim_id"),
        "statement_for_writing": _compact(
            claim.get("statement_for_writing") or claim.get("statement"), 500
        ),
        "statement": _compact(claim.get("statement"), 300),
        "writing_permission": claim.get("writing_permission", ""),
        "claim_state": claim.get("claim_state", ""),
        "evidence_binding_status": claim.get("evidence_binding_status", ""),
        "supported_components": list(claim.get("supported_components") or [])[:5],
        "missing_evidence_components": list(
            claim.get("missing_evidence_components") or []
        )[:3],
    }


def _paragraph_hard_failures(
    text: str,
    allowed_markers: set[str],
    max_words: int,
) -> list[str]:
    """Hard local failures only; length below reference is not one of them."""
    failures: list[str] = []
    if not str(text or "").strip():
        return ["empty"]
    unknown = set(re.findall(r"\[REF:([^\]]+)\]", text)) - allowed_markers
    if unknown:
        failures.append(f"unknown_reference_markers={sorted(unknown)}")
    words = _word_count(text)
    if words > max_words:
        failures.append(f"word_count={words}>max={max_words}")
    return failures


def _compact_paragraph_payload(
    packet: SectionMaterialPacket,
    *,
    paragraph_index: int,
    paragraph_function: Any,
    assigned_claims: list[dict[str, Any]],
    evidence_packets: list[EvidencePacket],
    allowed_markers: set[str],
    evidence_handles: EvidenceHandleRegistry | None = None,
    secondary_claim_hints: list[dict[str, Any]] | None = None,
    previous_paragraph_text: str,
    previous_paragraph_tail: str,
    word_targets: tuple[int, int, int],
    retry: bool = False,
    previous_attempt: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build the compact per-paragraph payload; never the full material packet."""
    contract = packet.section_contract
    functions = list(contract.get("paragraph_functions") or [])
    argument_sequence = list(contract.get("argument_sequence") or [])
    argument_step = (
        argument_sequence[paragraph_index]
        if paragraph_index < len(argument_sequence)
        else ""
    )
    reference_words, target_words, max_words = word_targets
    payload = {
        "section_identity": {
            "section_id": packet.section_id,
            "title": contract.get("title", ""),
            "argument_role": contract.get("argument_role", ""),
            "section_purpose": contract.get("section_purpose", ""),
            "central_thesis": contract.get("central_thesis", ""),
            "key_questions": list(contract.get("key_questions") or [])[:3],
            "novel_contribution_to_review": contract.get(
                "novel_contribution_to_review", ""
            ),
            "argument_step": argument_step,
        },
        "guardrails": {
            "forbidden_overclaims": list(
                contract.get("forbidden_overclaims") or []
            ),
            "scope_guardrails": list(contract.get("scope_guardrails") or []),
            "open_questions": list(contract.get("open_questions") or []),
            "transition_from_previous": _compact(
                packet.transition_contract.get("transition_from_previous"), 300
            ),
            "transition_to_next": _compact(
                packet.transition_contract.get("transition_to_next"), 300
            ),
            "previous_section_tail": _compact(
                packet.manuscript_context.get("previous_section_tail"), 500
            ),
        },
        "paragraph": {
            "index": paragraph_index,
            "function": paragraph_function,
            "previous_paragraph_function": (
                functions[paragraph_index - 1] if paragraph_index > 0 else ""
            ),
            "next_paragraph_function": (
                functions[paragraph_index + 1]
                if paragraph_index + 1 < len(functions)
                else ""
            ),
            "previous_paragraph_text": previous_paragraph_text,
            "previous_accepted_paragraph_tail": previous_paragraph_tail,
        },
        "assigned_claims": [
            _compact_claim_for_paragraph(claim) for claim in assigned_claims
        ],
        "word_targets": {
            "reference_words": reference_words,
            "target_words": target_words,
            "max_words": max_words,
        },
        "retry": retry,
        "previous_attempt": previous_attempt,
    }
    if evidence_handles is not None:
        payload = _sanitize_compact_payload(payload, None)
        payload["writing_mode"] = COMPACT_EVIDENCE_HANDLES_MODE
        payload["evidence_handles"] = evidence_handles.compact_rows()
        payload["allowed_reference_markers"] = sorted(
            evidence_handles.allowed_markers()
        )
        if secondary_claim_hints:
            payload["secondary_claim_hints"] = [
                _compact_claim_for_paragraph(claim)
                for claim in secondary_claim_hints
            ]
    else:
        payload["evidence_packets"] = [ep.to_dict() for ep in evidence_packets]
        payload["allowed_reference_markers"] = sorted(allowed_markers)
    return payload


def _normalize_compound_reference_markers(text: str) -> str:
    """Split accidental multi-source REF brackets into canonical markers.

    Models occasionally emit ``[REF:paper-a; REF:paper-b]`` even though the
    protocol requires one marker per source.  Without normalization the binder
    sees one nonexistent identifier and rejects both valid sources.
    """

    def replace(match: re.Match[str]) -> str:
        body = match.group(1).strip()
        parts = [
            part.removeprefix("REF:").strip()
            for part in re.split(r"\s*;\s*(?=REF:)", body)
            if part.strip()
        ]
        if len(parts) <= 1:
            return match.group(0)
        return " ".join(f"[REF:{part}]" for part in parts)

    return re.sub(r"\[REF:([^\]]+)\]", replace, str(text or ""))


def _drop_sentences_with_unknown_references(
    text: str,
    unknown_markers: set[str],
) -> tuple[str, list[str]]:
    """Fail closed locally instead of discarding an otherwise valid rewrite.

    A generated proposition attached to an unknown source cannot be retained
    as fact.  Removing the complete sentence is safer than merely deleting the
    marker, while preserving unrelated grounded paragraphs from an expensive
    section-level repair.
    """
    if not text or not unknown_markers:
        return text, []
    removed: list[str] = []
    rebuilt_paragraphs: list[str] = []
    for paragraph in re.split(r"\n\s*\n", str(text)):
        sentences = re.split(r"(?<=[.!?])\s+", paragraph.strip())
        kept: list[str] = []
        for sentence in sentences:
            markers = set(re.findall(r"\[REF:([^\]]+)\]", sentence))
            if markers & unknown_markers:
                removed.append(sentence.strip())
            elif sentence.strip():
                kept.append(sentence.strip())
        if kept:
            rebuilt_paragraphs.append(" ".join(kept))
    return "\n\n".join(rebuilt_paragraphs).strip(), removed


def remove_broken_visual_promises(draft: "SectionDraft") -> "SectionDraft":
    """Remove prose that promises a figure when no usable asset exists.

    Visual planning is intentionally non-blocking: missing figures must not
    delete useful scientific prose.  Conversely, the manuscript must not tell
    readers that a diagram "is presented" when every candidate for that
    section is still missing or rejected.
    """
    usable = any(
        str(row.get("local_image_path") or "")
        and str(row.get("asset_status") or "") != "missing_required_visual"
        and Path(str(row.get("local_image_path") or "")).exists()
        for row in (draft.figure_placements or [])
        if isinstance(row, dict)
    )
    if usable or not draft.english_text:
        return draft
    promise = re.compile(
        r"\b(?:figure|diagram|schematic|plot|chart)\b.*?"
        r"\b(?:is|are)\s+(?:presented|shown|provided|included|displayed)\b",
        re.I,
    )
    removed: list[str] = []
    rebuilt: list[str] = []
    for paragraph in re.split(r"\n\s*\n", draft.english_text):
        sentences = re.split(r"(?<=[.!?])\s+", paragraph.strip())
        kept = []
        for sentence in sentences:
            if promise.search(sentence):
                removed.append(sentence.strip())
            elif sentence.strip():
                kept.append(sentence.strip())
        if kept:
            rebuilt.append(" ".join(kept))
    if removed:
        draft.english_text = "\n\n".join(rebuilt).strip()
        draft.revision_history.append({
            "stage": "visual_promise_safety",
            "reason": "No approved or verified local visual asset was available.",
            "removed_sentences": removed,
        })
    return draft


# --------------------------------------------------------------------------- #
# Data structures
# --------------------------------------------------------------------------- #

@dataclass
class EvidencePacket:
    """Compact evidence unit for one claim in a section material packet."""
    claim_id: str
    paper_id: str
    chunk_id: str = ""   # canonical KB chunk identifier
    exact_spans: list[str] = field(default_factory=list)
    visual_refs: list[str] = field(default_factory=list)
    support_relation: str = "component_support"
    limitations: list[str] = field(default_factory=list)
    evidence_level: str = "fulltext"
    source_kind: str = "fulltext"
    scope_fit: str = "in_domain"
    retrieval_role: str = "evidence_candidate"
    source_title: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "claim_id": self.claim_id,
            "chunk_id": self.chunk_id,
            "paper_id": self.paper_id,
            "exact_spans": self.exact_spans,
            "visual_refs": self.visual_refs,
            "support_relation": self.support_relation,
            "limitations": self.limitations,
            "evidence_level": self.evidence_level,
            "source_kind": self.source_kind,
            "scope_fit": self.scope_fit,
            "retrieval_role": self.retrieval_role,
            "source_title": self.source_title,
        }


@dataclass
class SectionMaterialPacket:
    """Compact input for SectionWriter — never embeds full 5MB blueprint."""
    section_id: str
    section_contract: dict[str, Any] = field(default_factory=dict)
    claims: list[dict[str, Any]] = field(default_factory=list)
    evidence_packets: list[EvidencePacket] = field(default_factory=list)
    contradictions: list[dict[str, Any]] = field(default_factory=list)
    open_questions: list[str] = field(default_factory=list)
    transition_contract: dict[str, Any] = field(default_factory=dict)
    uncited_load_bearing_claim_ids: list[str] = field(default_factory=list)
    visual_evidence: list[dict[str, Any]] = field(default_factory=list)
    visual_gap_plan: list[dict[str, Any]] = field(default_factory=list)
    manuscript_context: dict[str, Any] = field(default_factory=dict)
    literature_coverage: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "section_id": self.section_id,
            "section_contract": self.section_contract,
            "claims": self.claims,
            "evidence_packets": [ep.to_dict() for ep in self.evidence_packets],
            "contradictions": self.contradictions,
            "open_questions": self.open_questions,
            "transition_contract": self.transition_contract,
            "uncited_load_bearing_claim_ids": self.uncited_load_bearing_claim_ids,
            "visual_evidence": self.visual_evidence,
            "visual_gap_plan": self.visual_gap_plan,
            "manuscript_context": self.manuscript_context,
            "literature_coverage": self.literature_coverage,
        }


@dataclass
class SectionDraft:
    section_id: str
    english_text: str = ""
    chinese_text: str = ""
    citation_map: dict[str, list[str]] = field(default_factory=dict)  # sentence_idx → [chunk_ids]
    overclaim_flags: list[dict[str, Any]] = field(default_factory=list)
    contradiction_notes: list[str] = field(default_factory=list)
    figure_placements: list[dict[str, Any]] = field(default_factory=list)
    status: str = "draft"  # draft|cited|audited|edited|final
    uncited_load_bearing: list[str] = field(default_factory=list)  # claim_ids without evidence
    revision_history: list[dict[str, Any]] = field(default_factory=list)


# --------------------------------------------------------------------------- #
# Shared deterministic citation-support validator
# --------------------------------------------------------------------------- #

_CITATION_SUPPORT_STOPWORDS = {
    "about", "after", "again", "against", "also", "among", "because", "been",
    "before", "between", "could", "from", "further", "however", "into", "more",
    "other", "over", "paper", "reported", "research", "results", "section", "show",
    "shown", "study", "than", "that", "their", "therefore", "these", "those", "through",
    "under", "using", "were", "which", "while", "with", "would",
}


def _citation_support_tokens(text: str) -> set[str]:
    plain = re.sub(r"\[REF:[^\]]+\]", "", str(text or "")).lower()
    return {
        token for token in re.findall(r"[a-z][a-z0-9-]{2,}", plain)
        if token not in _CITATION_SUPPORT_STOPWORDS
    }


_ASSERTED_NUMBER_PATTERN = re.compile(
    r"(?<![A-Za-z0-9])\d+(?:\.\d+)?(?![A-Za-z0-9])"
)
_UNIT_ATTACHED_NUMBER_PATTERN = re.compile(
    r"(?<![A-Za-z0-9])(\d+(?:\.\d+)?)(?=[A-Za-z])"
)
_EXPLICIT_NUMERIC_UNIT_PATTERN = re.compile(
    r"(?:nm|um|µm|μm|mm|cm|m|fs|ps|ns|s|nJ|uJ|µJ|μJ|mJ|J|"
    r"eV|meV|keV|Hz|kHz|MHz|GHz|THz|K|°C|mW|W|kW|MW|"
    r"dB|mV|V|mA|A|Pa|kPa|MPa|GPa|%)\b",
    re.I,
)


_SUPERSCRIPT_TRANS = str.maketrans("⁰¹²³⁴⁵⁶⁷⁸⁹", "0123456789")
_SOURCE_CARET_EXPONENT_RE = re.compile(
    r"(?<![A-Za-z])(\d+(?:\.\d+)?)\s*(?:\^|\*\*)\s*"
    r"([0-9⁰¹²³⁴⁵⁶⁷⁸⁹]+)"
)
_SOURCE_SUPERSCRIPT_RE = re.compile(
    r"(?<![A-Za-z])(\d+(?:\.\d+)?)([⁰¹²³⁴⁵⁶⁷⁸⁹]+)"
)
_TARGET_CARET_EXPONENT_RE = re.compile(
    r"(\d+(?:\.\d+)?)\s*\^\s*([0-9⁰¹²³⁴⁵⁶⁷⁸⁹]+)"
)
_TARGET_SUPERSCRIPT_RE = re.compile(
    r"(\d+(?:\.\d+)?)([⁰¹²³⁴⁵⁶⁷⁸⁹]+)"
)
_CJK_EXPONENT_NUMERAL_RE = re.compile(
    r"([0-9⁰¹²³⁴⁵⁶⁷⁸⁹零一二三四五六七八九十百]+)\s*次方"
)
_CHINESE_NUMERALS = {
    "零": 0, "一": 1, "二": 2, "三": 3, "四": 4,
    "五": 5, "六": 6, "七": 7, "八": 8, "九": 9,
    "十": 10, "百": 100, "千": 1000,
    "万": 10000, "亿": 100000000,
}


def _normalize_digits(value: str) -> str:
    return value.translate(_SUPERSCRIPT_TRANS)


def _parse_chinese_numeral(value: str) -> Optional[str]:
    """Parse small Chinese numerals (0-99) to their decimal string."""
    normalized = _normalize_digits(value.strip())
    if normalized.isdigit():
        return str(int(normalized))
    if not normalized:
        return None
    total = 0
    section = 0
    for char in normalized:
        if char.isdigit():
            section = section * 10 + int(char)
            continue
        if char not in _CHINESE_NUMERALS:
            return None
        digit = _CHINESE_NUMERALS[char]
        if digit in {10000, 100000000}:
            if not section and total == 0:
                section = 1
            total = (total + section) * digit
            section = 0
        elif digit in {10, 100, 1000}:
            total += (section or 1) * digit
            section = 0
        else:
            section = digit
    return str(total + section)


def _source_exponent_values(text: str) -> list[tuple[str, str]]:
    """Return (base, exponent) pairs for explicit exponent expressions."""
    entries: list[tuple[str, str]] = []
    for match in _SOURCE_CARET_EXPONENT_RE.finditer(str(text or "")):
        exponent = _normalize_digits(match.group(2))
        if exponent.isdigit():
            entries.append((match.group(1), str(int(exponent))))
    for match in _SOURCE_SUPERSCRIPT_RE.finditer(str(text or "")):
        exponent = _normalize_digits(match.group(2))
        if exponent.isdigit():
            entries.append((match.group(1), str(int(exponent))))
    return entries


def _target_exponent_value(text: str, base: str) -> Optional[str]:
    """Extract the translated exponent value for one base literal, if any."""
    for match in _TARGET_CARET_EXPONENT_RE.finditer(str(text or "")):
        if match.group(1) == base:
            exponent = _normalize_digits(match.group(2))
            if exponent.isdigit():
                return str(int(exponent))
    for match in _TARGET_SUPERSCRIPT_RE.finditer(str(text or "")):
        if match.group(1) == base:
            exponent = _normalize_digits(match.group(2))
            if exponent.isdigit():
                return str(int(exponent))
    for match in _CJK_EXPONENT_NUMERAL_RE.finditer(str(text or "")):
        exponent = _parse_chinese_numeral(match.group(1))
        if exponent is not None:
            return exponent
    return None


def _translated_exponent_literals_covered(
    source: str,
    target: str,
    target_numbers: list[str],
) -> set[str]:
    """Exponent digits faithfully represented semantically may be relaxed.

    ``10^4`` is one exponent quantity, not two unrelated prose literals.  When
    the translation keeps the base literal and represents the same exponent
    value with a superscript, caret, or Chinese ``次方`` phrase, the exponent
    digit is removed from the strict comparison.  If the exponent is changed,
    omitted, or the base is dropped, nothing is relaxed and the mismatch stays
    fail-closed.
    """
    target_set = set(target_numbers)
    covered: set[str] = set()
    for base, exponent in _source_exponent_values(source):
        if base not in target_set:
            continue
        target_exponent = _target_exponent_value(target, base)
        if (
            target_exponent == exponent
            and exponent not in target_set
        ):
            covered.add(exponent)
    return covered


_CHINESE_MAGNITUDE_RE = re.compile(
    r"([0-9⁰¹²³⁴⁵⁶⁷⁸⁹零一二三四五六七八九十百千万亿]{1,8})"
)
_CHINESE_MAGNITUDE_SPLIT_RE = re.compile(
    r"^(\d+)([零一二三四五六七八九十百千万亿]+)$"
)
_CHINESE_MAGNITUDE_SPACED_RE = re.compile(
    r"(\d+(?:\.\d+)?)\s*([零一二三四五六七八九十百千万亿]{1,6})"
)


def _is_power_of_ten(value: int) -> bool:
    if value <= 0:
        return False
    exponent = round(math.log10(value))
    return 10 ** exponent == value


def _chinese_magnitude_quantities(
    text: str,
    literals: set[str],
) -> tuple[set[int], set[str]]:
    """Return explicit Chinese power-of-ten magnitudes and covered bases.

    ``万`` (10^4), ``亿`` (10^8), and composite forms such as ``十万``,
    ``100万``, or ``万亿`` are canonicalized to their decimal value when the
    phrase is exactly a power of ten.  A matching ASCII base prefix (for
    example ``10`` in ``10万``) is removed from the strict literal set so the
    quantity is not double-counted.
    """
    values: set[int] = set()
    covered_literals: set[str] = set()
    spaced_spans: list[tuple[int, int]] = []
    for match in _CHINESE_MAGNITUDE_SPACED_RE.finditer(str(text or "")):
        base = match.group(1)
        suffix = match.group(2)
        if "万" not in suffix and "亿" not in suffix:
            continue
        parsed = _parse_chinese_numeral(base + suffix)
        if parsed is None:
            continue
        value = int(parsed)
        if value < 10000 or not _is_power_of_ten(value):
            continue
        values.add(value)
        if base in literals:
            covered_literals.add(base)
        spaced_spans.append((match.start(), match.end()))
    for match in _CHINESE_MAGNITUDE_RE.finditer(str(text or "")):
        if any(
            start <= match.start() and match.end() <= end
            for start, end in spaced_spans
        ):
            continue
        phrase = match.group(1)
        if "万" not in phrase and "亿" not in phrase:
            continue
        parsed = _parse_chinese_numeral(phrase)
        if parsed is None:
            continue
        value = int(parsed)
        if value < 10000 or not _is_power_of_ten(value):
            continue
        values.add(value)
        split = _CHINESE_MAGNITUDE_SPLIT_RE.match(phrase)
        if (
            split
            and split.group(1) in literals
            and ("万" in split.group(2) or "亿" in split.group(2))
        ):
            covered_literals.add(split.group(1))
    return values, covered_literals


def _canonical_numeric_view(
    text: str,
    covered_literals: set[str] | None = None,
) -> tuple[list[str], list[int]]:
    """Canonicalize exact power-of-ten quantities into decimal values.

    Prose literals stay strict.  Explicit exponent forms with base ``10``
    (caret, superscript, or Chinese ``次方``) and explicit Chinese magnitudes
    (``万``/``亿`` families) become one canonical decimal value, so a faithful
    translation such as ``10^4`` -> ``一万`` or ``万`` is equivalent while an
    omitted or changed quantity remains a mismatch.
    """
    literals = set(_extract_asserted_numeric_literals(text))
    remove = set(covered_literals or ())
    quantities: set[int] = set()

    def add_power(base: str, exponent: str) -> None:
        if base == "10" and exponent.isdigit():
            quantities.add(10 ** int(exponent))
            remove.add(base)
            remove.add(exponent)

    for base, exponent in _source_exponent_values(text):
        add_power(base, exponent)
    for match in _TARGET_CARET_EXPONENT_RE.finditer(text):
        add_power(match.group(1), _normalize_digits(match.group(2)))
    for match in _TARGET_SUPERSCRIPT_RE.finditer(text):
        add_power(match.group(1), _normalize_digits(match.group(2)))
    for match in _CJK_EXPONENT_NUMERAL_RE.finditer(text):
        exponent = _parse_chinese_numeral(match.group(1))
        if exponent is not None and exponent.isdigit() and "10" in literals:
            quantities.add(10 ** int(exponent))
            remove.add(exponent)
            remove.add("10")
    magnitude_values, magnitude_bases = _chinese_magnitude_quantities(
        text, literals
    )
    quantities.update(magnitude_values)
    remove.update(magnitude_bases)
    return sorted(set(literals) - remove), sorted(quantities)


def _extract_asserted_numeric_literals(text: str) -> set[str]:
    """Return prose or measurement numbers that require exact support.

    LaTeX equations routinely contain symbolic indices and exponents such as
    ``E_0``, ``H_2`` or ``1/N``.  Treating those glyphs as measured values
    created false blocking audit failures.  Numbers inside math spans are
    therefore ignored unless the same span explicitly carries a scientific
    unit or percentage.  Numbers in prose remain strict.
    """

    value = str(text or "")
    math_spans: list[tuple[int, int, str]] = []
    for match in re.finditer(r"\$(?:\\.|[^$])+\$", value):
        math_spans.append((match.start(), match.end(), match.group(0)))

    asserted: set[str] = set()
    for match in _ASSERTED_NUMBER_PATTERN.finditer(value):
        containing = next(
            (
                span_text
                for start, end, span_text in math_spans
                if start <= match.start() < end
            ),
            None,
        )
        if containing is not None and not _EXPLICIT_NUMERIC_UNIT_PATTERN.search(
            containing
        ):
            continue
        asserted.add(match.group(0))
    for match in _UNIT_ATTACHED_NUMBER_PATTERN.finditer(value):
        rest = value[match.end():]
        if not _EXPLICIT_NUMERIC_UNIT_PATTERN.match(rest):
            continue
        containing = next(
            (
                span_text
                for start, end, span_text in math_spans
                if start <= match.start() < end
            ),
            None,
        )
        if containing is not None and not _EXPLICIT_NUMERIC_UNIT_PATTERN.search(
            containing
        ):
            continue
        asserted.add(match.group(1))
    return asserted


def assess_citation_support(
    sentence: str,
    evidence_texts: list[str],
    *,
    multi_source_synthesis: bool = False,
) -> tuple[str, str, float]:
    """Judge whether local passages support a cited sentence.

    This is the deterministic, fail-closed portion of the mature
    :class:`CitationBinder` entailment policy.  It checks exact numeric values,
    lexical coverage, and unsupported causal language. It intentionally does
    not demand citations for uncited background or synthesis sentences; it only
    evaluates sentences that actually carry a ``[REF:*]`` marker.
    """
    plain = re.sub(r"\[REF:[^\]]+\]", "", str(sentence or "")).strip()
    evidence = " ".join(str(item or "") for item in evidence_texts).strip()
    if not plain or not evidence:
        return "not_entailed", "No local evidence text was available for the cited sentence.", 0.0

    sentence_numbers = _extract_asserted_numeric_literals(plain)
    evidence_numbers = _extract_asserted_numeric_literals(evidence)
    missing_numbers = sorted(sentence_numbers - evidence_numbers)
    if missing_numbers:
        return (
            "not_entailed",
            "Numeric value(s) absent from the cited chunk text: " + ", ".join(missing_numbers),
            0.0,
        )

    sentence_tokens = _citation_support_tokens(plain)
    evidence_tokens = _citation_support_tokens(evidence)
    overlap = sentence_tokens & evidence_tokens
    coverage = len(overlap) / max(1, len(sentence_tokens))

    # A high aggregate overlap can hide an unsupported second assertion, for
    # example "MoS2 shows saturable absorption and cures cancer".  Audit each
    # substantive coordinated clause before accepting the sentence as a
    # whole.  This stays deliberately conservative: very short connector
    # fragments are ignored, while a clause with at least two content tokens
    # must contribute some evidence overlap of its own.
    coordinated_clauses = re.split(
        r"\s+(?:and|but|whereas|while)\s+|\s*;\s*",
        plain,
        flags=re.I,
    )
    if len(coordinated_clauses) > 1:
        zero_overlap_clauses: list[str] = []
        weak_clauses: list[str] = []
        for clause in coordinated_clauses:
            clause_tokens = _citation_support_tokens(clause)
            if len(clause_tokens) < 2:
                continue
            clause_overlap = clause_tokens & evidence_tokens
            clause_coverage = len(clause_overlap) / len(clause_tokens)
            if not clause_overlap:
                zero_overlap_clauses.append(clause.strip())
            elif clause_coverage < 0.25:
                weak_clauses.append(clause.strip())
        if zero_overlap_clauses:
            return (
                "not_entailed",
                "Coordinated clause has zero evidence overlap with the cited chunk text: "
                + zero_overlap_clauses[0][:120],
                coverage,
            )
        if weak_clauses:
            return (
                "not_entailed",
                "A coordinated assertion is unsupported by the cited chunk text: "
                + weak_clauses[0][:120],
                coverage,
            )

    # A narrow, predicate-scoped negation check catches direct reversals such
    # as "does not exhibit" versus "exhibits" without treating any unrelated
    # "not" elsewhere in a long scientific passage as a contradiction.
    def predicate_stem(token: str) -> str:
        value = token.lower()
        if value.endswith("ing") and len(value) > 5:
            return value[:-3]
        if value.endswith("ed") and len(value) > 4 and not value.endswith("eed"):
            return value[:-2]
        if value.endswith("es") and len(value) > 4:
            return value[:-2]
        if value.endswith("s") and len(value) > 3:
            return value[:-1]
        return value

    def negated_predicates(value: str) -> set[str]:
        stems: set[str] = set()
        for match in re.finditer(
            r"\b(?:do|does|did|can|could|is|are|was|were|has|have|had)?\s*"
            r"(?:not|never|cannot|can't)\s+([a-z][a-z-]{2,})",
            value,
            re.I,
        ):
            stems.add(predicate_stem(match.group(1)))
        return stems

    sentence_negated = negated_predicates(plain)
    evidence_negated = negated_predicates(evidence)
    for stem in sentence_negated - evidence_negated:
        if re.search(rf"\b{re.escape(stem)}(?:s|es|ed|ing)?\b", evidence, re.I):
            return "not_entailed", "The sentence negates a predicate asserted by the cited evidence.", coverage

    causal = re.compile(r"\b(?:causes?|caused|drives?|driven by|results? in|arises? from|because of|leads? to)\b", re.I)
    if causal.search(plain) and not causal.search(evidence):
        return "not_entailed", "The sentence asserts causality that the cited passage does not state.", coverage

    # Do not infer contradiction from bag-of-words polarity or direction. A
    # scientific passage often contains both "higher" and "lower", or uses a
    # positive paraphrase for a source's negative construction. Without
    # subject-predicate alignment those checks create more false rejections
    # than protection. Numeric identity, clause coverage, causality and local
    # relevance remain deterministic; semantic contradiction belongs to the
    # reviewer/arbiter stage.

    minimum_overlap = 2 if len(sentence_tokens) >= 4 else 1
    # Scientific review prose normally paraphrases and combines terminology
    # rather than copying a source sentence. Numeric identity, ownership,
    # polarity, causality and coordinated-clause checks above remain strict;
    # lexical overlap is therefore a last-resort relevance floor, not an
    # exact-wording requirement.
    threshold = 0.28 if multi_source_synthesis else 0.32
    if len(overlap) < minimum_overlap or coverage < threshold:
        return (
            "not_entailed",
            f"Local evidence overlap is insufficient ({coverage:.2f}); the citation does not independently support the claim.",
            coverage,
        )
    return "entailed", f"Supported by local chunk text (coverage={coverage:.2f}).", coverage


# --------------------------------------------------------------------------- #
# SectionMaterialMapper
# --------------------------------------------------------------------------- #

class SectionMaterialMapper:
    """Maps a blueprint section to a compact SectionMaterialPacket.

    Reads claim evidence from the section dict; does NOT embed raw chunk text.
    Only includes:
    - section_contract: id, title, argument_role, key_questions, scope_guardrails
    - compact claim summaries (id, statement, evidence_type, claim_state, saturation)
    - evidence packets built from supporting_text_chunk_ids and evidence_relations
    """

    def __init__(self, kb_path: Path | None = None) -> None:
        self.kb_path = Path(kb_path) if kb_path else None

    def _resolve_sqlite(self) -> Path | None:
        """Resolve either a direct SQLite file or a KB directory."""
        if not self.kb_path or not self.kb_path.exists():
            return None
        if self.kb_path.is_file():
            return self.kb_path
        preferred = (
            self.kb_path / "review_knowledge_base.sqlite",
            self.kb_path / "knowledge_base.sqlite",
        )
        for candidate in preferred:
            if candidate.exists():
                return candidate
        candidates = sorted(self.kb_path.glob("*.sqlite")) + sorted(self.kb_path.glob("*.db"))
        return candidates[0] if candidates else None

    def _load_chunk_records(self, chunk_ids: list[str]) -> dict[str, dict[str, str]]:
        """Batch-load canonical chunk identity and text from KB SQLite."""
        sqlite_path = self._resolve_sqlite()
        if sqlite_path is None or not chunk_ids:
            return {}
        try:
            import sqlite3
            con = sqlite3.connect(str(sqlite_path))
            try:
                placeholders = ",".join("?" for _ in chunk_ids)
                try:
                    rows = con.execute(
                        f"SELECT t.chunk_id,t.paper_id,t.title,t.text,t.evidence_level,t.source_kind,"
                        f"t.provenance_json,t.raw_json,p.raw_json "
                        f"FROM text_chunks t LEFT JOIN papers p ON p.paper_id=t.paper_id "
                        f"WHERE t.chunk_id IN ({placeholders})",
                        chunk_ids,
                    ).fetchall()
                except Exception:
                    # Compatibility with lightweight fixtures and legacy KBs.
                    rows = con.execute(
                        f"SELECT chunk_id,paper_id,'' AS title,text,'fulltext' AS evidence_level,"
                        f"'fulltext' AS source_kind,'{{}}' AS provenance_json,'{{}}' AS raw_json,"
                        f"'{{}}' AS paper_raw_json FROM text_chunks WHERE chunk_id IN ({placeholders})",
                        chunk_ids,
                    ).fetchall()
                def parsed_dict(value: Any) -> dict[str, Any]:
                    try:
                        parsed = json.loads(str(value or "{}"))
                        return parsed if isinstance(parsed, dict) else {}
                    except Exception:
                        return {}

                def source_policy(row: tuple[Any, ...]) -> tuple[str, str, bool]:
                    provenance = parsed_dict(row[6])
                    chunk_raw = parsed_dict(row[7])
                    paper_raw = parsed_dict(row[8])
                    updates = paper_raw.get("m3_real_oa_updates")
                    candidate = updates[-1] if isinstance(updates, list) and updates else paper_raw
                    if not isinstance(candidate, dict):
                        candidate = {}
                    scope = str(
                        provenance.get("scope_fit")
                        or chunk_raw.get("scope_fit")
                        or candidate.get("llm_scope_fit")
                        or ""
                    ).strip().lower()
                    role = str(
                        provenance.get("retrieval_role")
                        or chunk_raw.get("retrieval_role")
                        or candidate.get("llm_retrieval_role")
                        or ""
                    ).strip().lower()
                    grade = str(candidate.get("llm_relevance_grade") or "").strip().lower()
                    support = str(candidate.get("llm_support_status") or "").strip().lower()
                    if scope not in {"in_domain", "cross_domain_analogy", "off_domain"} or role not in {
                        "evidence_candidate", "method_transfer", "background_only", "reject"
                    }:
                        if grade in {"direct", "strong", "strongly_relevant", "partial"}:
                            scope, role = "in_domain", "evidence_candidate"
                        elif grade in {"adjacent", "related", "background"}:
                            scope, role = "cross_domain_analogy", "method_transfer"
                        elif str(row[5] or "") in {"fulltext", "abstract"} and not grade:
                            # Curated upstream/core chunks predate M3 provenance.
                            scope, role = "in_domain", "evidence_candidate"
                        else:
                            scope, role = "off_domain", "reject"
                    if scope == "off_domain" or role == "reject":
                        return "off_domain", "reject", False
                    if scope == "cross_domain_analogy" and role == "evidence_candidate":
                        role = "method_transfer"
                    if support in {"not_established", "unsupported"} and role == "evidence_candidate":
                        role = "background_only"
                    allowed = scope == "in_domain" and role == "evidence_candidate"
                    return scope, role, allowed

                records: dict[str, dict[str, str]] = {}
                for row in rows:
                    if not row[0]:
                        continue
                    scope, role, allowed = source_policy(row)
                    records[str(row[0])] = {
                        "paper_id": str(row[1] or ""),
                        "title": _compact(row[2], 300),
                        "text": _compact(row[3], 1200),
                        "evidence_level": str(row[4] or "fulltext"),
                        "source_kind": str(row[5] or "fulltext"),
                        "scope_fit": scope,
                        "retrieval_role": role,
                        "factual_support_allowed": "true" if allowed else "false",
                    }
                return records
            finally:
                con.close()
        except Exception:
            return {}

    def _load_visual_records(self, chunk_ids: list[str]) -> list[dict[str, Any]]:
        sqlite_path = self._resolve_sqlite()
        if sqlite_path is None or not chunk_ids:
            return []
        try:
            import sqlite3
            con = sqlite3.connect(str(sqlite_path))
            try:
                placeholders = ",".join("?" for _ in chunk_ids)
                rows = con.execute(
                    f"SELECT chunk_id, paper_id, title, caption, local_image_path, "
                    f"visual_argument_type, visual_argument_claim FROM visual_chunks "
                    f"WHERE chunk_id IN ({placeholders})",
                    chunk_ids,
                ).fetchall()
                by_id = {
                    str(row[0]): {
                        "chunk_id": str(row[0]),
                        "paper_id": str(row[1] or ""),
                        "title": _compact(row[2], 180),
                        "caption": _compact(row[3], 700),
                        "local_image_path": str(row[4] or ""),
                        "visual_argument_type": str(row[5] or ""),
                        "visual_argument_claim": _compact(row[6], 400),
                    }
                    for row in rows if row[0]
                }
                return [by_id[cid] for cid in chunk_ids if cid in by_id]
            finally:
                con.close()
        except Exception:
            return []

    def map(
        self,
        section: dict[str, Any],
        *,
        max_evidence_spans: int = 6,
        include_contradictions: bool = True,
    ) -> SectionMaterialPacket:
        section_id = str(section.get("section_id") or "")
        source_contract = (
            dict(section.get("section_contract") or {})
            if isinstance(section.get("section_contract"), dict)
            else {}
        )
        try:
            word_budget = max(0, int(source_contract.get("word_budget") or 0))
        except (TypeError, ValueError):
            word_budget = 0
        section_contract = {
            "section_id": section_id,
            "title": _compact(section.get("title"), 180),
            "argument_role": _compact(section.get("argument_role"), 400),
            "section_purpose": _compact(source_contract.get("section_purpose"), 700),
            "central_thesis": _compact(source_contract.get("central_thesis"), 1000),
            "key_questions": list(section.get("key_questions") or [])[:5],
            "argument_sequence": list(source_contract.get("argument_sequence") or [])[:10],
            "paragraph_functions": list(source_contract.get("paragraph_functions") or [])[:12],
            "required_claim_kinds": list(section.get("required_claim_kinds") or [])[:10],
            "required_evidence_roles": list(
                source_contract.get("required_evidence_roles")
                or section.get("required_claim_kinds")
                or []
            )[:10],
            "scope_guardrails": list(section.get("scope_guardrails") or []),
            "forbidden_overclaims": list(
                source_contract.get("forbidden_overclaims")
                or section.get("scope_guardrails")
                or []
            )[:10],
            "novel_contribution_to_review": _compact(section.get("novel_contribution_to_review"), 300),
            "expected_visual_arguments": list(section.get("expected_visual_arguments") or []),
            "open_questions": list(source_contract.get("open_questions") or [])[:8],
            "word_budget": word_budget,
        }

        claims = list(section.get("claims") or [])
        compact_claims = []
        for c in claims:
            requirement = str(c.get("evidence_requirement") or "factual")
            binding = str(c.get("evidence_binding_status") or "")
            state = str(c.get("claim_state") or "planned")
            missing_components = list(c.get("missing_evidence_components") or [])[:6]
            supported_rewrite = _compact(c.get("supported_rewrite"), 500)
            supported_components = [
                _compact(row.get("component"), 260)
                for row in (c.get("evidence_component_map") or [])[:8]
                if isinstance(row, dict) and _compact(row.get("component"), 260)
            ]
            interpretive_synthesis = bool(
                c.get("load_bearing")
                and str(c.get("section_fit") or "") == "central"
                and binding == "partial"
                and any(
                    "synthesis" in str(item).lower()
                    and any(term in str(item).lower() for term in ("not explicit", "not stated", "framing"))
                    for item in missing_components
                )
            )
            if requirement == "none" or state == "dropped":
                permission = "omit"
            elif requirement == "normative":
                permission = "recommendation_only"
            elif requirement == "open_question" or state == "open_question":
                permission = "open_question_only"
            elif interpretive_synthesis:
                permission = "interpretive_synthesis"
            elif binding in {"direct", "synthesized"}:
                permission = "factual_assertion"
            elif binding == "partial":
                permission = "hedged_factual_assertion"
            else:
                permission = "evidence_gap_only"
            original_statement = _compact(c.get("statement"), 400)
            if binding == "partial" and supported_rewrite:
                statement_for_writing = supported_rewrite
            elif binding == "partial" and supported_components and not interpretive_synthesis:
                statement_for_writing = (
                    "Available evidence supports these bounded components: "
                    + "; ".join(supported_components)
                    + ". Do not restore the listed missing components."
                )
            else:
                statement_for_writing = original_statement
            compact_claims.append({
                "claim_id": c.get("claim_id"),
                "statement": original_statement,
                "evidence_type": c.get("evidence_type"),
                "claim_kind": c.get("claim_kind", "direct_fact"),
                "claim_state": state,
                "saturation_score": c.get("saturation_score", 0.0),
                "load_bearing": c.get("load_bearing", False),
                "evidence_binding_status": binding,
                "evidence_binding_confidence": str(
                    c.get("evidence_binding_confidence") or ""
                ),
                "evidence_binding_reason": _compact(
                    c.get("evidence_binding_reason"), 500
                ),
                "evidence_requirement": requirement,
                "closure_disposition": c.get("closure_disposition", ""),
                "missing_evidence_components": missing_components,
                "supported_components": supported_components,
                "supported_rewrite": supported_rewrite,
                "statement_for_writing": statement_for_writing,
                "writing_permission": permission,
            })

        evidence_packets: list[EvidencePacket] = []
        contradictions: list[dict[str, Any]] = []
        open_questions: list[str] = []

        # Normalize the verifier's parallel support views before loading the
        # KB.  Legacy artifacts can contain a valid chunk in evidence_spans or
        # evidence_component_map even when supporting_text_chunk_ids was
        # truncated.  The writing layer must preserve those verified anchors
        # rather than silently replacing them with a weaker neighboring span.
        verified_spans_by_claim: dict[str, dict[str, str]] = {}
        verified_policy_by_claim: dict[str, dict[str, tuple[str, str]]] = {}
        canonical_ids_by_claim: dict[str, list[str]] = {}
        for claim in claims:
            claim_id = str(claim.get("claim_id") or "")
            verified_spans: dict[str, str] = {}
            verified_policy: dict[str, tuple[str, str]] = {}
            span_ids: list[str] = []
            for span in (claim.get("evidence_spans") or [])[:8]:
                if not isinstance(span, dict):
                    continue
                chunk_id = str(span.get("chunk_id") or "")
                if not chunk_id:
                    continue
                span_ids.append(chunk_id)
                exact = str(
                    span.get("quote_translation")
                    or span.get("quote")
                    or ""
                ).strip()
                if exact:
                    verified_spans[chunk_id] = _compact(exact, 1200)
                scope_fit = str(span.get("scope_fit") or "in_domain").strip().lower()
                if scope_fit not in {"in_domain", "cross_domain_analogy", "off_domain"}:
                    scope_fit = "in_domain"
                retrieval_role = str(
                    span.get("retrieval_role") or "evidence_candidate"
                ).strip().lower()
                if retrieval_role not in {
                    "evidence_candidate", "method_transfer", "background_only", "reject"
                }:
                    retrieval_role = "evidence_candidate"
                if scope_fit == "off_domain":
                    retrieval_role = "reject"
                elif scope_fit == "cross_domain_analogy" and retrieval_role == "evidence_candidate":
                    retrieval_role = "method_transfer"
                verified_policy[chunk_id] = (scope_fit, retrieval_role)
            component_ids = [
                str(chunk_id)
                for component in (claim.get("evidence_component_map") or [])[:10]
                if isinstance(component, dict)
                for chunk_id in (component.get("chunk_ids") or [])[:8]
                if chunk_id
            ]
            canonical_ids_by_claim[claim_id] = list(dict.fromkeys(
                [
                    str(chunk_id)
                    for chunk_id in (claim.get("supporting_text_chunk_ids") or [])
                    if chunk_id
                ]
                + span_ids
                + component_ids
            ))
            verified_spans_by_claim[claim_id] = verified_spans
            verified_policy_by_claim[claim_id] = verified_policy

        # Collect all chunk IDs upfront for batch KB lookup
        all_fallback_chunks: list[str] = []
        for claim in claims:
            binding_verified = str(claim.get("evidence_binding_status") or "") in {
                "direct", "synthesized", "partial"
            }
            if binding_verified:
                all_fallback_chunks.extend(
                    canonical_ids_by_claim.get(str(claim.get("claim_id") or ""), [])
                )
        # Include relation chunks as well so incomplete relation records can be
        # enriched rather than passed downstream as empty evidence shells.
        relation_chunks = [
            str(rel.get("chunk_id") or "")
            for claim in claims
            if str(claim.get("evidence_binding_status") or "")
            in {"direct", "synthesized", "partial"}
            for rel in (claim.get("evidence_relations") or [])
            if isinstance(rel, dict) and rel.get("chunk_id")
        ]
        kb_records = self._load_chunk_records(list(dict.fromkeys(all_fallback_chunks + relation_chunks))[:100])

        for claim in claims:
            cid = str(claim.get("claim_id") or "")
            binding_verified = str(claim.get("evidence_binding_status") or "") in {
                "direct", "synthesized", "partial"
            }
            canonical_support_ids = canonical_ids_by_claim.get(cid, [])
            canonical_support_set = set(canonical_support_ids)
            verified_spans = verified_spans_by_claim.get(cid, {})
            verified_policy = verified_policy_by_claim.get(cid, {})
            # Build evidence packets from evidence_relations if available
            relations = [
                rel for rel in (claim.get("evidence_relations") or [])
                if binding_verified and isinstance(rel, dict)
            ]
            relations.sort(key=lambda rel: (
                0 if str(rel.get("chunk_id") or "") in canonical_support_set else 1,
                0 if kb_records.get(str(rel.get("chunk_id") or ""), {}).get("factual_support_allowed") == "true" else 1,
                1 if kb_records.get(str(rel.get("chunk_id") or ""), {}).get("retrieval_role") in {"method_transfer", "background_only"} else 0,
            ))
            for rel in relations:
                if sum(ep.claim_id == cid for ep in evidence_packets) >= max_evidence_spans:
                    break
                if not isinstance(rel, dict):
                    continue
                rel_chunk_id = str(rel.get("chunk_id") or "")
                record = kb_records.get(rel_chunk_id, {})
                claim_verified = rel_chunk_id in verified_spans
                claim_scope, claim_role = verified_policy.get(
                    rel_chunk_id, ("in_domain", "evidence_candidate")
                )
                if (
                    (claim_verified and (
                        claim_scope == "off_domain" or claim_role == "reject"
                    ))
                    or (not claim_verified and (
                        record.get("retrieval_role") == "reject"
                        or record.get("scope_fit") == "off_domain"
                    ))
                ):
                    continue
                exact_span = _complete_evidence_excerpt(
                    verified_spans.get(rel_chunk_id)
                    or rel.get("exact_span")
                    or "",
                    record.get("text") or "",
                )
                relation = str(rel.get("relation_type") or "component_support")
                if claim_verified and claim_role in {"method_transfer", "background_only"}:
                    relation = claim_role
                elif not claim_verified and record.get("retrieval_role") in {"method_transfer", "background_only"}:
                    relation = str(record.get("retrieval_role"))
                scope_fit = (
                    claim_scope if claim_verified
                    else str(record.get("scope_fit") or "in_domain")
                )
                retrieval_role = (
                    claim_role if claim_verified
                    else str(record.get("retrieval_role") or "evidence_candidate")
                )
                ep = EvidencePacket(
                    claim_id=cid,
                    paper_id=str(rel.get("paper_id") or record.get("paper_id") or ""),
                    chunk_id=rel_chunk_id,
                    exact_spans=[exact_span] if exact_span else [],
                    visual_refs=list(claim.get("supporting_visual_chunk_ids") or [])[:2],
                    support_relation=relation,
                    limitations=list(rel.get("limitations") or []) + (
                        ["Cross-domain material may be used only as an explicit analogy or method-transfer example."]
                        if retrieval_role == "method_transfer" else []
                    ),
                    evidence_level=str(record.get("evidence_level") or "fulltext"),
                    source_kind=str(record.get("source_kind") or "fulltext"),
                    scope_fit=scope_fit,
                    retrieval_role=retrieval_role,
                    source_title=str(record.get("title") or ""),
                )
                evidence_packets.append(ep)

            # Supplement relation packets with every canonical verifier anchor
            # that still fits the bounded packet budget.  Evidence relations
            # can be shorter than supporting_text_chunk_ids after an M3 update;
            # dropping the fourth verified anchor previously caused the writer
            # to cite a weaker neighboring passage even though the right source
            # was already present in the KB.
            if binding_verified:
                existing_claim_chunks = {
                    ep.chunk_id for ep in evidence_packets if ep.claim_id == cid
                }
                chunks = [
                    chunk_id for chunk_id in canonical_support_ids
                    if chunk_id not in existing_claim_chunks
                ]
                chunks.sort(key=lambda chunk_id: (
                    0 if str(chunk_id) in verified_spans else 1,
                    0 if kb_records.get(str(chunk_id), {}).get("factual_support_allowed") == "true" else 1,
                    1 if kb_records.get(str(chunk_id), {}).get("retrieval_role") in {"method_transfer", "background_only"} else 0,
                ))
                for chunk_id in chunks:
                    if sum(ep.claim_id == cid for ep in evidence_packets) >= max_evidence_spans:
                        break
                    record = kb_records.get(str(chunk_id), {})
                    claim_verified = str(chunk_id) in verified_spans
                    claim_scope, claim_role = verified_policy.get(
                        str(chunk_id), ("in_domain", "evidence_candidate")
                    )
                    if (
                        (claim_verified and (
                            claim_scope == "off_domain" or claim_role == "reject"
                        ))
                        or (not claim_verified and (
                            record.get("retrieval_role") == "reject"
                            or record.get("scope_fit") == "off_domain"
                        ))
                    ):
                        continue
                    preview = _complete_evidence_excerpt(
                        verified_spans.get(str(chunk_id))
                        or "",
                        record.get("text") or "",
                    )
                    role = (
                        claim_role if claim_verified
                        else str(record.get("retrieval_role") or "evidence_candidate")
                    )
                    scope_fit = (
                        claim_scope if claim_verified
                        else str(record.get("scope_fit") or "in_domain")
                    )
                    evidence_packets.append(EvidencePacket(
                        claim_id=cid,
                        paper_id=str(record.get("paper_id") or ""),
                        chunk_id=str(chunk_id),
                        exact_spans=[preview] if preview else [],
                        visual_refs=list(claim.get("supporting_visual_chunk_ids") or [])[:2],
                        support_relation=role if role in {"method_transfer", "background_only"} else "component_support",
                        limitations=(
                            ["Cross-domain material may be used only as an explicit analogy or method-transfer example."]
                            if role == "method_transfer" else []
                        ),
                        evidence_level=str(record.get("evidence_level") or "fulltext"),
                        source_kind=str(record.get("source_kind") or "fulltext"),
                        scope_fit=scope_fit,
                        retrieval_role=role,
                        source_title=str(record.get("title") or ""),
                    ))

            # Collect contradictions and open questions
            if include_contradictions:
                for flag in (claim.get("critic_flags") or []):
                    if "contradict" in str(flag).lower():
                        contradictions.append({"claim_id": cid, "flag": str(flag)})
            if claim.get("claim_state") == "open_question":
                open_questions.append(_compact(claim.get("statement"), 200))

        # Recompute writing permission from the material that actually reached
        # this packet.  A blueprint may still carry an older optimistic binding
        # made before source-scope provenance was introduced; cross-domain
        # analogies and background records never authorize a factual sentence.
        for compact_claim in compact_claims:
            if compact_claim.get("evidence_requirement") != "factual":
                continue
            claim_id = str(compact_claim.get("claim_id") or "")
            factual_packets = [
                ep for ep in evidence_packets
                if ep.claim_id == claim_id
                and ep.scope_fit == "in_domain"
                and ep.retrieval_role == "evidence_candidate"
                and ep.chunk_id
                and any(str(span).strip() for span in ep.exact_spans)
            ]
            if not factual_packets:
                compact_claim["writing_permission"] = "evidence_gap_only"
                compact_claim["material_gate_reason"] = "no_in_domain_factual_evidence_packet"
            elif all(ep.evidence_level != "fulltext" for ep in factual_packets):
                compact_claim["writing_permission"] = "hedged_factual_assertion"
                compact_claim["material_gate_reason"] = "abstract_or_background_level_only"

        # fail-closed: identify load-bearing claims with no factual evidence packets
        cited_claim_ids = {
            ep.claim_id for ep in evidence_packets
            if ep.chunk_id
            and ep.retrieval_role == "evidence_candidate"
            and ep.scope_fit == "in_domain"
            and any(str(span).strip() for span in ep.exact_spans)
        }
        uncited_lb = [
            str(c.get("claim_id") or "")
            for c in claims
            if c.get("load_bearing")
            and str(c.get("evidence_requirement") or "factual") == "factual"
            and str(c.get("claim_state") or "") not in {"open_question", "reframed", "dropped"}
            and str(c.get("claim_id") or "") not in cited_claim_ids
        ]

        transition_contract = {
            "transition_from_previous": _compact(section.get("transition_from_previous"), 300),
            "transition_to_next": _compact(section.get("transition_to_next"), 300),
        }

        visual_ids = list(dict.fromkeys(
            str(vid)
            for claim in claims
            for vid in (claim.get("supporting_visual_chunk_ids") or [])
            if vid
        ))
        visual_evidence = self._load_visual_records(visual_ids[:20])

        return SectionMaterialPacket(
            section_id=section_id,
            section_contract=section_contract,
            claims=compact_claims,
            evidence_packets=evidence_packets,
            contradictions=contradictions,
            open_questions=open_questions,
            transition_contract=transition_contract,
            uncited_load_bearing_claim_ids=uncited_lb,
            visual_evidence=visual_evidence,
            visual_gap_plan=list(section.get("visual_gap_plan") or []),
            literature_coverage=copy.deepcopy(section.get("literature_coverage") or {}),
        )


# --------------------------------------------------------------------------- #
# SectionEvidencePacketBuilder (alias for SectionMaterialMapper.map)
# --------------------------------------------------------------------------- #

class SectionEvidencePacketBuilder:
    """Builds structured EvidencePacket lists for a section's claims."""

    def __init__(self, mapper: SectionMaterialMapper | None = None) -> None:
        self._mapper = mapper or SectionMaterialMapper()

    def build(self, section: dict[str, Any]) -> list[EvidencePacket]:
        packet = self._mapper.map(section)
        return packet.evidence_packets


# --------------------------------------------------------------------------- #
# SectionWriter
# --------------------------------------------------------------------------- #

class SectionWriter:
    """Writes an English section draft from a SectionMaterialPacket."""

    def __init__(
        self,
        model_tier: str = "advanced_model",
        prompt_path: Path = SECTION_WRITER_PROMPT,
        real_llm: bool = False,
    ) -> None:
        self.model_tier = model_tier
        self.prompt_path = prompt_path
        self.real_llm = real_llm

    def write(self, packet: SectionMaterialPacket) -> SectionDraft:
        draft = SectionDraft(section_id=packet.section_id)
        if not self.real_llm:
            claims_text = "\n".join(
                f"- {c.get('statement', '')}" for c in packet.claims
            )
            draft.english_text = (
                f"[DRAFT — {packet.section_contract.get('title', packet.section_id)}]\n\n"
                f"{packet.section_contract.get('argument_role', '')}\n\n"
                f"{claims_text}"
            )
            draft.status = "draft"
            return draft

        system = _read_prompt(self.prompt_path)
        payload = packet.to_dict()
        compact_registry: EvidenceHandleRegistry | None = None
        compact_initial_failures: list[str] = []
        try:
            word_budget = max(
                0, int(packet.section_contract.get("word_budget") or 0)
            )
        except (TypeError, ValueError):
            word_budget = 0
        expected_paragraphs = len(
            packet.section_contract.get("paragraph_functions") or []
        )
        reference_target_words, hard_max_words = _section_word_guidance(
            word_budget,
            packet.section_contract.get("min_word_count"),
            packet.section_contract.get("max_word_count"),
        )
        output_tokens = min(
            12000,
            max(5000, int(word_budget * 2.2) + 1200),
        )
        router_result: ClaimRoutingResult | None = None
        routed_assignments = None
        secondary_claim_hints = None
        routing_diagnostics = None
        if _compact_evidence_handles_enabled(packet):
            payload, compact_registry = _compact_packet_payload(packet)
            system = system + COMPACT_EVIDENCE_HANDLES_SYSTEM_SUFFIX
        payload["length_plan"] = _build_length_plan(
            packet,
            word_budget=word_budget,
            expected_paragraphs=expected_paragraphs,
            reference_target_words=reference_target_words,
            hard_max_words=hard_max_words,
        )
        result = call_qwen_chat(
            "SectionWriterAgent",
            [
                {"role": "system", "content": system},
                {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
            ],
            model_tier=self.model_tier,
            temperature=0.3,
            max_tokens=output_tokens,
            response_format={"type": "json_object"},
            stream=True,
            force_mock=False,
            max_retries=0,
            timeout_seconds=150,
            max_transport_key_candidates=1,
            accept_partial_stream=False,
            allow_model_fallback=False,
        )
        content = str(result.get("content") or "")
        parsed = _safe_json(content)
        if not (isinstance(parsed, dict) and str(parsed.get("section_text") or "").strip()):
            # Long-form generation occasionally exhausts one provider/model
            # route without returning a complete JSON object. Make one
            # explicit, independently routed recovery call before declaring
            # the section failed. The failed checkpoint is then retryable on
            # resume rather than becoming a permanent empty chapter.
            recovery = call_qwen_chat(
                "SectionWriterRecoveryAgent",
                [
                    {"role": "system", "content": system},
                    {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
                ],
                model_tier="b_minus_model",
                temperature=0.2,
                max_tokens=output_tokens,
                response_format={"type": "json_object"},
                stream=True,
                force_mock=False,
                max_retries=0,
                timeout_seconds=150,
                max_transport_key_candidates=1,
                accept_partial_stream=False,
                allow_model_fallback=False,
            )
            parsed = _safe_json(str(recovery.get("content") or ""))
        if isinstance(parsed, dict) and str(parsed.get("section_text") or "").strip():
            draft.english_text = _normalize_compound_reference_markers(
                str(parsed.get("section_text")).strip()
            )
            before_markers = set(
                re.findall(r"\[REF:([^\]]+)\]", draft.english_text)
            )
            before_handles = (
                sorted(compact_registry.handle_markers(draft.english_text))
                if compact_registry is not None
                else []
            )
            normalized_text, compact_initial_failures = (
                _resolve_evidence_handle_text(
                    draft.english_text, compact_registry, packet
                )
            )
            if normalized_text != draft.english_text:
                draft.english_text = normalized_text
                resolved_handles = (
                    sorted(
                        handle
                        for handle in before_handles
                        if handle in compact_registry.allowed_markers()
                    )
                    if compact_registry is not None
                    else []
                )
                draft.revision_history.append({
                    "stage": (
                        "evidence_handle_resolution"
                        if compact_registry is not None
                        else "reference_alias_normalization"
                    ),
                    "accepted": True,
                    "before_markers": sorted(before_markers),
                    "before_handles": before_handles,
                    "resolved_handles": resolved_handles,
                    "after_markers": sorted(
                        re.findall(r"\[REF:([^\]]+)\]", draft.english_text)
                    ),
                    "evidence_handle_registry": (
                        compact_registry.to_dict()
                        if compact_registry is not None
                        else None
                    ),
                })
            draft.status = "draft"
        else:
            draft.english_text = ""
            draft.status = "failed"
            return draft

        initial_words = _word_count(draft.english_text)
        initial_hard_failures = _initial_draft_hard_failures(
            draft.english_text,
            expected_paragraphs=expected_paragraphs,
            allowed_markers=_allowed_reference_markers(packet),
            hard_max_words=hard_max_words,
            reference_target_words=reference_target_words,
        ) + compact_initial_failures
        below_reference_target = initial_words < reference_target_words
        if word_budget and below_reference_target and not initial_hard_failures:
            # A safe, evidence-limited draft is accepted as-is. The reference
            # target is diagnostic only; it must never spend extra model calls
            # chasing a number.
            draft.revision_history.append({
                "stage": "section_initial_soft_shortfall",
                "accepted": True,
                "recovery_required": False,
                "hard_failures": [],
                "initial_word_count": initial_words,
                "target_word_budget": word_budget,
                "reference_target_word_count": reference_target_words,
                "hard_max_word_count": hard_max_words,
                "below_reference_target": True,
                "meets_80_percent_budget": (
                    initial_words >= int(word_budget * 0.80)
                ),
            })
            return draft
        if word_budget and initial_hard_failures:
            draft.revision_history.append({
                "stage": "section_initial_contract_diagnostic",
                "accepted": False,
                "recovery_required": True,
                "hard_failures": initial_hard_failures,
                "initial_word_count": initial_words,
                "target_word_budget": word_budget,
                "reference_target_word_count": reference_target_words,
                "hard_max_word_count": hard_max_words,
                "below_reference_target": below_reference_target,
                "meets_80_percent_budget": (
                    initial_words >= int(word_budget * 0.80)
                ),
            })
            if expected_paragraphs > 0:
                recovery_kwargs: dict[str, Any] = {}
                if compact_registry is not None:
                    if router_result is None:
                        router_result = _router_result_for_packet(
                            packet, expected_paragraphs
                        )
                        routed_assignments = (
                            router_result.primary_by_paragraph,
                            "deterministic_local_routing",
                            router_result.primary_sources,
                        )
                        secondary_claim_hints = (
                            router_result.secondary_by_paragraph
                        )
                        routing_diagnostics = router_result.to_dict()
                    recovery_kwargs = {
                        "stage_name": "compact_evidence_handle_writing_retry",
                        "routed_assignments": routed_assignments,
                        "secondary_claim_hints": secondary_claim_hints,
                        "routing_diagnostics": routing_diagnostics,
                    }
                paragraph_recovery_accepted, recovered_text = (
                    self._compact_paragraph_recovery(
                        draft=draft,
                        packet=packet,
                        word_budget=word_budget,
                        expected_paragraphs=expected_paragraphs,
                        reference_target_words=reference_target_words,
                        hard_max_words=hard_max_words,
                        recovery_system=_read_prompt(
                            SECTION_PARAGRAPH_RECOVERY_PROMPT
                        ),
                        **recovery_kwargs,
                    )
                )
                if paragraph_recovery_accepted:
                    draft.english_text = recovered_text
                    return draft
            best_safe_candidate = draft.english_text
            best_safe_word_count = initial_words
            allowed_markers = _allowed_reference_markers(packet)
            repair_payload = {
                "material_packet": (
                    payload if compact_registry is not None else packet.to_dict()
                ),
                "previous_draft": (
                    compact_registry.encode_markers_to_handles(draft.english_text)
                    if compact_registry is not None
                    else draft.english_text
                ),
                "allowed_reference_markers": (
                    sorted(compact_registry.allowed_markers())
                    if compact_registry is not None
                    else sorted(allowed_markers)
                ),
                "contract_failure": {
                    "actual_word_count": initial_words,
                    "target_word_budget": word_budget,
                    "reference_target_word_count": reference_target_words,
                    "preferred_repair_word_count": word_budget,
                    "expected_paragraph_count": expected_paragraphs,
                    "reference_words_per_paragraph": (
                        math.ceil(reference_target_words * 1.05 / expected_paragraphs)
                        if expected_paragraphs else 0
                    ),
                },
            }
            repair_system = system + (
                "\n\nCONTRACT-REPAIR PASS: Rewrite the complete section, preserving the supplied "
                "paragraph order and using only the supplied claims and evidence. Aim for at least "
                "preferred_repair_word_count so ordinary model counting error does not leave the "
                "result just below the reference target. reference_target_word_count is a soft "
                "target: expand only from supplied evidence and stop shorter rather than repeat or "
                "invent when the material is exhausted. "
                "Develop every paragraph function separately and aim for reference_words_per_paragraph; "
                "do not compress the complete section into one continuous paragraph. "
                "Do not restore any missing_evidence_components. Keep one source per REF bracket. "
                "The text inside every [REF:...] must exactly equal one item in "
                "allowed_reference_markers. Never cite a chunk_id or text anchor ID."
            )
            repair = call_qwen_chat(
                "SectionWriterContractRepairAgent",
                [
                    {"role": "system", "content": repair_system},
                    {"role": "user", "content": json.dumps(repair_payload, ensure_ascii=False)},
                ],
                model_tier=self.model_tier,
                temperature=0.2,
                max_tokens=max(7000, output_tokens),
                response_format={"type": "json_object"},
                stream=True,
                force_mock=False,
                max_retries=0,
                timeout_seconds=180,
                max_transport_key_candidates=1,
                accept_partial_stream=False,
                allow_model_fallback=False,
            )
            repaired = _safe_json(str(repair.get("content") or ""))
            repair_recovery_used = False
            if not (
                isinstance(repaired, dict)
                and str(repaired.get("section_text") or "").strip()
            ):
                # A long structured response can be scientifically complete
                # yet arrive as truncated/invalid JSON. Retry once through an
                # independent high-capability route before accepting a short
                # chapter. This is bounded and does not change the evidence.
                recovery = call_qwen_chat(
                    "SectionWriterContractRepairRecoveryAgent",
                    [
                        {"role": "system", "content": repair_system},
                        {"role": "user", "content": json.dumps(repair_payload, ensure_ascii=False)},
                    ],
                    model_tier="b_plus_model",
                    temperature=0.1,
                    max_tokens=max(7000, output_tokens),
                    response_format={"type": "json_object"},
                    stream=True,
                    force_mock=False,
                    max_retries=0,
                    timeout_seconds=210,
                    max_transport_key_candidates=2,
                    accept_partial_stream=False,
                    allow_model_fallback=False,
                )
                repaired = _safe_json(str(recovery.get("content") or ""))
                repair_recovery_used = True
            raw_candidate = _normalize_compound_reference_markers(
                str(repaired.get("section_text") or "").strip()
                if isinstance(repaired, dict) else ""
            )
            raw_candidate, compact_repair_failures = (
                _resolve_evidence_handle_text(
                    raw_candidate, compact_registry, packet
                )
            )
            candidate = raw_candidate
            candidate_markers_before_safety = set(
                re.findall(r"\[REF:([^\]]+)\]", candidate)
            )
            unknown_candidate_markers = candidate_markers_before_safety - allowed_markers
            candidate, removed_unknown_sentences = _drop_sentences_with_unknown_references(
                candidate, unknown_candidate_markers
            )
            candidate_markers = set(re.findall(r"\[REF:([^\]]+)\]", candidate))
            candidate_words = _word_count(candidate)
            candidate_paragraphs = _paragraph_count(candidate)
            reference_safe = (
                not (candidate_markers - allowed_markers)
                and not compact_repair_failures
            )
            structure_safe = (
                not expected_paragraphs
                or candidate_paragraphs >= (
                    1 if expected_paragraphs == 1 else max(2, expected_paragraphs - 1)
                )
            )
            below_reference_target = candidate_words < reference_target_words
            length_safe = candidate_words <= hard_max_words
            improved = candidate_words > initial_words
            if (
                candidate
                and reference_safe
                and structure_safe
                and length_safe
                and candidate_words > best_safe_word_count
            ):
                best_safe_candidate = candidate
                best_safe_word_count = candidate_words
            accepted = bool(
                candidate
                and reference_safe
                and structure_safe
                and length_safe
                and improved
            )
            repair_record = {
                "stage": "section_contract_repair",
                "accepted": accepted,
                "initial_word_count": initial_words,
                "candidate_word_count": candidate_words,
                "target_word_budget": word_budget,
                "reference_target_word_count": reference_target_words,
                "hard_max_word_count": hard_max_words,
                "below_reference_target": below_reference_target,
                "candidate_paragraph_count": candidate_paragraphs,
                "expected_paragraph_count": expected_paragraphs,
                "unknown_reference_markers": sorted(unknown_candidate_markers),
                "removed_unknown_reference_sentences": removed_unknown_sentences,
                "meets_80_percent_budget": candidate_words >= int(word_budget * 0.80),
                "recovery_used": repair_recovery_used,
            }
            if compact_registry is not None:
                repair_record["compact_mode_failures"] = compact_repair_failures
            draft.revision_history.append(repair_record)
            # A long repair is expensive and can be otherwise excellent while
            # containing one invalid source marker.  Do not discard the whole
            # improvement immediately.  Give the writer one bounded correction
            # pass that must remove the unsupported proposition or recast it as
            # an explicit open question; merely deleting the marker while
            # preserving a factual assertion is forbidden.  The same hard
            # structural and identifier gates are applied again afterwards.
            if raw_candidate and not accepted:
                repair_contract_failures = {
                    "unknown_reference_markers": sorted(unknown_candidate_markers),
                    "actual_word_count": _word_count(raw_candidate),
                    "target_word_budget": word_budget,
                    "reference_target_word_count": reference_target_words,
                    "preferred_repair_word_count": word_budget,
                    "maximum_acceptable_word_count": hard_max_words,
                    "expected_paragraph_count": expected_paragraphs,
                    "reference_words_per_paragraph": (
                        math.ceil(reference_target_words * 1.05 / expected_paragraphs)
                        if expected_paragraphs else 0
                    ),
                }
                if compact_registry is not None:
                    repair_contract_failures["non_handle_reference_markers"] = (
                        compact_repair_failures
                    )
                retry_payload = {
                    "material_packet": (
                        payload if compact_registry is not None else packet.to_dict()
                    ),
                    "candidate_draft_to_correct": (
                        compact_registry.encode_markers_to_handles(raw_candidate)
                        if compact_registry is not None
                        else raw_candidate
                    ),
                    "contract_failures": repair_contract_failures,
                    "allowed_reference_markers": (
                        sorted(compact_registry.allowed_markers())
                        if compact_registry is not None
                        else sorted(allowed_markers)
                    ),
                }
                retry_system = repair_system + (
                    "\n\nFINAL SAFETY CORRECTION: Correct the supplied candidate draft. Use only "
                    "allowed_reference_markers. For every unsupported proposition linked to "
                    "an unknown marker, either remove the proposition or explicitly frame it "
                    "as an unresolved question without presenting it as fact. Do not simply "
                    "delete an invalid marker and leave its factual sentence unchanged. If the "
                    "candidate is below reference_target_word_count, expand the supplied "
                    "paragraph functions with deeper mechanism, boundary-condition, comparison, "
                    "limitation, and open-question analysis drawn only from authorized material; "
                    "stop shorter rather than repeat or invent when the material is exhausted; "
                    "aim for preferred_repair_word_count. "
                    "Return the complete section in the same JSON schema."
                )
                retry = call_qwen_chat(
                    "SectionWriterContractSafetyRetryAgent",
                    [
                        {"role": "system", "content": retry_system},
                        {
                            "role": "user",
                            "content": json.dumps(retry_payload, ensure_ascii=False),
                        },
                    ],
                    model_tier=self.model_tier,
                    temperature=0.1,
                    max_tokens=max(7000, output_tokens),
                    response_format={"type": "json_object"},
                    stream=True,
                    force_mock=False,
                    max_retries=0,
                    timeout_seconds=180,
                    max_transport_key_candidates=1,
                    accept_partial_stream=False,
                    allow_model_fallback=False,
                )
                retry_parsed = _safe_json(str(retry.get("content") or ""))
                retry_candidate = _normalize_compound_reference_markers(
                    str(retry_parsed.get("section_text") or "").strip()
                    if isinstance(retry_parsed, dict) else ""
                )
                retry_candidate, compact_retry_failures = (
                    _resolve_evidence_handle_text(
                        retry_candidate, compact_registry, packet
                    )
                )
                retry_markers_before_safety = set(
                    re.findall(r"\[REF:([^\]]+)\]", retry_candidate)
                )
                unknown_retry_markers = retry_markers_before_safety - allowed_markers
                retry_candidate, retry_removed_unknown_sentences = (
                    _drop_sentences_with_unknown_references(
                        retry_candidate, unknown_retry_markers
                    )
                )
                retry_markers = set(
                    re.findall(r"\[REF:([^\]]+)\]", retry_candidate)
                )
                retry_words = _word_count(retry_candidate)
                retry_paragraphs = _paragraph_count(retry_candidate)
                retry_accepted = bool(
                    retry_candidate
                    and not (retry_markers - allowed_markers)
                    and not compact_retry_failures
                    and (
                        not expected_paragraphs
                        or retry_paragraphs >= (
                            1 if expected_paragraphs == 1 else max(2, expected_paragraphs - 1)
                        )
                    )
                    and retry_words <= hard_max_words
                    and retry_words > initial_words
                )
                retry_structure_safe = bool(
                    not expected_paragraphs
                    or retry_paragraphs >= (
                        1 if expected_paragraphs == 1 else max(2, expected_paragraphs - 1)
                    )
                )
                if (
                    retry_candidate
                    and not (retry_markers - allowed_markers)
                    and not compact_retry_failures
                    and retry_structure_safe
                    and retry_words <= hard_max_words
                    and retry_words > best_safe_word_count
                ):
                    best_safe_candidate = retry_candidate
                    best_safe_word_count = retry_words
                retry_record = {
                    "stage": "section_contract_repair_safety_retry",
                    "accepted": retry_accepted,
                    "candidate_word_count": retry_words,
                    "target_word_budget": word_budget,
                    "reference_target_word_count": reference_target_words,
                    "hard_max_word_count": hard_max_words,
                    "below_reference_target": retry_words < reference_target_words,
                    "candidate_paragraph_count": retry_paragraphs,
                    "expected_paragraph_count": expected_paragraphs,
                    "unknown_reference_markers": sorted(unknown_retry_markers),
                    "removed_unknown_reference_sentences": retry_removed_unknown_sentences,
                    "meets_80_percent_budget": (
                        retry_words >= int(word_budget * 0.80)
                    ),
                }
                if compact_registry is not None:
                    retry_record["compact_mode_failures"] = compact_retry_failures
                draft.revision_history.append(retry_record)
                if retry_accepted:
                    candidate = retry_candidate
                    accepted = True
            if accepted:
                draft.english_text = candidate
            elif best_safe_word_count > initial_words:
                # Keep the strongest safe attempt for diagnosis and revision,
                # but expose that the contract remains unmet.  Reverting to a
                # shorter first draft would lose useful grounded synthesis and
                # make the next feedback round harder without improving safety.
                draft.english_text = best_safe_candidate
                draft.status = "draft_contract_shortfall"
        return draft


    # ----------------------------------------------------------------------- #
    # Compact paragraph-budgeted recovery for SectionWriter
    # ----------------------------------------------------------------------- #

    def _call_paragraph_recovery(
        self,
        *,
        agent_name: str,
        payload: dict[str, Any],
        recovery_system: str,
        allowed_markers: set[str],
        max_words: int,
        packet: SectionMaterialPacket,
        evidence_handles: EvidenceHandleRegistry | None = None,
    ) -> tuple[str, list[str], int, list[str]]:
        """One paragraph model call; returns (text, failures, words, handles)."""
        target_words = int(payload["word_targets"]["target_words"])
        result = call_qwen_chat(
            agent_name,
            [
                {"role": "system", "content": recovery_system},
                {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
            ],
            model_tier=self.model_tier,
            temperature=0.2,
            max_tokens=max(1200, min(4000, target_words * 4 + 300)),
            response_format={"type": "json_object"},
            stream=True,
            force_mock=False,
            max_retries=0,
            timeout_seconds=PARAGRAPH_RECOVERY_TIMEOUT_SECONDS,
            max_transport_key_candidates=1,
            accept_partial_stream=False,
            allow_model_fallback=False,
        )
        parsed = _safe_json(str(result.get("content") or ""))
        if not isinstance(parsed, dict):
            return "", ["invalid_json"], 0, []
        raw = str(parsed.get("paragraph_text") or "").strip()
        if not raw:
            return "", ["empty"], 0, []
        text = _normalize_compound_reference_markers(
            re.sub(r"\s*\n\s*", " ", raw).strip()
        )
        resolved_handles = (
            sorted(
                evidence_handles.allowed_markers()
                & evidence_handles.handle_markers(text)
            )
            if evidence_handles is not None
            else []
        )
        text, compact_failures = _resolve_evidence_handle_text(
            text, evidence_handles, packet
        )
        return (
            text,
            _paragraph_hard_failures(text, allowed_markers, max_words)
            + compact_failures,
            _word_count(text),
            resolved_handles,
        )

    def _compact_paragraph_recovery(
        self,
        *,
        draft: SectionDraft,
        packet: SectionMaterialPacket,
        word_budget: int,
        expected_paragraphs: int,
        reference_target_words: int,
        hard_max_words: int,
        recovery_system: str,
        stage_name: str = "section_paragraph_recovery",
        routed_assignments: (
            tuple[list[list[dict[str, Any]]], str, list[str]] | None
        ) = None,
        secondary_claim_hints: list[list[dict[str, Any]]] | None = None,
        routing_diagnostics: dict[str, Any] | None = None,
    ) -> tuple[bool, str]:
        """Attempt one compact paragraph recovery; fall back on hard failures.

        Each contracted paragraph is written from a compact payload that
        carries only its function, ready claims, matching evidence packets,
        paragraph-local REF markers, neighboring continuity, and soft word
        guidance. Reference length is not a hard gate: a paragraph gets at
        most one retry and only for an actual local hard failure (empty text,
        unknown citations, runaway length, or invalid JSON). A hard-safe
        shorter paragraph is accepted immediately with a recorded soft-target
        shortfall; the reference is diagnostic, not a retry trigger.
        Assembled acceptance requires known references, expected structure,
        nonempty useful paragraphs, no runaway length, and no regression
        against the initial draft.
        """
        allowed_markers = _allowed_reference_markers(packet)
        functions = list(packet.section_contract.get("paragraph_functions") or [])
        compact_handles = _compact_evidence_handles_enabled(packet)
        if compact_handles:
            recovery_system = (
                recovery_system + COMPACT_EVIDENCE_HANDLES_SYSTEM_SUFFIX
            )
        if expected_paragraphs <= 0 or not functions:
            draft.revision_history.append({
                "stage": stage_name,
                "accepted": False,
                "failure_reason": "no_paragraph_functions",
                "claim_assignment_mode": "none",
                "initial_word_count": _word_count(draft.english_text),
                "target_word_budget": word_budget,
                "reference_target_word_count": reference_target_words,
                "hard_max_word_count": hard_max_words,
                "expected_paragraph_count": expected_paragraphs,
                "per_paragraph": [],
            })
            return False, ""

        initial_paragraphs = _split_into_paragraphs(
            draft.english_text, expected_paragraphs
        )
        word_targets = _paragraph_word_targets(
            word_budget,
            expected_paragraphs,
            reference_target_words,
            hard_max_words,
        )
        if routed_assignments is not None:
            assigned_claims, claim_assignment_mode, claim_id_sources = (
                routed_assignments
            )
        else:
            assigned_claims, claim_assignment_mode, claim_id_sources = (
                _contract_paragraph_claim_assignment(packet, expected_paragraphs)
            )
        secondary_hints = (
            secondary_claim_hints
            if secondary_claim_hints is not None
            else [[] for _ in range(expected_paragraphs)]
        )
        evidence_by_claim: dict[str, list[EvidencePacket]] = {}
        for ep in packet.evidence_packets:
            evidence_by_claim.setdefault(ep.claim_id, []).append(ep)

        accepted_paragraphs: list[str] = []
        per_paragraph: list[dict[str, Any]] = []
        total_calls = 0
        for index in range(expected_paragraphs):
            previous_text = (
                accepted_paragraphs[-1]
                if accepted_paragraphs
                else (initial_paragraphs[index - 1] if index > 0 else "")
            )
            claims = assigned_claims[index]
            claim_ids = {str(c.get("claim_id") or "") for c in claims}
            evidence = [
                ep
                for claim_id in claim_ids
                for ep in evidence_by_claim.get(claim_id, [])
            ]
            paragraph_markers = _markers_for_evidence(evidence)
            evidence_handles = (
                EvidenceHandleRegistry(evidence) if compact_handles else None
            )
            reference_words, target_words, max_words = word_targets[index]
            payload = _compact_paragraph_payload(
                packet,
                paragraph_index=index,
                paragraph_function=functions[index],
                assigned_claims=claims,
                evidence_packets=evidence,
                allowed_markers=paragraph_markers,
                evidence_handles=evidence_handles,
                secondary_claim_hints=secondary_hints[index],
                previous_paragraph_text=previous_text,
                previous_paragraph_tail=_paragraph_tail(previous_text),
                word_targets=word_targets[index],
            )
            input_char_count = len(json.dumps(payload, ensure_ascii=False))
            diag: dict[str, Any] = {
                "paragraph_index": index,
                "function": functions[index],
                "reference_words": reference_words,
                "target_words": target_words,
                "max_words": max_words,
                "claim_assignment_mode": claim_assignment_mode,
                "claim_ids_source": claim_id_sources[index],
                "input_char_count": input_char_count,
                "assigned_claim_ids": [
                    str(c.get("claim_id") or "") for c in claims
                ],
                "first_attempt_word_count": 0,
                "first_attempt_failures": [],
                "first_attempt_resolved_handles": [],
                "first_attempt_below_reference": False,
                "retry_attempted": False,
                "retry_attempt_word_count": None,
                "retry_attempt_failures": [],
                "retry_attempt_resolved_handles": [],
                "retry_attempt_below_reference": None,
                "evidence_handle_registry": (
                    evidence_handles.to_dict()
                    if evidence_handles is not None
                    else None
                ),
                "below_reference_target": None,
                "call_attempts": 1,
                "accepted": False,
            }
            text, failures, words, resolved_handles = (
                self._call_paragraph_recovery(
                    agent_name="SectionWriterParagraphRecoveryAgent",
                    payload=payload,
                    recovery_system=recovery_system,
                    allowed_markers=paragraph_markers,
                    max_words=max_words,
                    packet=packet,
                    evidence_handles=evidence_handles,
                )
            )
            total_calls += 1
            diag["first_attempt_word_count"] = words
            diag["first_attempt_failures"] = failures
            diag["first_attempt_resolved_handles"] = resolved_handles
            diag["first_attempt_below_reference"] = words < reference_words
            if not failures:
                accepted_paragraphs.append(text)
                diag["accepted"] = True
                diag["below_reference_target"] = words < reference_words
                per_paragraph.append(diag)
                continue

            # At most one retry, and only for actual local hard failures.
            # Never retry merely because a safe paragraph is below the soft
            # reference target.
            retry_payload = dict(payload)
            retry_payload["retry"] = True
            retry_payload["previous_attempt"] = {
                "paragraph_text": (
                    evidence_handles.encode_markers_to_handles(text)
                    if evidence_handles is not None
                    else text
                ),
                "word_count": words,
                "failures": failures,
                "below_reference": words < reference_words,
            }
            retry_text, retry_failures, retry_words, retry_resolved_handles = (
                self._call_paragraph_recovery(
                    agent_name="SectionWriterParagraphRecoveryRetryAgent",
                    payload=retry_payload,
                    recovery_system=recovery_system,
                    allowed_markers=paragraph_markers,
                    max_words=max_words,
                    packet=packet,
                    evidence_handles=evidence_handles,
                )
            )
            total_calls += 1
            diag.update({
                "retry_attempted": True,
                "retry_attempt_word_count": retry_words,
                "retry_attempt_failures": retry_failures,
                "retry_attempt_resolved_handles": retry_resolved_handles,
                "retry_attempt_below_reference": retry_words < reference_words,
                "call_attempts": 2,
            })
            if not retry_failures:
                accepted_paragraphs.append(retry_text)
                diag["accepted"] = True
                diag["below_reference_target"] = retry_words < reference_words
                per_paragraph.append(diag)
                continue
            per_paragraph.append(diag)
            draft.revision_history.append({
                "stage": stage_name,
                "accepted": False,
                "failure_reason": f"paragraph_{index}_hard_failure_after_retry",
                "claim_assignment_mode": claim_assignment_mode,
                "initial_word_count": _word_count(draft.english_text),
                "target_word_budget": word_budget,
                "reference_target_word_count": reference_target_words,
                "hard_max_word_count": hard_max_words,
                "expected_paragraph_count": expected_paragraphs,
                "assembled_paragraph_count": len(accepted_paragraphs),
                "total_paragraph_calls": total_calls,
                "per_paragraph_input_char_counts": [
                    row.get("input_char_count", 0) for row in per_paragraph
                ],
                "unassigned_claim_ids": (
                    routing_diagnostics.get("unassigned_claim_ids", [])
                    if routing_diagnostics is not None
                    else []
                ),
                "routing_diagnostics": routing_diagnostics,
                "per_paragraph": per_paragraph,
            })
            return False, ""

        assembled = normalize_reference_markers(
            "\n\n".join(accepted_paragraphs).strip(), packet
        )
        assembled_words = _word_count(assembled)
        assembled_paragraphs = _paragraph_count(assembled)
        unknown_markers = (
            set(re.findall(r"\[REF:([^\]]+)\]", assembled)) - allowed_markers
        )
        length_safe = assembled_words <= hard_max_words
        no_regression = assembled_words >= _word_count(draft.english_text)
        reference_safe = not unknown_markers
        structure_safe = assembled_paragraphs == expected_paragraphs
        below_reference_target = assembled_words < reference_target_words
        accepted = bool(
            assembled
            and reference_safe
            and structure_safe
            and length_safe
            and no_regression
        )
        draft.revision_history.append({
            "stage": stage_name,
            "accepted": accepted,
            "claim_assignment_mode": claim_assignment_mode,
            "failure_reason": (
                ""
                if accepted
                else (
                    "unknown_reference_markers"
                    if not reference_safe
                    else "structure_mismatch"
                    if not structure_safe
                    else "length_out_of_contract"
                    if not length_safe
                    else "no_useful_improvement"
                )
            ),
            "initial_word_count": _word_count(draft.english_text),
            "candidate_word_count": assembled_words,
            "target_word_budget": word_budget,
            "reference_target_word_count": reference_target_words,
            "hard_max_word_count": hard_max_words,
            "below_reference_target": below_reference_target,
            "assembled_paragraph_count": assembled_paragraphs,
            "expected_paragraph_count": expected_paragraphs,
            "unknown_reference_markers": sorted(unknown_markers),
            "meets_80_percent_budget": assembled_words >= int(word_budget * 0.80),
            "total_paragraph_calls": total_calls,
            "per_paragraph_input_char_counts": [
                row.get("input_char_count", 0) for row in per_paragraph
            ],
            "unassigned_claim_ids": (
                routing_diagnostics.get("unassigned_claim_ids", [])
                if routing_diagnostics is not None
                else []
            ),
            "routing_diagnostics": routing_diagnostics,
            "per_paragraph": per_paragraph,
        })
        return accepted, assembled


# --------------------------------------------------------------------------- #
# CitationBinder
# --------------------------------------------------------------------------- #

class CitationBinder:
    """Binds citations to sentences in a section draft."""

    def __init__(
        self,
        model_tier: str = "advanced_model",
        prompt_path: Path = CITATION_BINDER_PROMPT,
        real_llm: bool = False,
    ) -> None:
        self.model_tier = model_tier
        self.prompt_path = prompt_path
        self.real_llm = real_llm

    def bind(
        self,
        draft: SectionDraft,
        packet: SectionMaterialPacket,
    ) -> SectionDraft:
        draft.english_text = _normalize_compound_reference_markers(draft.english_text)
        draft.english_text = normalize_reference_markers(
            draft.english_text, packet
        )
        # Citation entailment is recomputed from the current text and current
        # evidence packet.  Keeping failures from an older packet makes a
        # successfully repaired citation look permanently broken downstream.
        citation_flag_types = {
            "citation_entailment_failure",
            "uncited_after_entailment_rejection",
        }
        draft.overclaim_flags = [
            flag for flag in draft.overclaim_flags
            if not (
                isinstance(flag, dict)
                and str(flag.get("overclaim_type") or "") in citation_flag_types
            )
        ]
        if not self.real_llm:
            draft.status = "cited"
            return draft

        # The writer already emits auditable [REF:paper_id] markers.  Resolve
        # those deterministically before asking another model to rewrite the
        # whole section.  This avoids identifier drift (paper IDs returned
        # where canonical chunk IDs are required) and is faster and safer.
        coverage_packets: list[EvidencePacket] = []
        for source in (packet.literature_coverage.get("sources") or []):
            if not isinstance(source, dict) or not source.get("paper_id"):
                continue
            for chunk in (source.get("representative_chunks") or [])[:2]:
                if not isinstance(chunk, dict) or not chunk.get("chunk_id"):
                    continue
                coverage_packets.append(EvidencePacket(
                    claim_id="",
                    paper_id=str(source.get("paper_id") or ""),
                    chunk_id=str(chunk.get("chunk_id") or ""),
                    exact_spans=[str(chunk.get("text_preview") or "")],
                    support_relation="chapter_literature_context",
                    limitations=[
                        "Supports chapter-level context or synthesis; precise factual claims require direct verification."
                    ],
                    evidence_level="fulltext",
                    source_kind="fulltext",
                    scope_fit="in_domain",
                    retrieval_role="literature_context",
                    source_title=str(source.get("title") or chunk.get("title") or ""),
                ))
        packets = [
            ep for ep in list(packet.evidence_packets) + coverage_packets
            if ep.chunk_id and ep.paper_id
        ]
        sentences = re.split(r"(?<=[.!?])\s+", draft.english_text.strip())
        analogy_terms = (
            "analogy", "analogous", "method transfer", "transferable method",
            "cross-domain", "outside the target domain", "in a different domain",
            "parallel", "parallels", "by comparison", "as an example from",
        )

        def packet_allowed_for_sentence(ep: EvidencePacket, sentence: str) -> bool:
            if ep.retrieval_role == "literature_context":
                return True
            if ep.scope_fit == "off_domain" or ep.retrieval_role in {"reject", "background_only"}:
                return False
            if ep.scope_fit == "cross_domain_analogy" or ep.retrieval_role == "method_transfer":
                return any(term in sentence.lower() for term in analogy_terms)
            return True

        def marker_matches_packet(marker: str, ep: EvidencePacket) -> bool:
            """Resolve only exact source or source-claim markers.

            A marker such as ``paper:S01-C01`` must never resolve to another
            claim from the same paper merely because the paper prefix matches.
            """
            if marker == ep.paper_id:
                return True
            return bool(ep.claim_id and marker == f"{ep.paper_id}:{ep.claim_id}")

        # Add a citation only for very high-overlap, currently uncited factual
        # sentences.  This catches citation markers accidentally dropped by a
        # later editor while remaining fail-closed for weak semantic matches.
        stop = {
            "about", "after", "again", "against", "between", "could", "further",
            "however", "their", "therefore", "these", "those", "through", "while",
            "which", "would", "section", "review", "study", "results", "using",
        }
        def tokens(text: str) -> set[str]:
            return {
                word for word in re.findall(r"[a-z][a-z0-9-]{4,}", text.lower())
                if word not in stop
            }

        def requires_direct_entailment(sentence: str) -> bool:
            plain = re.sub(r"\[REF:[^\]]+\]", "", sentence)
            return bool(
                re.search(r"\b\d+(?:\.\d+)?\s*(?:nm|um|\u03bcm|mm|cm|%|K|W|dB|eV|Hz)\b", plain, re.I)
                or re.search(
                    r"\b(?:measured|reported|demonstrated|observed|achieved|increased|"
                    r"decreased|caused|resulted in|outperformed)\b",
                    plain,
                    re.I,
                )
            )
        # Never manufacture a replacement citation for an uncited sentence.
        # In particular, a marker rejected by the entailment audit must not be
        # "laundered" on a later bind pass through broad lexical similarity to
        # another claim from the same topic.  Writers/editors must preserve an
        # explicit source marker; otherwise the sentence remains visibly
        # uncited and is handled by the quality gate or an evidence-aware
        # revision round.

        sentences = re.split(r"(?<=[.!?])\s+", draft.english_text.strip())
        deterministic_map: dict[str, list[str]] = {}
        marker_count = 0
        resolved_count = 0
        for index, sentence in enumerate(sentences):
            resolved: list[str] = []
            for marker in re.findall(r"\[REF:([^\]]+)\]", sentence):
                marker_count += 1
                matches = [
                    ep for ep in packets
                    if packet_allowed_for_sentence(ep, sentence)
                    and marker_matches_packet(marker, ep)
                ]
                if matches:
                    resolved_count += 1
                    resolved.extend(ep.chunk_id for ep in matches)
            if resolved:
                deterministic_map[str(index)] = list(dict.fromkeys(resolved))
        # Identifier resolution alone is not evidence entailment.  Audit each
        # cited sentence against the best matching local passage.  Clear cases
        # are accepted deterministically; ambiguous cases are sent as small,
        # parallel payloads to the top-tier judge instead of one huge section
        # request that is slow and prone to missing clause-level mismatches.
        claim_by_id = {
            str(claim.get("claim_id") or ""): claim
            for claim in packet.claims if isinstance(claim, dict)
        }
        sentence_parts = re.split(r"(?<=[.!?])(\s+)", draft.english_text.strip())
        audited_sentences = sentence_parts[::2]
        separators = sentence_parts[1::2]
        accepted_map: dict[str, list[str]] = {}
        removed_markers: list[dict[str, str]] = []
        audit_failures: list[dict[str, str]] = []
        audit_decisions: list[dict[str, Any]] = []
        pending: list[dict[str, Any]] = []

        for index, sentence in enumerate(audited_sentences):
            sentence_tokens = tokens(re.sub(r"\[REF:[^\]]+\]", "", sentence))
            for marker in re.findall(r"\[REF:([^\]]+)\]", sentence):
                matches = [
                    ep for ep in packets
                    if packet_allowed_for_sentence(ep, sentence)
                    and marker_matches_packet(marker, ep)
                ]
                if not matches:
                    removed_markers.append({
                        "sentence_index": str(index), "marker": marker,
                        "claim_id": next(
                            (cid for cid in claim_by_id if marker.endswith(f":{cid}")), ""
                        ),
                        "reason": "identifier_or_scope_not_allowed",
                    })
                    continue
                ranked_matches = sorted(
                    matches,
                    key=lambda ep: (
                        0 if ep.retrieval_role == "literature_context" else 1,
                        len(sentence_tokens & tokens(" ".join(ep.exact_spans))),
                    ),
                    reverse=True,
                )
                ep = ranked_matches[0]
                evidence_tokens = tokens(" ".join(ep.exact_spans))
                claim_tokens = tokens(str(claim_by_id.get(ep.claim_id, {}).get("statement") or ""))
                evidence_overlap = len(sentence_tokens & evidence_tokens)
                evidence_coverage = evidence_overlap / max(1, len(sentence_tokens))
                claim_overlap = len(sentence_tokens & claim_tokens)
                contextual_accept = bool(
                    ep.retrieval_role == "literature_context"
                    and not requires_direct_entailment(sentence)
                    and evidence_overlap >= 1
                )
                deterministic_accept = (
                    contextual_accept
                    or (
                        evidence_overlap >= 8
                        and evidence_coverage >= 0.40
                        and claim_overlap >= 5
                    )
                )
                if deterministic_accept:
                    accepted_map.setdefault(str(index), []).append(ep.chunk_id)
                    audit_decisions.append({
                        "sentence_index": str(index),
                        "marker": marker,
                        "chunk_id": ep.chunk_id,
                        "decision": (
                            "accepted_chapter_context_relevance"
                            if contextual_accept else "accepted_deterministic_high_overlap"
                        ),
                        "evidence_overlap": evidence_overlap,
                        "evidence_coverage": round(evidence_coverage, 3),
                    })
                else:
                    pending.append({
                        "sentence_index": index,
                        "marker": marker,
                        "packet": ep,
                        "sentence": sentence,
                    })

        system = _read_prompt(CITATION_ENTAILMENT_PROMPT)

        def judge(item: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
            ep: EvidencePacket = item["packet"]
            payload = {
                "sentence": re.sub(r"\[REF:[^\]]+\]", "", item["sentence"]).strip(),
                "claim": claim_by_id.get(ep.claim_id, {}),
                "evidence": ep.to_dict(),
            }
            result = call_qwen_chat(
                "CitationEntailmentJudge",
                [
                    {"role": "system", "content": system},
                    {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
                ],
                model_tier=self.model_tier,
                temperature=0,
                max_tokens=500,
                response_format={"type": "json_object"},
                force_mock=False,
                max_retries=0,
                timeout_seconds=60,
                max_transport_key_candidates=1,
                allow_model_fallback=False,
            )
            return item, _safe_json(str(result.get("content") or ""))

        if pending:
            with ThreadPoolExecutor(max_workers=min(4, len(pending))) as pool:
                futures = [pool.submit(judge, item) for item in pending]
                for future in as_completed(futures):
                    try:
                        item, decision = future.result()
                    except Exception as exc:
                        audit_failures.append({"reason": type(exc).__name__})
                        continue
                    ep = item["packet"]
                    supported = bool(decision.get("supported"))
                    support_type = str(decision.get("support_type") or "unsupported")
                    confidence = str(decision.get("confidence") or "low")
                    if supported and support_type in {"direct", "partial", "analogy"} and confidence in {"high", "medium"}:
                        accepted_map.setdefault(str(item["sentence_index"]), []).append(ep.chunk_id)
                        audit_decisions.append({
                            "sentence_index": str(item["sentence_index"]),
                            "marker": str(item["marker"]),
                            "chunk_id": ep.chunk_id,
                            "decision": "accepted_llm_entailment",
                            "support_type": support_type,
                            "confidence": confidence,
                            "reason": _compact(decision.get("reason"), 400),
                        })
                    else:
                        removed_markers.append({
                            "sentence_index": str(item["sentence_index"]),
                            "marker": str(item["marker"]),
                            "claim_id": ep.claim_id,
                            "reason": _compact(decision.get("reason") or "semantic_support_not_verified", 300),
                        })

        cleaned_sentences: list[str] = []
        for index, sentence in enumerate(audited_sentences):
            for removal in [row for row in removed_markers if row["sentence_index"] == str(index)]:
                sentence = sentence.replace(f"[REF:{removal['marker']}]", "").replace("  ", " ")
            sentence = re.sub(r"\s+([,.;:!?])", r"\1", sentence)
            cleaned_sentences.append(sentence.strip())
        rebuilt: list[str] = []
        for index, sentence in enumerate(cleaned_sentences):
            rebuilt.append(sentence)
            if index < len(separators):
                rebuilt.append(separators[index])
        draft.english_text = "".join(rebuilt)
        draft.citation_map = {
            key: list(dict.fromkeys(values)) for key, values in accepted_map.items() if values
        }
        if removed_markers or audit_failures:
            draft.overclaim_flags.append({
                "overclaim_type": "citation_entailment_failure",
                "issue": "Citation marker removed or quarantined because sentence-level semantic support was not verified.",
                "removed_markers": removed_markers,
                "audit_failures": audit_failures,
                "sentence_fragment": "",
                "revised_sentence": "",
            })
        for sentence_index in sorted({row["sentence_index"] for row in removed_markers}):
            if accepted_map.get(sentence_index):
                continue
            claim_ids = list(dict.fromkeys(
                str(row.get("claim_id") or "")
                for row in removed_markers
                if row["sentence_index"] == sentence_index and row.get("claim_id")
            ))
            factual_claim_ids = [
                claim_id for claim_id in claim_ids
                if str(claim_by_id.get(claim_id, {}).get("writing_permission") or "")
                in {"factual_assertion", "hedged_factual_assertion"}
            ]
            if not factual_claim_ids:
                continue
            source_sentence = cleaned_sentences[int(sentence_index)]
            draft.overclaim_flags.append({
                "overclaim_type": "uncited_after_entailment_rejection",
                "issue": (
                    "All proposed citations for this factual sentence failed the sentence-level "
                    "entailment check; the sentence must be narrowed, re-evidenced, or removed."
                ),
                "claim_ids": factual_claim_ids,
                "sentence_fragment": source_sentence,
                "revised_sentence": "",
            })
        draft.revision_history.append({
            "stage": "citation_entailment_audit",
            "accepted_citations": sum(len(values) for values in draft.citation_map.values()),
            "removed_citations": len(removed_markers),
            "audit_failures": audit_failures,
            "decisions": sorted(audit_decisions, key=lambda row: int(row["sentence_index"])),
        })
        draft.status = "citation_audit_degraded" if audit_failures else "cited"
        return draft


# --------------------------------------------------------------------------- #
# OverclaimAuditor
# --------------------------------------------------------------------------- #

class OverclaimAuditor:
    """Flags overclaims and unsupported generalizations."""

    def __init__(
        self,
        model_tier: str = "advanced_model",
        prompt_path: Path = OVERCLAIM_AUDITOR_PROMPT,
        real_llm: bool = False,
    ) -> None:
        self.model_tier = model_tier
        self.prompt_path = prompt_path
        self.real_llm = real_llm

    def audit(self, draft: SectionDraft, packet: SectionMaterialPacket) -> SectionDraft:
        if not self.real_llm:
            draft.status = "audited"
            return draft

        prior_flags = list(draft.overclaim_flags)
        system = _read_prompt(self.prompt_path)
        payload = {
            "section_text": draft.english_text,
            "claims": packet.claims,
            "evidence_packets": [ep.to_dict() for ep in packet.evidence_packets],
            "prior_overclaim_flags": prior_flags,
        }
        output_budget = min(7000, max(3200, _word_count(draft.english_text) * 2 + 1200))
        result = call_qwen_chat(
            "OverclaimAuditorAgent",
            [
                {"role": "system", "content": system},
                {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
            ],
            model_tier=self.model_tier,
            temperature=0,
            max_tokens=output_budget,
            response_format={"type": "json_object"},
            stream=True,
            force_mock=False,
            max_retries=0,
            timeout_seconds=90,
            max_transport_key_candidates=1,
            accept_partial_stream=False,
            enable_thinking=False,
            allow_model_fallback=False,
        )
        parsed = _safe_json(str(result.get("content") or ""))
        if not (isinstance(parsed, dict) and "overclaim_flags" in parsed):
            # A malformed critic response is a recoverable process fault, not
            # a reason to discard a scientifically grounded draft.
            repair = call_qwen_chat(
                "OverclaimAuditorRepairAgent",
                [
                    {"role": "system", "content": system + "\nReturn the required JSON object only."},
                    {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
                ],
                model_tier="premium_model",
                temperature=0,
                max_tokens=output_budget,
                response_format={"type": "json_object"},
                stream=True,
                force_mock=False,
                max_retries=0,
                timeout_seconds=120,
                max_transport_key_candidates=2,
                accept_partial_stream=False,
                enable_thinking=False,
                allow_model_fallback=False,
            )
            parsed = _safe_json(str(repair.get("content") or ""))
        if isinstance(parsed, dict) and "overclaim_flags" in parsed:
            new_flags = list(parsed.get("overclaim_flags") or [])
            draft.overclaim_flags = prior_flags + [
                flag for flag in new_flags if flag not in prior_flags
            ]
            revised = parsed.get("revised_text")
            if revised:
                revised_text = _normalize_compound_reference_markers(str(revised).strip())
                source_markers = set(re.findall(r"\[REF:[^\]]+\]", draft.english_text))
                revised_markers = set(re.findall(r"\[REF:[^\]]+\]", revised_text))
                original_words = _word_count(draft.english_text)
                revised_words = _word_count(revised_text)
                original_paragraphs = _paragraph_count(draft.english_text)
                revised_paragraphs = _paragraph_count(revised_text)
                revision_safe = bool(
                    revised_text
                    and revised_markers.issubset(source_markers)
                    and revised_words >= max(20, int(original_words * 0.60))
                    and revised_words <= max(40, int(original_words * 1.20))
                    and revised_paragraphs >= max(1, original_paragraphs - 1)
                )
                if revision_safe:
                    draft.english_text = revised_text
                else:
                    draft.overclaim_flags.append({
                        "sentence_fragment": "",
                        "overclaim_type": "unsafe_full_revision_rejected",
                        "issue": (
                            "The proposed full-section overclaim revision changed citations, "
                            "length, or paragraph structure beyond the safety boundary."
                        ),
                        "revised_sentence": "",
                    })
            else:
                # Apply sentence-level fixes only when the critic quotes the
                # exact source sentence and does not introduce new references.
                for flag in draft.overclaim_flags:
                    if not isinstance(flag, dict):
                        continue
                    source = str(flag.get("sentence_fragment") or "")
                    replacement = str(flag.get("revised_sentence") or "")
                    if not source or not replacement or source not in draft.english_text:
                        continue
                    source_refs = set(re.findall(r"\[REF:[^\]]+\]", source))
                    replacement_refs = set(re.findall(r"\[REF:[^\]]+\]", replacement))
                    if not replacement_refs.issubset(source_refs):
                        continue
                    if not (0.35 <= len(replacement) / max(1, len(source)) <= 2.5):
                        continue
                    draft.english_text = draft.english_text.replace(source, replacement, 1)
        else:
            draft.overclaim_flags = prior_flags + [{
                "sentence_fragment": "",
                "overclaim_type": "audit_failure",
                "issue": "The overclaim auditor did not return a valid structured result.",
                "revised_sentence": "",
            }]
            draft.status = "audited_degraded"

        # Deterministic backstop for a frequent review-writing failure: a few
        # selected papers cannot establish prevalence across an entire field.
        prevalence_patterns = [
            (r"\bthe vast majority of studies\b", "the studies represented in the available evidence"),
            (
                r"\b(?:recent|existing|current) (?:conceptual )?"
                r"(?:frameworks|reviews|studies) (?:often|generally|commonly) "
                r"(?:characterize|frame|describe)\b",
                "This review frames",
            ),
            (
                r"\bthe field (?:often|generally|commonly) "
                r"(?:characterizes|frames|describes)\b",
                "This review frames",
            ),
            (
                r"\bmost ((?:[a-z][a-z-]*\s+){1,4})studies\b",
                r"many \1studies represented in the available evidence",
            ),
            (r"\bmost studies\b", "many studies represented in the available evidence"),
            (r"\brarely reported\b", "not consistently reported in the available evidence"),
            (r"\bseldom evaluated\b", "not consistently evaluated in the available evidence"),
            (r"\bexceptional rather than representative of the field broadly\b", "not yet demonstrably representative of the broader field"),
        ]
        has_corpus_evidence = any(
            ep.support_relation in {"corpus_prevalence", "systematic_review", "bibliometric_analysis"}
            for ep in packet.evidence_packets
        )
        if not has_corpus_evidence:
            for pattern, replacement in prevalence_patterns:
                for match in list(re.finditer(pattern, draft.english_text, flags=re.I)):
                    fragment = match.group(0)
                    if not any(
                        f.get("overclaim_type") == "unsupported_prevalence"
                        and str(f.get("sentence_fragment", "")).lower() == fragment.lower()
                        for f in draft.overclaim_flags
                    ):
                        draft.overclaim_flags.append({
                            "sentence_fragment": fragment,
                            "overclaim_type": "unsupported_prevalence",
                            "issue": "Selected evidence examples do not establish field-wide prevalence.",
                            "revised_sentence": replacement,
                        })
                    draft.english_text = re.sub(pattern, replacement, draft.english_text, flags=re.I)

            # The LLM sometimes repairs an absolute such as "no material can"
            # into the equally unsupported prevalence claim "few materials
            # can".  Rewrite only uncited sentences, preserving supported
            # prevalence statements that carry an explicit source marker.
            sentence_parts = re.split(r"(?<=[.!?])(\s+)", draft.english_text)
            sentences = sentence_parts[::2]
            separators = sentence_parts[1::2]
            repaired_parts: list[str] = []
            entity_nouns = (
                r"materials|methods|approaches|systems|devices|platforms|"
                r"models|architectures|strategies"
            )
            most_pattern = re.compile(
                rf"\bmost\s+(((?:[a-z][a-z-]*\s+){{0,4}}(?:{entity_nouns})))\b",
                flags=re.I,
            )
            few_pattern = re.compile(
                rf"\bfew\s+(((?:[a-z][a-z-]*\s+){{0,2}}(?:{entity_nouns})"
                rf"(?:\s+or\s+(?:[a-z][a-z-]*\s+){{0,2}}(?:{entity_nouns}))?))\s+can\b",
                flags=re.I,
            )
            for sentence in sentences:
                revised_sentence = sentence
                if "[REF:" not in sentence:
                    revised_sentence = most_pattern.sub(
                        lambda m: ("Some " if m.group(0)[0].isupper() else "some ") + m.group(1),
                        revised_sentence,
                    )
                    revised_sentence = few_pattern.sub(
                        lambda m: (
                            ("It" if m.group(0)[0].isupper() else "it")
                            + " remains unclear how many "
                            + m.group(1)
                            + " can"
                        ),
                        revised_sentence,
                    )
                if revised_sentence != sentence:
                    draft.overclaim_flags.append({
                        "sentence_fragment": sentence,
                        "overclaim_type": "unsupported_prevalence",
                        "issue": (
                            "An uncited frequency claim was introduced while hedging an "
                            "absolute proposition."
                        ),
                        "revised_sentence": revised_sentence,
                        "resolved": True,
                        "resolution_status": "rewritten_as_bounded_uncertainty",
                    })
                repaired_parts.append(revised_sentence)
            rebuilt_parts: list[str] = []
            for index, sentence in enumerate(repaired_parts):
                rebuilt_parts.append(sentence)
                if index < len(separators):
                    rebuilt_parts.append(separators[index])
            draft.english_text = "".join(rebuilt_parts)
        # Conservative lexical backstop for partially grounded physical-limit
        # claims.  Absolute language is inappropriate unless the evidence
        # packet establishes a formal theorem or bound.
        if any(c.get("writing_permission") == "hedged_factual_assertion" for c in packet.claims):
            absolute_rewrites = {
                r"\ban absolute thermodynamic bound\b": "fundamental thermodynamic constraints",
                r"\ba hard physical ceiling\b": "a fundamental physical constraint",
                r"\ban inescapable design constraint\b": "a fundamental design constraint",
                r"\binescapable constraints\b": "fundamental constraints",
            }
            for pattern, replacement in absolute_rewrites.items():
                draft.english_text = re.sub(pattern, replacement, draft.english_text, flags=re.I)

        # Review prose should state its proposition directly. Generated
        # "not X but Y" contrasts often invent a weak position that neither
        # readers nor the literature actually hold. Repair only the affected
        # sentences and preserve every reference marker and number.
        draft = self._refine_strawman_contrasts(draft)

        # Entailment-rejection flags describe the pre-audit sentence.  Once
        # that exact proposition has been removed or rewritten, keeping the
        # old flag as unresolved makes every downstream gate fail even though
        # the repair succeeded.  Preserve the finding for provenance, but
        # make its lifecycle explicit.  The independent quality judge still
        # reviews the replacement and can fail the section if the new wording
        # remains unsupported.
        resolved_rejections = 0
        unresolved_rejections = 0
        for flag in draft.overclaim_flags:
            if not isinstance(flag, dict) or flag.get("overclaim_type") != "uncited_after_entailment_rejection":
                continue
            source = str(flag.get("sentence_fragment") or "").strip()
            resolved = bool(source and source not in draft.english_text)
            flag["resolved"] = resolved
            flag["resolution_status"] = (
                "reworded_or_removed_after_audit"
                if resolved else "still_present_after_audit"
            )
            if resolved:
                resolved_rejections += 1
            else:
                unresolved_rejections += 1
        if resolved_rejections or unresolved_rejections:
            draft.revision_history.append({
                "stage": "entailment_rejection_resolution",
                "resolved": resolved_rejections,
                "unresolved": unresolved_rejections,
            })
        draft.status = "audited"
        return draft

    def _refine_strawman_contrasts(self, draft: SectionDraft) -> SectionDraft:
        patterns = (
            re.compile(r"\bnot\b[^.!?]{0,140}\bbut\b", re.I),
            re.compile(r"\brather than merely\b", re.I),
            re.compile(r"\bmore than (?:just|merely)\b", re.I),
            re.compile(r"\bnot (?:simply|merely|just)\b", re.I),
        )
        sentences = [
            row.strip()
            for row in re.split(r"(?<=[.!?])\s+", draft.english_text)
            if row.strip()
        ]
        targets = [
            sentence for sentence in sentences
            if any(pattern.search(sentence) for pattern in patterns)
        ]
        if not targets:
            return draft
        prompt = _read_prompt(POSITIVE_ASSERTION_REFINER_PROMPT)
        parsed: dict[str, Any] = {}
        try:
            result = call_qwen_chat(
                "PositiveAssertionStyleRefiner",
                [
                    {"role": "system", "content": prompt},
                    {"role": "user", "content": json.dumps({"sentences": targets}, ensure_ascii=False)},
                ],
                model_tier=self.model_tier,
                temperature=0,
                max_tokens=min(1800, 260 + 260 * len(targets)),
                response_format={"type": "json_object"},
                force_mock=False,
                max_retries=0,
                timeout_seconds=75,
                max_transport_key_candidates=1,
                allow_model_fallback=False,
            )
            value = _safe_json(str(result.get("content") or ""))
            parsed = value if isinstance(value, dict) else {}
        except Exception:
            parsed = {}
        repaired = 0
        for row in parsed.get("replacements") or []:
            if not isinstance(row, dict):
                continue
            source = str(row.get("source") or "").strip()
            replacement = str(row.get("replacement") or "").strip()
            if source not in targets or source not in draft.english_text or not replacement:
                continue
            if any(pattern.search(replacement) for pattern in patterns):
                continue
            if set(re.findall(r"\[REF:[^\]]+\]", source)) != set(
                re.findall(r"\[REF:[^\]]+\]", replacement)
            ):
                continue
            if re.findall(r"\d+(?:\.\d+)?", source) != re.findall(r"\d+(?:\.\d+)?", replacement):
                continue
            if not (0.45 <= len(replacement) / max(1, len(source)) <= 1.8):
                continue
            draft.english_text = draft.english_text.replace(source, replacement, 1)
            repaired += 1
        remaining = [
            sentence for sentence in re.split(r"(?<=[.!?])\s+", draft.english_text)
            if any(pattern.search(sentence) for pattern in patterns)
        ]
        draft.revision_history.append({
            "stage": "positive_assertion_style_refinement",
            "detected": len(targets),
            "repaired": repaired,
            "remaining": len(remaining),
        })
        for sentence in remaining:
            draft.overclaim_flags.append({
                "sentence_fragment": sentence.strip(),
                "overclaim_type": "strawman_contrast_style",
                "issue": "A contrast formula introduces an unnecessary or undocumented weak position.",
                "revised_sentence": "",
                "resolved": False,
            })
        return draft


# --------------------------------------------------------------------------- #
# ContradictionEditor
# --------------------------------------------------------------------------- #

class ContradictionEditor:
    """Edits text to appropriately handle contradictory evidence."""

    def __init__(
        self,
        model_tier: str = "advanced_model",
        prompt_path: Path = CONTRADICTION_EDITOR_PROMPT,
        real_llm: bool = False,
    ) -> None:
        self.model_tier = model_tier
        self.prompt_path = prompt_path
        self.real_llm = real_llm

    def edit(self, draft: SectionDraft, packet: SectionMaterialPacket) -> SectionDraft:
        if not packet.contradictions or not self.real_llm:
            draft.status = "edited"
            return draft

        system = _read_prompt(self.prompt_path)
        payload = {
            "section_text": draft.english_text,
            "contradictions": packet.contradictions,
        }
        result = call_qwen_chat(
            "ContradictionEditorAgent",
            [
                {"role": "system", "content": system},
                {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
            ],
            model_tier=self.model_tier,
            temperature=0.1,
            max_tokens=2048,
            response_format={"type": "json_object"},
            force_mock=False,
            max_retries=1,
        )
        parsed = _safe_json(str(result.get("content") or ""))
        if isinstance(parsed, dict):
            draft.contradiction_notes = list(parsed.get("contradiction_notes") or [])
            revised = parsed.get("revised_text")
            if revised:
                draft.english_text = str(revised)
        draft.status = "edited"
        return draft


# --------------------------------------------------------------------------- #
# CrossSectionEditor
# --------------------------------------------------------------------------- #

class CrossSectionEditor:
    """Deduplicates repeated content and adds transitions across sections."""

    def __init__(
        self,
        model_tier: str = "advanced_model",
        prompt_path: Path = CROSS_SECTION_EDITOR_PROMPT,
        real_llm: bool = False,
    ) -> None:
        self.model_tier = model_tier
        self.prompt_path = prompt_path
        self.real_llm = real_llm

    def edit(
        self,
        drafts: list[SectionDraft],
        packets: list[SectionMaterialPacket],
        audit_findings: list[dict[str, Any]] | None = None,
    ) -> list[SectionDraft]:
        if not self.real_llm or len(drafts) < 2:
            return drafts

        system = _read_prompt(self.prompt_path)
        findings_by_section: dict[str, list[dict[str, Any]]] = {}
        for finding in audit_findings or []:
            section_id = str(finding.get("section_id") or "")
            if section_id:
                findings_by_section.setdefault(section_id, []).append(finding)

        def boundary(text: str, *, head: bool) -> str:
            paragraphs = [row.strip() for row in re.split(r"\n\s*\n", text) if row.strip()]
            selected = paragraphs[:2] if head else paragraphs[-2:]
            return "\n\n".join(selected)

        def propose(index: int) -> tuple[str, list[dict[str, Any]]]:
            draft = drafts[index]
            packet = packets[index]
            previous = drafts[index - 1] if index > 0 else None
            following = drafts[index + 1] if index + 1 < len(drafts) else None
            payload = {
                "editable_section": {
                    "section_id": draft.section_id,
                    "text": draft.english_text,
                    "transition_from": packet.transition_contract.get(
                        "transition_from_previous", ""
                    ),
                    "transition_to": packet.transition_contract.get(
                        "transition_to_next", ""
                    ),
                },
                "previous_section_tail": {
                    "section_id": previous.section_id if previous else "",
                    "text": boundary(previous.english_text, head=False) if previous else "",
                },
                "next_section_opening": {
                    "section_id": following.section_id if following else "",
                    "text": boundary(following.english_text, head=True) if following else "",
                },
                "deterministic_continuity_findings": findings_by_section.get(
                    draft.section_id, []
                ),
            }
            messages = [
                {"role": "system", "content": system},
                {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
            ]
            parsed: dict[str, Any] = {}
            # Keep retries explicit and bounded.  A hidden multi-model fallback
            # chain multiplied across every section made a one-line continuity
            # repair take many minutes and exposed clean sections to needless
            # editing attempts.
            tiers = [self.model_tier]
            if self.model_tier != "b_plus_model":
                tiers.append("b_plus_model")
            for attempt_index, tier in enumerate(tiers, 1):
                result = call_qwen_chat(
                    f"CrossSectionEditorAgent:attempt_{attempt_index}",
                    messages,
                    model_tier=tier,
                    temperature=0.2,
                    max_tokens=2400,
                    response_format={"type": "json_object"},
                    stream=True,
                    force_mock=False,
                    max_retries=0,
                    timeout_seconds=150,
                    max_transport_key_candidates=1,
                    allow_model_fallback=False,
                )
                parsed = _safe_json(str(result.get("content") or ""))
                if parsed:
                    break
            operations = parsed.get("operations") if isinstance(parsed, dict) else []
            return draft.section_id, [
                row for row in (operations or [])
                if isinstance(row, dict)
                and str(row.get("section_id") or "") == draft.section_id
            ]

        # Whole-manuscript calls are slow and fragile at publication scale.
        # Each section is edited with its adjacent boundary context, while the
        # deterministic global audit supplies defects found outside that local
        # window.  Independent proposals can safely run in parallel because an
        # operation is allowed to modify only its editable section.
        target_indices = [
            index for index, draft in enumerate(drafts)
            if findings_by_section.get(draft.section_id)
        ]
        if not target_indices:
            return drafts
        proposed: dict[str, list[dict[str, Any]]] = {}
        with ThreadPoolExecutor(max_workers=min(3, len(target_indices))) as pool:
            futures = [pool.submit(propose, index) for index in target_indices]
            for future in as_completed(futures):
                try:
                    section_id, operations = future.result()
                    proposed[section_id] = operations
                except Exception:
                    continue

        by_id = {draft.section_id: draft for draft in drafts}
        originals = {draft.section_id: draft.english_text for draft in drafts}
        for section_id, operations in proposed.items():
            draft = by_id[section_id]
            for operation in operations:
                kind = str(operation.get("operation") or "")
                target = str(operation.get("target_text") or "")
                replacement = str(operation.get("replacement_text") or "")
                if kind == "remove_exact" and target and target in draft.english_text:
                    draft.english_text = draft.english_text.replace(target, "", 1)
                elif kind == "replace_exact" and target and target in draft.english_text:
                    draft.english_text = draft.english_text.replace(target, replacement, 1)
                elif kind == "prepend_transition" and replacement:
                    draft.english_text = replacement.strip() + "\n\n" + draft.english_text
                elif kind == "append_transition" and replacement:
                    draft.english_text = draft.english_text.rstrip() + "\n\n" + replacement.strip()

        # Fail closed: a coherence pass may make local edits, but it may never
        # silently summarize away a section.
        for draft in drafts:
            original = originals[draft.section_id]
            if len(draft.english_text) < int(len(original) * 0.80):
                draft.english_text = original
        return drafts


def audit_manuscript_continuity(
    drafts: list[SectionDraft],
    packets: list[SectionMaterialPacket],
) -> dict[str, Any]:
    """Detect manuscript-level defects that single-section judges cannot see."""
    packet_by_id = {packet.section_id: packet for packet in packets}
    findings: list[dict[str, Any]] = []
    acronym_owner: dict[str, str] = {}
    body_closures = (
        "in summary",
        "to summarize",
        "taken together",
        "these questions frame",
        "subsequent analysis",
        "subsequent sections",
        "the following section",
        "the next section",
        "this section has shown",
    )
    navigation_markers = (
        "this section will",
        "in this section, we",
        "the following section",
        "the next section",
        "subsequent analysis",
        "subsequent sections",
    )
    first_paragraph_tokens: list[tuple[str, set[str]]] = []

    for index, draft in enumerate(drafts):
        packet = packet_by_id.get(draft.section_id, SectionMaterialPacket(draft.section_id))
        role = str(packet.section_contract.get("section_role") or (
            "introduction" if index == 0 else "synthesis" if index == len(drafts) - 1 else "body"
        )).lower()
        if role not in {"introduction", "synthesis", "conclusion", "outlook"}:
            role = "body"
        paragraphs = [
            value.strip()
            for value in re.split(r"\n\s*\n", draft.english_text)
            if value.strip()
        ]
        opening = paragraphs[0] if paragraphs else draft.english_text
        closing = paragraphs[-1] if paragraphs else draft.english_text
        lowered_opening = opening.lower()
        lowered_closing = closing.lower()

        for full_name, acronym in re.findall(
            r"\b([A-Za-z][A-Za-z-]+(?:\s+[A-Za-z][A-Za-z-]+){1,8})\s*\(([A-Z][A-Z0-9-]{1,10})\)",
            opening,
        ):
            owner = acronym_owner.setdefault(acronym, draft.section_id)
            if owner != draft.section_id and role != "introduction":
                findings.append({
                    "severity": "major",
                    "section_id": draft.section_id,
                    "issue_type": "repeated_topic_definition",
                    "description": (
                        f"{acronym} is expanded again in {draft.section_id}; it was already "
                        f"introduced in {owner}. Continue with the abbreviation instead."
                    ),
                    "evidence": _compact(f"{full_name} ({acronym})", 220),
                })
        if role == "body":
            for marker in body_closures:
                if marker in lowered_closing:
                    findings.append({
                        "severity": "major",
                        "section_id": draft.section_id,
                        "issue_type": "body_mini_conclusion",
                        "description": (
                            f"The body section closes with independent-review language: {marker!r}."
                        ),
                        "evidence": _compact(closing, 500),
                    })
            for marker in navigation_markers:
                if marker in lowered_opening or marker in lowered_closing:
                    findings.append({
                        "severity": "minor",
                        "section_id": draft.section_id,
                        "issue_type": "document_navigation_instead_of_scientific_transition",
                        "description": f"Replace document navigation phrase {marker!r} with a scientific handoff.",
                    })

        tokens = {
            token for token in re.findall(r"[a-z][a-z-]{3,}", lowered_opening)
            if token not in {"this", "that", "with", "from", "have", "been", "review", "section"}
        }
        first_paragraph_tokens.append((draft.section_id, tokens))

    for left_index, (left_id, left) in enumerate(first_paragraph_tokens):
        for right_id, right in first_paragraph_tokens[left_index + 1:]:
            similarity = len(left & right) / max(1, len(left | right))
            if similarity >= 0.55:
                findings.append({
                    "severity": "major",
                    "section_id": right_id,
                    "issue_type": "repeated_section_opening",
                    "description": (
                        f"Opening paragraph substantially repeats {left_id} "
                        f"(token Jaccard={similarity:.2f})."
                    ),
                })

    return {
        "schema_version": "manuscript_continuity_audit.v1",
        "finding_count": len(findings),
        "major_count": sum(row.get("severity") == "major" for row in findings),
        "minor_count": sum(row.get("severity") == "minor" for row in findings),
        "passed": not any(row.get("severity") == "major" for row in findings),
        "findings": findings,
    }


# --------------------------------------------------------------------------- #
# FigurePlanner
# --------------------------------------------------------------------------- #

class FigurePlanner:
    """Plans figure insertions aligned with text arguments."""

    def __init__(
        self,
        model_tier: str = "standard_model",
        prompt_path: Path = FIGURE_PLANNER_PROMPT,
        real_llm: bool = False,
    ) -> None:
        self.model_tier = model_tier
        self.prompt_path = prompt_path
        self.real_llm = real_llm

    def plan(
        self,
        draft: SectionDraft,
        packet: SectionMaterialPacket,
    ) -> SectionDraft:
        visual_refs = list(dict.fromkeys(
            vid
            for ep in packet.evidence_packets
            for vid in ep.visual_refs
            if vid
        ))
        if not visual_refs:
            approved_conceptual = [
                plan for plan in packet.visual_gap_plan
                if isinstance(plan, dict)
                and bool(plan.get("human_approved"))
                and str(plan.get("asset_status") or "") == "approved_ai_conceptual_schematic"
                and str(plan.get("local_image_path") or "")
                and Path(str(plan.get("local_image_path"))).exists()
            ]
            approved_placements = [
                {
                    "visual_ref": str(plan.get("visual_plan_id") or ""),
                    "visual_plan_id": str(plan.get("visual_plan_id") or ""),
                    "placement": "after_relevant_claim_paragraph",
                    "caption_note": (
                        str((plan.get("model_review") or {}).get("approved_caption_boundary") or "")
                        or str(plan.get("argument_role") or "")
                    ),
                    "asset_status": "approved_ai_conceptual_schematic",
                    "creation_class": "author_synthesized_conceptual_schematic",
                    "evidence_status": "explanatory_not_empirical_evidence",
                    "source_paper_id": "AI-generated conceptual schematic",
                    "local_image_path": str(plan.get("local_image_path") or ""),
                    "attribution_required": True,
                    "needs_human_review": False,
                }
                for plan in approved_conceptual
            ]
            pending_placements = [
                {
                    "visual_ref": "",
                    "visual_plan_id": str(plan.get("visual_plan_id") or ""),
                    "placement": "after_relevant_claim_paragraph",
                    "caption_note": str(plan.get("argument_role") or ""),
                    "asset_status": "missing_required_visual",
                    "creation_class": str(plan.get("creation_class") or ""),
                    "permitted_creation_modes": list(plan.get("permitted_creation_modes") or []),
                    "evidence_status": str(plan.get("evidence_status") or ""),
                    "needs_human_review": True,
                }
                for plan in packet.visual_gap_plan
                if isinstance(plan, dict)
                and not bool(plan.get("human_approved"))
            ]
            draft.figure_placements = approved_placements + pending_placements
            return draft
        if not self.real_llm:
            draft.figure_placements = [
                {"visual_ref": v, "placement": "end_of_section", "caption_note": ""}
                for v in visual_refs
            ]
            return draft

        system = _read_prompt(self.prompt_path)
        payload = {
            "section_text": _compact(draft.english_text, 1500),
            "available_visual_refs": visual_refs,
            "available_visual_evidence": packet.visual_evidence,
            "expected_visual_arguments": packet.section_contract.get("expected_visual_arguments", []),
        }
        result = call_qwen_chat(
            "FigurePlannerAgent",
            [
                {"role": "system", "content": system},
                {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
            ],
            model_tier=self.model_tier,
            temperature=0,
            max_tokens=800,
            response_format={"type": "json_object"},
            force_mock=False,
            max_retries=1,
        )
        parsed = _safe_json(str(result.get("content") or ""))
        if isinstance(parsed, dict):
            allowed_refs = set(visual_refs)
            draft.figure_placements = [
                placement for placement in (parsed.get("figure_placements") or [])
                if isinstance(placement, dict)
                and str(placement.get("visual_ref") or "") in allowed_refs
            ]
            visual_by_ref = {
                str(row.get("chunk_id") or ""): row
                for row in packet.visual_evidence if isinstance(row, dict)
            }
            for placement in draft.figure_placements:
                source = visual_by_ref.get(str(placement.get("visual_ref") or ""), {})
                placement.setdefault("source_paper_id", str(source.get("paper_id") or ""))
                placement.setdefault("local_image_path", str(source.get("local_image_path") or ""))
                placement.setdefault("attribution_required", True)
        if not draft.figure_placements:
            # A verified direct visual must not disappear merely because the
            # placement model returned empty/invalid JSON.  Preserve it as an
            # explicit human-review placement rather than fabricating a role.
            primary = visual_refs[0]
            visual = next(
                (row for row in packet.visual_evidence if row.get("chunk_id") == primary),
                {},
            )
            draft.figure_placements = [{
                "visual_ref": primary,
                "placement": "after_relevant_claim_paragraph",
                "caption_note": _compact(visual.get("caption"), 500),
                "source": "deterministic_verified_visual_fallback",
                "source_paper_id": str(visual.get("paper_id") or ""),
                "local_image_path": str(visual.get("local_image_path") or ""),
                "attribution_required": True,
                "needs_human_review": True,
            }]
        return draft


# --------------------------------------------------------------------------- #
# FinalTranslator
# --------------------------------------------------------------------------- #

class FinalTranslator:
    """Translates English section draft to academic Chinese."""

    _MAX_CHUNK_CHARS = 4500
    _CITATION_PATTERN = re.compile(
        r"\[REF:[^\]]+\]|\[\d+(?:\s*[,;\-\u2013]\s*\d+)*\]"
    )

    def __init__(
        self,
        model_tier: str = "advanced_model",
        prompt_path: Path = FINAL_TRANSLATOR_PROMPT,
        real_llm: bool = False,
    ) -> None:
        self.model_tier = model_tier
        self.prompt_path = prompt_path
        self.real_llm = real_llm

    @classmethod
    def _split_translation_chunks(cls, text: str) -> list[str]:
        """Split on paragraph/sentence boundaries without losing source text."""
        value = str(text or "").strip()
        if not value:
            return []
        paragraphs: list[str] = []
        for paragraph in re.split(r"\n\s*\n", value):
            paragraph = paragraph.strip()
            if not paragraph:
                continue
            if len(paragraph) <= cls._MAX_CHUNK_CHARS:
                paragraphs.append(paragraph)
                continue
            sentences = re.split(r"(?<=[.!?])\s+", paragraph)
            current: list[str] = []
            current_len = 0
            for sentence in sentences:
                sentence = sentence.strip()
                if not sentence:
                    continue
                if current and current_len + len(sentence) + 1 > cls._MAX_CHUNK_CHARS:
                    paragraphs.append(" ".join(current))
                    current, current_len = [], 0
                while len(sentence) > cls._MAX_CHUNK_CHARS:
                    paragraphs.append(sentence[: cls._MAX_CHUNK_CHARS])
                    sentence = sentence[cls._MAX_CHUNK_CHARS :]
                if sentence:
                    current.append(sentence)
                    current_len += len(sentence) + (1 if current_len else 0)
            if current:
                paragraphs.append(" ".join(current))

        chunks: list[str] = []
        current = []
        current_len = 0
        for paragraph in paragraphs:
            if current and current_len + len(paragraph) + 2 > cls._MAX_CHUNK_CHARS:
                chunks.append("\n\n".join(current))
                current, current_len = [], 0
            current.append(paragraph)
            current_len += len(paragraph) + (2 if current_len else 0)
        if current:
            chunks.append("\n\n".join(current))
        return chunks

    @classmethod
    def _asserted_numeric_literals(cls, text: str) -> list[str]:
        """Asserted prose numbers with citation markers removed first.

        Citation IDs (for example ``[REF:paper1234:c5678]`` or ``[12]``) are
        already validated separately and must not enter the numeric literal
        comparison, where hash digits could either create a false mismatch or
        mask a missing scientific value.
        """
        stripped = cls._CITATION_PATTERN.sub("", str(text or ""))
        return sorted(_extract_asserted_numeric_literals(stripped))

    @classmethod
    def _validate_translation_chunk(
        cls,
        source: str,
        translated: str,
        *,
        require_chinese: bool = True,
    ) -> None:
        """Validate one translated chunk; language strictness is optional.

        Direct FinalTranslator use keeps ``require_chinese=True``.  Shared
        callers (for example the multi-section runner) may disable only the
        CJK-content requirement while retaining the exact citation and
        canonical numeric invariants.
        """
        target = str(translated or "").strip()
        if not target:
            raise RuntimeError("Final translation returned an empty chunk.")
        if require_chinese and not re.search(r"[\u3400-\u9fff]", target):
            raise RuntimeError("Final translation chunk contains no Chinese text.")
        source_citations = sorted(cls._CITATION_PATTERN.findall(source))
        target_citations = sorted(cls._CITATION_PATTERN.findall(target))
        if source_citations != target_citations:
            raise RuntimeError(
                "Final translation changed or dropped citation markers: "
                f"source={source_citations}, target={target_citations}"
            )
        stripped_source = cls._CITATION_PATTERN.sub("", source)
        stripped_target = cls._CITATION_PATTERN.sub("", target)
        target_numbers = _extract_asserted_numeric_literals(stripped_target)
        covered_exponents = _translated_exponent_literals_covered(
            stripped_source, stripped_target, target_numbers
        )
        source_view = _canonical_numeric_view(
            stripped_source, covered_literals=covered_exponents
        )
        target_view = _canonical_numeric_view(stripped_target)
        if source_view != target_view:
            raise RuntimeError(
                "Final translation changed numeric literals: "
                f"source={source_view[0]}, target={target_view[0]} "
                f"quantities={source_view[1]} vs {target_view[1]}"
            )
        if len(target) < max(20, int(len(source) * 0.20)):
            raise RuntimeError("Final translation chunk is implausibly short.")

    def _translate_legacy_whole_section(self, draft: SectionDraft) -> SectionDraft:
        if not self.real_llm:
            draft.chinese_text = f"[待翻译] {_compact(draft.english_text, 200)}..."
            draft.status = "final"
            return draft

        system = _read_prompt(self.prompt_path)
        result = call_qwen_chat(
            "FinalTranslatorAgent",
            [
                {"role": "system", "content": system},
                {"role": "user", "content": draft.english_text},
            ],
            model_tier=self.model_tier,
            temperature=0.2,
            max_tokens=8192,
            stream=True,
            force_mock=False,
            max_retries=1,
        )
        translated = str(result.get("content") or "").strip()
        if not translated:
            raise RuntimeError("Final translation returned an empty chunk.")
        draft.chinese_text = translated
        draft.status = "final"
        return draft

    def translate(self, draft: SectionDraft) -> SectionDraft:
        if not self.real_llm:
            draft.chinese_text = (
                f"[Mock translation not generated] {_compact(draft.english_text, 200)}..."
            )
            draft.status = "final"
            return draft

        system = _read_prompt(self.prompt_path)
        chunks = self._split_translation_chunks(draft.english_text)
        translated_chunks: list[str] = []
        for index, chunk in enumerate(chunks, start=1):
            print(
                f"[FinalTranslator] section={draft.section_id} "
                f"chunk={index}/{len(chunks)} status=running",
                flush=True,
            )
            base_call_kwargs = {
                "model_tier": self.model_tier,
                "max_tokens": 6000,
                "stream": True,
                "timeout_seconds": 180,
                "max_transport_key_candidates": 1,
                "allow_model_fallback": False,
                "accept_partial_stream": False,
                "enable_thinking": False,
                "force_mock": False,
                "max_retries": 0,
            }
            first_user_content = (
                f"Translate source chunk {index} of {len(chunks)}. "
                "Return only its Chinese translation.\n\n"
                f"{chunk}"
            )
            result = call_qwen_chat(
                "FinalTranslatorAgent",
                [
                    {"role": "system", "content": system},
                    {
                        "role": "user",
                        "content": first_user_content,
                    },
                ],
                temperature=0.1,
                **base_call_kwargs,
            )
            translated = str(result.get("content") or "").strip()
            try:
                self._validate_translation_chunk(chunk, translated)
            except RuntimeError as exc:
                # Exactly one bounded correction call for the failed chunk.
                # Already validated chunks are never retranslated.
                correction_user_content = (
                    "The previous Chinese translation of this chunk failed "
                    "validation.\n\n"
                    f"SOURCE CHUNK (English):\n{chunk}\n\n"
                    f"INVALID TRANSLATION:\n{translated}\n\n"
                    f"VALIDATOR FEEDBACK:\n{exc}\n\n"
                    "Return only the corrected Chinese translation. Preserve "
                    "exactly the same REF/citation markers and exactly the "
                    "same numeric literals as the source chunk. Do not add "
                    "English prose and do not omit or reword numeric values."
                )
                result = call_qwen_chat(
                    "FinalTranslatorCorrectionAgent",
                    [
                        {"role": "system", "content": system},
                        {
                            "role": "user",
                            "content": correction_user_content,
                        },
                    ],
                    temperature=0.0,
                    **base_call_kwargs,
                )
                translated = str(result.get("content") or "").strip()
                self._validate_translation_chunk(chunk, translated)
            translated_chunks.append(translated)
            print(
                f"[FinalTranslator] section={draft.section_id} "
                f"chunk={index}/{len(chunks)} status=validated",
                flush=True,
            )
        draft.chinese_text = "\n\n".join(translated_chunks)
        draft.status = "final"
        return draft


class EvidenceAwareRevisionAgent:
    """Safely applies supervisor feedback without inventing new evidence."""

    def __init__(self, model_tier: str = "premium_model", real_llm: bool = True) -> None:
        self.model_tier = model_tier
        self.real_llm = real_llm

    def revise(
        self,
        draft: SectionDraft,
        packet: SectionMaterialPacket,
        suggestions: list[dict[str, Any]],
    ) -> SectionDraft:
        if not self.real_llm or not suggestions:
            return draft
        system = _read_prompt(EVIDENCE_AWARE_REVISER_PROMPT)
        payload = {
            "section_text": draft.english_text,
            "claims": packet.claims,
            "evidence_packets": [ep.to_dict() for ep in packet.evidence_packets],
            "literature_coverage": packet.literature_coverage,
            "supervisor_suggestions": suggestions,
        }
        result = call_qwen_chat(
            "EvidenceAwareRevisionAgent",
            [
                {"role": "system", "content": system},
                {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
            ],
            model_tier=self.model_tier,
            temperature=0.1,
            max_tokens=5000,
            response_format={"type": "json_object"},
            stream=True,
            force_mock=False,
            max_retries=1,
        )
        parsed = _safe_json(str(result.get("content") or ""))
        revised = str(parsed.get("revised_text") or "") if isinstance(parsed, dict) else ""
        from optomind_research.scientific_text_english_normalizer import ensure_english_strings
        revised = ensure_english_strings([revised])[0]
        valid_claim_ids = {
            str(claim.get("claim_id") or "")
            for claim in packet.claims
            if claim.get("claim_id")
        }
        safe_state_updates: list[dict[str, str]] = []
        rejected_state_updates: list[dict[str, str]] = []
        raw_updates = parsed.get("claim_state_updates") if isinstance(parsed, dict) else []
        for raw in raw_updates if isinstance(raw_updates, list) else []:
            if not isinstance(raw, dict):
                continue
            claim_id = str(raw.get("claim_id") or "").strip()
            requirement = str(raw.get("evidence_requirement") or "").strip().lower()
            state = str(raw.get("claim_state") or "").strip().lower()
            reason = _compact(raw.get("reason"), 600)
            reason = ensure_english_strings([reason])[0]
            expected_state = {"open_question": "open_question", "normative": "reframed"}.get(requirement)
            normalized = {
                "claim_id": claim_id,
                "evidence_requirement": requirement,
                "claim_state": state,
                "reason": reason,
            }
            if claim_id in valid_claim_ids and expected_state == state and reason:
                safe_state_updates.append(normalized)
            else:
                rejected_state_updates.append(normalized)
        scope_guardrail_violations: list[str] = []
        if revised:
            analogy_markers = {
                f"[REF:{ep.paper_id}:{ep.claim_id}]"
                for ep in packet.evidence_packets
                if ep.paper_id and ep.claim_id
                and (ep.retrieval_role == "method_transfer" or ep.scope_fit == "cross_domain_analogy")
            }
            analogy_terms = (
                "analogy", "analogous", "method transfer", "transferable method",
                "cross-domain", "outside the target domain", "in a different domain",
                "parallel", "parallels", "by comparison", "as an example from",
            )
            for sentence in re.split(r"(?<=[.!?])\s+", revised):
                markers = analogy_markers & set(re.findall(r"\[REF:[^\]]+\]", sentence))
                if markers and not any(term in sentence.lower() for term in analogy_terms):
                    scope_guardrail_violations.append(
                        "Method-transfer citation used without an explicit analogy boundary: "
                        + ", ".join(sorted(markers))
                    )
        old_refs = set(re.findall(r"\[REF:[^\]]+\]", draft.english_text))
        new_refs = set(re.findall(r"\[REF:[^\]]+\]", revised))
        allowed_packet_refs = {
            marker
            for ep in packet.evidence_packets if ep.paper_id
            for marker in {
                f"[REF:{ep.paper_id}]",
                f"[REF:{ep.paper_id}:{ep.claim_id}]" if ep.claim_id else "",
            }
            if marker
        }
        allowed_packet_refs.update(
            f"[REF:{source.get('paper_id')}]"
            for source in (packet.literature_coverage.get("sources") or [])
            if isinstance(source, dict) and source.get("paper_id")
        )
        disallowed_new_refs = new_refs - old_refs - allowed_packet_refs
        original_paragraph_count = len([
            paragraph for paragraph in re.split(r"\n\s*\n", draft.english_text) if paragraph.strip()
        ])
        revised_paragraph_count = len([
            paragraph for paragraph in re.split(r"\n\s*\n", revised) if paragraph.strip()
        ])
        paragraph_structure_preserved = (
            original_paragraph_count <= 1
            or revised_paragraph_count >= max(2, original_paragraph_count - 1)
        )
        valid = bool(
            revised
            and len(revised) >= int(len(draft.english_text) * 0.70)
            and len(revised) <= int(len(draft.english_text) * 1.60)
            and not disallowed_new_refs
            and not scope_guardrail_violations
            and paragraph_structure_preserved
        )
        record = {
            "stage": "evidence_aware_supervisor_revision",
            "accepted": valid,
            "suggestion_count": len(suggestions),
            "changes": list(parsed.get("changes") or []) if isinstance(parsed, dict) else [],
            "unresolved_suggestions": list(parsed.get("unresolved_suggestions") or []) if isinstance(parsed, dict) else [],
            "reason": "validated conservative revision" if valid else "revision rejected by length/reference safety gate",
            "original_length": len(draft.english_text),
            "revised_length": len(revised),
            "new_reference_markers": sorted(new_refs - old_refs),
            "disallowed_new_reference_markers": sorted(disallowed_new_refs),
            "scope_guardrail_violations": scope_guardrail_violations,
            "original_paragraph_count": original_paragraph_count,
            "revised_paragraph_count": revised_paragraph_count,
            "paragraph_structure_preserved": paragraph_structure_preserved,
            "claim_state_updates": safe_state_updates if valid else [],
            "rejected_claim_state_updates": rejected_state_updates,
        }
        draft.revision_history.append(record)
        if valid:
            draft.english_text = revised
            draft.status = "revised"
        return draft


# --------------------------------------------------------------------------- #
# ReviewWritingPipeline — orchestrates all components
# --------------------------------------------------------------------------- #

class ReviewWritingPipeline:
    """Orchestrates the full section-by-section review writing workflow."""

    def __init__(
        self,
        real_llm: bool = False,
        model_tier_write: str = "b_minus_model",
        model_tier_audit: str = "advanced_model",
        model_tier_translate: str = "advanced_model",
        kb_path: Path | None = None,
        checkpoint_dir: Path | None = None,
        resume: bool = True,
        section_workers: int = 3,
    ) -> None:
        self.mapper = SectionMaterialMapper(kb_path=kb_path)
        self.writer = SectionWriter(model_tier=model_tier_write, real_llm=real_llm)
        # Citation placement is a constrained alignment task.  The fast model
        # is both substantially more reliable under long JSON payloads and
        # cheaper than using the reasoning model; higher tiers still perform
        # overclaim, cross-section, and supervisor review afterwards.
        self.citation_binder = CitationBinder(model_tier="premium_model", real_llm=real_llm)
        self.overclaim_auditor = OverclaimAuditor(model_tier="standard_model", real_llm=real_llm)
        self.contradiction_editor = ContradictionEditor(model_tier=model_tier_audit, real_llm=real_llm)
        self.cross_section_editor = CrossSectionEditor(model_tier="standard_model", real_llm=real_llm)
        self.figure_planner = FigurePlanner(model_tier="advanced_model", real_llm=real_llm)
        self.final_translator = FinalTranslator(model_tier=model_tier_translate, real_llm=real_llm)
        self.last_packets: list[SectionMaterialPacket] = []
        self.checkpoint_dir = Path(checkpoint_dir) if checkpoint_dir else None
        self.resume = bool(resume)
        self.section_workers = max(1, int(section_workers))
        if self.checkpoint_dir:
            self.checkpoint_dir.mkdir(parents=True, exist_ok=True)

    def _checkpoint_path(self, section_id: str) -> Path | None:
        return self.checkpoint_dir / f"{section_id}.checkpoint.json" if self.checkpoint_dir else None

    def _save_checkpoint(self, draft: SectionDraft, stage: int, stage_name: str) -> None:
        path = self._checkpoint_path(draft.section_id)
        if path is None:
            return
        payload = json.dumps({
            "pipeline_stage": stage,
            "stage_name": stage_name,
            "draft": {
                "section_id": draft.section_id,
                "english_text": draft.english_text,
                "chinese_text": draft.chinese_text,
                "citation_map": draft.citation_map,
                "overclaim_flags": draft.overclaim_flags,
                "contradiction_notes": draft.contradiction_notes,
                "figure_placements": draft.figure_placements,
                "status": draft.status,
                "uncited_load_bearing": draft.uncited_load_bearing,
                "revision_history": draft.revision_history,
            },
        }, ensure_ascii=False, indent=2)
        path.write_text(payload, encoding="utf-8")
        # Keep immutable stage snapshots so a faulty downstream editor can be
        # rolled back without regenerating expensive upstream work.
        stage_path = self.checkpoint_dir / f"{draft.section_id}.stage{stage}.{stage_name}.json"
        stage_path.write_text(payload, encoding="utf-8")

    def _load_checkpoint(self, section_id: str) -> tuple[SectionDraft | None, int]:
        path = self._checkpoint_path(section_id)
        if not self.resume or path is None or not path.exists():
            return None, 0
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
            row = value.get("draft") or {}
            draft = SectionDraft(
                section_id=str(row.get("section_id") or section_id),
                english_text=str(row.get("english_text") or ""),
                chinese_text=str(row.get("chinese_text") or ""),
                citation_map=dict(row.get("citation_map") or {}),
                overclaim_flags=list(row.get("overclaim_flags") or []),
                contradiction_notes=list(row.get("contradiction_notes") or []),
                figure_placements=list(row.get("figure_placements") or []),
                status=str(row.get("status") or "draft"),
                uncited_load_bearing=list(row.get("uncited_load_bearing") or []),
                revision_history=list(row.get("revision_history") or []),
            )
            stage = int(value.get("pipeline_stage") or 0)
            if draft.status == "audit_failed":
                stage = min(stage, 1)
                draft.status = "draft"
                draft.overclaim_flags = []
            elif draft.status == "failed" or not draft.english_text.strip():
                # Empty/failed stage-1 checkpoints are transport failures, not
                # completed scientific work. Resume must regenerate them.
                stage = 0
                draft = SectionDraft(section_id=section_id)
            elif stage >= 2 and not any(draft.citation_map.values()):
                stage = 1
                draft.status = "draft"
            return draft, stage
        except Exception:
            return None, 0

    def _process_section(
        self,
        item: tuple[int, int, dict[str, Any]],
        manuscript_context: dict[str, Any] | None = None,
    ) -> tuple[int, SectionMaterialPacket, SectionDraft, int]:
        """Run the independent per-section stages; safe for bounded parallel use."""
        index, total, section = item
        packet = self.mapper.map(section)
        packet.manuscript_context = dict(manuscript_context or {})
        sid = str(section.get("section_id") or f"section_{index}")
        draft, stage = self._load_checkpoint(sid)
        try:
            checkpoint_budget = max(
                0, int(packet.section_contract.get("word_budget") or 0)
            )
        except (TypeError, ValueError):
            checkpoint_budget = 0
        if (
            draft is not None
            and stage >= 1
            and checkpoint_budget
            and _word_count(draft.english_text) < int(checkpoint_budget * 0.80)
        ):
            print(
                f"[T8 {index}/{total}] {sid}: checkpoint below contract; regenerate",
                flush=True,
            )
            draft = None
            stage = 0
        if draft is not None:
            print(f"[T8 {index}/{total}] {sid}: resume after stage {stage}", flush=True)
        else:
            draft = SectionDraft(section_id=sid)
        if stage < 1:
            print(f"[T8 {index}/{total}] {sid}: writing English draft", flush=True)
            draft = self.writer.write(packet)
            self._save_checkpoint(draft, 1, "written")
        if draft.status == "failed":
            return index, packet, draft, stage
        draft.uncited_load_bearing = list(packet.uncited_load_bearing_claim_ids)
        if stage < 2:
            print(f"[T8 {index}/{total}] {sid}: binding citations", flush=True)
            draft = self.citation_binder.bind(draft, packet)
            self._save_checkpoint(draft, 2, "cited")
        pre_audit_text = draft.english_text
        if stage < 3:
            print(f"[T8 {index}/{total}] {sid}: auditing overclaims", flush=True)
            draft = self.overclaim_auditor.audit(draft, packet)
            self._save_checkpoint(draft, 3, "audited")
        if draft.status == "audit_failed":
            return index, packet, draft, max(stage, 3)
        if stage < 4 and self.writer.real_llm and (
            stage == 3 or draft.english_text != pre_audit_text
        ):
            print(f"[T8 {index}/{total}] {sid}: rebinding after audit edits", flush=True)
            draft = self.citation_binder.bind(draft, packet)
        if stage < 4:
            self._save_checkpoint(draft, 4, "post_audit_citations")
        if stage < 5:
            print(f"[T8 {index}/{total}] {sid}: resolving contradictions", flush=True)
            draft = self.contradiction_editor.edit(draft, packet)
            self._save_checkpoint(draft, 5, "contradiction_edited")
        if stage < 6:
            print(f"[T8 {index}/{total}] {sid}: planning figures", flush=True)
            draft = self.figure_planner.plan(draft, packet)
            draft = remove_broken_visual_promises(draft)
            self._save_checkpoint(draft, 6, "figure_planned")
        return index, packet, draft, max(stage, 6)

    @staticmethod
    def _rolling_manuscript_context(
        *,
        blueprint: dict[str, Any],
        section_index: int,
        prior_drafts: list[SectionDraft],
        prior_packets: list[SectionMaterialPacket],
    ) -> dict[str, Any]:
        """Build a bounded continuation context for sequential full-review writing.

        It deliberately contains the preceding prose boundary and a compact
        coverage ledger, not every prior evidence packet.  The writer can
        continue the manuscript without reopening definitions while citation
        authority remains confined to the current section's verified packet.
        """
        sections = list(blueprint.get("sections") or [])
        outline = [
            {
                "section_id": str(row.get("section_id") or ""),
                "section_role": str(row.get("section_role") or ""),
                "title": str(row.get("section_title") or row.get("title") or ""),
                "purpose": _compact(row.get("purpose"), 300),
                "planned_thesis": _compact(
                    (row.get("planned_thesis") or {}).get("text")
                    if isinstance(row.get("planned_thesis"), dict)
                    else row.get("planned_thesis"),
                    380,
                ),
            }
            for row in sections
        ]
        prior_coverage = []
        for draft, packet in zip(prior_drafts, prior_packets):
            paragraphs = [
                value.strip()
                for value in re.split(r"\n\s*\n", draft.english_text)
                if value.strip()
            ]
            prior_coverage.append({
                "section_id": draft.section_id,
                "section_role": packet.section_contract.get("section_role", "body"),
                "central_thesis": _compact(
                    packet.section_contract.get("central_thesis"), 380
                ),
                "closing_boundary": _compact(
                    paragraphs[-1] if paragraphs else draft.english_text, 1400
                ),
                "covered_claims": [
                    _compact(
                        claim.get("statement_for_writing") or claim.get("statement"),
                        240,
                    )
                    for claim in packet.claims
                    if claim.get("writing_permission") not in {"omit", "evidence_gap_only"}
                ][:10],
            })
        previous_tail = prior_coverage[-1]["closing_boundary"] if prior_coverage else ""
        return {
            "mode": "sequential_full_manuscript_continuation",
            "current_section_index": section_index,
            "full_outline": outline,
            "previous_section_tail": previous_tail,
            "prior_section_coverage_ledger": prior_coverage,
            "citation_boundary": (
                "Prior prose is continuity context only. Cite and assert facts only from "
                "the current section's authorized claims and evidence packets."
            ),
        }

    def run(
        self,
        blueprint: dict[str, Any],
        *,
        translate: bool = True,
        cross_section_edit: bool = True,
    ) -> list[SectionDraft]:
        sections = list(blueprint.get("sections") or [])
        packets: list[SectionMaterialPacket] = []
        drafts: list[SectionDraft] = []
        completed_stage_by_id: dict[str, int] = {}

        jobs = [
            (index, len(sections), section)
            for index, section in enumerate(sections, 1)
        ]
        if self.writer.real_llm and len(jobs) > 1:
            # Full-manuscript quality requires actual continuation.  Parallel
            # section generation made every chapter behave like an independent
            # mini-review because no writer could see the preceding final text.
            processed = []
            prior_drafts: list[SectionDraft] = []
            prior_packets: list[SectionMaterialPacket] = []
            for job in jobs:
                context = self._rolling_manuscript_context(
                    blueprint=blueprint,
                    section_index=job[0],
                    prior_drafts=prior_drafts,
                    prior_packets=prior_packets,
                )
                row = self._process_section(job, manuscript_context=context)
                processed.append(row)
                prior_packets.append(row[1])
                prior_drafts.append(row[2])
        else:
            processed = [self._process_section(job) for job in jobs]
        for _, packet, draft, completed_stage in sorted(processed, key=lambda row: row[0]):
            packets.append(packet)
            drafts.append(draft)
            completed_stage_by_id[draft.section_id] = completed_stage

        # Step 6: cross-section edit (whole-review)
        if (
            cross_section_edit
            and drafts
            and not all(completed_stage_by_id.get(d.section_id, 0) >= 7 for d in drafts)
        ):
            print("[T8] Cross-section coherence and deduplication", flush=True)
            drafts = self.cross_section_editor.edit(drafts, packets)
            for draft in drafts:
                self._save_checkpoint(draft, 7, "cross_section_edited")
                completed_stage_by_id[draft.section_id] = 7

        self.last_packets = packets

        # Step 8: translation is an explicit presentation step; the internal
        # scientific message flow remains English.
        if translate:
            for index, draft in enumerate(drafts, 1):
                if draft.status != "failed" and completed_stage_by_id.get(draft.section_id, 0) < 8:
                    print(f"[T8 {index}/{len(drafts)}] {draft.section_id}: final translation", flush=True)
                    self.final_translator.translate(draft)
                    self._save_checkpoint(draft, 8, "translated")

        return drafts

    def to_full_review(self, drafts: list[SectionDraft], *, lang: str = "zh") -> str:
        """Concatenate section drafts into a full review text."""
        parts = []
        for draft in drafts:
            text = draft.chinese_text if lang == "zh" and draft.chinese_text else draft.english_text
            parts.append(text.strip())
        return "\n\n---\n\n".join(parts)
