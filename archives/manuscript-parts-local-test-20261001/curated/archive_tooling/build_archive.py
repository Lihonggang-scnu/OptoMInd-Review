from __future__ import annotations

import hashlib
import json
import re
import shutil
from pathlib import Path
from typing import Any


SOURCE = Path(r"F:\OptoMind-Review-2\outputs\manuscript_parts_acceptance_20260930")
STAGING = Path(r"F:\OptoMind-Review-2\outputs\manuscript_parts_archive_20261001\worktree\archives\manuscript-parts-local-test-20261001")

MANIFEST: list[dict[str, Any]] = []
OMITTED: list[dict[str, Any]] = []
MATERIAL_POOL: dict[str, Any] = {}

SENSITIVE_KEYS = {
    "local_passages", "plain_text", "tei_xml", "fulltext", "full_text",
    "raw_text", "source_text", "quoted_text", "evidence_quotes", "quotes",
    "quote", "excerpts", "source_passages", "publisher_abstract",
    "original_abstract", "document_blocks", "reading_view", "html_source",
    "publisher_text", "extracted_text", "pdf_text", "xml_text",
    "references", "sources",
}

URL_SECRET = re.compile(r"(?i)([?&](?:api[_-]?key|access[_-]?token|token|signature|sig|x-amz-[^=]+)=)[^&#\s]+")
KEY_PATH = re.compile(r"(?i)(?:[A-Za-z]:)?[^\s\"']*api_keys[\\/][^\s\"']+")
ORIGINAL_BLOCK = re.compile(
    r"(?is)(<(?:abstract|fulltext|full_text|quote|quoted_text|source_text|plain_text|tei_xml)[^>]*>).*?(</(?:abstract|fulltext|full_text|quote|quoted_text|source_text|plain_text|tei_xml)>)"
)


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def scrub_text(text: str) -> str:
    text = URL_SECRET.sub(r"\1<redacted>", text)
    text = KEY_PATH.sub("api_keys/<redacted>", text)
    text = text.replace("qwen-api-key.txt", "<local-key-file>")
    return ORIGINAL_BLOCK.sub(lambda m: f"{m.group(1)}[REDACTED_ORIGINAL_TEXT]{m.group(2)}", text)


def redact(value: Any, path: tuple[str, ...] = ()) -> Any:
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for key, child in value.items():
            lower = str(key).lower()
            if lower in SENSITIVE_KEYS:
                if isinstance(child, str):
                    out[key] = {"redacted": True, "original_chars": len(child), "sha256": digest(child.encode("utf-8"))}
                elif isinstance(child, (dict, list)):
                    raw = json.dumps(child, ensure_ascii=False, sort_keys=True).encode("utf-8")
                    out[key] = {"redacted": True, "original_bytes": len(raw), "sha256": digest(raw)}
                else:
                    out[key] = {"redacted": True}
                continue
            if lower in {"citation", "reference", "bibliography", "citation_text"} and isinstance(child, str) and len(child) > 1000:
                out[key] = {"redacted": True, "original_chars": len(child), "sha256": digest(child.encode("utf-8")), "reason": "long_unclear_original_citation_text"}
                continue
            out[key] = redact(child, path + (lower,))
        return out
    if isinstance(value, list):
        return [redact(item, path + ("[]",)) for item in value]
    if isinstance(value, str):
        return scrub_text(value)
    return value


def transformed_json(raw: bytes) -> bytes:
    try:
        value = json.loads(raw.decode("utf-8-sig"))
    except Exception:
        raise ValueError("structured JSON parse failed")
    return (json.dumps(redact(value), ensure_ascii=False, indent=2, sort_keys=False) + "\n").encode("utf-8")


def structuralize(value: Any) -> Any:
    """Keep machine structure while replacing very long state/provenance strings."""
    if isinstance(value, dict):
        return {k: structuralize(v) for k, v in value.items()}
    if isinstance(value, list):
        return [structuralize(v) for v in value]
    if isinstance(value, str) and len(value) > 1024:
        return {"_structural_string": True, "chars": len(value), "sha256": digest(value.encode("utf-8"))}
    return scrub_text(value) if isinstance(value, str) else value


def shape_summary(value: Any) -> Any:
    if isinstance(value, dict):
        return {"_shape": "object", "key_count": len(value), "keys": sorted(str(k) for k in value)[:500]}
    if isinstance(value, list):
        return {"_shape": "array", "count": len(value), "item_types": sorted({type(v).__name__ for v in value})}
    if isinstance(value, str):
        return {"_shape": "string", "chars": len(value), "sha256": digest(value.encode("utf-8"))}
    return {"_shape": type(value).__name__}


def transformed_state(raw: bytes) -> bytes:
    try:
        value = json.loads(raw.decode("utf-8-sig"))
    except Exception:
        raise ValueError("state JSON parse failed")
    clean = redact(value)
    if isinstance(clean, dict):
        compact: dict[str, Any] = {}
        for key, child in clean.items():
            if key == "stage_inputs":
                compact[key] = shape_summary(child)
            elif key in {"topic", "research_question", "review_argument", "output", "plan_path", "pool_path"} and isinstance(child, str):
                compact[key] = scrub_text(child)
            elif isinstance(child, (dict, list)):
                compact[key] = shape_summary(child)
            else:
                compact[key] = child
        clean = compact
    return (json.dumps(clean, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def transformed_messages(raw: bytes) -> bytes:
    try:
        messages = json.loads(raw.decode("utf-8-sig"))
    except Exception:
        raise ValueError("messages JSON parse failed")
    if isinstance(messages, list):
        clean = []
        for message in messages:
            item = dict(message) if isinstance(message, dict) else {"value": message}
            content = item.get("content")
            if isinstance(content, str):
                item["content"] = redact_message_content(content)
            clean.append(redact(item))
        return (json.dumps(clean, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    return transformed_json(raw)


def redact_nested_strings(value: Any) -> Any:
    """Redact JSON objects embedded as strings inside provider raw-response envelopes."""
    if isinstance(value, dict):
        return {k: redact_nested_strings(v) for k, v in value.items()}
    if isinstance(value, list):
        return [redact_nested_strings(v) for v in value]
    if isinstance(value, str):
        if "{" in value or "[" in value:
            return redact_message_content(value)
        return scrub_text(value)
    return value


def transformed_raw(raw: bytes) -> bytes:
    text = raw.decode("utf-8-sig", errors="replace")
    try:
        value = json.loads(text)
    except Exception:
        return scrub_text(text).encode("utf-8")
    return (json.dumps(redact_nested_strings(redact(value)), ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def transformed_jsonl(raw: bytes) -> bytes:
    rows: list[str] = []
    for line in raw.decode("utf-8-sig", errors="replace").splitlines():
        if not line.strip():
            continue
        try:
            rows.append(json.dumps(redact(json.loads(line)), ensure_ascii=False, separators=(",", ":")))
        except Exception:
            rows.append(scrub_text(line))
    return ("\n".join(rows) + "\n").encode("utf-8")


def redact_message_content(content: str) -> str:
    decoder = json.JSONDecoder()
    pieces: list[str] = []
    pos = 0
    scan = 0
    while scan < len(content):
        candidates = [x for x in (content.find("{", scan), content.find("[", scan)) if x >= 0]
        if not candidates:
            break
        start = min(candidates)
        try:
            embedded, consumed = decoder.raw_decode(content[start:])
        except Exception:
            scan = start + 1
            continue
        if consumed < 40:
            scan = start + 1
            continue
        pieces.append(scrub_text(content[pos:start]))
        pieces.append(json.dumps(redact(embedded), ensure_ascii=False, indent=2))
        pos = start + consumed
        scan = pos
    pieces.append(scrub_text(content[pos:]))
    return "".join(pieces)


def normalize_material_row(row: Any, field: str) -> Any:
    """Keep identity plus model-derived A/B/deep evidence, dropping path/source baggage."""
    if not isinstance(row, dict):
        return row
    if field in {"source_materials", "candidate_materials"}:
        keep = {
            "source_handle", "paper_id", "doi", "title", "year", "material_depth",
            "study_summary_A", "review_planning_B", "deep_read_material",
            "supplement_material", "supplement_gap_material", "supplement_gap_materials",
        }
    else:
        keep = {
            "need_id", "question", "decision", "allowed_use", "conditions", "limits",
            "still_missing", "usable_content", "material_depth", "title", "doi", "year",
        }
    out = {k: redact(v) for k, v in row.items() if k in keep}
    return out


def add_file(src_rel: str, dst_rel: str | None = None, mode: str = "copy") -> None:
    src = Path(src_rel) if Path(src_rel).is_absolute() else SOURCE / src_rel
    if not src.is_file():
        OMITTED.append({"source_path": src_rel, "reason": "missing"})
        return
    dst_rel = dst_rel or src_rel
    dst = STAGING / dst_rel
    dst.parent.mkdir(parents=True, exist_ok=True)
    original = src.read_bytes()
    try:
        if mode == "json":
            data = transformed_json(original)
            transform = "json_recursive_original_text_redaction"
        elif mode == "state":
            data = transformed_state(original)
            transform = "structural_state_export_original_text_redaction"
        elif mode == "messages":
            data = transformed_messages(original)
            transform = "embedded_message_json_original_text_redaction"
        elif mode == "raw":
            data = transformed_raw(original)
            transform = "raw_response_nested_json_original_text_redaction"
        elif mode == "jsonl":
            data = transformed_jsonl(original)
            transform = "jsonl_recursive_original_text_redaction"
        elif mode == "text":
            data = scrub_text(original.decode("utf-8", errors="replace")).encode("utf-8")
            transform = "credential_signed_url_and_tagged_original_text_redaction"
        else:
            data = original
            transform = "byte_copy"
    except ValueError as exc:
        OMITTED.append({"source_path": src_rel, "reason": str(exc), "original_bytes": len(original), "original_sha256": digest(original)})
        return
    dst.write_bytes(data)
    MANIFEST.append({
        "relative_path": dst_rel.replace("\\", "/"),
        "purpose": "selected local acceptance evidence",
        "run": src_rel.split("\\", 1)[0],
        "original_path": src_rel.replace("\\", "/"),
        "original_sha256": digest(original),
        "original_bytes": len(original),
        "public_sha256": digest(data),
        "public_bytes": len(data),
        "transform": transform,
        "redaction": mode != "copy",
    })


def add_packet(src_rel: str, dst_rel: str) -> None:
    """Export a writer packet and split large material arrays into sidecars."""
    src = Path(src_rel) if Path(src_rel).is_absolute() else SOURCE / src_rel
    if not src.is_file():
        OMITTED.append({"source_path": src_rel, "reason": "missing"})
        return
    original = src.read_bytes()
    try:
        packet = redact(json.loads(original.decode("utf-8-sig")))
    except Exception:
        add_file(src_rel, dst_rel, "json")
        return
    sidecar_data: dict[str, Any] = {}
    core = dict(packet)
    for key in ("source_materials", "tool_materials", "candidate_materials"):
        if key in core:
            sidecar_data[key] = core.pop(key)
    core_bytes = (json.dumps(core, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    if sidecar_data:
        refs: list[dict[str, Any]] = []
        for field, value in sidecar_data.items():
            rows = value if isinstance(value, list) else [value]
            for row in rows:
                row = normalize_material_row(row, field)
                raw_row = json.dumps(row, ensure_ascii=False, sort_keys=True).encode("utf-8")
                key = digest(raw_row)
                MATERIAL_POOL[key] = row
                ref = {"record_sha256": key, "field": field}
                if isinstance(row, dict):
                    ref.update({k: row.get(k) for k in ("source_handle", "paper_id", "doi", "title", "year") if row.get(k) is not None})
                refs.append(ref)
        sidecar_data = {"material_refs": refs, "material_pool": "curated/materials/AB_DEEP_SUMMARIES.index.json"}
        side_bytes = (json.dumps(sidecar_data, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
        side_rel = str(Path(dst_rel).with_name(Path(dst_rel).stem + ".materials.json")).replace("\\", "/")
        (STAGING / side_rel).parent.mkdir(parents=True, exist_ok=True)
        (STAGING / side_rel).write_bytes(side_bytes)
        MANIFEST.append({"relative_path": side_rel, "purpose": "AB/deep/source-material references; records stored once in shared pool", "run": src_rel.split("\\", 1)[0], "original_path": src_rel.replace("\\", "/"), "original_sha256": digest(original), "original_bytes": len(original), "public_sha256": digest(side_bytes), "public_bytes": len(side_bytes), "transform": "writer_packet_material_reference_sidecar", "redaction": True})
    dst = STAGING / dst_rel
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_bytes(core_bytes)
    MANIFEST.append({"relative_path": dst_rel.replace("\\", "/"), "purpose": "sanitized writer packet structure", "run": src_rel.split("\\", 1)[0], "original_path": src_rel.replace("\\", "/"), "original_sha256": digest(original), "original_bytes": len(original), "public_sha256": digest(core_bytes), "public_bytes": len(core_bytes), "transform": "writer_packet_core_with_material_sidecar", "redaction": True})


def add_plan_sections(src_rel: str, dst_dir: str) -> None:
    src = Path(src_rel) if Path(src_rel).is_absolute() else SOURCE / src_rel
    if not src.is_file():
        OMITTED.append({"source_path": src_rel, "reason": "missing"})
        return
    original = src.read_bytes()
    try:
        plan = redact(json.loads(original.decode("utf-8-sig")))
    except Exception:
        OMITTED.append({"source_path": src_rel, "reason": "parse_failed"})
        return
    for key, value in plan.items():
        if key in {"source_materials", "material_records", "writer_packets", "tool_materials", "planning_tool_results"}:
            value = shape_summary(value)
        chunks: list[Any] = []
        encoded = json.dumps({key: value}, ensure_ascii=False, indent=2).encode("utf-8")
        if len(encoded) <= 4_500_000:
            chunks = [value]
        elif isinstance(value, list):
            for i in range(0, len(value), 100):
                chunks.append(value[i:i + 100])
        elif isinstance(value, dict):
            items = list(value.items())
            for i in range(0, len(items), 100):
                chunks.append(dict(items[i:i + 100]))
        else:
            chunks = [structuralize(value)]
        for i, chunk in enumerate(chunks):
            suffix = "" if len(chunks) == 1 else f".{i + 1:03d}"
            out_rel = f"{dst_dir}/{key}{suffix}.json"
            data = (json.dumps({key: chunk}, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
            if len(data) > 4_500_000:
                data = (json.dumps({key: shape_summary(chunk)}, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
            out = STAGING / out_rel
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_bytes(data)
            MANIFEST.append({"relative_path": out_rel, "purpose": "complete plan structural section", "run": src_rel.split("\\", 1)[0], "original_path": src_rel.replace("\\", "/"), "original_sha256": digest(original), "original_bytes": len(original), "public_sha256": digest(data), "public_bytes": len(data), "transform": "top_level_section_split_original_text_redaction", "redaction": True})


def add_glob(src_dir: str, pattern: str, dst_dir: str | None = None, mode: str = "copy") -> None:
    base = SOURCE / src_dir
    if not base.exists():
        OMITTED.append({"source_path": src_dir, "reason": "missing_directory"})
        return
    for src in sorted(base.rglob(pattern)):
        if not src.is_file():
            continue
        rel = src.relative_to(SOURCE).as_posix()
        out = Path(dst_dir or src_dir) / src.relative_to(base)
        if mode == "packet":
            add_packet(rel, out.as_posix())
        else:
            add_file(rel, out.as_posix(), mode)


def add_all_qwen_raw() -> None:
    """Add all model-labelled raw responses while avoiding already selected duplicates."""
    already = {str(item.get("original_path", "")).replace("\\", "/") for item in MANIFEST}
    for src in sorted(SOURCE.rglob("*.raw")):
        full = str(src).lower()
        if any(token in full for token in ("\\cache\\", "\\sources\\", "\\pdf_work\\", "\\worktree\\")):
            continue
        rel = src.relative_to(SOURCE).as_posix()
        if rel in already:
            continue
        try:
            text = src.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue
        if '"model"' not in text or "qwen" not in text.lower():
            continue
        add_file(rel, (Path("raw_public") / "all_qwen_raw" / rel).as_posix(), "raw")


def add_selected_units(src_dir: str, dst_dir: str, include_reparse: bool = False) -> None:
    base = SOURCE / src_dir
    if not base.is_dir():
        OMITTED.append({"source_path": src_dir, "reason": "missing_directory"})
        return
    for unit in sorted(base.iterdir()):
        if not unit.is_dir():
            continue
        for name in ("UNIT_INPUT.json", "UNIT_MESSAGES.json", "UNIT_RESULT.json", "UNIT_BODY.md"):
            p = unit / name
            if p.is_file():
                mode = "messages" if name == "UNIT_MESSAGES.json" else ("json" if name.endswith(".json") else "text")
                add_file(p.relative_to(SOURCE).as_posix(), (Path(dst_dir) / unit.name / name).as_posix(), mode)
        for raw in sorted(unit.rglob("*.raw")):
            add_file(raw.relative_to(SOURCE).as_posix(), (Path(dst_dir) / unit.name / raw.name).as_posix(), "raw")


def main() -> None:
    # The root agent owns these existing curated files; this worker adds only its own areas.
    for owned in (STAGING / "raw_public", STAGING / "curated" / "materials"):
        if owned.exists():
            shutil.rmtree(owned)
    (STAGING / "raw_public").mkdir(parents=True, exist_ok=True)
    (STAGING / "curated" / "materials").mkdir(parents=True, exist_ok=True)

    root_files = [
        "CURRENT_ACCEPTANCE.md", "FINAL_ACCEPTANCE_REPORT.md", "FINAL_ACCEPTANCE_RESULT.json",
        "RUN_NOTES.md", "BODY_PRELIMINARY_REVIEW.md", "MANUSCRIPT_PARTS_VERSIONS.md",
        "MANUSCRIPT_PARTS_VERSIONS.json", "IDENTITY_RECOVERY_PLAN.md", "IDENTITY_RECOVERY_PLAN.json",
        "IDENTITY_RECOVERY_APPLIED.json", "OUTPUT_RECOVERY_APPLIED.json", "RUN_PLAN.ps1",
        "RUN_PARTS.py", "RUN_BODY.py", "FULL_RUN.log", "FULL_RUN_AFTER_QUERY_FIX.log",
        "FULL_RUN_AFTER_IDENTITY_FIX.log", "FULL_RUN_CACHE_RECOVERY.log", "FULL_RUN_CONTEXT_PROJECTION.log",
        "FULL_RUN_OUTPUT32K.log", "FULL_RUN_CASE_FALLBACK_FIX.log", "FULL_RUN_OWNER_CANDIDATE_FIX.log",
        "LEVEL1_RUN.log", "LEVEL1_RETRY.log", "LEVEL1_AUTHORITY_RETRY.log", "LEVEL1_IDENTITY_RECOVERY.log",
        "LEVEL1_OUTPUT_RECOVERY.log", "LEVEL2_QUERY_RECOVERY.log",
        "body_restore_20261001/RUN_RESTORE_PLAN.ps1", "body_restore_20261001/RUN_RESTORED_BODY.ps1",
        "body_restore_20261001/BUILD_PLUS_BATCH.py", "body_restore_20261001/MANUSCRIPT_PARTS_EVOLUTION.json",
    ]
    for rel in root_files:
        mode = "state" if Path(rel).name == "RUN_STATE.json" else ("json" if rel.endswith(".json") else "text")
        add_file(rel, (Path("raw_public") / rel).as_posix(), mode)

    # Final and restored plan summaries. The complete raw JSON remains locally indexed only;
    # public copies retain every non-source field and split large plans into stable sections.
    for rel in [
        "new_plan/CALL_COST.json", "new_plan/CALL_COST_level1.json", "new_plan/CALL_COST_level2.json",
        "new_plan/RUN_STATE.json", "new_plan/PROGRESSIVE_REVIEW_PLAN.partial.json",
        "new_plan/stages/provisional_scope.json", "new_plan/stages/level1_outline.json",
        "new_plan/stages/whole_plan_improvement.json",
        "body_restore_20261001/plan/RUN_STATE.json", "body_restore_20261001/plan/PROGRESSIVE_REVIEW_PLAN.partial.json",
    ]:
        mode = "state" if Path(rel).name in {"RUN_STATE.json", "PROGRESSIVE_REVIEW_PLAN.partial.json"} else "json"
        add_file(rel, (Path("raw_public") / rel).as_posix(), mode)
    # Expanded planner Markdown repeats publisher passages in prose and is kept local;
    # the redacted complete-plan JSON sections above provide the public structural view.
    for rel in [
        "new_plan/DETAILED_REVIEW_PLAN.md", "new_plan/PROGRESSIVE_REVIEW_PLAN.partial.md",
        "body_restore_20261001/plan/DETAILED_REVIEW_PLAN.md",
    ]:
        OMITTED.append({"source_path": rel, "reason": "expanded Markdown contains untagged publisher passages; redacted JSON plan sections retained"})
    for src_dir, dst_dir in [("new_plan/writer_packets", "raw_public/new_plan/writer_packets"),
                             ("body_restore_20261001/plan/writer_packets", "raw_public/body_restore_20261001/plan/writer_packets")]:
        add_glob(src_dir, "*.json", dst_dir, "packet")

    add_plan_sections("new_plan/DETAILED_REVIEW_PLAN.json", "raw_public/new_plan/DETAILED_REVIEW_PLAN")
    add_plan_sections("body_restore_20261001/plan/DETAILED_REVIEW_PLAN.json", "raw_public/body_restore_20261001/plan/DETAILED_REVIEW_PLAN")

    # Actual Qwen planner responses are retained; downloader/source artifacts are not.
    add_glob("new_plan/raw_responses", "*.raw", "raw_public/new_plan/raw_responses", "raw")
    add_glob("body_restore_20261001/plan/raw_responses", "*.raw", "raw_public/body_restore_20261001/plan/raw_responses", "raw")

    # Body and manuscript-part artifacts. These are generated outputs; only explicit source text fields are redacted.
    for rel in [
        "body_restore_20261001/BODY_RESTORE_ACCEPTANCE_REPORT.md", "body_restore_20261001/MANUSCRIPT_PARTS_EVOLUTION.md",
        "body_restore_20261001/OFFLINE_HANDOFF_REVIEW.md", "body_restore_20261001/ROOT_BODY_READ_REVIEW.md",
        "body_restore_20261001/ROOT_BODY_PLUS_READ_REVIEW.md", "body_restore_20261001/OLD_BODY_READING_AID.md",
        "body_restore_20261001/RESTORE_STATE.json", "body_restore_20261001/BODY_RESTORE_ACCEPTANCE_RESULT.json",
        "body_restore_20261001/parts_tuned/MANUSCRIPT_FINAL.md",
        "body_restore_20261001/body_plus/assembly/REVIEW_DRAFT_HANDLES.md", "body_restore_20261001/body_plus/assembly/REVIEW_DRAFT.md",
        "body_restore_20261001/body_plus/assembly/REFERENCES.json", "body_restore_20261001/body_plus/assembly/ASSEMBLY_SUMMARY.json",
        "body_restore_20261001/body_plus/batch/MANIFEST.json", "body_restore_20261001/body_plus/assembly/RUN_REPORT.md",
        "body_run/RUN_BODY_REPORT.json", "body_run/ASSEMBLY_DIAGNOSTIC.log",
        "body_run/recovery_20261001/parts/MANUSCRIPT_FINAL.md", "body_run/recovery_20261001/assembly/REVIEW_DRAFT_HANDLES.md",
        "body_run/recovery_20261001/assembly/REVIEW_DRAFT.md", "body_run/recovery_20261001/assembly/REFERENCES.json",
        "body_run/recovery_20261001/assembly/ASSEMBLY_SUMMARY.json", "body_run/recovery_20261001/assembly/RUN_REPORT.md",
    ]:
        add_file(rel, (Path("raw_public") / rel).as_posix(), "json" if rel.endswith(".json") else "text")
    add_glob("body_restore_20261001", "*.log", "raw_public/body_restore_20261001", "text")
    add_glob("body_run", "*.log", "raw_public/body_run", "text")
    add_selected_units("body_restore_20261001/body_plus/writer", "raw_public/body_restore_20261001/body_plus/writer")

    # Five-chapter arrangement inputs and actual arrangement model responses. There were no
    # ARRANGEMENT_MESSAGES files in the run; that absence is retained in omissions.json.
    add_glob("body_restore_20261001/body/arrangement", "ARRANGEMENT_INPUT.json", "raw_public/body_restore_20261001/body/arrangement", "json")
    add_glob("body_restore_20261001/body/arrangement", "SOURCE_USAGE.json", "raw_public/body_restore_20261001/body/arrangement", "json")
    add_glob("body_restore_20261001/body/arrangement", "*.raw", "raw_public/body_restore_20261001/body/arrangement", "raw")
    OMITTED.append({"source_path": "body_restore_20261001/body/arrangement/**/ARRANGEMENT_MESSAGES.json", "reason": "not_recorded_in_run; no actual message file to publish"})
    OMITTED.append({"source_path": "body_restore_20261001/body/arrangement/**/CHAPTER_ARRANGEMENT.json", "reason": "large derived arrangement JSON omitted; corresponding actual raw responses and inputs retained"})

    # Both actual front/back runs: messages and Qwen outputs, with embedded JSON source text redacted.
    for base in ["body_restore_20261001/parts", "body_restore_20261001/parts_tuned", "body_run/recovery_20261001/parts"]:
        add_glob(base + "/messages", "*.json", "raw_public/" + base + "/messages", "messages")
        add_glob(base + "/raw_responses", "*.raw", "raw_public/" + base + "/raw_responses", "raw")
    for base in ["body_restore_20261001/parts", "body_restore_20261001/parts_tuned", "body_run/recovery_20261001/parts"]:
        for name in ["RUN_PARTS_CONTEXT.json", "RUN_PARTS_SELECTED_MATERIAL_PROVENANCE.json", "RUN_PARTS_SELECTED_MATERIAL_RECORDS.json", "RUN_PARTS_SELECTION_REPORT.json", "FRONT_BACK_REPORT.json", "RUN_PARTS_DRIVER_REPORT.json"]:
            if base == "body_restore_20261001/parts" and name in {"RUN_PARTS_CONTEXT.json", "RUN_PARTS_SELECTED_MATERIAL_PROVENANCE.json", "RUN_PARTS_SELECTED_MATERIAL_RECORDS.json", "RUN_PARTS_SELECTION_REPORT.json"}:
                continue
            add_file(base + "/" + name, "raw_public/" + base + "/" + name, "json")

    # Preserve the remaining Qwen call envelopes from the acceptance root.  Downloader,
    # source, cache, PDF-work, and worktree trees are excluded; selected files above are
    # deduplicated by original path.
    add_all_qwen_raw()

    # Earlier failure, plus the historical 176-source manuscript and comparison planning evidence.
    for rel in [
        "attempt01_missing_parts/RUN_FAILURE.json", "attempt01_missing_parts/RUN_STATE.json",
        "attempt01_missing_parts/stages/provisional_scope.json", "attempt01_missing_parts/raw_responses/progressive-review_provisional_scope_X1_microbiome_ICI/progressive-review_provisional_scope_X1_microbiome_ICI-key0-attempt0.raw",
        "attempt02_level1_before_adjustment/CALL_COST_level1.json", "attempt02_level1_before_adjustment/level1_outline.json",
        "attempt02_level1_before_adjustment/PROGRESSIVE_REVIEW_PLAN.partial.md", "attempt03_query_loss/chapter_need_analysis.json",
        "attempt03_query_loss/chapter_proposals.json", "attempt03_query_loss/harmonized_scope.json",
        "attempt03_query_loss/retrieval_loop.jsonl", "attempt03_query_loss/RUN_STATE.json",
        "worktree/advisor/20260930/delivery/real_manuscript/REVIEW_DRAFT_HANDLES.md",
        "worktree/advisor/20260930/delivery/real_manuscript/REVIEW_DRAFT.md",
        "worktree/advisor/20260930/delivery/real_manuscript/REFERENCES.json",
        "worktree/advisor/20260930/delivery/real_manuscript/RUN_REPORT.md",
        "worktree/advisor/20260930/delivery/content_handoff_03_real/real_run/CHAPTER_ARRANGEMENT.json",
        "worktree/advisor/20260930/delivery/content_handoff_03_real/real_run/CHAPTER_ARRANGEMENT.md",
        "worktree/advisor/20260930/delivery/content_handoff_03_real/REAL_COMPARISON_TABLE.json",
        "worktree/advisor/20260930/delivery/content_handoff_03_real/HUMAN_COMPARISON.md",
    ]:
        mode = "json" if rel.endswith(".json") else "text"
        if Path(rel).name == "RUN_STATE.json":
            mode = "state"
        if rel.startswith("worktree/advisor/"):
            dst = "raw_public/old_baseline/advisor/" + rel[len("worktree/advisor/"):]
        else:
            dst = "raw_public/old_baseline/attempts/" + rel
        add_file(rel, dst, mode)

    # Historical run585 planning is outside the acceptance output root but was explicitly
    # identified as the old comparison baseline. Export only redacted structural sections.
    old_plan = Path(r"F:\OptoMind-Review-2\outputs\progressive_review_plan\20260926_astra_repair\run585")
    add_file(str(old_plan.parent / "INPUT_POOL.jsonl"), "raw_public/old_baseline/INPUT_POOL.jsonl", "jsonl")
    add_plan_sections(str(old_plan / "DETAILED_REVIEW_PLAN.json"), "raw_public/old_baseline/run585/DETAILED_REVIEW_PLAN")
    OMITTED.append({"source_path": str(old_plan / "DETAILED_REVIEW_PLAN.md"), "reason": "expanded Markdown contains untagged publisher passages; redacted JSON plan sections retained"})
    add_file(str(old_plan / "CALL_COST.json"), "raw_public/old_baseline/run585/CALL_COST.json", "json")
    for packet in sorted((old_plan / "writer_packets").glob("*.json")):
        add_packet(str(packet), "raw_public/old_baseline/run585/writer_packets/" + packet.name)

    # A compact local-materials index records omitted original files without publishing them.
    source_index: list[dict[str, Any]] = []
    attached_originals = {str(item.get("original_path", "")).replace("\\", "/") for item in MANIFEST}
    for path in SOURCE.rglob("*"):
        if not path.is_file():
            continue
        rel = path.relative_to(SOURCE).as_posix()
        if not any(token in rel for token in ("/materials/", "/raw_responses/", "/cache/", "/sources/", "/pdf_work/")):
            continue
        if path.suffix.lower() not in {".pdf", ".docx", ".zip", ".mp4", ".jpg", ".jpeg", ".gif", ".bin", ".raw", ".jsonl", ".md", ".json"}:
            continue
        stat = path.stat()
        attached = rel in attached_originals
        source_index.append({"original_path": rel, "bytes": stat.st_size, "sha256": digest(path.read_bytes()), "published": attached, "attached_status": "attached_sanitized" if attached else "local_only_omitted", "reason": "selected_sanitized_model_output" if attached else "publisher_or_downloader_material"})
    (STAGING / "curated/materials/source_index.json").write_text(json.dumps(source_index, ensure_ascii=False, separators=(",", ":")) + "\n", encoding="utf-8")
    records = list(MATERIAL_POOL.items())
    pool_index: list[dict[str, Any]] = []
    for chunk_no in range(0, len(records), 100):
        chunk = dict(records[chunk_no:chunk_no + 100])
        pool_bytes = (json.dumps({"schema": "ab-deep-summary-pool.v1", "records": chunk}, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
        name = f"curated/materials/AB_DEEP_SUMMARIES.{chunk_no // 100 + 1:03d}.json"
        pool_path = STAGING / name
        pool_path.write_bytes(pool_bytes)
        pool_index.append({"path": name, "record_count": len(chunk), "sha256": digest(pool_bytes), "bytes": len(pool_bytes)})
        MANIFEST.append({"relative_path": name, "purpose": "deduplicated model-derived A/B/deep summaries", "run": "shared", "original_path": "new_plan+body_restore_20261001/plan/writer_packets", "original_sha256": None, "original_bytes": None, "public_sha256": digest(pool_bytes), "public_bytes": len(pool_bytes), "transform": "deduplicated_redacted_material_pool_split", "redaction": True})
    index_bytes = (json.dumps({"schema": "ab-deep-summary-pool-index.v1", "chunks": pool_index}, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    (STAGING / "curated/materials/AB_DEEP_SUMMARIES.index.json").write_bytes(index_bytes)
    MANIFEST.append({"relative_path": "curated/materials/AB_DEEP_SUMMARIES.index.json", "purpose": "AB/deep summary pool index", "run": "shared", "original_path": "new_plan+body_restore_20261001/plan/writer_packets", "original_sha256": None, "original_bytes": None, "public_sha256": digest(index_bytes), "public_bytes": len(index_bytes), "transform": "pool_index", "redaction": False})

    omissions = {
        "policy": "Publisher-originating abstracts/full text/passages, downloaded documents, caches, credentials, signed URL values, and binary databases are omitted or field-redacted.",
        "large_local_only": [
            "new_plan/DETAILED_REVIEW_PLAN.json", "body_restore_20261001/plan/DETAILED_REVIEW_PLAN.json",
            "new_plan/RUN_STATE.json", "body_restore_20261001/plan/RUN_STATE.json",
        ],
        "excluded_classes": ["materials/**/sources", "materials/**/cache/blobs", "DOCUMENT_BLOCKS.jsonl", "READING_VIEW.md", "*.pdf", "*.docx", "*.zip", "*.mp4", "*.bin", "api_keys/**", "*.sqlite"],
        "notes": ["The stale failed BODY report is preserved verbatim as historical evidence; later successful assembly/reparse artifacts are separate files.", "Old baseline code SHA is unknown; old manuscript is labeled comparison-only."]
    }
    (STAGING / "curated/PUBLICATION_AND_OMISSIONS.md").write_text(json.dumps(omissions, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    # Human-readable navigation for the split plan sections.  It contains filenames and
    # structural counts only; section JSON files carry the sanitized derived content.
    plan_index: list[dict[str, Any]] = []
    outline_lines = ["# Public plan outline", "", "Plan sections are split JSON exports with publisher-originating fields replaced by descriptors.", ""]
    for directory in [STAGING / "raw_public/new_plan/DETAILED_REVIEW_PLAN", STAGING / "raw_public/body_restore_20261001/plan/DETAILED_REVIEW_PLAN", STAGING / "raw_public/old_baseline/run585/DETAILED_REVIEW_PLAN"]:
        if not directory.is_dir():
            continue
        for path in sorted(directory.glob("*.json")):
            rel = path.relative_to(STAGING).as_posix()
            try:
                value = json.loads(path.read_text(encoding="utf-8"))
                key = next(iter(value)) if isinstance(value, dict) and value else path.stem
                payload = value.get(key) if isinstance(value, dict) else None
                if isinstance(payload, dict):
                    shape = {"type": "object", "keys": len(payload)}
                elif isinstance(payload, list):
                    shape = {"type": "array", "items": len(payload)}
                else:
                    shape = {"type": type(payload).__name__}
            except Exception:
                key, shape = path.stem, {"type": "unparsed"}
            plan_index.append({"path": rel, "section": key, "bytes": path.stat().st_size, "shape": shape})
            outline_lines.append(f"- `{key}`: `{rel}` ({path.stat().st_size} bytes; {shape['type']})")
    plan_index_bytes = (json.dumps({"schema": "public-plan-sections-index.v1", "sections": plan_index}, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    (STAGING / "curated/materials/PLAN_SECTIONS_INDEX.json").write_bytes(plan_index_bytes)
    (STAGING / "curated/materials/PUBLIC_PLAN_OUTLINE.md").write_text("\n".join(outline_lines) + "\n", encoding="utf-8")

    # Register worker-generated indexes and root-owned files (README/code/budget) in the
    # same manifest so the published tree has one auditable inventory.  Do not register
    # manifest.json itself because it is the container being written here.
    registered = {str(item.get("relative_path", "")).replace("\\", "/") for item in MANIFEST}
    for path in sorted(STAGING.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(STAGING).as_posix()
        if rel == "manifest.json" or rel in registered:
            continue
        data = path.read_bytes()
        MANIFEST.append({
            "relative_path": rel,
            "purpose": "root-owned archive documentation or generated index",
            "run": "archive",
            "original_path": "<archive-owned>",
            "original_sha256": digest(data),
            "original_bytes": len(data),
            "public_sha256": digest(data),
            "public_bytes": len(data),
            "transform": "root_owned_existing_or_generated_index",
            "redaction": False,
        })

    manifest = {"schema": "manuscript-parts-public-archive.v1", "source_root": str(SOURCE), "archive_root": str(STAGING), "files": MANIFEST, "omissions": OMITTED, "source_index": "curated/materials/source_index.json"}
    (STAGING / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"files": len(MANIFEST), "omissions": len(OMITTED), "bytes": sum(x["public_bytes"] for x in MANIFEST)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
