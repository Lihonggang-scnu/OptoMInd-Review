"""Deterministic article-level audit after body and completion assembly."""

from __future__ import annotations

import json
import re
from collections import Counter
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

from .article_completion_schemas import (
    ArticleCompletionPackage,
    ArticleRhetoricalContract,
)
from .artifact_store import atomic_write_json

_CJK = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]")
_REF = re.compile(r"\[REF:([^\]\s]+)\]")
_TOKEN = re.compile(r"[A-Za-z][A-Za-z0-9-]{2,}")
# P3-1 (round 3): deterministic sentence-opener repetition detector.  A
# sentence opener is the first alphabetic word after optional reference
# markers ([REF:...]), so citations never count toward repetition.  The
# threshold matches the style-governance contract: >=5 identical openers or
# one opener covering >=15% of sentences flags the manuscript for attention.
_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+")
_LEADING_REF_MARKS = re.compile(r"^(?:\s*\[REF:[^]\s]+\])+\s*")
_FIRST_WORD = re.compile(r"[A-Za-z]+")
_OPENER_REPEAT_MIN_COUNT = 5
_OPENER_REPEAT_MAX_SHARE = 0.15


def _sentence_openers(text: str) -> List[str]:
    """First meaningful word of each body sentence, refs stripped."""

    openers: List[str] = []
    for raw_sentence in _SENTENCE_SPLIT.split(text or ""):
        cleaned = _LEADING_REF_MARKS.sub("", raw_sentence)
        match = _FIRST_WORD.search(cleaned)
        if match:
            openers.append(match.group(0).lower())
    return openers


_GENERIC_CAVEATS = (
    "further research is needed",
    "more research is needed",
    "remains to be seen",
    "still unclear",
    "requires further investigation",
)


def _read_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def _tokens(text: str) -> Set[str]:
    stop = {
        "the",
        "and",
        "for",
        "that",
        "with",
        "from",
        "this",
        "into",
        "across",
        "review",
    }
    return {
        value.lower()
        for value in _TOKEN.findall(str(text or ""))
        if value.lower() not in stop
    }


def _promise_coverage(promise: str, body: str) -> float:
    target = _tokens(promise)
    if not target:
        return 1.0
    body_tokens = _tokens(body)
    return len(target & body_tokens) / len(target)


def _heading_key(value: str) -> str:
    """Stable, presentation-only fallback when a body block lacks its ID."""

    return re.sub(r"[^a-z0-9]+", "", str(value or "").lower())


def _planned_section_records(
    blueprint: Dict[str, Any],
    section_manifest_path: Optional[Path],
) -> List[Dict[str, str]]:
    """Return the planned body identity set, preferring the durable manifest."""

    manifest = _read_json(section_manifest_path, {}) if section_manifest_path else {}
    source = manifest.get("sections") if isinstance(manifest, dict) else None
    if not isinstance(source, list):
        source = blueprint.get("sections", [])
    rows: List[Dict[str, str]] = []
    for item in source:
        if not isinstance(item, dict):
            continue
        section_id = str(item.get("section_id") or "").strip()
        if not section_id:
            continue
        rows.append(
            {
                "section_id": section_id,
                "title": str(
                    item.get("title")
                    or item.get("section_title")
                    or ""
                ).strip(),
                "content_status": str(
                    item.get("content_status") or "enhanced"
                ).strip(),
            }
        )
    return rows


def _actual_body_section_ids(
    body: str,
    planned: List[Dict[str, str]],
) -> tuple[List[str], List[str]]:
    """Resolve body headings to planned IDs without counting non-body headings."""

    title_to_id = {
        _heading_key(row["title"]): row["section_id"]
        for row in planned
        if row["title"]
    }
    known_ids = {row["section_id"] for row in planned}
    actual: List[str] = []
    unexpected: List[str] = []
    for raw in re.findall(r"^##\s+(.+?)\s*$", body, re.MULTILINE):
        heading = raw.strip()
        match = re.match(r"(S\d+)\s*(?:[:.\-]|$)", heading, re.I)
        section_id = match.group(1).upper() if match else title_to_id.get(_heading_key(heading))
        if section_id in known_ids:
            actual.append(str(section_id))
        else:
            unexpected.append(heading)
    return actual, unexpected


def _heading_texts(headings: List[str]) -> List[str]:
    return [str(value).lstrip("#").strip() for value in headings]


def _outlook_heading_keys(planned: List[Dict[str, str]]) -> Set[str]:
    """Return accepted equivalent headings for the article outlook component."""

    keys = {_heading_key("Challenges and Future Outlook")}
    outlook_terms = (
        "challenge",
        "future",
        "outlook",
        "constraint",
        "scalability",
        "limitation",
        "bottleneck",
    )
    for row in planned:
        title = str(row.get("title") or "").strip()
        lowered = title.casefold()
        if title and any(term in lowered for term in outlook_terms):
            keys.add(_heading_key(title))
    return keys


def _validated_expected_order(
    planned_sections: List[str],
    expected_section_order: Optional[List[str]],
) -> List[str]:
    """Use an external editorial order only when it is a complete permutation."""

    if not expected_section_order:
        return list(planned_sections)
    normalized = [str(value).strip().upper() for value in expected_section_order if str(value).strip()]
    if len(normalized) != len(planned_sections) or set(normalized) != set(planned_sections):
        return list(planned_sections)
    return normalized


def audit_complete_manuscript(
    *,
    manuscript_path: Path,
    body_review_path: Path,
    completion_package_path: Path,
    blueprint_path: Path,
    output_path: Path,
    section_manifest_path: Optional[Path] = None,
    expected_section_order: Optional[List[str]] = None,
    body_is_complete_manuscript: bool = False,
) -> Dict[str, Any]:
    """Audit whole-article structure without enforcing sentence citation density."""

    manuscript = manuscript_path.read_text(encoding="utf-8")
    body = body_review_path.read_text(encoding="utf-8")
    package_raw = _read_json(completion_package_path, {})
    package = (
        ArticleCompletionPackage.model_validate(package_raw)
        if package_raw
        else None
    )
    blueprint = _read_json(blueprint_path, {})
    contract = ArticleRhetoricalContract.model_validate(
        blueprint.get("article_rhetorical_contract", {})
    )
    errors: List[Dict[str, Any]] = []
    warnings: List[Dict[str, Any]] = []

    headings = [
        line.strip()
        for line in manuscript.splitlines()
        if line.startswith("#")
    ]
    required = [
        "## Abstract",
        "## Introduction",
        "## Conclusion",
    ]
    heading_texts = _heading_texts(headings)
    positions = []
    for heading in required:
        try:
            positions.append(headings.index(heading))
        except ValueError:
            errors.append(
                {
                    "type": "missing_article_component",
                    "severity": "blocking",
                    "component": heading,
                }
            )
    outlook_keys = _outlook_heading_keys(
        _planned_section_records(blueprint, section_manifest_path)
    )
    outlook_position = next(
        (
            index
            for index, heading in enumerate(heading_texts)
            if _heading_key(heading) in outlook_keys
        ),
        None,
    )
    if outlook_position is None:
        errors.append(
            {
                "type": "missing_article_component",
                "severity": "blocking",
                "component": "## Challenges and Future Outlook",
            }
        )
    else:
        positions.insert(2, outlook_position)
    if len(positions) == len(required) and positions != sorted(positions):
        errors.append(
            {
                "type": "component_order",
                "severity": "blocking",
                "description": "Article components are out of order.",
            }
        )
    planned_records = _planned_section_records(blueprint, section_manifest_path)
    planned_sections = [row["section_id"] for row in planned_records]
    actual_sections, unexpected_sections = _actual_body_section_ids(
        body, planned_records
    )
    actual_set = set(actual_sections)
    missing_sections = [sid for sid in planned_sections if sid not in actual_set]
    if missing_sections:
        errors.append(
            {
                "type": "missing_body_sections",
                "severity": "blocking",
                "planned_section_ids": planned_sections,
                "missing_section_ids": missing_sections,
                "found_section_ids": actual_sections,
            }
        )
    if body_is_complete_manuscript:
        non_body_heading_keys = {
            _heading_key(value)
            for value in (
                "Abstract",
                "Introduction",
                "Conclusion",
                "References",
                "Acknowledgements",
                "Acknowledgments",
            )
        }
        unexpected_sections = [
            heading
            for heading in unexpected_sections
            if _heading_key(heading) not in non_body_heading_keys
        ]
    if unexpected_sections:
        errors.append(
            {
                "type": "unexpected_body_sections",
                "severity": "blocking",
                "unexpected_headings": unexpected_sections,
            }
        )
    expected_order = _validated_expected_order(
        planned_sections,
        expected_section_order,
    )
    if actual_sections != [sid for sid in expected_order if sid in actual_set]:
        errors.append(
            {
                "type": "body_section_order_mismatch",
                "severity": "blocking",
                "planned_section_ids": expected_order,
                "found_section_ids": actual_sections,
            }
        )
    if _CJK.search(manuscript):
        errors.append(
            {
                "type": "intermediate_language_violation",
                "severity": "blocking",
                "description": "English manuscript contains CJK text.",
            }
        )
    methodology_words = contract.methodology_identity.replace("_", " ")
    if package is not None and methodology_words not in package.introduction.lower():
        warnings.append(
            {
                "type": "methodology_disclosure",
                "severity": "important",
                "description": (
                    "Introduction does not state the declared methodology "
                    "using its canonical wording."
                ),
            }
        )

    body_refs = set(_REF.findall(body))
    conclusion_refs = set(_REF.findall(package.conclusion)) if package else set()
    new_conclusion_refs = sorted(conclusion_refs - body_refs)
    if new_conclusion_refs and contract.conclusion_contract.no_new_evidence:
        errors.append(
            {
                "type": "new_conclusion_evidence",
                "severity": "blocking",
                "paper_ids": new_conclusion_refs,
            }
        )

    for promise in (
        package.quality_self_check.introduction_promises if package else []
    ):
        coverage = _promise_coverage(promise, body)
        if coverage < 0.35:
            warnings.append(
                {
                    "type": "unfulfilled_introduction_promise",
                    "severity": "important",
                    "promise": promise,
                    "lexical_coverage": round(coverage, 3),
                }
            )
    caveat_counts = {
        phrase: manuscript.lower().count(phrase)
        for phrase in _GENERIC_CAVEATS
    }
    for phrase, count in caveat_counts.items():
        if count >= 3:
            warnings.append(
                {
                    "type": "repetitive_generic_caveat",
                    "severity": "important",
                    "phrase": phrase,
                    "count": count,
                }
            )

    normalized_paragraphs = [
        " ".join(paragraph.lower().split())
        for paragraph in re.split(r"\n\s*\n", manuscript)
        if len(paragraph.split()) >= 20
    ]
    duplicates = [
        paragraph
        for paragraph, count in Counter(normalized_paragraphs).items()
        if count > 1
    ]
    if duplicates:
        errors.append(
            {
                "type": "duplicate_paragraph",
                "severity": "blocking",
                "count": len(duplicates),
            }
        )

    # P3-1: sentence-opener repetition (reference markers excluded).
    openers = _sentence_openers(manuscript)
    total_sentences = len(openers)
    opener_counts: Dict[str, int] = {}
    for opener in openers:
        opener_counts[opener] = opener_counts.get(opener, 0) + 1
    repeated_openers = sorted(
        (
            {
                "opener": opener,
                "count": count,
                "share": round(count / total_sentences, 4),
            }
            for opener, count in opener_counts.items()
            if count >= _OPENER_REPEAT_MIN_COUNT
            or (total_sentences and count / total_sentences >= _OPENER_REPEAT_MAX_SHARE and count >= 3)
        ),
        key=lambda row: (-row["count"], row["opener"]),
    )
    if repeated_openers:
        warnings.append(
            {
                "type": "repeated_sentence_openers",
                "severity": "important",
                "sentences_total": total_sentences,
                "openers": repeated_openers,
            }
        )

    report = {
        "schema_version": "article_structure_audit.v2",
        "status": "failed" if errors else (
            "needs_attention" if warnings else "passed"
        ),
        "blocking_flags": errors,
        "nonblocking_flags": warnings,
        "summary": {
            "planned_body_sections": len(planned_sections),
            "found_body_sections": len(actual_sections),
            "planned_section_ids": planned_sections,
            "expected_section_order": expected_order,
            "missing_section_ids": missing_sections,
            "unexpected_section_headings": unexpected_sections,
            "body_reference_count": len(body_refs),
            "introduction_promise_count": len(
                package.quality_self_check.introduction_promises if package else []
            ),
            "outlook_item_count": len(package.outlook_items) if package else 0,
            "generic_caveat_counts": caveat_counts,
        },
    }
    atomic_write_json(output_path, report)
    return report
