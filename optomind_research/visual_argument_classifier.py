"""Visual argument classification for ReviewKnowledgeBase visual chunks.

This module turns a visual chunk from "what it looks like" into "what it can
argue in a scientific review". It is intentionally reusable: any upstream
pipeline that provides a visual chunk with image path, caption, and nearby text
can call VisualArgumentClassifier.classify_chunk().
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import mimetypes
import random
import re
import shutil
import sqlite3
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from llm.qwen_chat_client import call_qwen_chat
from optomind_research.common_io import clip
from optomind_research.subfigure_extractor import parse_json_response


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_KB_DIR = PROJECT_ROOT / "outputs" / "review_knowledge_base" / "core58-rkb-hqvisual-v1-20260703"
DEFAULT_VISUAL_CHUNKS_JSONL = DEFAULT_KB_DIR / "records" / "visual_chunks.jsonl"
DEFAULT_KB_SQLITE = DEFAULT_KB_DIR / "review_knowledge_base.sqlite"
DEFAULT_PROMPT_PATH = PROJECT_ROOT / "prompts" / "Visual Argument Classifier.txt"
DEFAULT_OUTPUT_ROOT = PROJECT_ROOT / "outputs" / "visual_argument_alignment"

VISUAL_ARGUMENT_TYPES = [
    "mechanism_anchor",
    "taxonomy_or_roadmap",
    "method_or_workflow",
    "quantitative_comparison",
    "trend_or_parameter_map",
    "representative_example",
    "anomaly_or_limitation",
    "synthesis_overview",
]
VISUAL_ARGUMENT_TYPE_SET = set(VISUAL_ARGUMENT_TYPES)
ARGUMENT_BASIS = {"image", "caption", "nearby_text", "body_callout", "existing_tags"}


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def safe_id(value: str) -> str:
    text = re.sub(r"[^A-Za-z0-9._-]+", "-", str(value or "")).strip("-")
    return text[:120] or "item"


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    if not path.exists():
        return rows
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            value = json.loads(line)
            if isinstance(value, dict):
                rows.append(value)
    return rows


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n",
        encoding="utf-8",
    )


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def contains_cjk(value: Any) -> bool:
    return bool(re.search(r"[\u3400-\u9fff]", json.dumps(value, ensure_ascii=False)))


def image_to_data_url(path: Path) -> str:
    mime = mimetypes.guess_type(str(path))[0] or "image/jpeg"
    data = base64.b64encode(path.read_bytes()).decode("ascii")
    return f"data:{mime};base64,{data}"


def prepare_image_for_vlm(image_path: Path, cache_dir: Path, max_side: int = 1200) -> Path:
    """Create a resized RGB JPEG cache image without modifying the original."""

    cache_dir.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha1((str(image_path.resolve()) + str(image_path.stat().st_mtime_ns)).encode("utf-8")).hexdigest()[:16]
    out = cache_dir / f"{safe_id(image_path.stem)}-{digest}.jpg"
    if out.exists():
        return out
    try:
        from PIL import Image

        img = Image.open(image_path).convert("RGB")
        width, height = img.size
        scale = min(1.0, float(max_side) / max(width, height))
        if scale < 1.0:
            img = img.resize((max(1, int(width * scale)), max(1, int(height * scale))), Image.Resampling.LANCZOS)
        img.save(out, format="JPEG", quality=88, optimize=True)
        return out
    except Exception:
        # Fall back to original if PIL cannot process it.
        return image_path


def compact_chunk_payload(chunk: dict[str, Any]) -> dict[str, Any]:
    return {
        "chunk_id": chunk.get("chunk_id", ""),
        "paper_id": chunk.get("paper_id", ""),
        "paper_title": clip(chunk.get("title", ""), 220),
        "parent_label": chunk.get("parent_label", ""),
        "subfigure_label": chunk.get("subfigure_label", ""),
        "caption": clip(chunk.get("caption", ""), 1800),
        "subfigure_caption_focus": clip(chunk.get("subfigure_caption_focus", ""), 1000),
        "nearby_text": clip(chunk.get("nearby_text", ""), 1200),
        "caption_neighbor_text": clip(chunk.get("caption_neighbor_text", ""), 1200),
        "body_callout_texts": [clip(x, 650) for x in (chunk.get("body_callout_texts") or [])[:3]],
        "existing_visual_tags": {
            "visual_role": chunk.get("visual_role", ""),
            "visual_content_type": chunk.get("visual_content_type", ""),
            "review_utility": chunk.get("review_utility", ""),
            "visual_profile": chunk.get("visual_profile", {}),
            "domain_hints": chunk.get("domain_hints", []),
        },
        "crop_quality": {
            "visual_crop_quality": chunk.get("visual_crop_quality", {}),
            "needs_human_review": chunk.get("needs_human_review", False),
            "human_review_status": chunk.get("human_review_status", ""),
        },
    }


def normalize_result(parsed: dict[str, Any], *, fallback_type: str = "representative_example") -> dict[str, Any]:
    typ = str(parsed.get("visual_argument_type") or "").strip()
    if typ not in VISUAL_ARGUMENT_TYPE_SET:
        typ = fallback_type
    secondary_raw = parsed.get("secondary_visual_argument_types") or []
    if not isinstance(secondary_raw, list):
        secondary_raw = []
    secondary: list[str] = []
    for item in secondary_raw:
        item_s = str(item or "").strip()
        if item_s in VISUAL_ARGUMENT_TYPE_SET and item_s != typ and item_s not in secondary:
            secondary.append(item_s)
        if len(secondary) >= 2:
            break
    basis_raw = parsed.get("argument_basis") or []
    if not isinstance(basis_raw, list):
        basis_raw = []
    basis = [str(x) for x in basis_raw if str(x) in ARGUMENT_BASIS]
    if not basis:
        basis = ["caption", "image"]
    try:
        confidence = float(parsed.get("confidence"))
    except Exception:
        confidence = 0.0
    confidence = max(0.0, min(1.0, confidence))
    risk_raw = parsed.get("risk_flags") or []
    if not isinstance(risk_raw, list):
        risk_raw = []
    risk_flags = [clip(x, 60) for x in risk_raw if clip(x, 60)][:4]
    needs_human_review = bool(parsed.get("needs_human_review")) or confidence < 0.55 or bool(
        set(risk_flags) & {"ambiguous_crop", "weak_caption", "axis_unreadable", "partial_context"}
    )
    return {
        "schema_version": "visual_argument_classification.v1",
        "visual_argument_type": typ,
        "secondary_visual_argument_types": secondary,
        "visual_argument_claim": clip(parsed.get("visual_argument_claim", ""), 360),
        "supported_aspect": clip(parsed.get("supported_aspect", ""), 160),
        "argument_basis": basis,
        "confidence": round(confidence, 3),
        "risk_flags": risk_flags,
        "needs_human_review": needs_human_review,
        "rationale": clip(parsed.get("rationale", ""), 260),
        "classification_status": "ok",
        "failure_reason": "",
    }


def failed_result(reason: str, message: str = "") -> dict[str, Any]:
    """Return an isolated failed result that downstream code should not treat as evidence."""

    return {
        "schema_version": "visual_argument_classification.v1",
        "visual_argument_type": "",
        "secondary_visual_argument_types": [],
        "visual_argument_claim": "",
        "supported_aspect": "",
        "argument_basis": [],
        "confidence": 0.0,
        "risk_flags": [clip(reason, 60)],
        "needs_human_review": True,
        "rationale": clip(message or reason, 260),
        "classification_status": "failed",
        "failure_reason": clip(reason, 120),
    }


def apply_consistency_rules(result: dict[str, Any], chunk: dict[str, Any]) -> dict[str, Any]:
    """Conservative deterministic boundary rules after VLM classification."""

    updated = dict(result)
    tag_text = " ".join(
        [
            str(chunk.get("visual_role") or ""),
            str(chunk.get("visual_content_type") or ""),
        ]
    ).lower()
    caption_text = " ".join(
        [
            str(chunk.get("caption") or ""),
            str(chunk.get("subfigure_caption_focus") or ""),
        ]
    ).lower()
    role_text = " ".join(
        [
            str(chunk.get("visual_role") or ""),
            str(chunk.get("visual_content_type") or ""),
            str(chunk.get("caption") or ""),
            str(chunk.get("subfigure_caption_focus") or ""),
            str(chunk.get("nearby_text") or ""),
        ]
    ).lower()
    risk_flags = list(updated.get("risk_flags") or [])

    method_tag_terms = [
        "experimental setup",
        "experimental_setup",
        "measurement setup",
        "methodology_flowchart",
        "characterization setup",
        "workflow",
        "process_flow",
        "fabrication_process",
        "circuit_diagram",
        "apparatus",
    ]
    method_caption_patterns = [
        r"experimental\s+set-?up",
        r"measurement\s+setup",
        r"characterization\s+setup",
        r"test\s+setup",
        r"roll[- ]to[- ]roll\s+(?:production|manufacturing|process|line)",
        r"manufacturing\s+process",
        r"fabrication\s+process",
        r"preparation\s+process",
        r"schematic\s+illustrations?\s+of\s+the\s+processes",
        r"methodology\s+framework",
        r"running\s+the\s+program\s+of",
    ]
    method_hit = any(term in tag_text for term in method_tag_terms) or any(
        re.search(pattern, caption_text) for pattern in method_caption_patterns
    )
    # Do not let generic scientific words such as "process" or "preparation"
    # override a mechanism/example label unless the visual is explicitly a
    # workflow, setup, circuit, or manufacturing/fabrication flow.
    legacy_broad_method_terms = [
        "test setup",
        "setup",
    ]
    if not method_hit and any(term in tag_text for term in legacy_broad_method_terms):
        method_hit = True
    if method_hit and updated.get("visual_argument_type") in {
        "mechanism_anchor",
        "representative_example",
        "taxonomy_or_roadmap",
    }:
        previous = str(updated.get("visual_argument_type") or "")
        updated["secondary_visual_argument_types"] = list(
            dict.fromkeys([previous] + list(updated.get("secondary_visual_argument_types") or []))
        )[:2]
        updated["visual_argument_type"] = "method_or_workflow"
        risk_flags.append("rule_adjusted_method_boundary")
        updated["rationale"] = clip(
            str(updated.get("rationale") or "")
            + " Deterministic boundary rule: apparatus, setup, fabrication, or workflow visuals are classified as method_or_workflow.",
            260,
        )

    taxonomy_terms = [
        "taxonomy",
        "roadmap",
        "classification",
        "classify",
        "classes",
        "categories",
        "families",
        "routes",
        "landscape",
        "timeline",
        "historical evolution",
        "overview of",
        "field map",
    ]
    if updated.get("visual_argument_type") == "taxonomy_or_roadmap" and not any(term in role_text for term in taxonomy_terms):
        if any(term in role_text for term in ["mechanism", "principle", "design strategy", "working principle"]):
            replacement = "mechanism_anchor"
        else:
            replacement = "representative_example"
        updated["secondary_visual_argument_types"] = list(
            dict.fromkeys(["taxonomy_or_roadmap"] + list(updated.get("secondary_visual_argument_types") or []))
        )[:2]
        updated["visual_argument_type"] = replacement
        risk_flags.append("rule_adjusted_taxonomy_boundary")
        updated["rationale"] = clip(
            str(updated.get("rationale") or "")
            + " Deterministic boundary rule: taxonomy_or_roadmap is reserved for multi-route, classification, roadmap, or landscape visuals.",
            260,
        )

    if updated.get("visual_argument_type") == "synthesis_overview":
        synthesis_terms = ["overview", "summary", "review", "roadmap", "landscape", "synthesis", "perspective"]
        if not any(term in role_text for term in synthesis_terms):
            updated["secondary_visual_argument_types"] = list(
                dict.fromkeys(["synthesis_overview"] + list(updated.get("secondary_visual_argument_types") or []))
            )[:2]
            updated["visual_argument_type"] = "representative_example"
            risk_flags.append("rule_adjusted_synthesis_boundary")

    updated["risk_flags"] = list(dict.fromkeys([x for x in risk_flags if x]))[:4]
    if any(str(x).startswith("rule_adjusted") for x in updated["risk_flags"]):
        updated["needs_human_review"] = bool(updated.get("needs_human_review")) or float(updated.get("confidence") or 0) < 0.9
    return updated


@dataclass
class VisualArgumentClassifier:
    prompt_path: Path = DEFAULT_PROMPT_PATH
    model_tier: str = "vision_plus_model"
    output_dir: Path = DEFAULT_OUTPUT_ROOT
    max_tokens: int = 1600
    max_image_side: int = 1200

    def __post_init__(self) -> None:
        self.system_prompt = self.prompt_path.read_text(encoding="utf-8")
        self.image_cache_dir = self.output_dir / "_image_cache"

    def classify_chunk(self, chunk: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
        image_path = Path(str(chunk.get("local_image_path") or ""))
        diagnostics: dict[str, Any] = {
            "chunk_id": chunk.get("chunk_id", ""),
            "local_image_path": str(image_path),
            "image_exists": image_path.exists(),
        }
        if not image_path.exists():
            result = failed_result("missing_image", "The classifier could not access the local image file.")
            diagnostics["status"] = "missing_image"
            return result, diagnostics

        prepared = prepare_image_for_vlm(image_path, self.image_cache_dir, self.max_image_side)
        payload = compact_chunk_payload(chunk)
        messages = [
            {"role": "system", "content": self.system_prompt},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": json.dumps(payload, ensure_ascii=False)},
                    {"type": "image_url", "image_url": {"url": image_to_data_url(prepared)}},
                ],
            },
        ]
        raw = ""
        try:
            result = call_qwen_chat(
                "VisualArgumentClassifierAgent",
                messages,
                model_tier=self.model_tier,
                temperature=0,
                max_tokens=self.max_tokens,
                response_format={"type": "json_object"},
                force_mock=False,
                max_retries=2,
            )
            raw = str(result.get("content") or "")
            parsed = parse_json_response(raw)
            if not parsed:
                raise ValueError("empty_or_invalid_json")
            normalized = apply_consistency_rules(normalize_result(parsed), chunk)
            diagnostics.update(
                {
                    "status": "ok",
                    "prepared_image_path": str(prepared),
                    "raw_chars": len(raw),
                    "parse_ok": True,
                    "cjk_in_result": contains_cjk(normalized),
                    "_llm_usage": result.get("_llm_usage", {}),
                }
            )
            if diagnostics["cjk_in_result"]:
                normalized["risk_flags"] = list(dict.fromkeys(normalized["risk_flags"] + ["non_english_output"]))[:4]
                normalized["needs_human_review"] = True
            return normalized, diagnostics
        except Exception as exc:
            diagnostics.update(
                {
                    "status": "error",
                    "error_type": type(exc).__name__,
                    "error_message": str(exc),
                    "raw_preview": clip(raw, 800),
                }
            )
            result = failed_result("classification_error", f"Classifier failed with {type(exc).__name__}: {exc}")
            return result, diagnostics


def stratified_sample(rows: list[dict[str, Any]], sample_size: int, seed: int = 7) -> list[dict[str, Any]]:
    if sample_size <= 0 or sample_size >= len(rows):
        return list(rows)
    rng = random.Random(seed)
    buckets: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        key = str(row.get("visual_role") or row.get("visual_content_type") or "unknown")
        buckets[key].append(row)
    selected: list[dict[str, Any]] = []
    for key in sorted(buckets):
        bucket = buckets[key]
        rng.shuffle(bucket)
        selected.append(bucket[0])
        if len(selected) >= sample_size:
            return selected
    remaining = [row for row in rows if row not in selected]
    rng.shuffle(remaining)
    selected.extend(remaining[: max(0, sample_size - len(selected))])
    return selected


def classify_rows(
    rows: list[dict[str, Any]],
    *,
    classifier: VisualArgumentClassifier,
    workers: int = 1,
    resume_map: dict[str, dict[str, Any]] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    resume_map = resume_map or {}
    output_rows: list[dict[str, Any] | None] = [None] * len(rows)
    diagnostics_rows: list[dict[str, Any]] = []

    def one(idx: int, row: dict[str, Any]) -> tuple[int, dict[str, Any], dict[str, Any]]:
        chunk_id = str(row.get("chunk_id") or "")
        if chunk_id in resume_map:
            cached = dict(resume_map[chunk_id])
            return idx, cached, {"chunk_id": chunk_id, "status": "resume_cache"}
        result, diagnostics = classifier.classify_chunk(row)
        updated = dict(row)
        updated["visual_argument"] = result
        updated["visual_argument_type"] = result["visual_argument_type"]
        updated["visual_argument_claim"] = result["visual_argument_claim"]
        updated["visual_argument_confidence"] = result["confidence"]
        updated["visual_argument_needs_human_review"] = result["needs_human_review"]
        updated["visual_argument_schema_version"] = result["schema_version"]
        updated["visual_argument_status"] = result.get("classification_status", "ok")
        updated["visual_argument_failure_reason"] = result.get("failure_reason", "")
        updated["visual_argument_model_tier"] = classifier.model_tier
        updated["visual_argument_prompt"] = str(classifier.prompt_path)
        updated["visual_argument_classified_at"] = utc_now()
        return idx, updated, diagnostics

    workers = max(1, int(workers or 1))
    if workers == 1 or len(rows) <= 1:
        for idx, row in enumerate(rows):
            _, updated, diagnostics = one(idx, row)
            output_rows[idx] = updated
            diagnostics_rows.append(diagnostics)
    else:
        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = {executor.submit(one, idx, row): idx for idx, row in enumerate(rows)}
            done = 0
            for future in as_completed(futures):
                idx, updated, diagnostics = future.result()
                output_rows[idx] = updated
                diagnostics_rows.append(diagnostics)
                done += 1
                if done % 25 == 0:
                    print(f"[visual-argument] classified {done}/{len(rows)}")

    return [row for row in output_rows if isinstance(row, dict)], diagnostics_rows


def retry_failed_rows(
    *,
    original_rows: list[dict[str, Any]],
    classified_rows: list[dict[str, Any]],
    diagnostics: list[dict[str, Any]],
    classifier: VisualArgumentClassifier,
    retry_model_tier: str,
    retry_workers: int,
    retry_max_tokens: int,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[str]]:
    """Retry failed rows with a stronger model and replace only successful outputs."""

    failed_ids = {
        str(row.get("chunk_id") or "")
        for row in classified_rows
        if row.get("visual_argument_status") == "failed" or not row.get("visual_argument_type")
    }
    failed_ids.update(str(d.get("chunk_id") or "") for d in diagnostics if d.get("status") not in {"ok", "resume_cache"})
    failed_ids = {x for x in failed_ids if x}
    if not failed_ids:
        return classified_rows, [], []

    retry_input = [row for row in original_rows if str(row.get("chunk_id") or "") in failed_ids]
    if not retry_input:
        return classified_rows, [], sorted(failed_ids)

    retry_classifier = VisualArgumentClassifier(
        prompt_path=classifier.prompt_path,
        model_tier=retry_model_tier,
        output_dir=classifier.output_dir,
        max_tokens=retry_max_tokens,
        max_image_side=classifier.max_image_side,
    )
    retry_rows, retry_diagnostics = classify_rows(
        retry_input,
        classifier=retry_classifier,
        workers=max(1, retry_workers),
    )
    successful_retry = {
        str(row.get("chunk_id") or ""): row
        for row in retry_rows
        if row.get("visual_argument_status") != "failed" and row.get("visual_argument_type")
    }
    merged: list[dict[str, Any]] = []
    unresolved: list[str] = []
    for row in classified_rows:
        chunk_id = str(row.get("chunk_id") or "")
        if chunk_id in successful_retry:
            replacement = dict(successful_retry[chunk_id])
            replacement["visual_argument_retry_of_failed"] = True
            merged.append(replacement)
        else:
            merged.append(row)
            if chunk_id in failed_ids:
                unresolved.append(chunk_id)
    return merged, retry_diagnostics, sorted(set(unresolved))


def high_tier_audit_sample(
    *,
    original_rows: list[dict[str, Any]],
    classified_rows: list[dict[str, Any]],
    classifier: VisualArgumentClassifier,
    audit_model_tier: str,
    audit_sample_size: int,
    audit_workers: int,
    audit_max_tokens: int,
    seed: int,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Reclassify a small sample with a stronger model and compare labels."""

    if audit_sample_size <= 0:
        return [], {"enabled": False}
    ok_rows = [row for row in classified_rows if row.get("visual_argument_type") and row.get("visual_argument_status") != "failed"]
    if not ok_rows:
        return [], {"enabled": True, "sample_size": 0, "agreement_rate": 0.0, "disagreement_count": 0}
    rng = random.Random(seed)
    sample_ids = [str(row.get("chunk_id") or "") for row in ok_rows]
    rng.shuffle(sample_ids)
    sample_ids = sample_ids[: min(audit_sample_size, len(sample_ids))]
    original_by_id = {str(row.get("chunk_id") or ""): row for row in original_rows}
    sampled_original = [original_by_id[cid] for cid in sample_ids if cid in original_by_id]
    audit_classifier = VisualArgumentClassifier(
        prompt_path=classifier.prompt_path,
        model_tier=audit_model_tier,
        output_dir=classifier.output_dir,
        max_tokens=audit_max_tokens,
        max_image_side=classifier.max_image_side,
    )
    audit_rows, audit_diagnostics = classify_rows(
        sampled_original,
        classifier=audit_classifier,
        workers=max(1, audit_workers),
    )
    classified_by_id = {str(row.get("chunk_id") or ""): row for row in classified_rows}
    comparison_rows: list[dict[str, Any]] = []
    for audit_row in audit_rows:
        cid = str(audit_row.get("chunk_id") or "")
        original = classified_by_id.get(cid, {})
        agreement = bool(original.get("visual_argument_type") == audit_row.get("visual_argument_type"))
        comparison_rows.append(
            {
                "chunk_id": cid,
                "original_type": original.get("visual_argument_type", ""),
                "audit_type": audit_row.get("visual_argument_type", ""),
                "agreement": agreement,
                "original_confidence": original.get("visual_argument_confidence", 0.0),
                "audit_confidence": audit_row.get("visual_argument_confidence", 0.0),
                "original_claim": original.get("visual_argument_claim", ""),
                "audit_claim": audit_row.get("visual_argument_claim", ""),
                "audit_status": audit_row.get("visual_argument_status", ""),
            }
        )
    agreement_count = sum(1 for row in comparison_rows if row["agreement"])
    audit_summary = {
        "enabled": True,
        "sample_size": len(comparison_rows),
        "agreement_count": agreement_count,
        "disagreement_count": len(comparison_rows) - agreement_count,
        "agreement_rate": round(agreement_count / len(comparison_rows), 4) if comparison_rows else 0.0,
        "audit_model_tier": audit_model_tier,
        "audit_diagnostic_status_counts": dict(Counter(str(d.get("status") or "") for d in audit_diagnostics)),
    }
    return comparison_rows, audit_summary


def summarize(rows: list[dict[str, Any]], diagnostics: list[dict[str, Any]]) -> dict[str, Any]:
    type_counts = Counter(str(row.get("visual_argument_type") or "") for row in rows if row.get("visual_argument_type"))
    confidence_values = [
        float(row.get("visual_argument_confidence"))
        for row in rows
        if isinstance(row.get("visual_argument_confidence"), (int, float))
    ]
    needs_review_count = sum(1 for row in rows if row.get("visual_argument_needs_human_review"))
    diag_counts = Counter(str(d.get("status") or "") for d in diagnostics)
    max_type_share = max(type_counts.values()) / len(rows) if rows else 0.0
    failed_count = sum(1 for row in rows if row.get("visual_argument_status") == "failed" or not row.get("visual_argument_type"))
    return {
        "row_count": len(rows),
        "type_counts": dict(type_counts),
        "max_type_share": round(max_type_share, 4),
        "avg_confidence": round(sum(confidence_values) / len(confidence_values), 4) if confidence_values else 0.0,
        "low_confidence_count": sum(1 for v in confidence_values if v < 0.55),
        "needs_human_review_count": needs_review_count,
        "failed_count": failed_count,
        "diagnostic_status_counts": dict(diag_counts),
        "cjk_result_count": sum(1 for d in diagnostics if d.get("cjk_in_result")),
    }


def write_audit_markdown(path: Path, rows: list[dict[str, Any]], summary: dict[str, Any], sample_size: int = 50) -> None:
    sample = rows[:sample_size]
    lines = [
        "# Visual Argument Classification Audit",
        "",
        "## Summary",
        "",
        f"- Rows: {summary.get('row_count')}",
        f"- Average confidence: {summary.get('avg_confidence')}",
        f"- Low-confidence count: {summary.get('low_confidence_count')}",
        f"- Needs human review: {summary.get('needs_human_review_count')}",
        f"- Max type share: {summary.get('max_type_share')}",
        "",
        "## Type distribution",
        "",
    ]
    for typ, count in sorted((summary.get("type_counts") or {}).items(), key=lambda x: (-x[1], x[0])):
        lines.append(f"- `{typ}`: {count}")
    lines.extend(["", "## Sample rows", ""])
    for row in sample:
        img = str(row.get("local_image_path") or "")
        lines.extend(
            [
                f"### {row.get('chunk_id')}",
                "",
                f"- Type: `{row.get('visual_argument_type')}`",
                f"- Confidence: `{row.get('visual_argument_confidence')}`",
                f"- Needs review: `{row.get('visual_argument_needs_human_review')}`",
                f"- Claim: {row.get('visual_argument_claim')}",
                f"- Caption: {clip(row.get('caption', ''), 260)}",
                f"- Image: `{img}`",
                "",
            ]
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")


def update_sqlite(sqlite_path: Path, rows: list[dict[str, Any]]) -> None:
    if not sqlite_path.exists():
        return
    conn = sqlite3.connect(str(sqlite_path))
    try:
        existing_cols = {row[1] for row in conn.execute("PRAGMA table_info(visual_chunks)")}
        new_cols = {
            "visual_argument_type": "TEXT",
            "visual_argument_confidence": "REAL",
            "visual_argument_claim": "TEXT",
            "visual_argument_needs_human_review": "INTEGER",
            "visual_argument_schema_version": "TEXT",
            "visual_argument_status": "TEXT",
            "visual_argument_failure_reason": "TEXT",
        }
        for col, ddl in new_cols.items():
            if col not in existing_cols:
                conn.execute(f"ALTER TABLE visual_chunks ADD COLUMN {col} {ddl}")
        for row in rows:
            chunk_id = row.get("chunk_id")
            if not chunk_id:
                continue
            conn.execute(
                """
                UPDATE visual_chunks
                SET visual_argument_type=?,
                    visual_argument_confidence=?,
                    visual_argument_claim=?,
                    visual_argument_needs_human_review=?,
                    visual_argument_schema_version=?,
                    visual_argument_status=?,
                    visual_argument_failure_reason=?,
                    raw_json=?
                WHERE chunk_id=?
                """,
                (
                    row.get("visual_argument_type", ""),
                    float(row.get("visual_argument_confidence") or 0.0),
                    row.get("visual_argument_claim", ""),
                    1 if row.get("visual_argument_needs_human_review") else 0,
                    row.get("visual_argument_schema_version", ""),
                    row.get("visual_argument_status", ""),
                    row.get("visual_argument_failure_reason", ""),
                    json.dumps(row, ensure_ascii=False),
                    chunk_id,
                ),
            )
        conn.commit()
    finally:
        conn.close()


def run(args: argparse.Namespace) -> dict[str, Any]:
    chunks_path = Path(args.visual_chunks_jsonl)
    kb_dir = Path(args.kb_dir)
    sqlite_path = Path(args.sqlite_path) if args.sqlite_path else kb_dir / "review_knowledge_base.sqlite"
    rows = read_jsonl(chunks_path)
    if not rows:
        raise SystemExit(f"No visual chunks found: {chunks_path}")
    run_id = args.run_id or datetime.now().strftime("visual-argument-%Y%m%d-%H%M%S")
    output_dir = Path(args.output_dir) / run_id
    output_dir.mkdir(parents=True, exist_ok=True)

    if args.chunk_ids_file:
        ids = {
            line.strip()
            for line in Path(args.chunk_ids_file).read_text(encoding="utf-8").splitlines()
            if line.strip()
        }
        selected_rows = [row for row in rows if str(row.get("chunk_id") or "") in ids]
    else:
        selected_rows = stratified_sample(rows, int(args.sample_size), seed=int(args.seed)) if args.sample_size else list(rows)
    if args.limit and int(args.limit) > 0:
        selected_rows = selected_rows[: int(args.limit)]

    classifier = VisualArgumentClassifier(
        prompt_path=Path(args.prompt_path),
        model_tier=str(args.model_tier),
        output_dir=output_dir,
        max_tokens=int(args.max_tokens),
        max_image_side=int(args.max_image_side),
    )

    classified, diagnostics = classify_rows(
        selected_rows,
        classifier=classifier,
        workers=int(args.workers),
    )
    retry_diagnostics: list[dict[str, Any]] = []
    unresolved_after_retry: list[str] = []
    if args.auto_retry_errors:
        classified, retry_diagnostics, unresolved_after_retry = retry_failed_rows(
            original_rows=selected_rows,
            classified_rows=classified,
            diagnostics=diagnostics,
            classifier=classifier,
            retry_model_tier=str(args.retry_model_tier),
            retry_workers=int(args.retry_workers),
            retry_max_tokens=int(args.retry_max_tokens),
        )
        if retry_diagnostics:
            write_jsonl(output_dir / "visual_argument_retry_diagnostics.jsonl", retry_diagnostics)
        if unresolved_after_retry:
            (output_dir / "unresolved_failed_chunk_ids.txt").write_text(
                "\n".join(unresolved_after_retry) + "\n",
                encoding="utf-8",
            )

    audit_rows: list[dict[str, Any]] = []
    audit_summary: dict[str, Any] = {"enabled": False}
    if int(args.audit_sample_size or 0) > 0:
        audit_rows, audit_summary = high_tier_audit_sample(
            original_rows=selected_rows,
            classified_rows=classified,
            classifier=classifier,
            audit_model_tier=str(args.audit_model_tier),
            audit_sample_size=int(args.audit_sample_size),
            audit_workers=int(args.audit_workers),
            audit_max_tokens=int(args.audit_max_tokens),
            seed=int(args.seed) + 97,
        )
        write_jsonl(output_dir / "visual_argument_high_tier_audit.jsonl", audit_rows)
        # A high-tier semantic disagreement is not a harmless metric.  Keep
        # both judgements for audit, but prevent the ambiguous visual from
        # silently entering claim support until a human (or a later consensus
        # pass) resolves it.
        disagreement_by_id = {
            str(row.get("chunk_id") or ""): row
            for row in audit_rows
            if not bool(row.get("agreement"))
        }
        for row in classified:
            disagreement = disagreement_by_id.get(str(row.get("chunk_id") or ""))
            if not disagreement:
                continue
            nested = row.get("visual_argument") if isinstance(row.get("visual_argument"), dict) else {}
            flags = list(nested.get("risk_flags") or [])
            flag = f"high_tier_type_disagreement:{disagreement.get('audit_type') or 'unknown'}"
            if flag not in flags:
                flags.append(flag)
            nested["risk_flags"] = flags
            nested["needs_human_review"] = True
            nested["classification_status"] = "needs_review"
            nested["high_tier_audit_type"] = disagreement.get("audit_type") or ""
            nested["high_tier_audit_claim"] = disagreement.get("audit_claim") or ""
            row["visual_argument"] = nested
            row["visual_argument_needs_human_review"] = True
            row["visual_argument_status"] = "needs_review"

    summary = summarize(classified, diagnostics)
    if retry_diagnostics:
        summary["retry"] = {
            "enabled": True,
            "retry_model_tier": str(args.retry_model_tier),
            "retry_attempted": len(retry_diagnostics),
            "retry_status_counts": dict(Counter(str(d.get("status") or "") for d in retry_diagnostics)),
            "unresolved_after_retry": len(unresolved_after_retry),
        }
    else:
        summary["retry"] = {"enabled": bool(args.auto_retry_errors), "retry_attempted": 0, "unresolved_after_retry": 0}
    summary["high_tier_audit"] = audit_summary
    write_jsonl(output_dir / "visual_argument_classified.jsonl", classified)
    write_jsonl(output_dir / "visual_argument_diagnostics.jsonl", diagnostics)
    write_json(output_dir / "summary.json", summary)
    write_audit_markdown(output_dir / "visual_argument_audit.md", classified, summary)

    if args.write_back:
        by_id = {str(row.get("chunk_id")): row for row in classified}
        merged = [by_id.get(str(row.get("chunk_id")), row) for row in rows]
        backup_path = chunks_path.with_suffix(chunks_path.suffix + f".bak-{datetime.now().strftime('%Y%m%d-%H%M%S')}")
        shutil.copy2(chunks_path, backup_path)
        write_jsonl(chunks_path, merged)
        update_sqlite(sqlite_path, merged)
        summary["write_back"] = {
            "visual_chunks_jsonl": str(chunks_path),
            "backup_path": str(backup_path),
            "sqlite_path": str(sqlite_path),
            "updated_rows": len(classified),
        }
        write_json(output_dir / "summary.json", summary)

    return {
        "run_id": run_id,
        "output_dir": str(output_dir),
        "input_rows": len(rows),
        "classified_rows": len(classified),
        "summary": summary,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Classify visual chunks by their review argument role.")
    parser.add_argument("--kb-dir", default=str(DEFAULT_KB_DIR))
    parser.add_argument("--visual-chunks-jsonl", default=str(DEFAULT_VISUAL_CHUNKS_JSONL))
    parser.add_argument("--sqlite-path", default=str(DEFAULT_KB_SQLITE))
    parser.add_argument("--prompt-path", default=str(DEFAULT_PROMPT_PATH))
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_ROOT))
    parser.add_argument("--run-id", default="")
    parser.add_argument("--model-tier", default="vision_plus_model")
    parser.add_argument("--workers", type=int, default=3)
    parser.add_argument("--sample-size", type=int, default=50, help="0 means classify all rows.")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--chunk-ids-file", default="", help="Optional newline-delimited chunk_id list to classify.")
    parser.add_argument("--max-tokens", type=int, default=1600)
    parser.add_argument("--max-image-side", type=int, default=1200)
    parser.add_argument("--auto-retry-errors", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--retry-model-tier", default="vision_premium_model")
    parser.add_argument("--retry-workers", type=int, default=2)
    parser.add_argument("--retry-max-tokens", type=int, default=2400)
    parser.add_argument("--audit-sample-size", type=int, default=0)
    parser.add_argument("--audit-model-tier", default="vision_premium_model")
    parser.add_argument("--audit-workers", type=int, default=2)
    parser.add_argument("--audit-max-tokens", type=int, default=2000)
    parser.add_argument("--write-back", action="store_true")
    return parser


def main() -> None:
    args = build_parser().parse_args()
    result = run(args)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
