"""Deterministic, topic-agnostic quality gate for a completed review package.

The evaluator does not reward sentence-level citation density.  It checks
whether the promised article was actually delivered, whether traceable
references and images resolve, and whether obvious cross-section writing
defects remain.  Scientific judgement stays with the author/editor agents and
the human reviewer.
"""

from __future__ import annotations

import json
import re
from collections import Counter
from pathlib import Path
from typing import Any, Dict, Iterable, List

from .artifact_store import atomic_write_json
from .topic_identity import assess_topic_alignment

_CJK = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]")
_REF = re.compile(r"\[REF:([^\]]+)\]")
_WORD = re.compile(r"[A-Za-z][A-Za-z0-9'-]*")
_HEADING = re.compile(r"^##\s+(.+?)\s*$", re.MULTILINE)
_STRAWMAN = re.compile(
    r"\b(?:is|are|was|were|should be|can be)\s+not\s+[^.;!?\n]{0,80}?\s+but\s+",
    re.IGNORECASE,
)
_REINTRO = re.compile(
    r"\b(?:is defined as|refers to|represents an? (?:approach|technology|"
    r"paradigm)|stands for)\b",
    re.IGNORECASE,
)
_LOCAL_CONCLUSION = re.compile(
    r"^\s*(?:in conclusion|to conclude|in summary|overall,)\b",
    re.IGNORECASE,
)
# P1-1 style-metrics口径（与工单第一节锁定逐字一致，不许增删）：
# 段首词 = 段落第一个字母词，区分大小写。
_OPENER_WORD = re.compile(r"[^A-Za-z]*([A-Za-z']+)")
# 模板化段首六词，固定，不许增删。
_TEMPLATE_OPENERS = ("While", "Building", "The", "To", "Beyond", "Despite")
# 抽象主语九短语：全文计数、大小写不敏感；裸词 landscape/underscores 已废弃。
_ABSTRACT_SUBJECT_PHRASES = (
    "the field",
    "this approach",
    "these approaches",
    "the landscape",
    "researchers have",
    "the trade-off",
    "this paradigm",
    "the community",
    "is not merely",
)


def paragraph_opener_distribution(long_paragraphs: List[str]) -> Dict[str, int]:
    """Count case-sensitive opener words over already-split long paragraphs."""

    counts: Dict[str, int] = {}
    for paragraph in long_paragraphs:
        match = _OPENER_WORD.match(paragraph)
        if match:
            counts[match.group(1)] = counts.get(match.group(1), 0) + 1
    return counts


def abstract_subject_hits(full_text: str) -> Dict[str, int]:
    """Count the nine locked abstract-subject phrases across the whole text."""

    lowered = full_text.lower()
    return {phrase: lowered.count(phrase) for phrase in _ABSTRACT_SUBJECT_PHRASES}


def _read_json(path: Path | None) -> Dict[str, Any]:
    if not path or not path.exists():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except Exception:
        return {}


def _has_resolved_final_text_identity(row: Dict[str, Any]) -> bool:
    """Return whether a final-text-only citation still has a real identity.

    The final citation map can be assembled after the section-level trace map
    is no longer available.  That is an honest ``final_text_only`` provenance
    status, not an unresolved citation, when DOI/S2 identity and bibliographic
    metadata were resolved.  A bare marker without identity must remain a hard
    failure.
    """

    if str(row.get("trace_status") or "").strip().casefold() != "final_text_only":
        return False
    identity = str(row.get("citation_identity") or "").strip().casefold()
    if identity.startswith("doi:") or identity.startswith("s2:"):
        return True
    if str(row.get("doi") or "").strip():
        return True
    if str(row.get("s2_id") or row.get("s2id") or "").strip():
        return True
    return bool(str(row.get("title") or "").strip() and row.get("year"))


def _tokens(text: str) -> set[str]:
    return {
        word.lower()
        for word in _WORD.findall(text)
        if len(word) >= 4
    }


def _paragraphs(text: str) -> List[str]:
    return [
        block.strip()
        for block in re.split(r"\n\s*\n", text)
        if len(_WORD.findall(block)) >= 25 and not block.startswith("#")
    ]


def _duplicate_paragraph_pairs(
    paragraphs: Iterable[str],
    threshold: float = 0.82,
) -> List[Dict[str, Any]]:
    items = list(paragraphs)
    tokenized = [_tokens(item) for item in items]
    matches = []
    for left in range(len(items)):
        if not tokenized[left]:
            continue
        for right in range(left + 1, len(items)):
            if not tokenized[right]:
                continue
            overlap = len(tokenized[left] & tokenized[right])
            union = len(tokenized[left] | tokenized[right])
            score = overlap / max(1, union)
            if score >= threshold:
                matches.append(
                    {
                        "paragraph_a": left + 1,
                        "paragraph_b": right + 1,
                        "jaccard": round(score, 3),
                    }
                )
    return matches[:20]


def _title_tokens(value: str) -> set[str]:
    stop = {
        "and", "the", "for", "from", "with", "into", "toward", "towards",
        "review", "overview", "perspective",
    }
    words = {
        token.lower()
        for token in re.findall(r"[A-Za-z][A-Za-z0-9'-]*|\d+", str(value or ""))
        if (len(token) >= 3 or token.isdigit())
    }
    return {token for token in words if token not in stop}


def _normalized_title(value: str) -> str:
    return " ".join(
        re.findall(r"[a-z0-9]+", str(value or "").lower())
    )


def _planned_heading_coverage(
    headings: List[str],
    sections: List[Dict[str, Any]],
) -> tuple[int, List[str]]:
    actual = [
        {
            "index": index,
            "normalized": _normalized_title(heading),
            "tokens": _title_tokens(heading),
        }
        for index, heading in enumerate(headings)
    ]
    candidates: List[tuple[float, int, int]] = []
    for section_index, section in enumerate(sections):
        title = str(section.get("title") or "")
        planned = _title_tokens(title)
        normalized = _normalized_title(title)
        for candidate in actual:
            overlap = len(planned & candidate["tokens"])
            recall = overlap / max(1, len(planned))
            precision = overlap / max(1, len(candidate["tokens"]))
            score = 1.0 if (
                normalized and normalized == candidate["normalized"]
            ) else (0.7 * recall + 0.3 * precision)
            if planned and recall >= 0.45 and score >= 0.45:
                candidates.append((
                    score,
                    section_index,
                    int(candidate["index"]),
                ))

    # One actual heading may satisfy only one planned section.  Without this
    # assignment rule, similarly worded titles can hide a missing chapter.
    matched_sections: set[int] = set()
    matched_headings: set[int] = set()
    for _, section_index, heading_index in sorted(
        candidates,
        key=lambda item: (-item[0], item[1], item[2]),
    ):
        if (
            section_index in matched_sections
            or heading_index in matched_headings
        ):
            continue
        matched_sections.add(section_index)
        matched_headings.add(heading_index)

    missing = []
    for section_index, section in enumerate(sections):
        if section_index not in matched_sections:
            title = str(section.get("title") or "")
            missing.append(str(section.get("section_id") or title))
    return len(matched_sections), missing


def evaluate_review_content(
    *,
    final_review_path: Path | None,
    blueprint: Dict[str, Any],
    visual_plan_path: Path | None,
    citation_map_path: Path | None,
    output_dir: Path,
    research_plan_path: Path | None = None,
) -> Dict[str, Any]:
    """Evaluate content completeness and write JSON/Markdown audit artifacts."""

    output_dir.mkdir(parents=True, exist_ok=True)
    blocking: List[str] = []
    warnings: List[str] = []
    metrics: Dict[str, Any] = {}
    text = ""
    if not final_review_path or not final_review_path.exists():
        blocking.append("final_review_missing")
    else:
        text = final_review_path.read_text(encoding="utf-8", errors="replace")

    sections = [
        item
        for item in blueprint.get("sections", [])
        if isinstance(item, dict)
    ]
    headings = _HEADING.findall(text)
    words = _WORD.findall(text)
    planned_min_words = sum(
        int((item.get("target_word_range") or {}).get("min", 0) or 0)
        for item in sections
    )
    heading_coverage, missing_sections = _planned_heading_coverage(
        headings, sections
    )
    cjk_count = len(_CJK.findall(text))
    refs = _REF.findall(text)
    reference_counts = Counter(refs)
    unresolved_inline = sorted(
        {ref for ref in refs if not ref.strip() or ref.upper() == "UNKNOWN"}
    )
    long_paragraphs = _paragraphs(text)
    duplicate_pairs = _duplicate_paragraph_pairs(long_paragraphs)
    opener_counts = paragraph_opener_distribution(long_paragraphs)
    abstract_hits = abstract_subject_hits(text)
    template_opener_hits = sum(
        opener_counts.get(word, 0) for word in _TEMPLATE_OPENERS
    )
    paragraph_total = len(long_paragraphs)
    max_share = (
        round(max(opener_counts.values()) / paragraph_total, 3)
        if opener_counts
        else 0.0
    )
    template_share = (
        round(template_opener_hits / paragraph_total, 3)
        if paragraph_total
        else 0.0
    )

    metrics.update(
        {
            "planned_section_count": len(sections),
            "actual_h2_heading_count": len(headings),
            "planned_heading_coverage_count": heading_coverage,
            "missing_planned_sections": missing_sections,
            "word_count": len(words),
            "planned_min_word_count": planned_min_words,
            "planned_min_word_ratio": round(
                len(words) / planned_min_words, 3
            )
            if planned_min_words
            else None,
            "cjk_character_count": cjk_count,
            "inline_reference_marker_count": len(refs),
            "unique_inline_reference_count": len(set(refs)),
            "recommended_minimum_unique_papers": max(
                len(sections) * 4,
                (len(words) + 249) // 250,
            )
            if sections
            else 0,
            "largest_single_source_marker_share": round(
                max(reference_counts.values()) / max(1, len(refs)), 3
            )
            if reference_counts
            else 0.0,
            "unresolved_inline_references": unresolved_inline,
            "near_duplicate_paragraph_pairs": duplicate_pairs,
            "strawman_not_but_count": len(_STRAWMAN.findall(text)),
            "paragraph_opener_distribution": opener_counts,
            "paragraph_opener_max_share": max_share,
            "template_opener_share": template_share,
            "abstract_subject_hits": abstract_hits,
        }
    )

    if cjk_count:
        blocking.append("non_english_machine_output")
    # A final review cannot be publishable while a planned argument stage is
    # absent.  Partial section tolerance belongs to resumable run state, not to
    # the final content-quality verdict.
    if sections and heading_coverage < len(sections):
        blocking.append("planned_sections_not_delivered")
    if planned_min_words and len(words) < planned_min_words * 0.65:
        blocking.append("review_substantially_shorter_than_blueprint")
    if unresolved_inline:
        blocking.append("unresolved_inline_reference")
    if duplicate_pairs:
        warnings.append("near_duplicate_paragraphs")
    if metrics["strawman_not_but_count"]:
        warnings.append("strawman_not_but_constructions")
    if (
        metrics["recommended_minimum_unique_papers"]
        and len(set(refs)) < metrics["recommended_minimum_unique_papers"]
    ):
        warnings.append("low_review_wide_source_diversity")
    if metrics["largest_single_source_marker_share"] > 0.25:
        warnings.append("single_source_citation_concentration")
    # P1-1 风格度量：只进 warnings，绝不进 blocking（红线）。
    # 阈值与第八节验收目标同源：0.30 / 0.10 / 15。
    if metrics["template_opener_share"] > 0.30:
        warnings.append("high_template_opener_share")
    if metrics["paragraph_opener_max_share"] > 0.10:
        warnings.append("repetitive_paragraph_openers")
    if sum(metrics["abstract_subject_hits"].values()) > 15:
        warnings.append("abstract_subject_overuse")

    topic_identity = blueprint.get("topic_identity", {})
    if isinstance(topic_identity, dict) and topic_identity.get("valid"):
        # Headings are copied from the blueprint and therefore cannot prove
        # that the authored body stayed on topic.
        authored_body = _HEADING.sub("", text)
        topic_alignment = assess_topic_alignment(
            authored_body,
            topic_identity,
            strict=True,
        )
        authored_sections = re.split(
            r"^##\s+.+?$",
            text,
            flags=re.MULTILINE,
        )[1:]
        section_topic_alignment = [
            assess_topic_alignment(
                block,
                topic_identity,
                strict=False,
            )
            for block in authored_sections
        ]
        section_topic_pass_rate = round(
            sum(
                item.get("status") == "passed"
                for item in section_topic_alignment
            )
            / max(1, len(section_topic_alignment)),
            3,
        )
        topic_alignment["section_pass_rate"] = section_topic_pass_rate
        topic_alignment["section_results"] = section_topic_alignment
        if authored_sections and section_topic_pass_rate < 0.75:
            topic_alignment["status"] = "failed"
            topic_alignment["reason"] = (
                "review_sections_drift_from_scientific_object"
            )
    else:
        topic_alignment = {
            "status": "failed",
            "reason": "topic_identity_unavailable",
            "core_hits": [],
        }
    metrics["topic_alignment"] = topic_alignment
    if topic_alignment["status"] != "passed":
        blocking.append("review_topic_identity_mismatch")

    # Repeated topic definitions are usually a sign that independently written
    # sections were concatenated without article-level editing.
    section_blocks = re.split(r"^##\s+.+?$", text, flags=re.MULTILINE)[1:]
    reintro_sections = [
        index + 1
        for index, block in enumerate(section_blocks)
        if _REINTRO.search(" ".join(block.split()[:120]))
    ]
    local_conclusions = [
        index + 1
        for index, block in enumerate(section_blocks[:-1])
        if any(
            _LOCAL_CONCLUSION.search(paragraph)
            for paragraph in _paragraphs(block)[-2:]
        )
    ]
    metrics["topic_reintroduction_section_numbers"] = reintro_sections
    metrics["premature_local_conclusion_section_numbers"] = local_conclusions
    if len(reintro_sections) >= 3:
        warnings.append("repeated_topic_reintroduction")
    if local_conclusions:
        warnings.append("sections_read_as_independent_essays")

    citation_map = _read_json(citation_map_path)
    mapped = citation_map.get("citations", [])
    mapped = mapped if isinstance(mapped, list) else []
    final_text_only_mapped = [
        item
        for item in mapped
        if isinstance(item, dict) and _has_resolved_final_text_identity(item)
    ]
    unresolved_mapped = [
        item
        for item in mapped
        if isinstance(item, dict)
        and str(item.get("trace_status") or "") != "verified"
        and not _has_resolved_final_text_identity(item)
    ]
    mapped_papers = {
        str(item.get("paper_id") or "")
        for item in mapped
        if isinstance(item, dict) and item.get("paper_id")
    }
    metrics["citation_map_entry_count"] = len(mapped)
    metrics["citation_map_unique_paper_count"] = len(mapped_papers)
    metrics["citation_map_final_text_only_count"] = len(final_text_only_mapped)
    metrics["citation_map_unresolved_count"] = len(unresolved_mapped)
    if refs and not mapped:
        blocking.append("citation_map_missing_for_referenced_review")
    if unresolved_mapped:
        blocking.append("citation_map_contains_unresolved_entries")
    if final_text_only_mapped:
        warnings.append("citation_map_final_text_only_entries")
    if (
        sections
        and len(mapped_papers)
        < metrics["recommended_minimum_unique_papers"]
    ):
        warnings.append("low_review_wide_source_diversity")

    visual = _read_json(visual_plan_path)
    final_visual_mode = isinstance(visual.get("figures"), list)
    if final_visual_mode:
        placements = visual.get("figures", [])
        # conceptual_visual_request_count 的口径仅适用于
        # visual_editorial_plan 模式。FINAL_VISUAL_PACKAGE 没有
        # conceptual_figure_requests 键；从 unfilled 反推会把「主动决定
        # 不画」的条目也数进去，从 figures 反推会漏掉被上限挡住的请求，
        # 都是在编数字而不是测量。置 None 并在此注释说明，不伪装可测。
        # P1-3 (round 3): factory now records the measured request count in
        # visual_plan_ingestion (generation_request_count), so final mode CAN
        # report the real number instead of an unconditional None.  Packages
        # produced before that field existed still yield None -- honest.
        _ingestion = visual.get("visual_plan_ingestion") or {}
        _gen_requests = _ingestion.get("generation_request_count")
        requests = (
            [None] * int(_gen_requests)
            if isinstance(_gen_requests, int) and _gen_requests >= 0
            else None
        )
        unfilled = visual.get("unfilled_visual_opportunities", [])
    else:
        placements = visual.get("placements", [])
        requests = visual.get("conceptual_figure_requests", [])
        unfilled = visual.get("unfilled_visual_needs", [])
    placements = placements if isinstance(placements, list) else []
    # None 是「final 模式下该口径不可测」的显式信号，不能被归一成 []。
    requests = (
        requests
        if (requests is None or isinstance(requests, list))
        else []
    )
    unfilled = unfilled if isinstance(unfilled, list) else []
    missing_paths = []
    path_counter: Counter[str] = Counter()
    for item in placements:
        if not isinstance(item, dict):
            continue
        raw_path = str(
            item.get("local_path")
            or item.get("local_image_path")
            or ""
        )
        path_counter[raw_path] += 1
        if not raw_path or not Path(raw_path).is_file():
            missing_paths.append(raw_path or "<empty>")
    duplicate_visual_paths = [
        path for path, count in path_counter.items() if path and count > 1
    ]
    metrics.update(
        {
            "verified_visual_placement_count": len(placements) - len(missing_paths),
            "renderable_final_visual_count": (
                len(placements) - len(missing_paths)
                if final_visual_mode
                else 0
            ),
            "visual_input_contract": (
                "final_visual_package"
                if final_visual_mode
                else "visual_editorial_plan"
            ),
            "conceptual_visual_request_count": (
                len(requests) if requests is not None else None
            ),
            "unfilled_visual_need_count": len(unfilled),
            "missing_visual_paths": missing_paths,
            "duplicate_visual_paths": duplicate_visual_paths,
        }
    )
    if missing_paths:
        blocking.append("selected_visual_path_missing")
    if duplicate_visual_paths:
        warnings.append("duplicate_visual_reuse")
    if not placements and not requests:
        warnings.append("no_article_level_visual_plan")
    if unfilled:
        warnings.append("unfilled_visual_needs")
    visual_text = json.dumps(visual, ensure_ascii=True, sort_keys=True)
    if (
        visual
        and topic_identity.get("valid")
        and len(_WORD.findall(visual_text)) >= 40
    ):
        visual_alignment = assess_topic_alignment(
            visual_text,
            topic_identity,
            strict=False,
        )
        metrics["visual_plan_topic_alignment"] = visual_alignment
        if visual_alignment["status"] != "passed":
            blocking.append("visual_plan_topic_identity_mismatch")
    elif visual:
        metrics["visual_plan_topic_alignment"] = {
            "status": "not_assessed",
            "reason": "insufficient_scientific_text_in_visual_plan",
        }

    research_plan: Any = {}
    if research_plan_path and research_plan_path.exists():
        if research_plan_path.suffix.lower() == ".json":
            research_plan = _read_json(research_plan_path)
        else:
            research_plan = research_plan_path.read_text(
                encoding="utf-8",
                errors="replace",
            )
    if research_plan and topic_identity.get("valid"):
        research_plan_alignment = assess_topic_alignment(
            research_plan,
            topic_identity,
            strict=True,
        )
        metrics["research_plan_topic_alignment"] = research_plan_alignment
        if research_plan_alignment["status"] != "passed":
            blocking.append("research_plan_topic_identity_mismatch")

    # Deduplicate while preserving diagnosis order.
    blocking = list(dict.fromkeys(blocking))
    warnings = list(dict.fromkeys(warnings))
    status = "failed" if blocking else ("needs_attention" if warnings else "passed")
    # P2 (round 3): a needs_attention verdict must come with a remediation
    # path per warning -- an issue without a next action just parks runs.
    _REMEDIATION_HINTS = {
        "near_duplicate_paragraphs": (
            "merge or rewrite one of each duplicated paragraph pair"
        ),
        "strawman_not_but_constructions": (
            "rewrite is/are-not-but constructions into direct statements "
            "(style pipeline flags these as strawman_not_but)"
        ),
        "low_review_wide_source_diversity": (
            "add citations from additional distinct sources"
        ),
        "duplicate_visual_reuse": (
            "replace duplicated figures with distinct visuals"
        ),
        "no_article_level_visual_plan": (
            "produce a visual plan or record an explicit no-figure decision"
        ),
    }
    report = {
        "schema_version": "research_harness.content_quality.v1",
        "status": status,
        "blocking_issues": blocking,
        "warnings": warnings,
        "remediation_hints": {
            warning: _REMEDIATION_HINTS.get(
                warning,
                "review and either fix or explicitly accept this finding",
            )
            for warning in warnings
        },
        "metrics": metrics,
        "evidence_policy": (
            "The gate checks traceability for explicit citations and pivotal "
            "facts; it does not require every synthesis sentence to carry a citation."
        ),
    }
    json_path = output_dir / "REVIEW_HARNESS_QUALITY_REPORT.json"
    md_path = output_dir / "REVIEW_HARNESS_QUALITY_REPORT.md"
    atomic_write_json(json_path, report)
    lines = [
        "# Review Harness Quality Report",
        "",
        f"- Status: `{status}`",
        f"- Planned sections delivered: {heading_coverage}/{len(sections)}",
        f"- Words: {len(words)} (blueprint minimum {planned_min_words})",
        f"- Traceable cited papers: {len(mapped_papers)}",
        f"- Verified existing visual placements: {len(placements) - len(missing_paths)}",
        (
            "- Conceptual visual requests: not measurable "
            "(final-package mode has no request ledger)"
            if requests is None
            else f"- Conceptual visual requests: {len(requests)}"
        ),
        "",
        "## Blocking issues",
        "",
    ]
    lines.extend(f"- {item}" for item in blocking)
    if not blocking:
        lines.append("- None")
    lines.extend(["", "## Warnings", ""])
    lines.extend(f"- {item}" for item in warnings)
    if not warnings:
        lines.append("- None")
    md_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    report["json_path"] = str(json_path)
    report["markdown_path"] = str(md_path)
    return report
