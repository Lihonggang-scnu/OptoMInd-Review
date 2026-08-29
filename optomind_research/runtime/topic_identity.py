"""Deterministic topic-identity contract for cross-stage drift prevention.

The contract is derived only after a valid English Query Planner result exists.
It is not a scientific ontology and does not contain topic-specific patches.
Its job is to preserve the user's scientific object while the review moves
through blueprinting, retrieval, writing, visual planning and research-plan
generation.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections import Counter
from typing import Any, Dict, Iterable

_TOKEN = re.compile(r"[A-Za-z][A-Za-z0-9-]{2,}")
_GENERIC = {
    "also",
    "among",
    "and",
    "about",
    "between",
    "during",
    "for",
    "from",
    "including",
    "into",
    "main",
    "over",
    "their",
    "these",
    "through",
    "under",
    "what",
    "which",
    "while",
    "with",
    "application",
    "applications",
    "approach",
    "approaches",
    "background",
    "challenge",
    "challenges",
    "current",
    "design",
    "designs",
    "development",
    "engineering",
    "evaluation",
    "experimental",
    "experiments",
    "fabrication",
    "field",
    "fields",
    "film",
    "films",
    "framework",
    "future",
    "important",
    "literature",
    "material",
    "materials",
    "mechanism",
    "mechanisms",
    "method",
    "methods",
    "optical",
    "performance",
    "perspective",
    "problem",
    "problems",
    "progress",
    "properties",
    "research",
    "review",
    "route",
    "routes",
    "science",
    "scientific",
    "simulation",
    "state",
    "studies",
    "study",
    "system",
    "systems",
    "technical",
    "technologies",
    "technology",
    "thin",
    "toward",
    "using",
}
_NON_EXECUTABLE_MARKERS = (
    "requires english normalization",
    "needs human input",
    "reformulate the user's research question",
    "workflow checking",
)


def _canonical_token(token: str) -> str:
    value = token.lower().strip("-")
    if value.endswith(("lens", "glass", "mass", "class", "analysis")):
        return value
    if value.endswith("ies") and len(value) > 5:
        return value[:-3] + "y"
    if value.endswith(("ses", "xes", "zes", "ches", "shes")) and len(value) > 5:
        return value[:-2]
    if value.endswith("s") and not value.endswith("ss") and len(value) > 5:
        return value[:-1]
    return value


def topic_tokens(text: str) -> list[str]:
    return [
        canonical
        for token in _TOKEN.findall(str(text or "").replace("-", " "))
        if (canonical := _canonical_token(token)) not in _GENERIC
    ]


def _query_output(query_plan: Dict[str, Any]) -> Dict[str, Any]:
    output = query_plan.get("output", query_plan)
    return output if isinstance(output, dict) else {}


def _keyword_phrases(query_plan: Dict[str, Any]) -> list[str]:
    output = _query_output(query_plan)
    block = output.get("keyword_decomposition", {})
    raw = block.get("keywords", []) if isinstance(block, dict) else []
    return [
        str(item).strip()
        for item in raw
        if isinstance(item, str) and str(item).strip()
    ]


def build_topic_identity_contract(
    query_plan: Dict[str, Any],
) -> Dict[str, Any]:
    """Build a reusable English topic fingerprint from a confirmed plan."""

    output = _query_output(query_plan)
    phrases = _keyword_phrases(query_plan)
    phrase_tokens = [set(topic_tokens(phrase)) for phrase in phrases]
    frequency: Counter[str] = Counter()
    for tokens in phrase_tokens:
        frequency.update(tokens)

    normalized_question = str(
        (query_plan.get("input") or {}).get("user_query")
        or output.get("problem_understanding")
        or ""
    ).strip()
    context_tokens = set(
        topic_tokens(
            " ".join(
                [
                    normalized_question,
                    str(output.get("problem_understanding") or ""),
                    str(
                        (output.get("scope_definition") or {}).get(
                            "main_scope", ""
                        )
                        if isinstance(
                            output.get("scope_definition"), dict
                        )
                        else output.get("scope_definition", "")
                    ),
                ]
            )
        )
    )
    ranked_repeated = sorted(
        (
            (count, token)
            for token, count in frequency.items()
            if count >= 2 and token in context_tokens
        ),
        key=lambda item: (-item[0], item[1]),
    )
    # A topic contract must identify the scientific object, not turn every
    # requested metric into a mandatory anchor.  Repeated high-frequency
    # search terms are the most stable object fingerprint; lower-frequency
    # mechanisms and evaluation dimensions remain supporting anchors.
    core = {token for _count, token in ranked_repeated[:6]}
    if not core:
        ranked = [
            token
            for token, _count in frequency.most_common()
            if token in context_tokens
        ]
        core = set(ranked[:4])
    if not core:
        core = set(sorted(context_tokens)[:4])

    support = set().union(*phrase_tokens) if phrase_tokens else set()
    support.update(context_tokens)
    anchor_phrases = [
        phrase
        for phrase, tokens in zip(phrases, phrase_tokens)
        if tokens & core and len(tokens) >= 1
    ][:12]
    payload = {
        "normalized_question": normalized_question,
        "core_anchor_tokens": sorted(core),
        "supporting_anchor_tokens": sorted(support - core)[:60],
        "anchor_phrases": anchor_phrases,
    }
    fingerprint = hashlib.sha256(
        json.dumps(payload, sort_keys=True).encode("utf-8")
    ).hexdigest()[:16]
    normalized_payload_text = " ".join(
        [
            normalized_question,
            str(output.get("problem_understanding") or ""),
            " ".join(phrases),
        ]
    ).lower()
    placeholder_markers = [
        marker
        for marker in _NON_EXECUTABLE_MARKERS
        if marker in normalized_payload_text
    ]
    return {
        "schema_version": "research_harness.topic_identity.v1",
        "fingerprint": fingerprint,
        **payload,
        "valid": bool(core) and not placeholder_markers,
        "placeholder_markers": placeholder_markers,
        "policy": (
            "Core anchors preserve the scientific object; supporting anchors "
            "describe mechanisms, metrics, constraints and applications."
        ),
    }


def assess_topic_alignment(
    text_or_items: str | Iterable[Any],
    contract: Dict[str, Any],
    *,
    strict: bool = True,
) -> Dict[str, Any]:
    """Return a deterministic topic-alignment assessment."""

    if isinstance(text_or_items, str):
        text = text_or_items
    else:
        text = " ".join(
            json.dumps(item, ensure_ascii=True, sort_keys=True)
            if not isinstance(item, str)
            else item
            for item in text_or_items
        )
    present = set(topic_tokens(text))
    core = set(contract.get("core_anchor_tokens", []))
    support = set(contract.get("supporting_anchor_tokens", []))
    core_hits = sorted(core & present)
    support_hits = sorted(support & present)
    missing_core = sorted(core - present)
    required_core_hits = (
        0
        if not core
        else 1
        if len(core) <= 3
        else max(2, math.ceil(len(core) * 0.34))
    )
    core_coverage = len(core_hits) / max(1, len(core))
    aligned = bool(core) and len(core_hits) >= required_core_hits
    if strict and len(core) >= 4:
        aligned = aligned and core_coverage >= 0.34
    status = "passed" if aligned else "failed"
    return {
        "schema_version": "research_harness.topic_alignment.v1",
        "status": status,
        "topic_fingerprint": str(contract.get("fingerprint") or ""),
        "core_anchor_count": len(core),
        "required_core_hits": required_core_hits,
        "core_hits": core_hits,
        "missing_core_anchors": missing_core,
        "supporting_hits": support_hits[:30],
        "core_coverage": round(core_coverage, 3),
        "reason": (
            "scientific_object_preserved"
            if aligned
            else "scientific_object_anchor_missing"
        ),
    }


def topic_search_anchor_phrase(contract: Dict[str, Any]) -> str:
    """Return a compact, human-readable subject anchor for retrieval."""

    core = set(contract.get("core_anchor_tokens", []))
    candidates: list[tuple[int, int, str]] = []
    for raw in contract.get("anchor_phrases", []):
        phrase = " ".join(str(raw or "").split()).strip()
        if not phrase:
            continue
        hits = core & set(topic_tokens(phrase))
        if hits:
            candidates.append((-len(hits), len(phrase.split()), phrase))
    if candidates:
        phrase = sorted(candidates)[0][2]
        ordered_core: list[str] = []
        for token in _TOKEN.findall(phrase.replace("-", " ")):
            canonical = _canonical_token(token)
            if canonical in core and canonical not in ordered_core:
                ordered_core.append(canonical)
        if len(ordered_core) >= 2:
            return " ".join(ordered_core[:4])
        return " ".join(phrase.split()[:8])
    return " ".join(str(item) for item in sorted(core)[:4])


def assess_retrieval_query_alignment(
    query: str,
    contract: Dict[str, Any],
) -> Dict[str, Any]:
    """Check whether one retrieval query still names the scientific object."""

    core = set(contract.get("core_anchor_tokens", []))
    present = set(topic_tokens(query))
    hits = sorted(core & present)
    required = 0 if not core else 1 if len(core) <= 3 else 2
    return {
        "status": "passed" if len(hits) >= required else "failed",
        "topic_fingerprint": str(contract.get("fingerprint") or ""),
        "required_core_hits": required,
        "core_hits": hits,
        "missing_core_anchors": sorted(core - present),
    }


def anchor_retrieval_query(
    query: str,
    contract: Dict[str, Any],
) -> tuple[str, Dict[str, Any]]:
    """Mechanically restore a lost subject anchor without changing intent."""

    normalized = " ".join(str(query or "").split()).strip()
    before = assess_retrieval_query_alignment(normalized, contract)
    if before["status"] == "passed" or not contract.get("valid"):
        return normalized, {
            "changed": False,
            "before": before,
            "after": before,
            "anchor_phrase": "",
        }
    anchor = topic_search_anchor_phrase(contract)
    corrected = " ".join(part for part in (normalized, anchor) if part).strip()
    after = assess_retrieval_query_alignment(corrected, contract)
    return corrected, {
        "changed": corrected != normalized,
        "before": before,
        "after": after,
        "anchor_phrase": anchor,
    }


def assess_blueprint_topic_alignment(
    blueprint: Dict[str, Any],
    contract: Dict[str, Any],
) -> Dict[str, Any]:
    """Check both the review-wide thesis and every planned chapter."""

    overall = assess_topic_alignment(
        [
            blueprint.get("review_thesis", ""),
            blueprint.get("full_review_argument", ""),
            blueprint.get("taxonomy_principle", ""),
            blueprint.get("sections", []),
        ],
        contract,
        strict=True,
    )
    sections: Dict[str, Any] = {}
    failed_section_ids: list[str] = []
    for index, section in enumerate(blueprint.get("sections", []), start=1):
        if not isinstance(section, dict):
            continue
        section_id = str(section.get("section_id") or f"S{index:02d}")
        alignment = assess_topic_alignment(
            [
                section.get("title", ""),
                section.get("chapter_argument", ""),
                section.get("key_questions", []),
                section.get("synthesis_task", ""),
            ],
            contract,
            strict=False,
        )
        sections[section_id] = alignment
        if alignment["status"] != "passed":
            failed_section_ids.append(section_id)
    passed = (
        overall["status"] == "passed"
        and bool(sections)
        and not failed_section_ids
    )
    return {
        **overall,
        "status": "passed" if passed else "failed",
        "reason": (
            "review_and_all_sections_preserve_scientific_object"
            if passed
            else "review_or_section_lost_scientific_object"
        ),
        "review_wide_alignment": overall,
        "section_alignments": sections,
        "failed_section_ids": failed_section_ids,
    }


def topic_contract_from_blueprint(
    blueprint: Dict[str, Any],
) -> Dict[str, Any]:
    value = blueprint.get("topic_identity", {})
    return value if isinstance(value, dict) else {}
    "between",
    "during",
    "for",
    "from",
    "including",
    "into",
    "main",
    "over",
    "their",
    "these",
    "through",
    "under",
    "what",
    "which",
    "while",
    "with",
