"""Build the small local V34 public BODY acceptance staging package.

This is deliberately a file projection/restoration script.  It never calls a
model, gate, ledger, network, or source-control command.  It refuses to write
the package until the native DELIVERY_REPORT says that all 22 BODY units and
the article editor are ready.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple


E = Path(r"F:\OptoMind-Review-2\outputs\e2e_optics_20261012_80cny")
D = E / "preparation" / "downstream_supervision_20261014"
W = E / "worktree" / "outputs" / "optics_e2e" / "09_body_delivery" / "body"
P = E / "worktree" / "outputs" / "optics_e2e" / "10_post_body_v34_parts"
C37 = E / "worktree" / "outputs" / "optics_e2e" / "11_post_body_v37_checker_delivery"
STAGE = D / "public_body_v34_staging_20261015"
REPORT_PATH = W / "DELIVERY_REPORT.json"
C37_REPORT_PATH = C37 / "MANUSCRIPT_DELIVERY_REPORT.json"
UIR_ISSUES_PATH = E / "preparation" / "ab_unattended_20261013" / "RUN_ISSUES_AND_UNATTENDED_REPAIR_BACKLOG_20261014.json"
BINDING_PATH = E / "RUN_BINDING_DOWNSTREAM_V34_COMPLETION_RECOVERY_20261015.json"
PROCESS_PATH = E / "CURRENT_PROCESS_BODY_V34_FULL_BOOK_EDITOR.json"
U1_REPAIR_PATH = D / "ROOT_BODY_V34_CH5_U1_NATIVE_REPAIR_TRIAL.jsonl"
RESUME_DIR = D / "codex_capacity_resume_20261015"
SOURCE_COMMIT = "0b42997aea7f53cef060aa357d125acd455c0d08"
C37_SOURCE_COMMIT = "cef2650c5ad48690e2100ec160603b79e63bdf1e"
BINDING_SHA = "d69ba6ce7c6fc854b97e306b20f8fa514bdea2fb6119f46c96c047c028da79a1"
PRIOR_PUSHED_STAGE = "661201fb89a5ccb233412aef4ab8d10c4808495a"

UNITS: List[Tuple[str, str]] = [
    ("CH1", "U1"),
    ("CH1", "U2"),
    ("CH1", "U3"),
    ("CH1", "U4"),
    ("CH1", "U5"),
    ("CH2", "CH2_U01"),
    ("CH2", "CH2_U02"),
    ("CH2", "CH2_U03"),
    ("CH2", "CH2_U04"),
    ("CH2", "CH2_U05"),
    ("CH3", "CH3_U01"),
    ("CH3", "CH3_U02"),
    ("CH3", "CH3_U03"),
    ("CH3", "CH3_U04"),
    ("CH4", "CH4-U1"),
    ("CH4", "CH4-U2"),
    ("CH4", "CH4-U3"),
    ("CH4", "CH4-U4"),
    ("CH5", "U1_OPM_SNR_Tradeoff"),
    ("CH5", "U2_MDL_vs_Metalens"),
    ("CH5", "U3_Hybrid_Large_Aperture"),
    ("CH5", "U4_Application_Verdict"),
]


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=False) + "\n",
        encoding="utf-8",
        newline="\n",
    )


def inside(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def logical(path: Path) -> str:
    path = path.resolve()
    for root, alias in ((STAGE, "<STAGE>"), (C37, "<C37>"), (W, "<BODY_W>"), (D, "<D>"), (E, "<E>")):
        if inside(path, root):
            return f"{alias}/{path.relative_to(root.resolve()).as_posix()}"
    return str(path)


def pointer(path: Path) -> Dict[str, Any]:
    path = path.resolve()
    if not path.exists() or not path.is_file():
        return {"logical_path": logical(path), "exists": False}
    return {
        "logical_path": logical(path),
        "sha256": sha256_file(path),
        "bytes": path.stat().st_size,
    }


def source_record(stage_path: Path, source_path: Path, kind: str, **extra: Any) -> Dict[str, Any]:
    rec: Dict[str, Any] = {
        "stage_path": stage_path.relative_to(STAGE).as_posix(),
        "source": pointer(source_path),
        "kind": kind,
        "source_commit": SOURCE_COMMIT,
    }
    rec.update(extra)
    return rec


def copy_file(source: Path, target: Path, records: List[Dict[str, Any]], kind: str, **extra: Any) -> None:
    if not source.exists() or not source.is_file():
        raise FileNotFoundError(str(source))
    if target.resolve() == source.resolve():
        raise RuntimeError(f"Refusing in-place staging copy: {source}")
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, target)
    records.append(source_record(target, source, kind, **extra))


def add_json(value: Any, target: Path, records: List[Dict[str, Any]], kind: str, source: Optional[Path] = None, **extra: Any) -> None:
    write_json(target, value)
    records.append(
        source_record(target, source if source else target, kind, **extra)
    )


def clean_for_public(value: Any) -> Any:
    """Keep projections small and remove accidental full payload fields."""
    if isinstance(value, dict):
        out: Dict[str, Any] = {}
        for k, v in value.items():
            if k in {"reasoning_content", "raw_response", "normalized", "normalized_response"}:
                continue
            out[k] = clean_for_public(v)
        return out
    if isinstance(value, list):
        return [clean_for_public(v) for v in value]
    return value


def source_handles(pack: Dict[str, Any]) -> List[str]:
    return [str(x.get("source_handle")) for x in pack.get("sources", []) if x.get("source_handle")]


def material_summary_projection(unit_input: Dict[str, Any]) -> Dict[str, Any]:
    summary = unit_input.get("material_summary") or {}
    keys = [
        "sources",
        "with_study_summary_A",
        "with_review_planning_B",
        "with_deep_read_material",
        "with_supplement_material",
        "with_supplement_materials",
        "with_local_passages",
        "with_tool_supplement_materials",
        "chapter_tool_materials",
        "read_from_disk",
        "capacity_threshold_exceeded",
        "truncated",
    ]
    return {k: clean_for_public(summary.get(k)) for k in keys if k in summary}


def effective_request(profile: Dict[str, Any]) -> Dict[str, Any]:
    answer = profile.get("max_output_tokens")
    thinking = profile.get("thinking_budget")
    total = answer + thinking if isinstance(answer, int) and isinstance(thinking, int) else None
    return {
        "model": profile.get("model"),
        "answer_tokens": answer,
        "enable_thinking": profile.get("thinking"),
        "thinking_budget": thinking,
        "max_completion_tokens": total,
        "max_tokens": None,
        "stream": profile.get("stream"),
        "stream_options": {"include_usage": True},
        "stream_inactivity_timeout_seconds": profile.get("timeout_seconds"),
        "stream_overall_timeout_seconds": profile.get("stream_overall_timeout_seconds"),
        "total_output_tokens": total,
    }


def choose_sse(attempt: Path) -> Optional[Path]:
    files = sorted((attempt / "transport").glob("*.sse.raw"), key=lambda p: p.stat().st_mtime)
    return files[-1] if files else None


def find_unit_root(start: Path) -> Path:
    """Find the attempt-hash folder carrying UNIT_PACK.json."""
    candidate = start.resolve()
    for parent in (candidate, *candidate.parents):
        if not inside(parent, W):
            continue
        if (parent / "UNIT_PACK.json").is_file():
            return parent
    raise FileNotFoundError(f"UNIT_PACK.json ancestor not found under BODY_W: {start}")


def make_model_projection(
    entry: Dict[str, Any],
    chapter_id: str,
    unit_id: str,
    attempt: Path,
    unit_root: Path,
    quality_dir: Path,
    target: Path,
    records: List[Dict[str, Any]],
    source_attempt: Optional[Path] = None,
) -> Dict[str, Any]:
    # A format-recovery derivative is the selected author body, while the
    # native model request/response remains in format_recovery.original_attempt.
    model_attempt = source_attempt or attempt
    raw_path = model_attempt / "RAW_RESPONSE.json"
    request_path = model_attempt / "REQUEST.json"
    input_path = model_attempt / "UNIT_INPUT.json"
    messages_path = model_attempt / "UNIT_MESSAGES.json"
    pack_path = unit_root / "UNIT_PACK.json"
    result_path = model_attempt / "UNIT_RESULT.json"
    for required in (raw_path, request_path, input_path, messages_path, pack_path, result_path):
        if not required.exists():
            raise FileNotFoundError(str(required))
    raw = read_json(raw_path)
    request = read_json(request_path)
    unit_input = read_json(input_path)
    pack = read_json(pack_path)
    result = read_json(result_path)
    profile = request.get("profile") or {}
    content = raw.get("content")
    if content is None:
        content = result.get("body_markdown")
    elapsed = raw.get("elapsed_seconds")
    if elapsed is None:
        elapsed = result.get("elapsed_seconds")
    sse_path = choose_sse(model_attempt)
    quality_identity = (entry.get("quality") or {}).get("identity") or {}
    generation_class = "cached" if entry.get("cache_hit") else "new_generation"
    projection = {
        "schema": "optomind.public_body_v34_model_response_projection.v1",
        "chapter_id": chapter_id,
        "unit_id": unit_id,
        "attempt_dir": logical(attempt),
        "model_source_attempt_dir": logical(model_attempt),
        "attempt": attempt.name,
        "status": entry.get("status"),
        "cache_hit": bool(entry.get("cache_hit")),
        "generation_class": generation_class,
        "recovery_class": "format_recovery" if entry.get("format_recovery") else None,
        "format_recovery": clean_for_public(entry.get("format_recovery") or {}) if entry.get("format_recovery") else None,
        "content": content,
        "content_sha256": sha256_bytes(str(content or "").encode("utf-8")),
        "usage": clean_for_public(raw.get("usage") or result.get("usage")),
        "effective_request": effective_request(profile),
        "elapsed_seconds": elapsed,
        "elapsed_source": "native_author_raw_or_unit_result; null_when_not_recorded",
        "returned_model": raw.get("returned_model") or result.get("model"),
        "request_id": raw.get("request_id"),
        "finish_reason": raw.get("finish_reason") or result.get("finish_reason"),
        "complete": raw.get("complete", result.get("complete")),
        "quality_status": (entry.get("quality") or {}).get("status"),
        "quality_identity": clean_for_public(quality_identity),
        "request_projection": {
            "projection_only": True,
            "request_file": pointer(request_path),
            "unit_input_file": pointer(input_path),
            "unit_pack_file": pointer(pack_path),
            "unit_messages_pointer": pointer(messages_path),
            "identity": {
                "chapter_id": pack.get("chapter_id", chapter_id),
                "unit_id": pack.get("unit_id", unit_id),
                "language": pack.get("language"),
                "source_handles": source_handles(pack),
            },
            "duty_parameters": {
                "unit_focus": pack.get("unit_focus"),
                "paragraph_tasks": clean_for_public(pack.get("paragraph_tasks", [])),
                "table_tasks": clean_for_public(pack.get("table_tasks", [])),
                "owner_fields_present": sorted((pack.get("owner_unit_context") or {}).keys()),
                "owner_field_value_names": sorted((pack.get("owner_unit_context") or {}).get("owner_field_values", {}).keys()),
            },
            "writer_profile": clean_for_public(profile),
            "estimate_projection": clean_for_public(unit_input.get("estimate") or {}),
            "material_summary_projection": material_summary_projection(unit_input),
        },
        "raw_pointer": {
            "response_file": pointer(raw_path),
            "sse": pointer(sse_path) if sse_path else {"exists": False},
            "response_payload_sha256": raw.get("raw_response_sha256"),
            "sse_payload_sha256": raw.get("raw_stream_sha256"),
        },
        "quality_body_source": pointer(quality_dir / "QUALITY_BODY.md"),
        "excluded_from_copy": [
            "full RAW_RESPONSE.json",
            "full SSE/raw stream",
            "reasoning_content",
            "UNIT_MESSAGES body",
            "full UNIT_INPUT materials",
            "full UNIT_PACK sources",
        ],
    }
    add_json(projection, target, records, "model_response_projection", source=raw_path)
    return projection


def unit_entry(report_entry: Dict[str, Any], chapter_id: str, unit_id: str, records: List[Dict[str, Any]]) -> Dict[str, Any]:
    attempt_raw = report_entry.get("attempt_dir")
    if not attempt_raw:
        raise RuntimeError(f"{chapter_id}:{unit_id} has no attempt_dir after readiness")
    attempt = Path(attempt_raw)
    if not inside(attempt, W) or not attempt.exists():
        raise RuntimeError(f"attempt outside BODY_W or missing: {attempt}")
    # The report may point to a format-recovery derivative.  Keep that selected
    # body, but source model request/response metadata from the recorded native
    # attempt and locate UNIT_PACK by ancestor rather than fixed depth.
    recovery = report_entry.get("format_recovery") or {}
    source_attempt = Path(recovery.get("original_attempt", "")) if recovery.get("original_attempt") else attempt
    if not inside(source_attempt, W) or not source_attempt.exists():
        source_attempt = attempt
    unit_root = find_unit_root(source_attempt)
    quality_dir = Path((report_entry.get("quality") or {}).get("output_dir", ""))
    if not quality_dir.exists() or not inside(quality_dir, W):
        raise RuntimeError(f"quality output outside BODY_W or missing: {quality_dir}")
    author = attempt / "UNIT_BODY.md"
    if not author.exists():
        author = attempt / "COMPLETED_BODY.md"
    quality = quality_dir / "QUALITY_BODY.md"
    if not author.exists() or not quality.exists():
        raise RuntimeError(f"missing public BODY files for {chapter_id}:{unit_id}")
    unit_stage = STAGE / "units" / chapter_id / unit_id
    copy_file(author, unit_stage / "ORIGINAL_AUTHOR_BODY.md", records, "unit_author_body")
    copy_file(quality, unit_stage / "QUALITY_BODY.md", records, "unit_quality_body")
    make_model_projection(
        report_entry, chapter_id, unit_id, attempt, unit_root, quality_dir,
        unit_stage / "MODEL_RESPONSE.json", records,
        source_attempt=source_attempt,
    )
    return {
        "chapter_id": chapter_id,
        "unit_id": unit_id,
        "status": report_entry.get("status"),
        "cache_hit": bool(report_entry.get("cache_hit")),
        "generation_class": "cached" if report_entry.get("cache_hit") else "new_generation",
        "recovery_class": "format_recovery" if recovery else None,
        "attempt": logical(attempt),
        "model_source_attempt": logical(source_attempt),
        "quality_output": logical(quality_dir),
        "public_files": {
            "original_author_body": f"units/{chapter_id}/{unit_id}/ORIGINAL_AUTHOR_BODY.md",
            "quality_body": f"units/{chapter_id}/{unit_id}/QUALITY_BODY.md",
            "model_response": f"units/{chapter_id}/{unit_id}/MODEL_RESPONSE.json",
        },
    }


def projection_pointer_list(paths: Iterable[Path]) -> List[Dict[str, Any]]:
    return [pointer(p) for p in paths if p.exists() and p.is_file()]


def stage_assembled(report: Dict[str, Any], records: List[Dict[str, Any]]) -> Dict[str, Any]:
    selected = report.get("selected_body") or {}
    numbering = selected.get("numbering") or {}
    source_map = {
        "native_assembled_review_draft": W / "assembled" / "REVIEW_DRAFT.md",
        "actual_selected_handles": Path(selected.get("handles_draft", "")),
        "actual_selected_reader": Path(selected.get("reader_draft", "")),
        "actual_selected_references": Path(selected.get("references_path", "")),
        "actual_selected_figure_citation_map": Path(numbering.get("mapping_path", "")),
    }
    public_files: Dict[str, str] = {}
    for name, source in source_map.items():
        if not source.exists():
            raise FileNotFoundError(str(source))
        suffix = source.suffix
        target_name = {
            "native_assembled_review_draft": "ORIGINAL_ASSEMBLED_REVIEW_DRAFT.md",
            "actual_selected_handles": "ACTUAL_SELECTED_BODY_HANDLES.md",
            "actual_selected_reader": "ACTUAL_SELECTED_BODY_READER.md",
            "actual_selected_references": "REFERENCES.json",
            "actual_selected_figure_citation_map": "FIGURE_CITATION_MAP.json",
        }[name]
        target = STAGE / "assembled" / target_name
        copy_file(source, target, records, "assembled_public_artifact")
        public_files[name] = target.relative_to(STAGE).as_posix()
    numbering_projection = clean_for_public(numbering)
    for key in ("handles_draft", "reader_draft", "references_path", "mapping_path"):
        value = numbering.get(key)
        if value:
            numbering_projection[key] = pointer(Path(value))
    numbering_projection.pop("assets_dir", None)
    selected_projection = {
        "schema": "optomind.public_body_v34_selected_body_projection.v1",
        "status": selected.get("status"),
        "source": selected.get("source"),
        "selected_body": {
            "handles_draft": pointer(Path(selected["handles_draft"])) if selected.get("handles_draft") else None,
            "reader_draft": pointer(Path(selected["reader_draft"])) if selected.get("reader_draft") else None,
            "references_path": pointer(Path(selected["references_path"])) if selected.get("references_path") else None,
            "numbering": numbering_projection,
        },
        "note": "Selected body is copied from the ready native DELIVERY_REPORT selection; no preview artifact is used.",
    }
    add_json(selected_projection, STAGE / "assembled" / "SELECTED_BODY.projection.json", records, "selected_body_projection", source=REPORT_PATH)
    assembly = report.get("assembly") or {}
    execution_projection = {
        "schema": "optomind.public_body_v34_assembly_execution_projection.v1",
        "report": {
            "status": report.get("status"),
            "generation_complete": report.get("generation_complete"),
            "assembly_complete": report.get("assembly_complete"),
            "body_delivery": clean_for_public(report.get("body_delivery") or {}),
        },
        "assembly_summary": pointer(W / "assembled" / "ASSEMBLY_SUMMARY.json"),
        "assembly_run_report": pointer(W / "assembled" / "RUN_REPORT.md"),
        "assembly_fields": {
            k: clean_for_public(assembly.get(k))
            for k in ["status", "planning_status", "arrangement_status", "loaded_units", "missing_units", "errors", "editorial_repairs", "table_count"]
            if k in assembly
        },
    }
    add_json(execution_projection, STAGE / "assembled" / "ASSEMBLY_EXECUTION.projection.json", records, "assembly_execution_projection", source=REPORT_PATH)
    return {"public_files": public_files, "selected_body": selected_projection, "execution": execution_projection}


def stage_article_editor(report: Dict[str, Any], records: List[Dict[str, Any]]) -> Dict[str, Any]:
    root = W / "article_edit"
    attempts = [p for p in root.iterdir() if p.is_dir()] if root.exists() else []
    raw_candidates: List[Path] = []
    for attempt in attempts:
        for pattern in ("full/RAW_RESPONSE.json", "full/RESPONSE.json", "RAW_RESPONSE.json", "RESPONSE.json"):
            candidate = attempt / pattern
            if candidate.exists():
                raw_candidates.append(candidate)
    raw_candidates.sort(key=lambda p: p.stat().st_mtime)
    if not raw_candidates:
        pending = {"schema": "optomind.public_body_v34_article_editor_projection.v1", "status": "pending_native_response", "source_dir": logical(root)}
        add_json(pending, STAGE / "article_edit" / "ARTICLE_EDITOR.projection.json", records, "article_editor_pending", source=REPORT_PATH)
        return pending
    raw_path = raw_candidates[-1]
    raw = read_json(raw_path)
    attempt_root = raw_path.parent.parent if raw_path.parent.name == "full" else raw_path.parent
    profile_path = attempt_root / "full" / "PROFILE.json"
    estimate_path = attempt_root / "full" / "ESTIMATE.json"
    identity_path = attempt_root / "EDIT_IDENTITY.json"
    profile = read_json(profile_path) if profile_path.exists() else {}
    effective = effective_request(profile)
    projection = {
        "schema": "optomind.public_body_v34_article_editor_projection.v1",
        "status": "response_present",
        "attempt": logical(attempt_root),
        "content": raw.get("content"),
        "content_sha256": sha256_bytes(str(raw.get("content") or "").encode("utf-8")),
        "usage": clean_for_public(raw.get("usage")),
        "effective_request": effective,
        "elapsed_seconds": raw.get("elapsed_seconds"),
        "returned_model": raw.get("returned_model"),
        "request_id": raw.get("request_id"),
        "finish_reason": raw.get("finish_reason"),
        "complete": raw.get("complete"),
        "raw_pointer": pointer(raw_path),
        "execution_record": {
            "edit_identity": pointer(identity_path),
            "profile": pointer(profile_path),
            "estimate": pointer(estimate_path),
            "delivery_report_article_edit": clean_for_public(report.get("article_edit") or {}),
        },
        "excluded_from_copy": ["full editor RAW_RESPONSE payload", "editor SSE/raw stream", "editor full MESSAGES"],
    }
    add_json(projection, STAGE / "article_edit" / "ARTICLE_EDITOR.projection.json", records, "article_editor_projection", source=raw_path)
    return projection


PART_NAMES = ("conception", "conclusion", "introduction", "abstract")


def stage_parts(report: Dict[str, Any], records: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Project the four paid post-BODY part calls without copying messages/context."""
    live_path = P / "LIVE_REPORT.json"
    attempt = P / "attempts" / "attempt_001"
    if not live_path.exists() or not attempt.exists():
        raise FileNotFoundError(f"post-BODY parts output missing: {P}")
    live = read_json(live_path)
    runtime = live.get("runtime_report") or {}
    if live.get("status") != "generated" or live.get("model_calls") != 4 or live.get("external_requests") != 4:
        raise RuntimeError("post-BODY parts LIVE_REPORT is not the four-call generated result")
    if runtime.get("body_preserved") is not True:
        raise RuntimeError("post-BODY parts does not prove BODY preservation")
    final_path = Path(live.get("final_manuscript", ""))
    if not inside(final_path, P) or not final_path.exists():
        raise RuntimeError(f"post-BODY final manuscript missing or outside parts output: {final_path}")
    front_path = attempt / "FRONT_BACK_REPORT.json"
    generated_path = attempt / "GENERATED_PARTS.json"
    if not front_path.exists() or not generated_path.exists():
        raise FileNotFoundError("post-BODY execution reports missing")
    front = read_json(front_path)
    if front.get("body_preserved") is not True or front.get("body_sha256") != runtime.get("body_sha256"):
        raise RuntimeError("front/back report BODY preservation/hash evidence mismatch")

    parts_stage = STAGE / "parts"
    copy_file(final_path, parts_stage / "MANUSCRIPT_FINAL.md", records, "post_body_generated_manuscript")
    copy_file(generated_path, parts_stage / "GENERATED_PARTS.json", records, "post_body_generated_parts")

    stage_entries: List[Dict[str, Any]] = []
    wire_request_sha = (live.get("estimate") or {}).get("wire_request_sha256")
    for part in PART_NAMES:
        cache = P / "cache" / part
        raw_path = cache / "RAW_RESPONSE.json"
        request_path = cache / "REQUEST.json"
        profile_path = cache / "PROFILE.json"
        estimate_path = cache / "ESTIMATE.json"
        messages_path = cache / "MESSAGES.json"
        sse_files = sorted((cache / "transport").glob("*.sse.raw"))
        sse_path = sse_files[-1] if sse_files else None
        for required in (raw_path, request_path, profile_path, estimate_path, messages_path):
            if not required.exists():
                raise FileNotFoundError(str(required))
        raw = read_json(raw_path)
        request = read_json(request_path)
        profile = read_json(profile_path)
        estimate = read_json(estimate_path)
        projection = {
            "schema": "optomind.public_body_v34_part_response_projection.v1",
            "part": part,
            "generation_class": "new_generation",
            "free_recovery": False,
            "content": raw.get("content"),
            "content_sha256": sha256_bytes(str(raw.get("content") or "").encode("utf-8")),
            "usage": clean_for_public(raw.get("usage") or {}),
            "effective_request": clean_for_public(raw.get("effective_request") or effective_request(profile)),
            "elapsed_seconds": raw.get("elapsed_seconds"),
            "returned_model": raw.get("returned_model"),
            "request_id": raw.get("request_id"),
            "finish_reason": raw.get("finish_reason"),
            "complete": raw.get("complete"),
            "request_projection": {
                "part": part,
                "request_file": pointer(request_path),
                "profile_file": pointer(profile_path),
                "estimate_file": pointer(estimate_path),
                "messages_pointer": pointer(messages_path),
                "messages_sha256": request.get("messages_sha256"),
                "wire_request_sha256": wire_request_sha,
                "profile": clean_for_public(request.get("profile") or profile),
                "estimate_projection": clean_for_public(estimate),
            },
            "raw_pointer": {
                "response_file": pointer(raw_path),
                "sse": pointer(sse_path) if sse_path else {"exists": False},
                "response_payload_sha256": raw.get("raw_response_sha256"),
                "sse_payload_sha256": raw.get("raw_stream_sha256"),
            },
            "excluded_from_copy": [
                "full RAW_RESPONSE wrapper/reasoning",
                "full MESSAGES/context materials",
                "raw SSE stream",
                "CONTEXT.json and INPUT.json",
            ],
        }
        add_json(projection, parts_stage / "responses" / f"{part}.projection.json", records, "post_body_part_response_projection", source=raw_path)
        stage_entries.append({
            "part": part,
            "generation_class": "new_generation",
            "free_recovery": False,
            "projection": f"parts/responses/{part}.projection.json",
            "raw_pointer": pointer(raw_path),
            "messages_sha256": request.get("messages_sha256"),
            "effective_request": clean_for_public(raw.get("effective_request") or effective_request(profile)),
            "usage": clean_for_public(raw.get("usage") or {}),
            "elapsed_seconds": raw.get("elapsed_seconds"),
        })

    selected = report.get("selected_body") or {}
    body_handles = Path(selected.get("handles_draft", "")) if selected.get("handles_draft") else Path("")
    body_reader = Path(selected.get("reader_draft", "")) if selected.get("reader_draft") else Path("")
    ref_path = D / "ROOT_REFERENCE_COVERAGE_90_REVIEW.json"
    ref = read_json(ref_path) if ref_path.exists() else {}
    execution = {
        "schema": "optomind.public_body_v34_parts_execution_projection.v1",
        "status": live.get("status"),
        "parts": stage_entries,
        "model_calls": live.get("model_calls"),
        "external_requests": live.get("external_requests"),
        "generation_class": "new_generation",
        "free_recovery": False,
        "cost": {
            "actual_cny": 0.4568144,
            "live_report_project_actual_delta_cny": live.get("project_actual_delta_cny"),
            "estimated_max_cny": (live.get("estimate") or {}).get("estimated_max_cost_cny"),
            "held_delta_cny": live.get("project_held_delta_cny"),
            "source": pointer(live_path),
        },
        "final_manuscript": pointer(final_path),
        "front_back_report": pointer(front_path),
        "generated_parts": pointer(generated_path),
        "body_lineage": {
            "body_preserved": True,
            "body_sha256": runtime.get("body_sha256"),
            "source_draft": pointer(Path(front.get("source_draft", ""))) if front.get("source_draft") else None,
            "selected_body_handles": pointer(body_handles) if body_handles else None,
            "selected_body_reader": pointer(body_reader) if body_reader else None,
            "selected_body_source": selected.get("source"),
            "selected_body_status": selected.get("status"),
            "no_second_body_rewrite": True,
        },
        "identity_summary": {
            "book_identities": ref.get("book_identities"),
            "body_cited_identities": ref.get("body_cited_identities"),
            "book_supplied_uncited": ref.get("book_supplied_uncited"),
            "selected_body_source": ref.get("selected_body_source"),
            "selected_body_sha256": ref.get("selected_body_sha256"),
        },
        "free_recovery_reference": pointer(RESUME_DIR / "FREE_RESUME_PROOF.json"),
        "excluded_from_copy": ["P/CONTEXT.json", "P/CONCEPTION_MESSAGES.json", "P/attempts/*/INPUT.json", "P/*/MESSAGES.json bodies"],
    }
    add_json(execution, parts_stage / "PARTS_EXECUTION.projection.json", records, "post_body_parts_execution_projection", source=live_path)
    return {
        "status": live.get("status"),
        "model_calls": live.get("model_calls"),
        "external_requests": live.get("external_requests"),
        "actual_cost_cny": 0.4568144,
        "estimated_max_cost_cny": (live.get("estimate") or {}).get("estimated_max_cost_cny"),
        "generated_parts": list(PART_NAMES),
        "generation_class": "new_generation",
        "free_recovery": False,
        "body_preserved": True,
        "body_sha256": runtime.get("body_sha256"),
        "final_manuscript": pointer(final_path),
        "execution_projection": "parts/PARTS_EXECUTION.projection.json",
    }


def stage_c37_checker_publication(records: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Add the saved-response C37 checker and final publication projections.

    C37 is a free recovery of two already-paid checker responses.  Keep the
    provider content, usage, and effective/request parameter projections, but
    never copy the full provider envelope, SSE, messages, or context material.
    """
    if not C37.exists() or not C37_REPORT_PATH.exists():
        raise FileNotFoundError(f"C37 checker/publication output missing: {C37}")
    checker_root = C37 / "03_manuscript_checker"
    identity_path = checker_root / "CHECKER_IDENTITY.json"
    checker_report_path = checker_root / "CHECKER_REPORT.json"
    seal_path = checker_root / "CHECKER_RESULT_SEAL.json"
    estimate_path = C37 / "CHECKER_ESTIMATE.json"
    for required in (identity_path, checker_report_path, seal_path, estimate_path):
        if not required.exists():
            raise FileNotFoundError(str(required))

    checker_stage = STAGE / "c37_checker"
    for source in (identity_path, checker_report_path, seal_path, estimate_path):
        copy_file(
            source,
            checker_stage / source.name,
            records,
            "c37_checker_report",
            source_commit=C37_SOURCE_COMMIT,
        )

    identity = read_json(identity_path)
    checker_report = read_json(checker_report_path)
    round_records: List[Dict[str, Any]] = []
    for round_number in (1, 2):
        round_name = f"round_{round_number}"
        round_root = checker_root / round_name
        raw_path = round_root / "RAW_RESPONSE.json"
        request_path = round_root / "REQUEST.json"
        profile_path = round_root / "PROFILE.json"
        round_estimate_path = round_root / "ESTIMATE.json"
        messages_path = round_root / "MESSAGES.json"
        for required in (raw_path, request_path, profile_path, round_estimate_path, messages_path):
            if not required.exists():
                raise FileNotFoundError(str(required))
        raw = read_json(raw_path)
        request = read_json(request_path)
        profile = read_json(profile_path)
        estimate = read_json(round_estimate_path)
        content = raw.get("content")
        response_target = checker_stage / round_name / "MODEL_RESPONSE.projection.json"
        sse_files = sorted((round_root / "transport").glob("*.sse.raw"))
        response_projection = {
            "schema": "optomind.public_body_v37_checker_response_projection.v1",
            "round": round_number,
            "recovery_class": "free_saved_response_recovery",
            "new_model_calls": 0,
            "new_external_requests": 0,
            "response_source": "saved_provider_response",
            "call_id": raw.get("call_id"),
            "request_id": raw.get("request_id"),
            "requested_model": raw.get("requested_model"),
            "returned_model": raw.get("returned_model"),
            "content": content,
            "content_sha256": sha256_bytes(str(content or "").encode("utf-8")),
            "usage": clean_for_public(raw.get("usage") or {}),
            "effective_request": clean_for_public(raw.get("effective_request") or {}),
            "elapsed_seconds": raw.get("elapsed_seconds"),
            "complete": raw.get("complete"),
            "finish_reason": raw.get("finish_reason"),
            "status_code": raw.get("status_code"),
            "request_projection": {
                "task": "manuscript_checker",
                "identity": {
                    "source_path": identity.get("source_path"),
                    "source_sha256": identity.get("source_sha256"),
                    "first_messages_sha256": identity.get("first_messages_sha256"),
                    "response_source": identity.get("response_source"),
                },
                "messages_sha256": request.get("messages_sha256"),
                "parameters": clean_for_public(profile),
                "estimate": clean_for_public(estimate),
                "request_file": pointer(request_path),
                "profile_file": pointer(profile_path),
                "estimate_file": pointer(round_estimate_path),
                "messages_pointer": pointer(messages_path),
            },
            "raw_pointer": {
                "response_file": pointer(raw_path),
                "sse": pointer(sse_files[-1]) if sse_files else {"exists": False},
                "response_payload_sha256": raw.get("raw_response_sha256"),
                "sse_payload_sha256": raw.get("raw_stream_sha256"),
                "embedded_sse_path": raw.get("raw_stream_path"),
            },
            "excluded_from_copy": [
                "full RAW_RESPONSE.json envelope",
                "raw SSE stream body",
                "reasoning_content",
                "normalized_response copies",
                "MESSAGES.json body and checker context",
            ],
        }
        add_json(
            response_projection,
            response_target,
            records,
            "c37_checker_response_projection",
            source=raw_path,
            source_commit=C37_SOURCE_COMMIT,
        )

        format_source = round_root / "FORMAT_RECOVERY.json"
        format_target = checker_stage / round_name / "FORMAT_RECOVERY.json"
        if format_source.exists():
            copy_file(
                format_source,
                format_target,
                records,
                "c37_checker_format_recovery",
                source_commit=C37_SOURCE_COMMIT,
            )
            format_record: Dict[str, Any] = {
                "status": "copied",
                "file": format_target.relative_to(STAGE).as_posix(),
                "source": pointer(format_source),
            }
        else:
            format_record = {"status": "not_present", "source": pointer(format_source)}
        round_records.append(
            {
                "round": round_number,
                "projection": response_target.relative_to(STAGE).as_posix(),
                "format_recovery": format_record,
            }
        )

    execution_projection = {
        "schema": "optomind.public_body_v37_checker_execution_projection.v1",
        "source_commit": C37_SOURCE_COMMIT,
        "run": logical(C37),
        "identity": {
            "source_path": identity.get("source_path"),
            "source_sha256": identity.get("source_sha256"),
            "response_source": identity.get("response_source"),
        },
        "report": {
            "status": checker_report.get("status"),
            "applied_count": len(checker_report.get("applied") or []),
            "pending_count": len(checker_report.get("pending") or []),
            "diagnostics_count": len(checker_report.get("diagnostics") or []),
            "logical_rounds": checker_report.get("logical_rounds"),
            "model_calls": checker_report.get("model_calls"),
            "external_requests": checker_report.get("external_requests"),
            "provider_calls": checker_report.get("provider_calls"),
            "scientific_acceptance": checker_report.get("scientific_acceptance"),
            "cache_hit": checker_report.get("cache_hit"),
            "selected_manuscript": clean_for_public(checker_report.get("selected_manuscript") or {}),
        },
        "rounds": round_records,
        "free_recovery": {
            "saved_response_count": 2,
            "new_model_calls": 0,
            "new_external_requests": 0,
            "classification": "free_saved_response_recovery",
            "paid_generation_is_prior_evidence": True,
            "evidence_pointers": [
                pointer(p)
                for p in (
                    D / "CHECKER_COMMAND_V37_FREE_FINAL.json",
                    D / "CHECKER_V37_FREE_FINAL.stdout.log",
                    D / "CHECKER_V37_FREE_FINAL.stderr.log",
                )
                if p.exists()
            ],
        },
        "excluded_from_copy": [
            "CHECKER_LIVE_BINDING full payload",
            "full provider RAW_RESPONSE envelopes",
            "full SSE streams",
            "full MESSAGES/context materials",
        ],
    }
    add_json(
        execution_projection,
        checker_stage / "CHECKER_EXECUTION.projection.json",
        records,
        "c37_checker_execution_projection",
        source=C37_REPORT_PATH,
        source_commit=C37_SOURCE_COMMIT,
    )

    publication_stage = STAGE / "c37_publication"
    publication_sources = [
        (C37_REPORT_PATH, publication_stage / "MANUSCRIPT_DELIVERY_REPORT.json"),
        (C37 / "04_NUMBERING_INPUT_HANDLES.md", publication_stage / "numbering" / "NUMBERING_INPUT_HANDLES.md"),
        (C37 / "04_figures_citations" / "STAGE_REPORT.json", publication_stage / "numbering" / "STAGE_REPORT.json"),
        (C37 / "04_figures_citations" / "FIGURE_CITATION_MAP.json", publication_stage / "numbering" / "FIGURE_CITATION_MAP.json"),
        (C37 / "04_figures_citations" / "REFERENCES.json", publication_stage / "numbering" / "REFERENCES.json"),
        (C37 / "05_publication" / "DELIVERY_REPORT.json", publication_stage / "DELIVERY_REPORT.json"),
        (C37 / "05_publication" / "MANUSCRIPT_SELECTION.json", publication_stage / "MANUSCRIPT_SELECTION.json"),
        (C37 / "05_publication" / "PUBLICATION_METADATA.json", publication_stage / "PUBLICATION_METADATA.json"),
        (C37 / "05_publication" / "CONTENT_PACKAGE.json", publication_stage / "CONTENT_PACKAGE.json"),
        (C37 / "05_publication" / "MANUSCRIPT_PUBLISHED.md", publication_stage / "MANUSCRIPT_PUBLISHED.md"),
        (C37 / "05_publication" / "tex" / "main.pdf", publication_stage / "main.pdf"),
        (C37 / "05_publication" / "tex" / "LATEX_BUILD_REPORT.json", publication_stage / "LATEX_BUILD_REPORT.json"),
        (C37 / "05_publication" / "tex" / "PUBLICATION_INTEGRITY_AUDIT.json", publication_stage / "PUBLICATION_INTEGRITY_AUDIT.json"),
        (C37 / "05_publication" / "tex" / "REFERENCE_METADATA_AUDIT.json", publication_stage / "REFERENCE_METADATA_AUDIT.json"),
        (C37 / "05_publication" / "tex" / "FIGURE_ASSET_AUDIT.json", publication_stage / "FIGURE_ASSET_AUDIT.json"),
    ]
    publication_files: List[Dict[str, Any]] = []
    for source, target in publication_sources:
        if source.exists() and source.is_file():
            copy_file(
                source,
                target,
                records,
                "c37_publication_artifact",
                source_commit=C37_SOURCE_COMMIT,
            )
            publication_files.append({"file": target.relative_to(STAGE).as_posix(), "source": pointer(source)})

    root_candidates = [
        D / "ROOT_FINAL_DELIVERY_REVIEW.json",
        D / "ROOT_FINAL_DELIVERY_REVIEW.md",
        C37 / "ROOT_FINAL_DELIVERY_REVIEW.json",
        C37 / "ROOT_FINAL_DELIVERY_REVIEW.md",
    ]
    root_files: List[Dict[str, Any]] = []
    seen_root_names = set()
    for source in root_candidates:
        if source.name in seen_root_names or not source.exists() or not source.is_file():
            continue
        seen_root_names.add(source.name)
        target = publication_stage / "root_review" / source.name
        copy_file(source, target, records, "c37_root_final_review", source_commit=C37_SOURCE_COMMIT)
        root_files.append({"file": target.relative_to(STAGE).as_posix(), "source": pointer(source)})
    root_review = {
        "schema": "optomind.public_body_v37_root_final_delivery_review_pointers.v1",
        "status": "copied" if root_files else "pending_root_review",
        "files": root_files,
        "expected_sources": [logical(p) for p in root_candidates],
        "note": "Root will add the final PDF/publication review when available; this package does not infer scientific acceptance.",
    }
    add_json(
        root_review,
        publication_stage / "root_review" / "ROOT_FINAL_DELIVERY_REVIEW_POINTERS.json",
        records,
        "c37_root_final_review_pointer",
        source=C37_REPORT_PATH,
        source_commit=C37_SOURCE_COMMIT,
    )

    uir_projection_path = publication_stage / "root_review" / "UIR_032_034_ISSUES.projection.json"
    uir_markdown_path = publication_stage / "root_review" / "UIR_032_034_ISSUES.md"
    uir_ids = ("UIR-032", "UIR-033", "UIR-034")
    uir_fields = (
        "id",
        "classification",
        "title",
        "trigger",
        "actual_impact",
        "formal_capability_status",
        "current_boundary",
        "verification",
        "evidence",
    )
    if UIR_ISSUES_PATH.exists():
        uir_source = read_json(UIR_ISSUES_PATH)
        selected_issues = [
            {
                key: clean_for_public(issue.get(key))
                for key in uir_fields
                if key in issue
            }
            for issue in (uir_source.get("issues") or [])
            if issue.get("id") in uir_ids
        ]
        uir_status = "projected" if {x.get("id") for x in selected_issues} == set(uir_ids) else "partial_or_pending"
        uir_projection = {
            "schema": "optomind.public_body_v37_uir_issue_projection.v1",
            "status": uir_status,
            "source": pointer(UIR_ISSUES_PATH),
            "latest_update_utc": uir_source.get("latest_update_utc"),
            "issues": selected_issues,
            "excluded_from_copy": [
                "full RUN_ISSUES_AND_UNATTENDED_REPAIR_BACKLOG state_snapshot",
                "execution state and ledgers",
                "credentials and database contents",
            ],
        }
        uir_lines = [
            "# UIR-032/033/034 issue projection",
            "",
            "This is a public projection of the three final engineering-seam records; the full issue backlog, execution state, and ledgers are excluded.",
            "",
            f"Source: {logical(UIR_ISSUES_PATH)}",
            f"Source SHA256: {sha256_file(UIR_ISSUES_PATH)}",
            f"Latest update: {uir_source.get('latest_update_utc')}",
        ]
        for issue in selected_issues:
            uir_lines.extend(
                [
                    "",
                    f"## {issue.get('id')}: {issue.get('title')}",
                    f"- Classification: {issue.get('classification')}",
                    f"- Trigger: {issue.get('trigger')}",
                    f"- Actual impact: {issue.get('actual_impact')}",
                    f"- Capability status: {issue.get('formal_capability_status')}",
                    f"- Current boundary: {issue.get('current_boundary')}",
                    f"- Verification: {issue.get('verification')}",
                ]
            )
            for evidence in issue.get("evidence") or []:
                uir_lines.append(f"- Evidence pointer: {evidence.get('path')} ({evidence.get('locator')})")
        uir_markdown_path.parent.mkdir(parents=True, exist_ok=True)
        uir_markdown_path.write_text("\n".join(uir_lines) + "\n", encoding="utf-8", newline="\n")
        records.append(
            source_record(
                uir_markdown_path,
                UIR_ISSUES_PATH,
                "c37_uir_issue_projection_markdown",
                source_commit=C37_SOURCE_COMMIT,
            )
        )
        uir_source_for_record: Optional[Path] = UIR_ISSUES_PATH
    else:
        uir_projection = {
            "schema": "optomind.public_body_v37_uir_issue_projection.v1",
            "status": "pending_source",
            "source": pointer(UIR_ISSUES_PATH),
            "issues": [],
            "excluded_from_copy": ["full issue backlog", "execution state and ledgers", "credentials and database contents"],
        }
        uir_source_for_record = C37_REPORT_PATH
    add_json(
        uir_projection,
        uir_projection_path,
        records,
        "c37_uir_issue_projection",
        source=uir_source_for_record,
        source_commit=C37_SOURCE_COMMIT,
    )
    root_review["issues_projection"] = {
        "status": uir_projection.get("status"),
        "json": uir_projection_path.relative_to(STAGE).as_posix(),
        "markdown": uir_markdown_path.relative_to(STAGE).as_posix() if UIR_ISSUES_PATH.exists() else None,
        "source": pointer(UIR_ISSUES_PATH),
    }
    publication_projection = {
        "schema": "optomind.public_body_v37_publication_projection.v1",
        "source_commit": C37_SOURCE_COMMIT,
        "run": logical(C37),
        "status": "complete" if publication_files else "pending",
        "files": publication_files,
        "reference_summary": {
            "numbered_entries": 90,
            "doi_entries": 88,
            "duplicate_canonical_entries": 0,
            "source": pointer(C37 / "04_figures_citations" / "REFERENCES.json"),
        },
        "pdf": {
            "file": "c37_publication/main.pdf",
            "page_count": 31,
            "source": pointer(C37 / "05_publication" / "tex" / "main.pdf"),
        },
        "delivery_report": pointer(C37 / "05_publication" / "DELIVERY_REPORT.json"),
        "checker_report": pointer(checker_report_path),
        "root_final_review": root_review,
        "uir_issues": root_review["issues_projection"],
        "new_model_calls": 0,
        "new_external_requests": 0,
        "excluded_from_copy": [
            "full checker messages/context materials",
            "raw provider SSE/envelopes",
            "credentials and ledgers",
            "original paper PDFs/XML",
            "TeX build intermediates and source archive",
        ],
    }
    add_json(
        publication_projection,
        publication_stage / "PUBLICATION.projection.json",
        records,
        "c37_publication_projection",
        source=C37_REPORT_PATH,
        source_commit=C37_SOURCE_COMMIT,
    )
    return {
        "source_commit": C37_SOURCE_COMMIT,
        "checker": {
            "execution_projection": "c37_checker/CHECKER_EXECUTION.projection.json",
            "rounds": round_records,
            "report": "c37_checker/CHECKER_REPORT.json",
            "new_model_calls": 0,
            "new_external_requests": 0,
        },
        "publication": {
            "projection": "c37_publication/PUBLICATION.projection.json",
            "delivery_report": "c37_publication/DELIVERY_REPORT.json",
            "published_markdown": "c37_publication/MANUSCRIPT_PUBLISHED.md",
            "pdf": "c37_publication/main.pdf",
            "reference_entries": 90,
            "doi_entries": 88,
            "duplicate_canonical_entries": 0,
            "pdf_pages": 31,
        },
        "root_final_review": root_review,
    }


def stage_evidence(report: Dict[str, Any], records: List[Dict[str, Any]]) -> Dict[str, Any]:
    root_dir = STAGE / "evidence" / "root_reviews"
    root_reviews: Dict[str, Any] = {}
    for chapter_number in range(1, 6):
        candidates: List[Path] = []
        for phase in ("V34", "V33", "V32"):
            phase_candidates = sorted(
                [p for p in D.glob(f"ROOT_BODY_{phase}_CH{chapter_number}*REVIEW*.json") if inside(p, D) and not inside(p, STAGE)],
                key=lambda p: p.stat().st_mtime,
            )
            if phase_candidates:
                candidates = phase_candidates
                break
        if not candidates:
            root_reviews[f"CH{chapter_number}"] = {"status": "pending_root_path"}
            continue
        source = candidates[-1]
        target = root_dir / source.name
        copy_file(source, target, records, "root_chapter_review")
        review_payload = read_json(source)
        root_reviews[f"CH{chapter_number}"] = {
            "status": "copied",
            "file": target.relative_to(STAGE).as_posix(),
            "source": pointer(source),
            "review_schema": review_payload.get("schema"),
            "review_source_commit": review_payload.get("source_commit"),
            "lineage": "current_V34" if "V34" in source.name else "prior_phase_review_preserved",
        }
    add_json({"schema": "optomind.public_body_v34_root_review_pointers.v1", "reviews": root_reviews}, root_dir / "ROOT_REVIEW_POINTERS.json", records, "root_review_pointer_projection", source=REPORT_PATH)

    repair_entries: List[Any] = []
    if U1_REPAIR_PATH.exists():
        for line in U1_REPAIR_PATH.read_text(encoding="utf-8").splitlines():
            if line.strip():
                repair_entries.append(clean_for_public(json.loads(line)))
        repair = {"schema": "optomind.public_body_v34_u1_native_repair_projection.v1", "source": pointer(U1_REPAIR_PATH), "entries": repair_entries}
    else:
        repair = {"schema": "optomind.public_body_v34_u1_native_repair_projection.v1", "status": "pending_source", "source": pointer(U1_REPAIR_PATH)}
    add_json(repair, STAGE / "evidence" / "native_u1_repair" / "U1_NATIVE_REPAIR.projection.json", records, "native_u1_repair_projection", source=U1_REPAIR_PATH if U1_REPAIR_PATH.exists() else REPORT_PATH)

    resume_sources = [RESUME_DIR / "FREE_RESUME_PROOF.json", RESUME_DIR / "RESUME_FACTS.json"]
    resume = {
        "schema": "optomind.public_body_v34_capacity_resume_projection.v1",
        "files": [{"source": pointer(p), "content": clean_for_public(read_json(p))} for p in resume_sources if p.exists()],
        "note": "Free recovery proof is evidence only; no ledger or manuscript mutation is inferred from it.",
    }
    add_json(resume, STAGE / "evidence" / "capacity_resume" / "FREE_RESUME.projection.json", records, "capacity_resume_projection", source=resume_sources[0] if resume_sources[0].exists() else REPORT_PATH)

    audit_dir = D / "unused_source_audit_20261015"
    audit_stage = STAGE / "evidence" / "unused_source_audit"
    audit_files = [audit_dir / "UNUSED_SOURCE_AUDIT.md", audit_dir / "UNUSED_SOURCE_EVIDENCE.json"]
    audit_records: List[Dict[str, Any]] = []
    for source in audit_files:
        if source.exists() and source.is_file():
            target = audit_stage / source.name
            copy_file(source, target, records, "free_unused_source_audit")
            audit_records.append({"file": target.relative_to(STAGE).as_posix(), "source": pointer(source)})
    audit_projection = {
        "schema": "optomind.public_body_v34_unused_source_audit_pointers.v1",
        "status": "copied" if len(audit_records) == len(audit_files) else "partial_or_pending",
        "free_audit": True,
        "files": audit_records,
        "note": "These are local free-audit records; they are evidence only and do not imply model calls, ledger mutation, or manuscript edits.",
    }
    add_json(audit_projection, audit_stage / "UNUSED_SOURCE_AUDIT_POINTERS.json", records, "free_unused_source_audit_pointer", source=REPORT_PATH)

    parts_root_dir = STAGE / "evidence" / "parts_root_reviews"
    parts_root_sources = [
        D / "ROOT_PARTS_V34_COMPLETE_REVIEW.json",
        D / "ROOT_PARTS_V34_FREE_PREVIEW_REVIEW.json",
        D / "ROOT_PARTS_V34_IO_REVIEWS.jsonl",
    ]
    parts_root_files: List[Dict[str, Any]] = []
    for source in parts_root_sources:
        if source.exists() and source.is_file():
            target = parts_root_dir / source.name
            copy_file(source, target, records, "post_body_parts_root_review")
            parts_root_files.append({"file": target.relative_to(STAGE).as_posix(), "source": pointer(source)})
    parts_root_projection = {
        "schema": "optomind.public_body_v34_parts_root_review_pointers.v1",
        "status": "copied" if len(parts_root_files) == len(parts_root_sources) else "partial_or_pending",
        "files": parts_root_files,
        "note": "Root review and IO receipts for the four post-BODY parts; no second BODY rewrite is inferred.",
    }
    add_json(parts_root_projection, parts_root_dir / "PARTS_ROOT_REVIEW_POINTERS.json", records, "post_body_parts_root_review_pointer", source=REPORT_PATH)

    reference_dir = STAGE / "evidence" / "reference_coverage"
    reference_sources = [
        D / "ROOT_REFERENCE_COVERAGE_90_REVIEW.md",
        D / "ROOT_REFERENCE_COVERAGE_90_REVIEW.json",
    ]
    reference_files: List[Dict[str, Any]] = []
    for source in reference_sources:
        if source.exists() and source.is_file():
            target = reference_dir / source.name
            copy_file(source, target, records, "root_reference_coverage_review")
            reference_files.append({"file": target.relative_to(STAGE).as_posix(), "source": pointer(source)})
    reference_payload = read_json(D / "ROOT_REFERENCE_COVERAGE_90_REVIEW.json") if (D / "ROOT_REFERENCE_COVERAGE_90_REVIEW.json").exists() else {}
    reference_projection = {
        "schema": "optomind.public_body_v34_reference_coverage_projection.v1",
        "status": "copied" if len(reference_files) == len(reference_sources) else "partial_or_pending",
        "files": reference_files,
        "book_identities": reference_payload.get("book_identities"),
        "body_cited_identities": reference_payload.get("body_cited_identities"),
        "book_supplied_uncited": reference_payload.get("book_supplied_uncited"),
        "selected_body_source": reference_payload.get("selected_body_source"),
        "selected_body_sha256": reference_payload.get("selected_body_sha256"),
        "actual_selected_numbering_unknown_handles": reference_payload.get("actual_selected_numbering_unknown_handles"),
        "expenses_cny": reference_payload.get("expenses_cny"),
        "decision": reference_payload.get("decision"),
        "delegated_audit": pointer(Path(reference_payload["delegated_audit"])) if reference_payload.get("delegated_audit") else None,
        "note": "Current selected BODY has 90 cited identities; audit is evidence and is not injected as a model correction.",
    }
    add_json(reference_projection, reference_dir / "REFERENCE_COVERAGE.projection.json", records, "root_reference_coverage_projection", source=D / "ROOT_REFERENCE_COVERAGE_90_REVIEW.json")
    return {
        "root_reviews": root_reviews,
        "native_u1_repair": repair,
        "capacity_resume": resume,
        "unused_source_audit": audit_projection,
        "parts_root_reviews": parts_root_projection,
        "reference_coverage": reference_projection,
    }


def stage_provenance(report: Dict[str, Any], records: List[Dict[str, Any]]) -> Dict[str, Any]:
    process = read_json(PROCESS_PATH) if PROCESS_PATH.exists() else {}
    delegated = [
        D / "ROOT_BODY_CAPACITY_RESUME_20261015_IO_REVIEWS.jsonl",
        D / "DELEGATED_BODY_RESUMED_20261015_REQUEST_REVIEWS.jsonl",
        D / "DELEGATED_BODY_V34_ACTUAL_IO_REVIEWS.jsonl",
    ]
    provenance = {
        "schema": "optomind.public_body_v34_provenance_projection.v1",
        "source": {
            "source_commit": SOURCE_COMMIT,
            "upload_status": "local_only_not_uploaded",
            "source_worktree": logical(E / "execution_sources" / "downstream_v34_completion_recovery"),
            "binding": pointer(BINDING_PATH),
        },
        "current_process": clean_for_public(process),
        "delivery_report": pointer(REPORT_PATH),
        "ownership": {
            "root": "Root owns full-book editor, five chapter scientific review, first/last framing, and final QA.",
            "delegated": [{"source": pointer(p), "role": "request/input supervision evidence"} for p in delegated if p.exists()],
        },
        "free_recovery": {
            "proof": pointer(RESUME_DIR / "FREE_RESUME_PROOF.json"),
            "facts": pointer(RESUME_DIR / "RESUME_FACTS.json"),
        },
        "generation_classes": ["cached", "new_generation", "free_reparse_or_recovery"],
        "prior_public_stage": {
            "published_commit": PRIOR_PUSHED_STAGE,
            "published": True,
            "relationship": "historical 661 phase package; separate from this local V34 staging package",
        },
    }
    add_json(provenance, STAGE / "evidence" / "provenance" / "PROVENANCE.json", records, "provenance_projection", source=REPORT_PATH)
    return provenance


def ready_report(report: Dict[str, Any]) -> Tuple[bool, List[str]]:
    problems: List[str] = []
    delivery = report.get("body_delivery") or {}
    if delivery.get("ready") is not True:
        problems.append("body_delivery.ready is not true")
    if report.get("generation_complete") is not True:
        problems.append("generation_complete is not true")
    if report.get("full_unit_count") != 22:
        problems.append(f"full_unit_count={report.get('full_unit_count')!r}")
    if (report.get("selected_body") or {}).get("status") != "complete":
        problems.append("selected_body.status is not complete")
    if (report.get("article_edit") or {}).get("status") not in {"edited", "complete"}:
        problems.append("article_edit.status is not edited/complete")
    listed = {(x.get("chapter_id"), x.get("unit_id")) for x in report.get("units", [])}
    expected = set(UNITS)
    if listed != expected:
        problems.append("DELIVERY_REPORT unit identity set is not the fixed 22-unit set")
    for chapter_id, unit_id in UNITS:
        item = next((x for x in report.get("units", []) if x.get("chapter_id") == chapter_id and x.get("unit_id") == unit_id), None)
        if not item or item.get("status") != "complete":
            problems.append(f"unit incomplete: {chapter_id}:{unit_id}")
    return (not problems, problems)


def build() -> int:
    if not REPORT_PATH.exists():
        print(f"WAITING: missing {REPORT_PATH}")
        return 2
    report = read_json(REPORT_PATH)
    ok, problems = ready_report(report)
    if not ok:
        print("WAITING: " + "; ".join(problems))
        return 2
    if not BINDING_PATH.exists() or sha256_file(BINDING_PATH) != BINDING_SHA:
        raise RuntimeError("B34 binding SHA mismatch")
    STAGE.mkdir(parents=True, exist_ok=True)
    records: List[Dict[str, Any]] = []
    report_by_unit = {(x.get("chapter_id"), x.get("unit_id")): x for x in report.get("units", [])}
    unit_records = [unit_entry(report_by_unit[(ch, uid)], ch, uid, records) for ch, uid in UNITS]
    assembled = stage_assembled(report, records)
    editor = stage_article_editor(report, records)
    parts = stage_parts(report, records)
    c37 = stage_c37_checker_publication(records)
    evidence = stage_evidence(report, records)
    provenance = stage_provenance(report, records)
    # Keep the control files in the restore map too, while excluding the map itself.
    for control in (STAGE / "STAGING_PLAN.json", STAGE / "BUILD_PUBLIC_BODY_V34_STAGING.py"):
        if control.exists():
            records.append(source_record(control, control, "staging_control"))
    records.sort(key=lambda x: x["stage_path"])
    manifest = "\n".join(f"{r['stage_path']}|{r['source'].get('sha256')}|{r['source'].get('bytes')}|{r['kind']}" for r in records).encode("utf-8")
    archive_sha = sha256_bytes(manifest)
    index = {
        "schema": "optomind.public_body_v34_staging_restore_index.v1",
        "status": "body_delivery_ready_staged",
        "created_utc": now(),
        "source_commit": SOURCE_COMMIT,
        "source_commit_uploaded": False,
        "supervision_binding": pointer(BINDING_PATH),
        "body_report": pointer(REPORT_PATH),
        "body_snapshot": {
            "body_delivery": clean_for_public(report.get("body_delivery") or {}),
            "generation_complete": report.get("generation_complete"),
            "assembly_complete": report.get("assembly_complete"),
            "full_unit_count": report.get("full_unit_count"),
            "complete_units": report.get("complete_units"),
            "staged_unit_count": len(unit_records),
        },
        "public_unit_records": unit_records,
        "assembled": assembled,
        "article_editor": {"status": editor.get("status") if isinstance(editor, dict) else None},
        "parts": parts,
        "c37_checker_publication": c37,
        "evidence": {
            "root_reviews": evidence.get("root_reviews"),
            "capacity_resume": "included",
            "native_u1_repair": "included",
            "unused_source_audit": evidence.get("unused_source_audit"),
            "parts_root_reviews": evidence.get("parts_root_reviews"),
            "reference_coverage": evidence.get("reference_coverage"),
        },
        "records": records,
        "excluded": [
            "credentials_and_keys",
            "SQLite_ledgers",
            "PDF_original_papers",
            "XML_original_content",
            "FULL_BODY_INPUT.json",
            "REGISTERED_FULL_BODY_INPUT.json",
            "UNIT_MESSAGES.json_bodies",
            "RAW_RESPONSE.json_full_payloads",
            "SSE_raw_stream_bodies",
            "large_source_materials",
        ],
        "archive": {
            "status": "not_submitted",
            "unsubmitted_archive_sha256": archive_sha,
            "prior_pushed_stage_commit_sha": PRIOR_PUSHED_STAGE,
        },
        "restore_rule": "Restore original full artifacts only from native logical pointers and recorded SHA256; this package is a public projection and carries no secret or full source payload.",
    }
    index_path = STAGE / "RESTORE_INDEX.json"
    write_json(index_path, index)
    print(json.dumps({"status": index["status"], "stage": str(STAGE), "unit_count": len(unit_records), "record_count": len(records), "archive_sha256": archive_sha}, ensure_ascii=False))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.parse_args()
    return build()


if __name__ == "__main__":
    raise SystemExit(main())
