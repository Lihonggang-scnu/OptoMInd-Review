"""Budgeted live adapter for the unchanged atomic post-BODY parts runner."""
from __future__ import annotations

from contextlib import closing
import hashlib
import json
import math
from pathlib import Path
import sqlite3
from typing import Any

from .dual_budget import bind_json, read_json, write_json
from .serial_manuscript_parts import (
    SerialPartsError, build_stage_messages, messages_sha256, normalize_context,
    parse_stage_response, run_serial_parts, prompt_contract_sha256, _decode_response,
)
from .serial_parts_application import extract_body, preflight_placement
from .unit_realization import _cached_call
from .writer_candidates import _estimate, _output_lock, _profile


def _normalize_paragraph_response(stage, response):
    """Accept lossless paragraph arrays only at the live representation boundary."""
    if stage not in {"introduction", "conclusion", "abstract"}:
        return response, None
    data = _decode_response(response, stage)
    paragraphs = data.get(stage)
    if not (isinstance(paragraphs, list) and paragraphs
            and all(isinstance(p, str) and p.strip() for p in paragraphs)):
        return response, None
    data[stage] = "\n\n".join(paragraphs)
    content = json.dumps(data, ensure_ascii=False)
    receipt = {"stage": stage, "field": stage, "original_type": "array",
        "normalized_type": "string", "paragraph_count": len(paragraphs),
        "source_content_sha256": hashlib.sha256(response["content"].encode("utf-8")).hexdigest(),
        "normalized_content_sha256": hashlib.sha256(content.encode("utf-8")).hexdigest()}
    return {**response, "content": content}, receipt


def _format_repair_messages(stage, actual_messages, content, error, *, legacy=False):
    if legacy:
        schema_hint = "数组字段必须使用非空字符串数组，不能用单个字符串。"
    elif stage == "conception":
        schema_hint = "本阶段返回manuscript_parts_plan对象；职责卡中的focus、boundary、finalize_from为非空字符串数组，其他字段类型严格沿用原任务schema。"
    elif stage == "abstract":
        schema_hint = "本阶段title和abstract必须是单个非空字符串；keywords必须是非空字符串数组。abstract段落在字符串内用双换行分隔，不使用数组。"
    else:
        schema_hint = f"本阶段{stage}必须是单个非空字符串，段落在字符串内用双换行分隔，不使用数组。"
    return [*actual_messages, {"role": "assistant", "content": content},
        {"role": "user", "content": "机器结构校验失败：" + error +
         "。仅修复上次响应的 JSON 字段结构，使其严格符合原任务 schema；"
         "保留原响应的科学内容、措辞与范围，不新增事实、主张或来源。" + schema_hint +
         "返回完整 JSON 对象，不返回补丁、说明或代码围栏。"}]


def ledger_snapshot(path: str | Path, expected_limit: float) -> dict[str, Any]:
    """Read an existing ledger only; preview must never initialize a budget."""
    path = Path(path).resolve()
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as db:
        row = db.execute("SELECT value FROM budget_meta WHERE key='limit_cny'").fetchone()
        if row is None or abs(float(row[0]) - expected_limit) > 1e-9:
            raise ValueError("serial_parts_existing_budget_limit_conflict")
        rows = db.execute("SELECT amount_cny,actual_cny,status FROM reservations").fetchall()
    used = actual = held = 0.0
    for amount, cost, status in rows:
        if status not in {"settled", "reserved", "uncertain"}:
            raise ValueError("serial_parts_invalid_budget_state")
        if not math.isfinite(float(amount)) or float(amount) < 0 or (cost is not None and
                (not math.isfinite(float(cost)) or float(cost) < 0)):
            raise ValueError("serial_parts_invalid_budget_amount")
        actual += float(cost or 0)
        held += float(amount) if status != "settled" else 0
        used += float(cost if cost is not None else amount) if status == "settled" else float(amount)
    return {"path": str(path), "limit_cny": expected_limit, "used_cny": used,
            "actual_cny": actual, "held_cny": held, "reservation_count": len(rows)}


def run_live_parts(*, args, context, chapter_roles, token_counter, tokenizer_info,
                   client_factory=None):
    """One persistent request cache; each atomic assembly gets a fresh attempt.

    Existing successful responses are parsed again for free. A saved failed or
    uncertain physical attempt is never automatically purchased a second time.
    The incremental ceiling is fixed at the first preview, across all resumes.
    """
    from scripts.upgrade3.writer_candidates import make_live_factory
    from .review_delivery import _post_body_identity_context

    output = Path(args.output).resolve()
    profile = _profile({"model": "qwen3.5-plus", "thinking": True,
        "thinking_budget": args.thinking_budget, "max_output_tokens": args.output_tokens,
        "stream": True, "timeout_seconds": 900.0,
        "stream_overall_timeout_seconds": 1800.0}, "serial_parts")
    if not math.isfinite(args.incremental_budget) or args.incremental_budget <= 0:
        raise ValueError("serial_parts_incremental_budget_must_be_positive")
    project = ledger_snapshot(args.ledger, args.budget_limit)
    account = ledger_snapshot(args.account_ledger, args.account_budget_limit)
    draft = Path(args.draft).resolve()
    original = draft.read_bytes()
    manuscript = original.decode("utf-8")
    conflicts, _ = preflight_placement(manuscript, chapter_roles)
    if conflicts:
        raise SerialPartsError("placement_conflict")
    source_report = read_json(args.body_report) if args.body_report else None
    catalogs = [read_json(path) for path in args.identity_catalog]
    if source_report:
        selected = source_report.get("selected_body") or {}
        if Path(selected.get("handles_draft", "")).resolve() != draft:
            raise ValueError("serial_parts_selected_body_path_mismatch")
        if not (source_report.get("body_delivery") or {}).get("ready"):
            raise ValueError("serial_parts_body_not_ready")
        if not catalogs:
            catalogs = [read_json(selected["references_path"])]
    context = _post_body_identity_context(context, catalogs)
    body = extract_body(manuscript, chapter_roles)
    normalized = normalize_context(context, research_question=args.research_question,
                                   body_text=body, chapter_roles=chapter_roles)
    messages = build_stage_messages("conception", body_text=body,
        research_question=args.research_question, chapter_roles=chapter_roles,
        language=args.language, normalized_context=normalized)
    estimate = _estimate(messages, profile, None, token_counter)
    immutable = {"schema_version": "optomind.serial_parts.live.v1",
        "draft_path": str(draft), "draft_sha256": hashlib.sha256(original).hexdigest(),
        "conception_messages_sha256": messages_sha256(messages),
        "prompt_contract_sha256": prompt_contract_sha256(), "profile": profile,
        "language": args.language, "project_path": project["path"],
        "project_limit_cny": args.budget_limit, "account_path": account["path"],
        "account_limit_cny": args.account_budget_limit,
        "mapping_dir": str(Path(args.account_mapping_dir).resolve()),
        "incremental_limit_cny": args.incremental_budget, "tokenizer": tokenizer_info}
    with _output_lock(output):
        binding_path = output / "LIVE_BINDING.json"
        if binding_path.exists():
            binding = read_json(binding_path)
            if binding["inputs"] != immutable:
                raise ValueError("serial_parts_live_binding_changed:retain_budget_and_cache")
        else:
            binding = {"inputs": immutable, "baseline_project": project,
                "baseline_account": account,
                "reservation_ceiling_cny": min(args.budget_limit,
                    project["used_cny"] + args.incremental_budget)}
            bind_json(binding_path, binding)
        write_json(output / "CONCEPTION_MESSAGES.json", messages)
        write_json(output / "CONTEXT.json", context)
        write_json(output / "IDENTITY_CATALOGS.json", catalogs)
        report = {"stage": "serial_parts_live", "status": "preview", "model_calls": 0,
            "external_requests": 0, "profile": profile, "estimate": estimate,
            "model_calls_scope": "this_invocation_only; cumulative budget deltas use fixed first-preview baseline",
            "context_shape": {"body_chars": len(body), "allowed_handles": len(normalized["allowed_source_handles"]),
                "identity_count": len(normalized["source_identity_map"]),
                "material_records": len(normalized["material_records"]),
                "final_outline_chapters": len(normalized.get("final_outline", []))},
            "budget_binding": binding, "source_body_delivery":
                (source_report or {}).get("body_delivery"), "stage_calls": [],
            "final_manuscript": None, "scientific_acceptance": False}

        def budget_deltas():
            current = ledger_snapshot(args.ledger, args.budget_limit)
            report.update(project_after=current,
                project_actual_delta_cny=current["actual_cny"] - binding["baseline_project"]["actual_cny"],
                project_held_delta_cny=current["held_cny"] - binding["baseline_project"]["held_cny"],
                account_after=ledger_snapshot(args.account_ledger, args.account_budget_limit))

        budget_deltas()
        if args.run:
            if not estimate["fits"]:
                raise ValueError("serial_parts_input_capacity_exceeded")
            args.responses = None
            args.allow_max = False
            args.budget_ledger = args.ledger
            factory = client_factory or make_live_factory(args, token_counter=token_counter,
                reservation_ceiling_cny=binding["reservation_ceiling_cny"])

            report["status"] = "running"
            write_json(output / "LIVE_REPORT.json", report)

            def fetch(stage, actual_messages):
                audit = []
                raw, calls, state = _cached_call(output / "cache" / stage, actual_messages,
                    profile, factory, True, token_counter, attempt_log=audit)
                report["model_calls"] += calls
                report["external_requests"] += calls
                report["stage_calls"].append({"stage": stage, "calls": calls, "state": state,
                    "messages_sha256": messages_sha256(actual_messages), "attempts": audit})
                budget_deltas()
                write_json(output / "LIVE_REPORT.json", report)
                if raw is None:
                    raise SerialPartsError("live_stage_" + state + ":" + stage)
                if not raw.get("complete") or raw.get("finish_reason") != "stop" or (
                        raw.get("returned_model") or raw.get("model")) != profile["model"]:
                    raise SerialPartsError("live_response_incomplete_or_model_mismatch:" + stage)
                # The core validates the manuscript response, not transport
                # audit events/raw wire data. Full provider evidence stays in
                # cache/RAW_RESPONSE.json and its transport directory.
                model_content = output / "cache" / stage / "MODEL_CONTENT.txt"
                if not model_content.exists():
                    model_content.write_text(raw["content"], encoding="utf-8")
                return {"content": raw["content"], "finish_reason": raw["finish_reason"]}

            def parse(stage, response, cache_stage):
                response, receipt = _normalize_paragraph_response(stage, response)
                if receipt:
                    content_path = output / "cache" / cache_stage / "MODEL_CONTENT.txt"
                    receipt.update(source_cache_stage=cache_stage, source_model_content_path=str(content_path),
                        source_model_content_sha256=hashlib.sha256(content_path.read_bytes()).hexdigest())
                    write_json(output / "cache" / cache_stage / "NORMALIZATION_RECEIPT.json", receipt)
                parsed = parse_stage_response(stage, response,
                    allowed_source_handles=normalized["allowed_source_handles"])
                return response, parsed

            def provider(stage, actual_messages):
                response = fetch(stage, actual_messages)
                try:
                    response, parsed = parse(stage, response, stage)
                except SerialPartsError as exc:
                    error = str(exc)
                    write_json(output / "cache" / stage / "VALIDATION_ERROR.json", {"error": error})
                    # A format repair never supplies a scientific answer or
                    # resolves an unowned citation/unsupported placement.
                    structural = error.startswith(("nonempty_array_required:", "invalid_part_plan_shape:",
                        "invalid_manuscript_parts_plan_shape", "response_not_", "response_wrong_keys:",
                        "duplicate_response_key:", "keywords_must_be_nonempty_array"))
                    if not getattr(args, "repair_invalid", False) or not structural:
                        raise
                    repair_messages = _format_repair_messages(stage, actual_messages, response["content"], error)
                    repair_stage = stage + "_format_repair"
                    repair_dir = output / "cache" / repair_stage
                    if (repair_dir / "RAW_RESPONSE.json").is_file() and (repair_dir / "MESSAGES.json").is_file():
                        saved_raw = read_json(repair_dir / "RAW_RESPONSE.json")
                        if isinstance(saved_raw, dict) and saved_raw.get("complete") is True and saved_raw.get("finish_reason") == "stop":
                            if not (repair_dir / "REQUEST.json").is_file():
                                raise SerialPartsError("format_repair_cached_request_missing:" + stage)
                            saved_messages = read_json(repair_dir / "MESSAGES.json")
                            legacy_messages = _format_repair_messages(stage, actual_messages, response["content"], error, legacy=True)
                            if saved_messages not in (repair_messages, legacy_messages):
                                raise SerialPartsError("format_repair_cached_stage_messages_mismatch:" + stage)
                            # _cached_call still validates the original request/profile hash.
                            repair_messages = saved_messages
                    response = fetch(repair_stage, repair_messages)
                    try:
                        response, parsed = parse(stage, response, repair_stage)
                    except SerialPartsError as repair_error:
                        write_json(output / "cache" / repair_stage / "VALIDATION_ERROR.json",
                            {"error": str(repair_error)})
                        raise
                    write_json(output / "cache" / repair_stage / "PARSED_RESPONSE.json", parsed)
                else:
                    write_json(output / "cache" / stage / "PARSED_RESPONSE.json", parsed)
                return response

            attempts = output / "attempts"
            attempts.mkdir(exist_ok=True)
            attempt = attempts / f"attempt_{len(list(attempts.iterdir())) + 1:03d}"
            result = run_serial_parts(draft_path=draft, research_question=args.research_question,
                chapter_roles=chapter_roles, out_dir=attempt, context=context,
                language=args.language, provider=provider)
            result.update(model_calls=report["model_calls"], external_requests=report["external_requests"],
                call_accounting="production_client_dual_persistent_ledgers",
                source_body_delivery=report["source_body_delivery"], scientific_acceptance=False)
            write_json(attempt / "FRONT_BACK_REPORT.json", result)
            report.update(status=result["status"], final_manuscript=result["final_manuscript"],
                runtime_report=result, attempt_dir=str(attempt),
                project_after=ledger_snapshot(args.ledger, args.budget_limit),
                account_after=ledger_snapshot(args.account_ledger, args.account_budget_limit))
            failures = result.get("failures") or []
            report.update(failed_stage=failures[0]["stage"] if failures else None,
                          error=failures[0]["error"] if failures else None)
            budget_deltas()
        if draft.read_bytes() != original:
            raise ValueError("serial_parts_source_changed_during_run")
        write_json(output / "LIVE_REPORT.json", report)
        return report
