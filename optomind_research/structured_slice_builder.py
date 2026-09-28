"""Build reversible structured text slices from selected paper full texts.

The builder is conservative:
- every slice stores exact character offsets into a canonicalized text string;
- slices are non-overlapping and reconstructable by concatenation;
- multiple deterministic slicing strategies can be compared on a small sample;
- optional low-tier LLM profiling labels slices but does not remove text.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import re
import statistics
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from llm.qwen_chat_client import call_qwen_chat
from optomind_research.common_io import DEFAULT_CARDS_JSONL, as_list, clip, contains_cjk, write_json, write_text


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT_ROOT = PROJECT_ROOT / "outputs" / "structured_slices"
DEFAULT_PROMPT = PROJECT_ROOT / "prompts" / "Text Slice Role Profiler.txt"
DEFAULT_REVIEW_FRAMEWORK_JSON = (
    PROJECT_ROOT
    / "outputs"
    / "review_blueprints"
    / "core58-hqvisual-blueprint-v3-consensus-final-20260703"
    / "review_blueprint.v3.visual_aware.json"
)


SECTION_PATTERNS = [
    "abstract",
    "introduction",
    "background",
    "related work",
    "materials and methods",
    "methods",
    "experimental",
    "experiment",
    "results",
    "discussion",
    "results and discussion",
    "conclusion",
    "conclusions",
    "outlook",
    "limitations",
    "references",
    "acknowledgements",
    "acknowledgments",
    "supporting information",
]

BOILERPLATE_TERMS = [
    "download date",
    "take down policy",
    "cookie",
    "all rights reserved",
    "terms and conditions",
    "advertisement",
    "sign in",
    "view article",
    "download pdf",
    "supplementary material",
]


def utc_stamp() -> str:
    return datetime.utcnow().strftime("%Y%m%d-%H%M%S")


def print_event(event: str, **payload: Any) -> None:
    print(json.dumps({"event": event, **payload}, ensure_ascii=False), flush=True)


def read_text(path: str | Path) -> str:
    p = Path(path)
    return p.read_text(encoding="utf-8", errors="replace") if p.exists() else ""


def canonicalize_text(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n")


def normalize_space(text: str) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip()


def sanitize_generated_text(value: Any) -> Any:
    replacements = {
        "\u00b0C": "degC",
        "\u00b5m": "micrometer",
        "\u03bcm": "micrometer",
        "\u03bb": "lambda",
        "\u03b5": "epsilon",
        "\u03b1": "alpha",
        "\u63b3C": "degC",
        "\u788cm": "micrometer",
        "\u6e2dm": "micrometer",
        "\u4f4d": "lambda",
        "\u9225": "'",
        "m\u00b2": "m2",
        "K\u00f6ppen": "Koppen",
    }
    if isinstance(value, str):
        out = value
        for src, dst in replacements.items():
            out = out.replace(src, dst)
        return out
    if isinstance(value, list):
        return [sanitize_generated_text(item) for item in value]
    if isinstance(value, dict):
        return {str(k): sanitize_generated_text(v) for k, v in value.items()}
    return value


def safe_id(text: str, fallback: str = "paper") -> str:
    raw = normalize_space(text) or fallback
    out = re.sub(r"[^A-Za-z0-9._-]+", "-", raw).strip("-").lower()
    return out[:90] or fallback


def normalize_doi_local(value: str) -> str:
    doi = str(value or "").strip().lower()
    doi = re.sub(r"^https?://(?:dx\.)?doi\.org/", "", doi, flags=re.I)
    doi = doi.replace("doi:", "").strip()
    doi = re.sub(r"^(10\.\d+)//+", r"\1/", doi)
    doi = re.sub(r"/{2,}", "/", doi)
    return doi


def text_hash(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8", errors="ignore")).hexdigest()


def load_card_records(cards_jsonl: Path) -> list[dict[str, Any]]:
    records = []
    for i, line in enumerate(read_text(cards_jsonl).splitlines(), 1):
        if not line.strip():
            continue
        try:
            rec = json.loads(line)
        except Exception:
            continue
        ident = rec.get("paper_identity") if isinstance(rec.get("paper_identity"), dict) else {}
        meta = rec.get("_local_metadata") if isinstance(rec.get("_local_metadata"), dict) else {}
        parsed_path = Path(str(meta.get("parsed_text_path") or ""))
        doi = normalize_doi_local(str(ident.get("doi") or ""))
        title = str(ident.get("title") or f"paper-{i}").strip()
        paper_id = f"doi:{doi}" if doi else f"local:{safe_id(title)}"
        records.append(
            {
                "record_index": i,
                "paper_id": paper_id,
                "doi": doi,
                "title": title,
                "year": ident.get("year"),
                "venue": ident.get("venue"),
                "parsed_text_path": str(parsed_path),
                "parsed_text_exists": parsed_path.exists(),
                "full_text_chars": int(meta.get("full_text_chars") or 0),
                "chunk_count_existing": int(meta.get("chunk_count") or 0),
            }
        )
    return records


def load_review_framework(path: str | Path) -> dict[str, Any]:
    p = Path(path) if path else DEFAULT_REVIEW_FRAMEWORK_JSON
    if not p.exists():
        return {
            "schema_version": "review_framework_stub.v1",
            "note": "No external review framework file was found. Use only the review objective.",
            "perspectives": [],
            "priority_gap_queries": [],
        }
    try:
        data = json.loads(p.read_text(encoding="utf-8", errors="replace"))
    except Exception:
        return {
            "schema_version": "review_framework_unreadable.v1",
            "source_path": str(p),
            "perspectives": [],
            "priority_gap_queries": [],
        }
    compact = {
        "schema_version": data.get("schema_version", "review_framework.v1") if isinstance(data, dict) else "review_framework.v1",
        "source_path": str(p),
        "topic_framing": data.get("topic_framing", "") if isinstance(data, dict) else "",
        "perspectives": [],
        "cross_perspective_tensions": as_list(data.get("cross_perspective_tensions"))[:8] if isinstance(data, dict) else [],
        "priority_gap_queries": [],
    }
    if isinstance(data, dict):
        for item in as_list(data.get("perspectives"))[:8]:
            if not isinstance(item, dict):
                continue
            compact["perspectives"].append(
                {
                    "perspective_id": item.get("perspective_id"),
                    "role_name": item.get("role_name"),
                    "lens_focus": item.get("lens_focus"),
                    "key_questions": [
                        {
                            "question_id": q.get("question_id"),
                            "question": q.get("question"),
                            "coverage_status": q.get("coverage_status"),
                        }
                        for q in as_list(item.get("key_questions"))[:5]
                        if isinstance(q, dict)
                    ],
                }
            )
        rec = data.get("recommendations_for_review_architect")
        if isinstance(rec, dict):
            compact["priority_gap_queries"] = as_list(rec.get("priority_gap_queries"))[:5]
    return compact


@dataclass
class Paragraph:
    index: int
    start: int
    end: int
    text: str
    is_heading: bool
    heading_level: int
    heading_text: str


def iter_paragraphs(text: str) -> list[Paragraph]:
    paragraphs: list[Paragraph] = []
    pattern = re.compile(r"\S(?:.*?\S)?(?=\n\s*\n|\Z)", re.S)
    for idx, match in enumerate(pattern.finditer(text)):
        raw = match.group(0)
        stripped = raw.strip()
        is_heading, level, heading = classify_heading(stripped)
        paragraphs.append(
            Paragraph(
                index=idx,
                start=match.start(),
                end=match.end(),
                text=raw,
                is_heading=is_heading,
                heading_level=level,
                heading_text=heading,
            )
        )
    if not paragraphs and text:
        is_heading, level, heading = classify_heading(text[:120])
        paragraphs.append(Paragraph(0, 0, len(text), text, is_heading, level, heading))
    return paragraphs


def classify_heading(paragraph: str) -> tuple[bool, int, str]:
    lines = [line.strip() for line in paragraph.splitlines() if line.strip()]
    if not lines:
        return False, 0, ""
    first = lines[0]
    if first.startswith("#"):
        m = re.match(r"^(#{1,6})\s+(.+)$", first)
        if m:
            return True, len(m.group(1)), normalize_space(m.group(2))[:160]
    clean = normalize_space(first)
    low = clean.lower().strip(" .:")
    if low in SECTION_PATTERNS:
        return True, 2, clean[:160]
    if re.match(r"^\d+(\.\d+)*\s+[A-Z][A-Za-z0-9 ,:/()\\-]{2,100}$", clean):
        return True, 2 + clean.count("."), clean[:160]
    if len(lines) == 1 and 4 <= len(clean) <= 90 and clean.count(".") == 0:
        words = clean.split()
        title_like = sum(1 for word in words if word[:1].isupper() or word.isupper())
        if words and title_like / max(1, len(words)) >= 0.55:
            return True, 3, clean[:160]
    return False, 0, ""


def nearest_paragraph_breaks(paragraphs: list[Paragraph]) -> list[int]:
    points = sorted({0, *(p.end for p in paragraphs)})
    return points


def slice_from_breaks(text: str, paper: dict[str, Any], method: str, breakpoints: list[int], paragraphs: list[Paragraph]) -> list[dict[str, Any]]:
    breakpoints = sorted(set(int(x) for x in breakpoints if 0 <= int(x) <= len(text)))
    if not breakpoints or breakpoints[0] != 0:
        breakpoints.insert(0, 0)
    if breakpoints[-1] != len(text):
        breakpoints.append(len(text))
    para_by_range = paragraphs
    slices = []
    section_stack: list[str] = []
    para_cursor = 0
    for ordinal, (start, end) in enumerate(zip(breakpoints, breakpoints[1:])):
        if end <= start:
            continue
        while para_cursor < len(para_by_range) and para_by_range[para_cursor].end <= start:
            para_cursor += 1
        local_paras = [p for p in para_by_range if p.start < end and p.end > start]
        for p in local_paras:
            if p.is_heading and p.heading_text:
                while len(section_stack) >= max(1, p.heading_level):
                    section_stack.pop()
                section_stack.append(p.heading_text)
        chunk_text = text[start:end]
        compact = normalize_space(chunk_text)
        boiler_hits = sum(1 for term in BOILERPLATE_TERMS if term in compact.lower())
        slice_id = f"{safe_id(paper['paper_id'])}:{method}:s{ordinal:04d}"
        slices.append(
            {
                "schema_version": "structured_text_slice.v1",
                "slice_id": slice_id,
                "paper_id": paper["paper_id"],
                "doi": paper.get("doi", ""),
                "title": paper.get("title", ""),
                "method": method,
                "ordinal": ordinal,
                "char_start": start,
                "char_end": end,
                "char_count": end - start,
                "word_count_estimate": len(re.findall(r"\b\w+\b", chunk_text)),
                "text_sha1": text_hash(chunk_text),
                "section_path": section_stack[-4:],
                "starts_with_heading": bool(local_paras and local_paras[0].is_heading),
                "paragraph_indices": [p.index for p in local_paras],
                "boilerplate_score": boiler_hits,
                "text": chunk_text,
            }
        )
    return slices


def make_fixed_char_slices(text: str, paper: dict[str, Any], paragraphs: list[Paragraph], *, target_chars: int = 3200) -> list[dict[str, Any]]:
    para_breaks = nearest_paragraph_breaks(paragraphs)
    breakpoints = [0]
    pos = 0
    while pos < len(text):
        target = min(len(text), pos + target_chars)
        candidates = [b for b in para_breaks if pos + target_chars * 0.55 <= b <= pos + target_chars * 1.35]
        nxt = min(candidates, key=lambda b: abs(b - target)) if candidates else min(len(text), target)
        if nxt <= pos:
            nxt = min(len(text), pos + target_chars)
        breakpoints.append(nxt)
        pos = nxt
    return slice_from_breaks(text, paper, "fixed_char_3200", breakpoints, paragraphs)


def make_paragraph_balanced_slices(text: str, paper: dict[str, Any], paragraphs: list[Paragraph], *, target_chars: int = 2600, max_chars: int = 4200) -> list[dict[str, Any]]:
    breakpoints = [0]
    current_start = 0
    for p in paragraphs:
        current_len = p.end - current_start
        if current_len >= target_chars and (p.end - current_start) <= max_chars:
            breakpoints.append(p.end)
            current_start = p.end
        elif current_len > max_chars:
            breakpoints.append(max(p.start, current_start + target_chars))
            current_start = breakpoints[-1]
    if breakpoints[-1] != len(text):
        breakpoints.append(len(text))
    return slice_from_breaks(text, paper, "paragraph_balanced", breakpoints, paragraphs)


def make_section_aware_slices(text: str, paper: dict[str, Any], paragraphs: list[Paragraph], *, target_chars: int = 3600, max_chars: int = 5600) -> list[dict[str, Any]]:
    breakpoints = [0]
    current_start = 0
    for p in paragraphs:
        if p.is_heading and p.start > current_start and p.start - current_start >= 900:
            breakpoints.append(p.start)
            current_start = p.start
        if p.end - current_start >= max_chars:
            breakpoints.append(p.end)
            current_start = p.end
        elif p.end - current_start >= target_chars and not p.is_heading:
            breakpoints.append(p.end)
            current_start = p.end
    if breakpoints[-1] != len(text):
        breakpoints.append(len(text))
    return slice_from_breaks(text, paper, "section_aware", breakpoints, paragraphs)


def make_atomic_paragraph_slices(text: str, paper: dict[str, Any], paragraphs: list[Paragraph], *, target_chars: int = 1400, max_chars: int = 2400) -> list[dict[str, Any]]:
    breakpoints = [0]
    current_start = 0
    for p in paragraphs:
        if p.end - current_start >= target_chars:
            breakpoints.append(p.end)
            current_start = p.end
        elif p.end - current_start >= max_chars:
            breakpoints.append(p.end)
            current_start = p.end
    if breakpoints[-1] != len(text):
        breakpoints.append(len(text))
    return slice_from_breaks(text, paper, "atomic_paragraph", breakpoints, paragraphs)


def evaluate_slices(text: str, slices: list[dict[str, Any]]) -> dict[str, Any]:
    reconstructed = "".join(str(s.get("text") or "") for s in slices)
    lengths = [int(s.get("char_count") or 0) for s in slices]
    words = [int(s.get("word_count_estimate") or 0) for s in slices]
    tiny = sum(1 for n in lengths if n < 500)
    huge = sum(1 for n in lengths if n > 6500)
    heading_rate = sum(1 for s in slices if s.get("starts_with_heading")) / max(1, len(slices))
    section_rate = sum(1 for s in slices if s.get("section_path")) / max(1, len(slices))
    boiler = sum(int(s.get("boilerplate_score") or 0) for s in slices)
    reversible = reconstructed == text
    median_len = statistics.median(lengths) if lengths else 0
    mean_len = statistics.mean(lengths) if lengths else 0
    balance_penalty = abs(median_len - 3000) / 3000
    score = 0.0
    score += 35 if reversible else 0
    score += max(0, 20 * (1 - min(1, balance_penalty)))
    score += max(0, 15 * (1 - tiny / max(1, len(slices))))
    score += max(0, 10 * (1 - huge / max(1, len(slices))))
    score += 10 * min(1, section_rate)
    score += 5 * min(1, heading_rate * 2)
    score += max(0, 5 * (1 - boiler / max(1, len(slices) * 2)))
    return {
        "reversible": reversible,
        "original_chars": len(text),
        "reconstructed_chars": len(reconstructed),
        "slice_count": len(slices),
        "min_chars": min(lengths) if lengths else 0,
        "median_chars": median_len,
        "mean_chars": round(mean_len, 1) if lengths else 0,
        "max_chars": max(lengths) if lengths else 0,
        "median_words": statistics.median(words) if words else 0,
        "tiny_slice_count": tiny,
        "huge_slice_count": huge,
        "section_path_rate": round(section_rate, 3),
        "heading_start_rate": round(heading_rate, 3),
        "boilerplate_score_total": boiler,
        "quality_score": round(score, 2),
    }


def build_all_methods(text: str, paper: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    paragraphs = iter_paragraphs(text)
    return {
        "fixed_char_3200": make_fixed_char_slices(text, paper, paragraphs),
        "paragraph_balanced": make_paragraph_balanced_slices(text, paper, paragraphs),
        "section_aware": make_section_aware_slices(text, paper, paragraphs),
        "atomic_paragraph": make_atomic_paragraph_slices(text, paper, paragraphs),
    }


def build_english_working_slices(
    slices: list[dict[str, Any]],
    *,
    enabled: bool = True,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Keep reversible source offsets while making active slice text English."""
    rows = [dict(row) for row in slices]
    if not enabled or not any(contains_cjk(str(row.get("text") or "")) for row in rows):
        for row in rows:
            row.setdefault("source_language", "english")
            row.setdefault("translation_status", "original_english")
        return rows, {
            "enabled": enabled,
            "translation_required": 0,
            "translated": 0,
            "quarantined": 0,
        }

    from optomind_research.scientific_text_english_normalizer import ScientificTextEnglishNormalizer

    normalizer = ScientificTextEnglishNormalizer()
    normalized = normalizer.normalize([str(row.get("text") or "") for row in rows])
    for row, record in zip(rows, normalized):
        original = str(row.get("text") or "")
        row["source_language"] = record.source_language
        row["translation_status"] = record.translation_status
        row["translation_model"] = record.translation_model
        row["translation_validation_errors"] = list(record.validation_errors or [])
        if record.source_language != "english":
            row["source_text"] = original
            row["source_text_sha1"] = str(row.get("text_sha1") or text_hash(original))
            row["text"] = record.text_en
            row["text_sha1"] = text_hash(record.text_en)
            row["word_count_estimate"] = len(re.findall(r"\b\w+\b", record.text_en))
    return rows, dict(normalizer.last_audit)


def select_hybrid_method(text: str, method_slices: dict[str, list[dict[str, Any]]], method_metrics: dict[str, dict[str, Any]]) -> tuple[str, str]:
    """Select one deterministic reversible slicing method for a paper.

    The selector is deliberately simple and auditable. It first excludes
    non-reversible methods, then applies task-neutral preferences:
    section-aware for long, well-structured documents; fixed-char for medium
    documents where it is clearly best; paragraph-balanced as the robust
    default.
    """

    reversible = {m: v for m, v in method_metrics.items() if v.get("reversible")}
    if not reversible:
        best = max(method_metrics.items(), key=lambda kv: kv[1].get("quality_score", 0))[0]
        return best, "no_reversible_candidate_quality_fallback"
    best_by_score = max(reversible.items(), key=lambda kv: kv[1].get("quality_score", 0))[0]
    length = len(text)
    section = reversible.get("section_aware")
    fixed = reversible.get("fixed_char_3200")
    paragraph = reversible.get("paragraph_balanced")
    best_score = reversible[best_by_score].get("quality_score", 0)

    if length >= 50000 and section:
        if section.get("section_path_rate", 0) >= 0.9 and section.get("huge_slice_count", 0) <= 1 and best_score - section.get("quality_score", 0) <= 3.0:
            return "section_aware", "long_document_with_usable_section_structure"
    if 12000 <= length < 50000 and fixed:
        if fixed.get("huge_slice_count", 0) == 0 and best_score - fixed.get("quality_score", 0) <= 1.5:
            return "fixed_char_3200", "medium_document_fixed_char_balanced_and_safe"
    if paragraph:
        if paragraph.get("huge_slice_count", 0) == 0 and best_score - paragraph.get("quality_score", 0) <= 3.0:
            return "paragraph_balanced", "robust_default_paragraph_boundary_preservation"
    return best_by_score, "highest_quality_score"


def rewrite_slice_method(slices: list[dict[str, Any]], *, selected_method: str, paper: dict[str, Any]) -> list[dict[str, Any]]:
    out = []
    for i, item in enumerate(slices):
        row = dict(item)
        row["base_method"] = row.get("method")
        row["method"] = "hybrid_selector"
        row["hybrid_selected_method"] = selected_method
        row["slice_id"] = f"{safe_id(paper['paper_id'])}:hybrid:s{i:04d}"
        out.append(row)
    return out


def choose_sample(records: list[dict[str, Any]], *, sample_size: int, seed: int) -> list[dict[str, Any]]:
    eligible = [r for r in records if r.get("parsed_text_exists")]
    rng = random.Random(seed)
    if len(eligible) <= sample_size:
        return eligible
    return rng.sample(eligible, sample_size)


def choose_records(records: list[dict[str, Any]], *, all_papers: bool, sample_size: int, seed: int) -> list[dict[str, Any]]:
    eligible = [r for r in records if r.get("parsed_text_exists")]
    if all_papers or sample_size <= 0:
        return eligible
    return choose_sample(records, sample_size=sample_size, seed=seed)


def parse_json_response(text: str) -> dict[str, Any]:
    try:
        return json.loads(text)
    except Exception:
        m = re.search(r"\{.*\}", str(text or ""), re.S)
        if not m:
            return {}
        try:
            return json.loads(m.group(0))
        except Exception:
            return {}


def normalize_slice_profiles(profiles: list[dict[str, Any]]) -> list[dict[str, Any]]:
    normalized = []
    allowed_roles = {
        "background",
        "motivation",
        "method",
        "mechanism",
        "experiment",
        "result",
        "benchmark",
        "limitation",
        "discussion",
        "conclusion",
        "reference_or_boilerplate",
        "mixed",
        "unclear",
    }
    allowed_utility = {"high", "medium", "low", "exclude"}
    allowed_density = {"high", "medium", "low", "none"}

    def compact_list(value: Any, limit: int = 8) -> list[str]:
        out = []
        for x in as_list(value):
            s = normalize_space(str(x or ""))
            if s:
                out.append(s)
            if len(out) >= limit:
                break
        return out

    def split_framework_alignment(value: Any) -> tuple[list[str], list[str]]:
        ids: list[str] = []
        notes: list[str] = []
        for x in as_list(value):
            s = normalize_space(str(x or ""))
            if not s:
                continue
            if re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]*(?:-[A-Za-z0-9]+)*", s) and len(s) <= 40:
                ids.append(s)
            else:
                notes.append(s)
        return ids[:8], notes[:8]

    for item in profiles:
        if not isinstance(item, dict):
            continue
        intrinsic = item.get("intrinsic_labels") if isinstance(item.get("intrinsic_labels"), dict) else {}
        task = item.get("review_task_labels") if isinstance(item.get("review_task_labels"), dict) else {}
        role = str(intrinsic.get("rhetorical_role") or item.get("rhetorical_role") or "unclear")
        if role not in allowed_roles:
            role = "unclear"
        utility = str(task.get("review_utility") or item.get("review_utility") or "medium")
        if utility not in allowed_utility:
            utility = "medium"
        density = str(intrinsic.get("evidence_density") or item.get("evidence_density") or "low")
        if density not in allowed_density:
            density = "low"
        alignment_ids, legacy_alignment_notes = split_framework_alignment(task.get("framework_alignment"))
        explicit_ids, explicit_notes_from_ids = split_framework_alignment(task.get("framework_alignment_ids"))
        alignment_ids = (explicit_ids or alignment_ids)[:8]
        alignment_notes = compact_list(task.get("framework_alignment_notes"), 8)
        if explicit_notes_from_ids:
            alignment_notes.extend(explicit_notes_from_ids)
        if legacy_alignment_notes:
            alignment_notes.extend(legacy_alignment_notes)
        alignment_notes = alignment_notes[:8]
        normalized.append(
            {
                "slice_id": str(item.get("slice_id") or ""),
                "intrinsic_labels": {
                    "rhetorical_role": role,
                    "scientific_content_type": str(intrinsic.get("scientific_content_type") or ""),
                    "evidence_density": density,
                    "contains_mixed_boilerplate": bool(intrinsic.get("contains_mixed_boilerplate", False)),
                    "concise_label": str(intrinsic.get("concise_label") or item.get("concise_label") or ""),
                    "key_terms": compact_list(intrinsic.get("key_terms") or item.get("key_terms"), 8),
                    "entities_or_materials": compact_list(intrinsic.get("entities_or_materials"), 8),
                    "methods_or_instruments": compact_list(intrinsic.get("methods_or_instruments"), 8),
                    "metrics_or_quantities": compact_list(intrinsic.get("metrics_or_quantities"), 8),
                    "is_boilerplate": bool(intrinsic.get("is_boilerplate", role == "reference_or_boilerplate")),
                    "intrinsic_reason": str(intrinsic.get("intrinsic_reason") or item.get("reason") or ""),
                },
                "review_task_labels": {
                    "review_utility": utility,
                    "likely_review_sections": compact_list(task.get("likely_review_sections"), 8),
                    "argument_function": str(task.get("argument_function") or ""),
                    "framework_alignment_ids": alignment_ids,
                    "framework_alignment_notes": alignment_notes,
                    "candidate_claims_supported": compact_list(task.get("candidate_claims_supported"), 8),
                    "limitations_or_caveats": compact_list(task.get("limitations_or_caveats"), 8),
                    "task_specific_reason": str(task.get("task_specific_reason") or ""),
                    "followup_needed": str(task.get("followup_needed") or ""),
                },
            }
        )
    return sanitize_generated_text(normalized)


def profile_slices_with_llm(
    *,
    paper: dict[str, Any],
    slices: list[dict[str, Any]],
    prompt_path: Path,
    review_objective: str,
    review_framework: dict[str, Any],
    model_tier: str,
    max_slices: int,
    slice_selection: str = "spread",
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    selected = []
    for s in select_profile_slice_candidates(
        slices,
        max_slices=int(max_slices),
        mode=str(slice_selection),
        seed_text=str(paper.get("paper_id") or paper.get("title") or ""),
    ):
        compact = normalize_space(s.get("text") or "")
        selected.append(
            {
                "slice_id": s.get("slice_id"),
                "section_path": s.get("section_path"),
                "char_count": s.get("char_count"),
                "text": clip(compact, 1800),
            }
        )
    if not selected:
        return [], {"skipped": True, "reason": "no_profile_candidates"}
    system = prompt_path.read_text(encoding="utf-8")
    payload = {
        "paper_metadata": {k: paper.get(k) for k in ("paper_id", "doi", "title", "year", "venue")},
        "review_objective": review_objective,
        "review_framework": review_framework,
        "slices": selected,
    }
    result = call_qwen_chat(
        "TextSliceRoleProfilerAgent",
        [{"role": "system", "content": system}, {"role": "user", "content": json.dumps(payload, ensure_ascii=False)}],
        model_tier=model_tier,
        temperature=0,
        max_tokens=min(6500, max(2600, 900 + max_slices * 750)),
        response_format={"type": "json_object"},
        force_mock=False,
        max_retries=1,
    )
    raw = str(result.get("content") or "")
    parsed = parse_json_response(raw)
    profiles = as_list(parsed.get("profiles")) if isinstance(parsed, dict) else []
    normalized = normalize_slice_profiles([p for p in profiles if isinstance(p, dict)])
    usage = result.get("_llm_usage") or {}
    raw_sanitized = str(sanitize_generated_text(raw))
    usage["_profile_diagnostics"] = {
        "raw_chars": len(raw),
        "parse_ok": bool(parsed),
        "profile_count": len(normalized),
        "raw_preview": clip(raw_sanitized, 1200),
    }
    usage["_raw_content"] = raw_sanitized
    return normalized, usage


def select_profile_indices(total: int, limit: int, mode: str, seed: int) -> set[int]:
    if total <= 0 or limit <= 0:
        return set()
    limit = min(total, limit)
    mode = str(mode or "spread").lower()
    if mode == "first":
        return set(range(limit))
    if mode == "random":
        rng = random.Random(seed + 7919)
        return set(rng.sample(range(total), limit))
    if limit == 1:
        return {0}
    return {round(i * (total - 1) / (limit - 1)) for i in range(limit)}


def select_profile_slice_candidates(slices: list[dict[str, Any]], *, max_slices: int, mode: str, seed_text: str) -> list[dict[str, Any]]:
    eligible = []
    for s in slices:
        compact = normalize_space(s.get("text") or "")
        if len(compact) < 300:
            continue
        eligible.append(s)
    if not eligible or max_slices <= 0:
        return []
    if len(eligible) <= max_slices:
        return eligible
    mode = str(mode or "spread").lower()
    if mode == "first":
        return eligible[:max_slices]
    if mode == "random":
        seed = int(hashlib.sha1(str(seed_text or "").encode("utf-8")).hexdigest()[:8], 16)
        rng = random.Random(seed)
        indexes = sorted(rng.sample(range(len(eligible)), max_slices))
        return [eligible[i] for i in indexes]
    if max_slices == 1:
        return [eligible[len(eligible) // 2]]
    indexes = sorted({round(i * (len(eligible) - 1) / (max_slices - 1)) for i in range(max_slices)})
    return [eligible[i] for i in indexes[:max_slices]]


def run(args: argparse.Namespace) -> int:
    output_dir = Path(args.output_dir) if args.output_dir else DEFAULT_OUTPUT_ROOT / utc_stamp()
    output_dir.mkdir(parents=True, exist_ok=True)
    print_event("start", output_dir=str(output_dir))

    records = load_card_records(Path(args.cards_jsonl))
    selected = choose_records(records, all_papers=bool(args.all_papers), sample_size=int(args.sample_size), seed=int(args.seed))
    write_json(output_dir / "selected_papers.json", selected)
    print_event("papers_selected", total_records=len(records), selected=len(selected), seed=int(args.seed), all_papers=bool(args.all_papers))

    review_objective = args.review_objective or "Write a high-quality scholarly literature review based on the selected scientific paper set."
    review_framework = load_review_framework(args.review_framework_json)
    write_json(output_dir / "review_framework.compact.json", review_framework)
    method_rows = []
    paper_reports = []
    best_method_votes = Counter()
    hybrid_method_votes = Counter()
    llm_usage = []
    profile_done = 0
    profile_errors = []
    profile_jobs: list[dict[str, Any]] = []
    total_hybrid_slices = 0
    aggregate_path = output_dir / "all_slices.jsonl"
    if aggregate_path.exists():
        aggregate_path.unlink()
    profile_indices = select_profile_indices(
        len(selected),
        int(args.profile_paper_limit),
        str(args.profile_selection),
        int(args.seed),
    )

    for selected_index, paper in enumerate(selected):
        path = Path(str(paper.get("parsed_text_path") or ""))
        text = canonicalize_text(read_text(path))
        if not text:
            paper_reports.append({"paper": paper, "error": "missing_text"})
            continue
        method_slices = build_all_methods(text, paper)
        method_metrics = {}
        for method, slices in method_slices.items():
            metrics = evaluate_slices(text, slices)
            method_metrics[method] = metrics
            method_rows.append({"paper_id": paper["paper_id"], "title": paper["title"], "method": method, **metrics})
            if args.write_method_samples:
                method_dir = output_dir / "method_samples" / safe_id(paper["paper_id"]) / method
                method_dir.mkdir(parents=True, exist_ok=True)
                with (method_dir / "slices.jsonl").open("w", encoding="utf-8", newline="\n") as f:
                    for s in slices:
                        f.write(json.dumps(s, ensure_ascii=False) + "\n")
                write_json(method_dir / "metrics.json", metrics)
        best_method = max(method_metrics.items(), key=lambda kv: kv[1]["quality_score"])[0]
        best_method_votes[best_method] += 1
        hybrid_method, hybrid_reason = select_hybrid_method(text, method_slices, method_metrics)
        hybrid_method_votes[hybrid_method] += 1
        source_hybrid_slices = rewrite_slice_method(method_slices[hybrid_method], selected_method=hybrid_method, paper=paper)
        hybrid_metrics = evaluate_slices(text, source_hybrid_slices)
        hybrid_slices, english_normalization = build_english_working_slices(
            source_hybrid_slices,
            enabled=bool(args.normalize_non_english),
        )
        total_hybrid_slices += len(hybrid_slices)

        paper_dir = output_dir / "paper_slices" / safe_id(paper["paper_id"])
        paper_dir.mkdir(parents=True, exist_ok=True)
        write_text(paper_dir / "source_text.txt", text)
        with (paper_dir / "source_slices.jsonl").open("w", encoding="utf-8", newline="\n") as f:
            for s in source_hybrid_slices:
                f.write(json.dumps(s, ensure_ascii=False) + "\n")
        with (paper_dir / "slices.jsonl").open("w", encoding="utf-8", newline="\n") as f:
            for s in hybrid_slices:
                f.write(json.dumps(s, ensure_ascii=False) + "\n")
        with aggregate_path.open("a", encoding="utf-8", newline="\n") as f:
            for s in hybrid_slices:
                f.write(json.dumps(s, ensure_ascii=False) + "\n")
        write_json(
            paper_dir / "manifest.json",
            {
                "schema_version": "structured_slice_paper_manifest.v1",
                "paper": paper,
                "source_text_path": str(path),
                "local_source_text_copy": str(paper_dir / "source_text.txt"),
                "source_slices_jsonl": str(paper_dir / "source_slices.jsonl"),
                "active_english_slices_jsonl": str(paper_dir / "slices.jsonl"),
                "source_text_sha1": text_hash(text),
                "source_text_chars": len(text),
                "hybrid_selected_method": hybrid_method,
                "hybrid_selection_reason": hybrid_reason,
                "hybrid_metrics": hybrid_metrics,
                "candidate_method_metrics": method_metrics,
                "english_working_copy": english_normalization,
                "slices_jsonl": str(paper_dir / "slices.jsonl"),
            },
        )
        paper_report = {
            "paper": paper,
            "source_text_path": str(path),
            "paper_dir": str(paper_dir),
            "source_text_sha1": text_hash(text),
            "source_text_chars": len(text),
            "methods": method_metrics,
            "best_method_by_score": best_method,
            "hybrid_selected_method": hybrid_method,
            "hybrid_selection_reason": hybrid_reason,
            "hybrid_metrics": hybrid_metrics,
            "english_working_copy": english_normalization,
            "hybrid_slice_count": len(hybrid_slices),
            "llm_profile_count": 0,
        }
        paper_reports.append(paper_report)
        report_index = len(paper_reports) - 1
        if args.real_llm and selected_index in profile_indices:
            profile_jobs.append(
                {
                    "report_index": report_index,
                    "paper": paper,
                    "paper_dir": str(paper_dir),
                    "slices": hybrid_slices,
                    "hybrid_method": hybrid_method,
                }
            )
        print_event("paper_done", paper_id=paper["paper_id"], method=hybrid_method, slices=len(hybrid_slices), profile_queued=selected_index in profile_indices)

    if args.real_llm and profile_jobs:
        workers = max(1, int(args.profile_workers))
        workers = min(workers, len(profile_jobs))
        print_event("profile_start", jobs=len(profile_jobs), workers=workers, model_tier=args.model_tier, selection=args.profile_selection)

        def run_profile_job(job: dict[str, Any]) -> tuple[dict[str, Any], list[dict[str, Any]], dict[str, Any]]:
            profiles, usage = profile_slices_with_llm(
                paper=job["paper"],
                slices=job["slices"],
                prompt_path=Path(args.prompt_path),
                review_objective=review_objective,
                review_framework=review_framework,
                model_tier=args.model_tier,
                max_slices=int(args.max_profile_slices),
                slice_selection=str(args.profile_slice_selection),
            )
            return job, profiles, usage

        with ThreadPoolExecutor(max_workers=workers) as pool:
            future_map = {pool.submit(run_profile_job, job): job for job in profile_jobs}
            for future in as_completed(future_map):
                job = future_map[future]
                paper = job["paper"]
                paper_dir = Path(job["paper_dir"])
                try:
                    job_result, profile_result, profile_usage = future.result()
                    raw_profile = str(profile_usage.pop("_raw_content", "") or "")
                    llm_usage.append(
                        {
                            "paper_id": paper["paper_id"],
                            "method": "text_slice_role_profiler",
                            "selected_base_method": job_result["hybrid_method"],
                            "usage": profile_usage,
                        }
                    )
                    write_json(paper_dir / "llm_slice_profiles.test.json", profile_result)
                    write_text(paper_dir / "llm_slice_profiles.raw.txt", raw_profile)
                    paper_reports[job["report_index"]]["llm_profile_count"] = len(profile_result)
                    profile_done += 1
                    print_event("profile_done", paper_id=paper["paper_id"], profiles=len(profile_result))
                except Exception as exc:
                    err = {"paper_id": paper.get("paper_id"), "error": str(exc)}
                    profile_errors.append(err)
                    write_json(paper_dir / "llm_slice_profiles.error.json", err)
                    print_event("profile_error", **err)

    method_summary = {}
    for method in sorted({row["method"] for row in method_rows}):
        rows = [row for row in method_rows if row["method"] == method]
        method_summary[method] = {
            "papers": len(rows),
            "avg_quality_score": round(statistics.mean(row["quality_score"] for row in rows), 2) if rows else 0,
            "all_reversible": all(row["reversible"] for row in rows),
            "avg_slice_count": round(statistics.mean(row["slice_count"] for row in rows), 1) if rows else 0,
            "avg_median_chars": round(statistics.mean(row["median_chars"] for row in rows), 1) if rows else 0,
            "avg_tiny_slice_count": round(statistics.mean(row["tiny_slice_count"] for row in rows), 1) if rows else 0,
            "avg_huge_slice_count": round(statistics.mean(row["huge_slice_count"] for row in rows), 1) if rows else 0,
            "avg_section_path_rate": round(statistics.mean(row["section_path_rate"] for row in rows), 3) if rows else 0,
        }
    recommended = ""
    if method_summary:
        recommended = max(method_summary.items(), key=lambda kv: (kv[1]["all_reversible"], kv[1]["avg_quality_score"]))[0]
    hybrid_rows = [report.get("hybrid_metrics") for report in paper_reports if isinstance(report.get("hybrid_metrics"), dict)]
    hybrid_summary = {
        "papers": len(hybrid_rows),
        "all_reversible": all(row.get("reversible") for row in hybrid_rows) if hybrid_rows else False,
        "total_slice_count": total_hybrid_slices,
        "avg_slice_count": round(statistics.mean(row.get("slice_count", 0) for row in hybrid_rows), 1) if hybrid_rows else 0,
        "avg_median_chars": round(statistics.mean(row.get("median_chars", 0) for row in hybrid_rows), 1) if hybrid_rows else 0,
        "avg_quality_score": round(statistics.mean(row.get("quality_score", 0) for row in hybrid_rows), 2) if hybrid_rows else 0,
        "method_votes": dict(hybrid_method_votes),
    }
    english_rows = [
        report.get("english_working_copy") or {}
        for report in paper_reports
        if isinstance(report, dict)
    ]
    english_summary = {
        "enabled": bool(args.normalize_non_english),
        "translation_required": sum(int(row.get("translation_required") or 0) for row in english_rows),
        "translated": sum(int(row.get("translated") or 0) for row in english_rows),
        "quarantined": sum(int(row.get("quarantined") or 0) for row in english_rows),
    }
    audit = {
        "schema_version": "structured_slice_run_audit.v2",
        "passed": (
            bool(hybrid_rows)
            and all(row.get("reversible") for row in hybrid_rows)
            and english_summary["quarantined"] == 0
        ),
        "selected_paper_count": len(selected),
        "all_papers": bool(args.all_papers),
        "method_summary": method_summary,
        "best_method_votes": dict(best_method_votes),
        "hybrid_summary": hybrid_summary,
        "english_working_copy": english_summary,
        "recommended_method": recommended,
        "contains_cjk_in_prompt": contains_cjk(read_text(args.prompt_path)),
        "real_llm": bool(args.real_llm),
        "profile_paper_limit": int(args.profile_paper_limit),
        "profiled_paper_count": profile_done,
        "profile_selection": str(args.profile_selection),
        "profile_slice_selection": str(args.profile_slice_selection),
        "profile_workers": int(args.profile_workers),
        "profile_errors": profile_errors,
        "llm_model_tier": args.model_tier,
        "aggregate_slices_jsonl": str(aggregate_path),
    }
    write_json(output_dir / "method_comparison_rows.json", method_rows)
    write_json(output_dir / "paper_reports.json", paper_reports)
    write_json(output_dir / "llm_usage_log.json", llm_usage)
    write_json(output_dir / "quality_audit.json", audit)
    lines = [
        "# StructuredSliceBuilder sample evaluation",
        "",
        f"- Selected papers: {len(selected)}",
        f"- Hybrid slices: {total_hybrid_slices}",
        f"- Hybrid reversible: {audit['passed']}",
        f"- Hybrid method votes: {dict(hybrid_method_votes)}",
        f"- LLM profiled papers: {profile_done}",
        f"- LLM model tier: {args.model_tier if args.real_llm else '-'}",
        "",
        "## Method summary",
    ]
    for method, summary in method_summary.items():
        lines.append(
            f"- {method}: avg_score={summary['avg_quality_score']}, avg_slices={summary['avg_slice_count']}, "
            f"avg_median_chars={summary['avg_median_chars']}, section_rate={summary['avg_section_path_rate']}"
        )
    lines.extend(["", "## Selected papers"])
    for report in paper_reports:
        paper = report.get("paper") or {}
        lines.append(
            f"- {paper.get('record_index')}: {paper.get('title')} | hybrid={report.get('hybrid_selected_method')} | "
            f"slices={report.get('hybrid_slice_count')} | chars={report.get('source_text_chars')}"
        )
    write_text(output_dir / "evaluation_report.md", "\n".join(lines).strip() + "\n")
    print_event("done", passed=audit["passed"], hybrid_slices=total_hybrid_slices, profiled_papers=profile_done, output_dir=str(output_dir))
    return 0 if audit["passed"] else 2


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Evaluate reversible structured text slicing strategies on selected paper full texts.")
    parser.add_argument("--cards-jsonl", default=str(DEFAULT_CARDS_JSONL))
    parser.add_argument("--output-dir", default="")
    parser.add_argument("--all-papers", action=argparse.BooleanOptionalAction, default=False)
    parser.add_argument("--sample-size", type=int, default=3)
    parser.add_argument("--seed", type=int, default=20260702)
    parser.add_argument("--real-llm", action=argparse.BooleanOptionalAction, default=False)
    parser.add_argument("--model-tier", default="standard_model")
    parser.add_argument("--prompt-path", default=str(DEFAULT_PROMPT))
    parser.add_argument("--profile-paper-limit", type=int, default=3)
    parser.add_argument("--max-profile-slices", type=int, default=4)
    parser.add_argument("--profile-workers", type=int, default=4)
    parser.add_argument("--profile-selection", choices=["first", "random", "spread"], default="spread")
    parser.add_argument("--profile-slice-selection", choices=["first", "random", "spread"], default="spread")
    parser.add_argument("--review-framework-json", default=str(DEFAULT_REVIEW_FRAMEWORK_JSON))
    parser.add_argument("--review-objective", default="")
    parser.add_argument(
        "--normalize-non-english",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Preserve source slices while exposing English working text to downstream agents.",
    )
    parser.add_argument("--write-method-samples", action=argparse.BooleanOptionalAction, default=False)
    return parser


def main(argv: list[str] | None = None) -> int:
    os.environ.setdefault("PYTHONUTF8", "1")
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")
    return run(build_parser().parse_args(argv))


if __name__ == "__main__":
    raise SystemExit(main())
