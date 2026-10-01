"""Preview or explicitly run the live v2 manuscript-parts front/back pass.

Preview is deliberately the default.  It reads the frozen plan, the newly
assembled BODY, and current writer packets, then writes the exact serial
messages and local token estimates without constructing a paid client.  The
``--run`` switch is the only path that constructs ``QwenDirectClient`` and
invokes the accepted live adapter.
"""

from __future__ import annotations

import argparse
import json
import math
import re
from collections import defaultdict
from pathlib import Path
from typing import Any, Mapping, Sequence


ACCEPTANCE_ROOT = Path(__file__).resolve().parent
REPO_ROOT = ACCEPTANCE_ROOT / "worktree"
DEFAULT_PLAN = ACCEPTANCE_ROOT / "new_plan" / "DETAILED_REVIEW_PLAN.json"
DEFAULT_BODY = ACCEPTANCE_ROOT / "body_run" / "assembly" / "REVIEW_DRAFT_HANDLES.md"
DEFAULT_OUTPUT = ACCEPTANCE_ROOT / "parts_run"
DEFAULT_LEDGER = ACCEPTANCE_ROOT / "budget.sqlite"
DEFAULT_KEY = REPO_ROOT / "api_keys" / "<local-key-file>"
DEFAULT_TOKENIZER = ACCEPTANCE_ROOT.parents[1] / "data" / "tokenizers" / "qwen3_5_9b" / "tokenizer.json"

MODEL = "qwen3.5-plus"
MAX_OUTPUT_TOKENS = 18_000
THINKING_BUDGET = 8_192
TIMEOUT_SECONDS = 900.0
MATERIAL_LIMIT = 100_000
TOKEN_MARGIN_MULTIPLIER = 1.12
TOKEN_FRAMING_MARGIN = 8_192
MAX_INPUT_TOKENS = 991_808
PLAN_SCHEMA = "optomind.progressive_review_plan.v2"
PARTS_CONTRACT = "optomind.manuscript_parts_plan.v1"
STAGE_ORDER = ("conclusion", "introduction", "abstract")


class DriverError(RuntimeError):
    pass


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise DriverError(f"missing_json:{path}") from exc
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise DriverError(f"invalid_json:{path}:{type(exc).__name__}") from exc


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def _write_text(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value, encoding="utf-8", newline="\n")


def _chapter_id(row: Mapping[str, Any]) -> str:
    direct = str(row.get("chapter_id") or "").strip()
    if direct:
        return direct
    nested = row.get("chapter")
    if isinstance(nested, Mapping):
        return str(nested.get("chapter_id") or "").strip()
    return ""


def _chapter_title(row: Mapping[str, Any]) -> str:
    for candidate in (row.get("title"), row.get("name")):
        if str(candidate or "").strip():
            return str(candidate).strip()
    nested = row.get("chapter")
    if isinstance(nested, Mapping):
        return str(nested.get("title") or nested.get("name") or "").strip()
    plan = row.get("chapter_plan")
    if isinstance(plan, Mapping):
        return str(plan.get("title") or "").strip()
    return ""


def _chapter_role(row: Mapping[str, Any]) -> str:
    for candidate in (row.get("role"), row.get("chapter_role")):
        if str(candidate or "").strip():
            return str(candidate).strip()
    nested = row.get("chapter")
    if isinstance(nested, Mapping):
        return str(nested.get("role") or nested.get("chapter_role") or "").strip()
    return ""


def _safe_path(path: Path) -> str:
    return str(path.resolve())


def _reject_historical_path(path: Path, label: str) -> None:
    lowered = {part.casefold() for part in path.resolve().parts}
    banned = {
        "advisor",
        "attempt01_missing_parts",
        "20260927_run01",
        "run585",
        "old_body",
        "old-body",
    }
    if lowered & banned:
        raise DriverError(f"{label}_historical_or_archived_path:{path}")


def _identity_text(value: Any, *, doi: bool = False) -> str:
    text = " ".join(str(value or "").split()).strip().casefold()
    return text.replace("https://doi.org/", "").replace("doi:", "") if doi else text


def _same_identity(row: Mapping[str, Any], identity: Mapping[str, Any]) -> bool:
    for key in ("paper_id", "doi", "title"):
        left = _identity_text(row.get(key), doi=key == "doi")
        right = _identity_text(identity.get(key), doi=key == "doi")
        if left and right and left != right:
            return False
    return True


def _material_flags(row: Mapping[str, Any]) -> tuple[bool, bool]:
    has_ab = bool(row.get("study_summary_A") or row.get("review_planning_B"))
    has_deep = bool(row.get("deep_read_material"))
    return has_ab, has_deep


def _clip_citation(value: str, limit: int = 32) -> str:
    value = value.strip()
    if len(value) <= limit:
        return value
    boundary = value.rfind(" ", 0, limit)
    return value[: boundary if boundary > 16 else limit].rstrip(" ,.;:") + "…"


def _compact_reference_identity(raw: Any) -> dict[str, Any]:
    """Keep reference identity fields without copying a bibliography body."""

    if not isinstance(raw, Mapping):
        return {"citation": _clip_citation(str(raw))} if raw is not None else {}
    citation = str(raw.get("citation") or "").strip()
    result: dict[str, Any] = {}
    for key in ("reference_id", "source_marker"):
        value = str(raw.get(key) or "").strip()
        if value:
            result[key] = value

    # The archived citation strings concatenate DOI/PMID/PMC fields.  Accept
    # only a clean DOI token; an ambiguous concatenation is represented by the
    # bounded citation hint below instead of emitting a malformed DOI.
    doi_start = len(citation)
    for match in reversed(list(re.finditer(r"10\.\d{4,9}/[^\s]+", citation, re.IGNORECASE))):
        token = match.group(0).rstrip(".,;)")
        trailing_digits = re.search(r"\d+$", token)
        if "pmc" in token.lower() or "pmid" in token.lower():
            continue
        if trailing_digits and len(trailing_digits.group(0)) >= 7:
            continue
        result["doi"] = token
        doi_start = match.start()
        break
    years = re.findall(r"(?<!\d)(?:19|20)\d{2}(?!\d)", citation[:doi_start])
    if years:
        result["year"] = years[-1]
    if "doi" not in result and citation:
        # No DOI is available in a subset of real references; retain a
        # word-boundary citation hint rather than a mid-word truncation.
        result["citation"] = _clip_citation(citation)
    return result


def _compact_deep_material(raw: Any) -> Any:
    """Project deep material for the parts prompt while retaining provenance."""

    if not isinstance(raw, Mapping):
        return raw
    # Preserve the full deep object shape, including future substantive
    # fields; only the duplicated bibliography bodies are projected.
    compact: dict[str, Any] = dict(raw)
    references = raw.get("references")
    if isinstance(references, list):
        compact["references"] = [
            item
            for item in (_compact_reference_identity(entry) for entry in references)
            if item
        ]
    return compact


def _compact_material_record(raw: Mapping[str, Any]) -> dict[str, Any]:
    """Build the bounded model payload; caller retains ``raw`` separately."""

    compact = dict(raw)
    if "deep_read_material" in compact:
        compact["deep_read_material"] = _compact_deep_material(
            compact.get("deep_read_material")
        )
    return compact


def _serialized_size(rows: Sequence[Mapping[str, Any]]) -> int:
    return len(json.dumps(list(rows), ensure_ascii=False))


def _resolve_packet_path(plan_path: Path, row: Mapping[str, Any]) -> Path:
    relative = str(row.get("json_path") or "").strip()
    chapter_id = str(row.get("chapter_id") or "").strip()
    candidate = (plan_path.parent / relative).resolve() if relative else (
        plan_path.parent / "writer_packets" / f"{chapter_id}.json"
    ).resolve()
    if not candidate.is_relative_to(plan_path.parent.resolve()):
        raise DriverError(f"writer_packet_outside_final_plan:{chapter_id}")
    return candidate


def _load_plan(plan_path: Path) -> dict[str, Any]:
    plan_path = plan_path.resolve()
    _reject_historical_path(plan_path, "plan")
    if plan_path.name != "DETAILED_REVIEW_PLAN.json":
        raise DriverError(f"final_plan_filename_required:{plan_path}")
    plan = _read_json(plan_path)
    if not isinstance(plan, dict):
        raise DriverError("final_plan_not_object")
    required = {
        "schema_version": PLAN_SCHEMA,
        "manuscript_parts_contract_version": PARTS_CONTRACT,
        "manuscript_parts_plan_revision": "v2",
        "status": "complete",
    }
    for key, expected in required.items():
        if plan.get(key) != expected:
            raise DriverError(f"final_plan_gate:{key}={plan.get(key)!r}:expected={expected!r}")
    if plan.get("manuscript_parts_plan_frozen") is not True:
        raise DriverError("final_plan_gate:manuscript_parts_plan_frozen!=true")
    if not isinstance(plan.get("research_question"), str) or not plan["research_question"].strip():
        raise DriverError("final_plan_gate:research_question_missing")
    if not isinstance(plan.get("shared_outline"), list) or not plan["shared_outline"]:
        raise DriverError("final_plan_gate:shared_outline_missing")
    if not isinstance(plan.get("writer_packets"), list) or not plan["writer_packets"]:
        raise DriverError("final_plan_gate:writer_packets_missing")
    identity_map = plan.get("source_identity_map")
    if not isinstance(identity_map, Mapping) or not identity_map:
        raise DriverError("final_plan_gate:source_identity_map_missing")
    for handle, identity in identity_map.items():
        if not isinstance(identity, Mapping):
            raise DriverError(f"final_plan_gate:source_identity_not_object:{handle}")
        declared = str(identity.get("source_handle") or handle).strip()
        if declared != str(handle).strip():
            raise DriverError(f"final_plan_gate:source_identity_handle_mismatch:{handle}")
    return plan


def _load_packets(plan_path: Path, plan: Mapping[str, Any]) -> tuple[dict[str, dict[str, Any]], list[str]]:
    packets: dict[str, dict[str, Any]] = {}
    for row in plan["writer_packets"]:
        if not isinstance(row, Mapping):
            raise DriverError("writer_packet_index_not_object")
        chapter_id = str(row.get("chapter_id") or "").strip()
        if not chapter_id or chapter_id in packets:
            raise DriverError(f"writer_packet_chapter_id_invalid:{chapter_id}")
        path = _resolve_packet_path(plan_path, row)
        packet = _read_json(path)
        if not isinstance(packet, dict):
            raise DriverError(f"writer_packet_not_object:{path}")
        if packet.get("schema_version") != PLAN_SCHEMA:
            raise DriverError(f"writer_packet_schema_invalid:{chapter_id}")
        materials = packet.get("source_materials")
        if materials is not None and not isinstance(materials, list):
            raise DriverError(f"writer_packet_source_materials_invalid:{chapter_id}")
        packet["_driver_path"] = str(path)
        packets[chapter_id] = packet

    outline_ids = []
    for row in plan["shared_outline"]:
        if not isinstance(row, Mapping):
            raise DriverError("shared_outline_row_not_object")
        chapter_id = _chapter_id(row)
        if not chapter_id or chapter_id in outline_ids:
            raise DriverError(f"shared_outline_chapter_id_invalid:{chapter_id}")
        outline_ids.append(chapter_id)
    if set(outline_ids) != set(packets):
        raise DriverError("shared_outline_writer_packets_mismatch")
    return packets, outline_ids


def _select_material_records(
    plan: Mapping[str, Any],
    packets: Mapping[str, Mapping[str, Any]],
    chapter_order: Sequence[str],
) -> dict[str, Any]:
    identity_map = plan["source_identity_map"]
    available: dict[str, dict[str, Any]] = {}
    available_original: dict[str, dict[str, Any]] = {}
    locations: dict[str, set[str]] = defaultdict(set)
    order: list[str] = []
    invalid: list[dict[str, Any]] = []
    no_content: list[dict[str, Any]] = []
    duplicate_handles: list[str] = []

    for chapter_id in chapter_order:
        packet = packets[chapter_id]
        rows = packet.get("source_materials") or []
        for index, raw in enumerate(rows):
            if not isinstance(raw, Mapping):
                invalid.append({"chapter_id": chapter_id, "index": index, "reason": "record_not_object"})
                continue
            row = dict(raw)
            handle = str(row.get("source_handle") or "").strip()
            if not handle or handle not in identity_map:
                invalid.append({"chapter_id": chapter_id, "index": index, "source_handle": handle, "reason": "handle_not_in_final_source_identity_map"})
                continue
            if not _same_identity(row, identity_map[handle]):
                invalid.append({"chapter_id": chapter_id, "index": index, "source_handle": handle, "reason": "identity_mismatch"})
                continue
            has_ab, has_deep = _material_flags(row)
            if not has_ab and not has_deep:
                no_content.append({"chapter_id": chapter_id, "source_handle": handle, "reason": "no_A_B_or_deep_material"})
                continue
            locations[handle].add(chapter_id)
            if handle in available:
                duplicate_handles.append(handle)
                continue
            available[handle] = row
            available_original[handle] = row
            available[handle] = _compact_material_record(row)
            order.append(handle)

    by_chapter: dict[str, list[str]] = {chapter_id: [] for chapter_id in chapter_order}
    for handle in order:
        for chapter_id in sorted(locations[handle], key=chapter_order.index):
            by_chapter[chapter_id].append(handle)

    def priority(handle: str) -> tuple[int, int, int]:
        row = available_original[handle]
        has_ab, has_deep = _material_flags(row)
        return (0 if has_deep else 1, 0 if has_ab else 1, order.index(handle))

    for chapter_id in chapter_order:
        by_chapter[chapter_id].sort(key=priority)

    selected: list[str] = []
    selected_set: set[str] = set()
    skipped: list[dict[str, Any]] = []

    def add(handle: str, reason: str) -> bool:
        if handle in selected_set:
            return True
        candidate = available[handle]
        next_rows = [available[item] for item in selected] + [candidate]
        size = _serialized_size(next_rows)
        if size > MATERIAL_LIMIT:
            skipped.append({
                "source_handle": handle,
                "chapter_ids": sorted(locations[handle], key=chapter_order.index),
                "reason": "material_limit_whole_entry_not_selected",
                "record_chars": len(json.dumps(candidate, ensure_ascii=False)),
                "original_record_chars": len(json.dumps(available_original[handle], ensure_ascii=False)),
                "requested_for": reason,
            })
            return False
        selected.append(handle)
        selected_set.add(handle)
        return True

    def first_fitting(chapter_id: str, predicate: Any, reason: str) -> str | None:
        for handle in by_chapter[chapter_id]:
            if handle in selected_set or not predicate(available_original[handle]):
                continue
            if add(handle, reason):
                return handle
        return None

    def selected_for_chapter(chapter_id: str) -> list[str]:
        return [handle for handle in selected if chapter_id in locations[handle]]

    # Every chapter first gets a distinct compact representative.  Deep
    # material is preferred when it fits; if the first candidate is too large,
    # continue through the chapter's candidates instead of blocking the pass.
    chapter_representatives: dict[str, str] = {}
    for chapter_id in chapter_order:
        handle = first_fitting(
            chapter_id,
            lambda row: bool(_material_flags(row)[1]),
            f"chapter_representative_deep:{chapter_id}",
        )
        if handle is None:
            handle = first_fitting(
                chapter_id,
                lambda row: bool(_material_flags(row)[0]),
                f"chapter_representative_A_B:{chapter_id}",
            )
        if handle is not None:
            chapter_representatives[chapter_id] = handle
        else:
            # A selected handle may legitimately serve another chapter.  Use
            # that only as a last-resort coverage record after distinct
            # candidates were tried and found not to fit.
            reused = selected_for_chapter(chapter_id)
            if reused:
                chapter_representatives[chapter_id] = reused[0]

    # If the representative did not carry deep material, retain one existing
    # deep record for that chapter when the bounded input permits it.
    for chapter_id in chapter_order:
        if any(
            chapter_id in locations[h] and _material_flags(available_original[h])[1]
            for h in selected
        ):
            continue
        first_fitting(
            chapter_id,
            lambda row: bool(_material_flags(row)[1]),
            f"chapter_deep_material:{chapter_id}",
        )

    # Replace the old first-chapter-only extras with bounded round-robin
    # breadth.  The two rounds preserve the previous breadth intent while
    # giving later chapters equal opportunity for additional A/B evidence.
    for round_index in range(2):
        for chapter_id in chapter_order:
            first_fitting(
                chapter_id,
                lambda row: bool(_material_flags(row)[0] or _material_flags(row)[1]),
                f"round_robin_breadth:{round_index + 1}:{chapter_id}",
            )

    selected_rows = [available[handle] for handle in selected]
    selected_original_rows = [available_original[handle] for handle in selected]
    selected_chars = _serialized_size(selected_rows)
    selected_original_chars = _serialized_size(selected_original_rows)
    missing_coverage = [chapter_id for chapter_id in chapter_order if not any(chapter_id in locations[h] for h in selected)]
    return {
        "limit_chars": MATERIAL_LIMIT,
        "available_unique_records": len(available),
        "available_source_handles": order,
        "available_by_chapter": {chapter_id: len(by_chapter[chapter_id]) for chapter_id in chapter_order},
        "available_deep_records": sum(1 for row in available.values() if _material_flags(row)[1]),
        "available_A_B_records": sum(1 for row in available.values() if _material_flags(row)[0]),
        "invalid_records": invalid,
        "records_without_material": no_content,
        "duplicate_source_handles": sorted(set(duplicate_handles)),
        "selected_source_handles": selected,
        "selected_records": [
            {
                "source_handle": handle,
                "chapter_ids": sorted(locations[handle], key=chapter_order.index),
                "record_chars": len(json.dumps(available[handle], ensure_ascii=False)),
                "original_record_chars": len(json.dumps(available_original[handle], ensure_ascii=False)),
                "has_A_B": _material_flags(available[handle])[0],
                "has_deep": _material_flags(available[handle])[1],
            }
            for handle in selected
        ],
        "chapter_representatives": chapter_representatives,
        "selected_chars": selected_chars,
        "selected_original_chars": selected_original_chars,
        "coverage_required": list(chapter_order),
        "coverage_missing": missing_coverage,
        "skipped": skipped,
        "selected_rows": selected_rows,
        "selected_original_rows": selected_original_rows,
    }


def _chapter_roles(plan: Mapping[str, Any], chapter_order: Sequence[str]) -> list[dict[str, str]]:
    outline = {_chapter_id(row): row for row in plan["shared_outline"] if isinstance(row, Mapping)}
    details = {}
    for row in plan.get("chapters") or []:
        if isinstance(row, Mapping):
            chapter_id = _chapter_id(row)
            if chapter_id:
                details[chapter_id] = row
    roles = []
    for chapter_id in chapter_order:
        row = outline.get(chapter_id, {})
        detail = details.get(chapter_id, {})
        roles.append({
            "chapter_id": chapter_id,
            "title": _chapter_title(row) or _chapter_title(detail),
            "role": _chapter_role(row) or _chapter_role(detail),
        })
    return roles


def _context(plan: Mapping[str, Any], records: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    fields = (
        "research_question", "review_argument", "shared_scope",
        "material_theme_inventory", "source_identity_map", "manuscript_parts_plan",
        "material_access", "status", "manuscript_parts_plan_frozen",
        "manuscript_parts_plan_revision",
    )
    result = {key: plan[key] for key in fields if key in plan}
    result["material_records"] = [dict(row) for row in records]
    return result


def _load_body(body_path: Path) -> str:
    body_path = body_path.resolve()
    _reject_historical_path(body_path, "body")
    if body_path.name != "REVIEW_DRAFT_HANDLES.md":
        raise DriverError(f"assembled_body_filename_required:{body_path}")
    try:
        text = body_path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise DriverError(f"new_body_missing:{body_path}") from exc
    if len(text) > 400_000:
        raise DriverError(f"manuscript_too_long_for_single_pass:{len(text)}>400000")
    if "<!-- manuscript-part:" in text:
        raise DriverError("new_body_already_contains_manuscript_part_markers")
    historical = REPO_ROOT / "advisor" / "20260930" / "delivery" / "real_manuscript" / "REVIEW_DRAFT_HANDLES.md"
    if historical.is_file() and historical.read_bytes() == body_path.read_bytes():
        raise DriverError("new_body_matches_historical_advisor_body")
    return text


def _preview_messages(
    *,
    fb: Any,
    counter: Any,
    body_text: str,
    plan: Mapping[str, Any],
    context: Mapping[str, Any],
    chapter_roles: Sequence[Mapping[str, Any]],
    output_tokens: int,
    thinking_budget: int,
) -> tuple[dict[str, list[dict[str, str]]], list[dict[str, Any]]]:
    messages_by_stage: dict[str, list[dict[str, str]]] = {}
    estimates: list[dict[str, Any]] = []
    prior: dict[str, str] = {}
    for stage in STAGE_ORDER:
        messages = fb.build_stage_messages(
            stage,
            body_text=body_text,
            research_question=str(plan["research_question"]),
            chapter_roles=chapter_roles,
            prior_parts=prior,
            planning_context=context,
        )
        measured = int(counter(b"", messages))
        estimated_input = int(math.ceil(measured * TOKEN_MARGIN_MULTIPLIER)) + TOKEN_FRAMING_MARGIN
        total = estimated_input + int(output_tokens) + int(thinking_budget)
        messages_by_stage[stage] = messages
        estimates.append({
            "stage": stage,
            "measured_input_tokens": measured,
            "estimated_input_tokens": estimated_input,
            "output_tokens": int(output_tokens),
            "thinking_budget": int(thinking_budget),
            "estimated_total_tokens": total,
            "input_limit_exceeded": estimated_input > MAX_INPUT_TOKENS,
            "total_context_limit_exceeded": total >= 1_000_000,
        })
        if stage == "conclusion":
            prior["conclusion"] = "[preview placeholder: live conclusion will be inserted after parsing]"
        elif stage == "introduction":
            prior["introduction"] = "[preview placeholder: live introduction will be inserted after parsing]"
    return messages_by_stage, estimates


def _render_preview(
    *,
    plan_path: Path,
    body_path: Path,
    output_root: Path,
    context: Mapping[str, Any],
    selection: Mapping[str, Any],
    estimates: Sequence[Mapping[str, Any]],
    messages: Mapping[str, Sequence[Mapping[str, Any]]],
    model: str,
    output_tokens: int,
    thinking_budget: int,
) -> str:
    lines = [
        "# RUN_PARTS preview",
        "",
        "No model client was constructed and no paid request was sent.",
        "",
        f"- final plan: `{_safe_path(plan_path)}`",
        f"- new BODY: `{_safe_path(body_path)}`",
        f"- output root: `{_safe_path(output_root)}`",
        f"- model: `{model}`; visible output tokens: `{output_tokens}`; thinking budget: `{thinking_budget}`",
        f"- selected material records: `{len(selection['selected_source_handles'])}` / `{selection['selected_chars']}` serialized characters",
        f"- coverage missing: `{selection['coverage_missing']}`",
        "",
        "## Token estimates",
        "",
        "| Stage | measured input | estimated input | estimated total | limit flags |",
        "| --- | ---: | ---: | ---: | --- |",
    ]
    for row in estimates:
        flags = []
        if row["input_limit_exceeded"]:
            flags.append("input")
        if row["total_context_limit_exceeded"]:
            flags.append("total")
        lines.append(
            f"| {row['stage']} | {row['measured_input_tokens']} | {row['estimated_input_tokens']} | "
            f"{row['estimated_total_tokens']} | {','.join(flags) or 'none'} |"
        )
    lines.extend(["", "## Exact preview messages", ""])
    for stage in STAGE_ORDER:
        lines.extend([f"### {stage}", ""])
        for message in messages[stage]:
            lines.extend([f"#### {message['role']}", "", "```text", message["content"], "```", ""])
    return "\n".join(lines)


def _prepare(args: argparse.Namespace) -> dict[str, Any]:
    plan_path = Path(args.plan).resolve()
    body_path = Path(args.body).resolve()
    output_root = Path(args.output_root).resolve()
    plan = _load_plan(plan_path)
    packets, chapter_order = _load_packets(plan_path, plan)
    body_text = _load_body(body_path)
    selection = _select_material_records(plan, packets, chapter_order)
    if selection["invalid_records"]:
        raise DriverError(f"writer_packet_material_identity_errors:{len(selection['invalid_records'])}")
    if not selection["selected_source_handles"]:
        raise DriverError("no_actual_A_B_or_deep_material_selected")
    context = _context(plan, selection["selected_rows"])
    import optomind_research.runtime.upgrade3.manuscript_front_back as fb

    try:
        normalized_context = fb.normalize_planning_context(context)
    except Exception as exc:  # noqa: BLE001 - report only the contract class/message
        raise DriverError(f"planning_context_invalid:{type(exc).__name__}:{exc}") from exc
    assert normalized_context is not None
    unsupported = fb._unsupported_placements(normalized_context)
    conflicts, warnings = fb._placement_conflicts(
        body_text, _chapter_roles(plan, chapter_order), normalized_context
    )
    if unsupported:
        raise DriverError(f"unsupported_placement_preflight:{unsupported}")
    if conflicts:
        raise DriverError(f"placement_conflict_preflight:{conflicts}")
    if selection["coverage_missing"]:
        raise DriverError(f"material_chapter_coverage_missing:{selection['coverage_missing']}")
    if not isinstance(normalized_context.get("manuscript_parts_plan"), Mapping):
        raise DriverError("planning_context_parts_plan_missing")
    counter = None
    messages: dict[str, list[dict[str, str]]] = {}
    estimates: list[dict[str, Any]] = []
    try:
        from optomind_research.runtime.upgrade3.progressive_review_plan import (
            qwen_local_token_counter,
        )
        counter = qwen_local_token_counter(Path(args.tokenizer))
        messages, estimates = _preview_messages(
            fb=fb,
            counter=counter,
            body_text=body_text,
            plan=plan,
            context=normalized_context,
            chapter_roles=_chapter_roles(plan, chapter_order),
            output_tokens=args.max_output_tokens,
            thinking_budget=args.thinking_budget,
        )
    except Exception as exc:  # noqa: BLE001 - fail before any paid client exists
        raise DriverError(f"preview_message_preflight_failed:{type(exc).__name__}:{exc}") from exc
    estimate_failures = [
        row["stage"] for row in estimates
        if row["input_limit_exceeded"] or row["total_context_limit_exceeded"]
    ]
    if estimate_failures:
        raise DriverError(f"token_context_preflight_exceeded:{estimate_failures}")
    return {
        "plan_path": plan_path,
        "body_path": body_path,
        "output_root": output_root,
        "plan": plan,
        "packets": packets,
        "chapter_order": chapter_order,
        "chapter_roles": _chapter_roles(plan, chapter_order),
        "body_text": body_text,
        "selection": selection,
        "context": normalized_context,
        "warnings": warnings,
        "messages": messages,
        "estimates": estimates,
        "tokenizer": Path(args.tokenizer).resolve(),
    }


def _save_preview(prepared: Mapping[str, Any], args: argparse.Namespace) -> dict[str, str]:
    output_root = prepared["output_root"]
    output_root.mkdir(parents=True, exist_ok=True)
    selection = prepared["selection"]
    selection_report = {
        key: value
        for key, value in selection.items()
        if key not in {"selected_rows", "selected_original_rows"}
    }
    _write_json(output_root / "RUN_PARTS_CONTEXT.json", prepared["context"])
    _write_json(output_root / "RUN_PARTS_SELECTED_MATERIAL_RECORDS.json", selection["selected_rows"])
    _write_json(
        output_root / "RUN_PARTS_SELECTED_MATERIAL_PROVENANCE.json",
        selection["selected_original_rows"],
    )
    _write_json(output_root / "RUN_PARTS_SELECTION_REPORT.json", selection_report)
    _write_json(output_root / "RUN_PARTS_MESSAGES_PREVIEW.json", prepared["messages"])
    preview = {
        "status": "preview",
        "paid_call": False,
        "model": args.model,
        "max_output_tokens": args.max_output_tokens,
        "thinking_budget": args.thinking_budget,
        "tokenizer": str(prepared["tokenizer"]),
        "final_plan": _safe_path(prepared["plan_path"]),
        "new_body": _safe_path(prepared["body_path"]),
        "chapter_order": prepared["chapter_order"],
        "chapter_roles": prepared["chapter_roles"],
        "material_selection": selection_report,
        "token_estimates": prepared["estimates"],
        "placement_warnings": prepared["warnings"],
        "files": {
            "context": str(output_root / "RUN_PARTS_CONTEXT.json"),
            "selected_material": str(output_root / "RUN_PARTS_SELECTED_MATERIAL_RECORDS.json"),
            "selected_material_provenance": str(
                output_root / "RUN_PARTS_SELECTED_MATERIAL_PROVENANCE.json"
            ),
            "selection_report": str(output_root / "RUN_PARTS_SELECTION_REPORT.json"),
            "messages": str(output_root / "RUN_PARTS_MESSAGES_PREVIEW.json"),
        },
    }
    _write_json(output_root / "RUN_PARTS_PREVIEW.json", preview)
    _write_text(
        output_root / "RUN_PARTS_PREVIEW.md",
        _render_preview(
            plan_path=prepared["plan_path"],
            body_path=prepared["body_path"],
            output_root=output_root,
            context=prepared["context"],
            selection=selection,
            estimates=prepared["estimates"],
            messages=prepared["messages"],
            model=args.model,
            output_tokens=args.max_output_tokens,
            thinking_budget=args.thinking_budget,
        ),
    )
    return {key: str(value) for key, value in preview["files"].items()} | {
        "preview": str(output_root / "RUN_PARTS_PREVIEW.md"),
        "summary": str(output_root / "RUN_PARTS_PREVIEW.json"),
    }


def _run_live(prepared: Mapping[str, Any], args: argparse.Namespace) -> int:
    from optomind_research.runtime.upgrade3.module4.runtime import (
        GlobalBudgetLedger,
        QwenDirectClient,
    )
    from optomind_research.runtime.upgrade3.manuscript_front_back import run_front_back_stage

    output_root = prepared["output_root"]
    output_root.mkdir(parents=True, exist_ok=True)
    ledger = GlobalBudgetLedger(path=Path(args.ledger).resolve(), limit_cny=float(args.limit_cny))
    client = QwenDirectClient(
        model=args.model,
        key_file=Path(args.key_file).resolve(),
        max_retries=1,
        timeout_seconds=float(args.timeout_seconds),
        max_output_tokens=int(args.max_output_tokens),
        thinking=True,
        thinking_budget=int(args.thinking_budget),
        json_mode=False,
        budget_ledger=ledger,
        raw_response_dir=output_root / "raw_responses",
        prompt_token_counter=(
            __import__(
                "optomind_research.runtime.upgrade3.progressive_review_plan",
                fromlist=["qwen_local_token_counter"],
            ).qwen_local_token_counter(Path(args.tokenizer))
        ),
        prompt_token_multiplier=TOKEN_MARGIN_MULTIPLIER,
        prompt_token_framing_margin=TOKEN_FRAMING_MARGIN,
    )
    report = run_front_back_stage(
        draft_path=prepared["body_path"],
        research_question=str(prepared["plan"]["research_question"]),
        chapter_roles=prepared["chapter_roles"],
        out_dir=output_root,
        planning_context=prepared["context"],
        client=client,
        model=args.model,
        max_output_tokens=int(args.max_output_tokens),
        thinking_budget=int(args.thinking_budget),
    )
    safe_report = {
        "status": report.get("status"),
        "generated": report.get("generated", []),
        "missing_stages": report.get("missing_stages", []),
        "failures": report.get("failures", []),
        "model_calls": report.get("model_calls", 0),
        "successful_model_calls": report.get("successful_model_calls", 0),
        "model_attempts": report.get("model_attempts", 0),
        "external_requests": report.get("external_requests", 0),
        "final_manuscript": report.get("final_manuscript", ""),
        "report": str(output_root / "FRONT_BACK_REPORT.json"),
        "raw_response_dir": str(output_root / "raw_responses"),
    }
    _write_json(output_root / "RUN_PARTS_DRIVER_REPORT.json", safe_report)
    expected = list(STAGE_ORDER)
    if report.get("status") != "generated" or report.get("generated") != expected:
        print(json.dumps(safe_report, ensure_ascii=False, indent=2))
        return 2
    if report.get("model_calls") != 3 or report.get("successful_model_calls") != 3:
        print(json.dumps(safe_report, ensure_ascii=False, indent=2))
        return 2
    print(json.dumps(safe_report, ensure_ascii=False, indent=2))
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Preview or explicitly run live v2 manuscript parts.")
    parser.add_argument("--run", action="store_true", help="root-authorized live mode; preview is the default")
    parser.add_argument("--plan", type=Path, default=DEFAULT_PLAN)
    parser.add_argument("--body", type=Path, default=DEFAULT_BODY)
    parser.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--ledger", type=Path, default=DEFAULT_LEDGER)
    parser.add_argument("--key-file", type=Path, default=DEFAULT_KEY)
    parser.add_argument("--tokenizer", type=Path, default=DEFAULT_TOKENIZER)
    parser.add_argument("--model", default=MODEL)
    parser.add_argument("--max-output-tokens", type=int, default=MAX_OUTPUT_TOKENS)
    parser.add_argument("--thinking-budget", type=int, default=THINKING_BUDGET)
    parser.add_argument("--timeout-seconds", type=float, default=TIMEOUT_SECONDS)
    parser.add_argument("--limit-cny", type=float, default=100.0)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.model != MODEL:
        print(f"blocked:explicit_model_required:{MODEL}")
        return 2
    try:
        prepared = _prepare(args)
        files = _save_preview(prepared, args)
        if not args.run:
            print(json.dumps({
                "status": "preview",
                "paid_call": False,
                "files": files,
                "selected_source_handles": prepared["selection"]["selected_source_handles"],
                "selected_chars": prepared["selection"]["selected_chars"],
                "token_estimates": prepared["estimates"],
            }, ensure_ascii=False, indent=2))
            return 0
        return _run_live(prepared, args)
    except DriverError as exc:
        print(f"blocked:{exc}")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
