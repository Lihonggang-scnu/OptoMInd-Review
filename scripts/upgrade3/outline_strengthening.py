"""Opt-in CLI for generic autonomous outline strengthening.

The default command is offline ``prepare``.  ``--run`` is an explicit paid
boundary and requires a caller supplied key file and shared budget ledger.
"""
from __future__ import annotations

import argparse
import hashlib
import inspect
import importlib.metadata
import json
import math
import sys
from pathlib import Path
from typing import Any, Mapping


ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

DEFAULT_PROFILE = "strong_outline"
DEFAULT_REVIEWER_PROFILE = "strong_outline"
DEFAULT_OWNER_PROFILE = "strong_outline"
DEFAULT_ACCESS_PROFILE = "autonomous_outline"
DEFAULT_SELECTION_PROFILE = "outline_selection"

from optomind_research.runtime.upgrade3 import chapter_arrangement as arranging
from optomind_research.runtime.upgrade3 import outline_on_demand as on_demand
from optomind_research.runtime.upgrade3 import outline_selection as selection
from optomind_research.runtime.upgrade3 import outline_strengthening as strengthening
from optomind_research.runtime.upgrade3 import progressive_review_plan as planning
from optomind_research.runtime.upgrade3.quality_capacity import load_quality_profile
from optomind_research.runtime.upgrade3.module4 import runtime


def _load(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _dump(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")


def _hash(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")).hexdigest()


def _payload(raw: Mapping[str, Any]) -> dict[str, Any]:
    if raw.get("autonomous_outline_strengthening", {}).get("enabled"):
        return dict(raw)
    required = ("research_question", "chapter_id", "chapter_plan", "source_materials")
    missing = [key for key in required if key not in raw]
    if missing:
        raise SystemExit("input_missing:" + ",".join(missing))
    return strengthening.build_strengthening_payload(
        research_question=str(raw.get("research_question") or ""),
        chapter_id=str(raw.get("chapter_id") or ""),
        chapter_plan=raw.get("chapter_plan") or {},
        source_materials=raw.get("source_materials") or [],
        readonly_neighbor_unit_roles=raw.get("readonly_neighbor_unit_roles") or [],
        full_chapter_context=raw.get("full_chapter_context") or {},
        chapter=raw.get("chapter") or {},
        actual_local_body=str(raw.get("actual_local_body") or ""),
        shared_outline=raw.get("shared_outline") or [],
        shared_scope=raw.get("shared_scope") or {},
        review_argument=raw.get("review_argument") or "",
        review_argument_status=str(raw.get("review_argument_status") or ""),
        review_argument_source=str(raw.get("review_argument_source") or ""),
        source_identity_map=raw.get("source_identity_map") or {},
        candidate_materials=raw.get("candidate_materials") or [],
        candidate_navigation=raw.get("candidate_navigation") or {},
        tool_materials=raw.get("tool_materials") or [],
        modifiable_unit_ids=raw.get("modifiable_unit_ids"),
        read_only_unit_ids=raw.get("read_only_unit_ids"),
        topic_id=str(raw.get("topic_id") or ""),
        call_id=str(raw.get("call_id") or ""),
    )


def _on_demand_profiles(args: argparse.Namespace) -> tuple[str, str]:
    return str(args.access_profile or DEFAULT_ACCESS_PROFILE), str(args.profile or DEFAULT_PROFILE)


def _on_demand_execution_policy(args: argparse.Namespace) -> dict[str, Any]:
    """Explicit on-demand transport settings, shared by preview and dispatch."""
    timeout = float(getattr(args, "on_demand_request_timeout", 1800.0))
    overall = float(getattr(args, "on_demand_stream_overall_timeout", 3600.0))
    if not math.isfinite(timeout) or timeout < 5 or not math.isfinite(overall) or overall < timeout:
        raise SystemExit("invalid_on_demand_timeouts:require_5<=request<=overall")
    return {
        "stream": bool(getattr(args, "on_demand_stream", True)),
        "timeout_seconds": timeout,
        "stream_overall_timeout_seconds": overall,
    }


def _on_demand_meter(args: argparse.Namespace) -> tuple[Any, dict[str, Any]]:
    tokenizer = Path(args.tokenizer) if args.tokenizer else planning.DEFAULT_TOKENIZER_PATH
    counter = planning.qwen_local_token_counter(tokenizer) if tokenizer.is_file() else None
    # The asset hash and metering implementation bind prepare to execution;
    # moving identical assets to another path does not require re-preparation.
    implementations = (planning.qwen_local_token_counter, runtime.estimate_prompt_tokens,
                       runtime.build_qwen_wire_body, strengthening.estimate_strengthening_request)
    code = []
    for implementation in implementations:
        try:
            code.append(inspect.getsource(implementation))
        except (OSError, TypeError):
            code.append(f"{type(implementation).__module__}.{type(implementation).__qualname__}")
    try:
        tokenizer_version = importlib.metadata.version("tokenizers") if counter is not None else None
    except importlib.metadata.PackageNotFoundError:
        tokenizer_version = None
    return counter, {
        "mode": "local_tokenizer" if counter is not None else "utf8_byte_upper_bound",
        "tokenizer_sha256": hashlib.sha256(tokenizer.read_bytes()).hexdigest() if counter is not None else None,
        "meter_implementation_sha256": _hash(code),
        "tokenizer_implementation_version": tokenizer_version,
    }


def _selection_input(raw: Any) -> dict[str, Any]:
    """Accept a current selector envelope or genuine complete chapter packets.

    Historical outline-only selector files cannot reconstruct missing science;
    callers should supply their original complete packets instead.
    """
    if isinstance(raw, Mapping) and isinstance(raw.get("selection_mode"), Mapping):
        return dict(raw)
    if isinstance(raw, Mapping) and isinstance(raw.get("chapter_plan"), Mapping):
        rows = [raw]
    elif isinstance(raw, Mapping):
        rows = list(raw.values())
    elif isinstance(raw, list):
        rows = raw
    else:
        raise SystemExit("selection_input_requires_complete_chapter_packets")
    if not rows or any(not isinstance(row, Mapping) or not isinstance(row.get("chapter_plan"), Mapping) for row in rows):
        raise SystemExit("selection_input_requires_complete_chapter_packets")
    first = rows[0]
    return selection.build_selection_payload(
        rows,
        research_question=str(first.get("research_question") or ""),
        shared_outline=first.get("shared_outline") or [],
        shared_scope=first.get("shared_scope") or {},
        review_argument=first.get("review_argument") or "",
        review_argument_status=str(first.get("review_argument_status") or ""),
        review_argument_source=str(first.get("review_argument_source") or ""),
    )


def prepare_selection(args: argparse.Namespace) -> dict[str, Any]:
    """Prepare the first-layer selector without crossing the paid boundary."""

    payload = _selection_input(_load(Path(args.input)))
    selection._verify_selection_payload(payload)
    profile_name = str(args.selection_profile or DEFAULT_SELECTION_PROFILE)
    profile = load_quality_profile(profile_name)
    tokenizer = Path(args.tokenizer) if args.tokenizer else planning.DEFAULT_TOKENIZER_PATH
    counter = planning.qwen_local_token_counter(tokenizer) if tokenizer.is_file() else None
    include_material_index = bool(getattr(args, "include_selection_material_index", False))
    model_payload = selection.model_visible_selection_payload(payload, include_material_index=include_material_index)
    projection_check = selection.verify_model_visible_projection(payload, model_payload)
    messages = selection.selection_messages(payload, model_payload=model_payload)
    estimate = selection.estimate_selection_request(messages, profile=profile, token_counter=counter)
    request = {
        "mode": "select",
        "role": profile_name,
        "stage": selection.SELECTION_ROLE,
        "model": profile["model"],
        "thinking": profile["thinking"],
        "thinking_budget": profile["thinking_budget"],
        "max_output_tokens": profile["max_output_tokens"],
        "expected_max_completion_tokens": profile["thinking_budget"] + profile["max_output_tokens"],
        "request_sha256": _hash(messages),
        "messages": messages,
        "payload": payload,
        "model_payload": model_payload,
        "model_visible_projection": projection_check,
        "include_selection_material_index": include_material_index,
        "profile": profile,
        "estimate": estimate,
    }
    out = Path(args.output)
    _dump(out / "SELECTION_REQUEST.json", request)
    _dump(out / "SELECTION_ESTIMATE.json", estimate)
    report = {
        "status": "prepared_no_paid_calls",
        "mode": "select",
        "request_path": str(out / "SELECTION_REQUEST.json"),
        "request_sha256": request["request_sha256"],
        "profile_name": profile_name,
        "profile": profile,
        "estimate": estimate,
        "chapter_count": len(payload.get("chapters") or []),
        "unit_count": len(payload.get("all_unit_ids") or []),
        "cross_chapter_groups_allowed": bool((payload.get("selection_mode") or {}).get("allow_cross_chapter_groups")),
        "include_selection_material_index": include_material_index,
        "model_visible_projection": projection_check,
    }
    _dump(out / "PREPARE_REPORT.json", report)
    return report


def run_selection_cli(args: argparse.Namespace) -> dict[str, Any]:
    """Run/recover the selector after exact prepared-request verification."""

    out = Path(args.output)
    request = _load(out / "SELECTION_REQUEST.json")
    if request.get("mode") != "select":
        raise SystemExit("prepared_mode_mismatch:rerun_prepare")
    payload = request.get("payload") or {}
    selection._verify_selection_payload(payload)
    model_payload = request.get("model_payload") or {}
    projection_check = selection.verify_model_visible_projection(payload, model_payload)
    include_material_index = bool(getattr(args, "include_selection_material_index", False))
    if bool(request.get("include_selection_material_index")) != include_material_index:
        raise SystemExit("prepared_selection_material_index_mode_changed:rerun_prepare")
    profile_name = str(args.selection_profile or DEFAULT_SELECTION_PROFILE)
    profile = load_quality_profile(profile_name)
    current_projection = selection.model_visible_selection_payload(payload, include_material_index=include_material_index)
    if current_projection != model_payload:
        raise SystemExit("prepared_selection_model_payload_changed:rerun_prepare")
    current_messages = selection.selection_messages(payload, model_payload=model_payload)
    if request.get("request_sha256") != _hash(request.get("messages") or []):
        raise SystemExit("request_fingerprint_mismatch")
    if _hash(current_messages) != request.get("request_sha256"):
        raise SystemExit("prepared_selection_messages_changed:rerun_prepare")
    expected_profile = {key: request.get(key) for key in ("model", "thinking", "thinking_budget", "max_output_tokens")}
    actual_profile = {key: profile.get(key) for key in expected_profile}
    if request.get("role") != profile_name or actual_profile != expected_profile:
        raise SystemExit("prepared_profile_changed:rerun_prepare")
    ledger_path = Path(args.budget_ledger) if args.budget_ledger else None
    key_path = Path(args.key_file) if args.key_file else None
    if ledger_path is None or key_path is None:
        raise SystemExit("run_requires_budget_ledger_and_key_file")
    tokenizer = Path(args.tokenizer) if args.tokenizer else planning.DEFAULT_TOKENIZER_PATH
    counter = planning.qwen_local_token_counter(tokenizer) if tokenizer.is_file() else None
    ledger = runtime.GlobalBudgetLedger(path=ledger_path, limit_cny=args.budget_limit)
    client = strengthening.make_strengthening_client(
        role=profile_name, key_file=key_path, budget_ledger=ledger,
        raw_response_dir=out / "raw_responses", prompt_token_counter=counter,
    )
    result = selection.run_selection(
        payload, client=client, model=profile["model"],
        thinking_budget=profile["thinking_budget"], max_output_tokens=profile["max_output_tokens"],
        call_id=str(payload.get("call_id") or "outline-unit-selection"),
        checkpoint_dir=out / "stages", resume=True, profile=profile, model_payload=model_payload,
        retry_unresolved=bool(getattr(args, "retry_unresolved", False)),
    )
    _dump(out / "SELECTION_RESULT.json", result)
    report = {
        "status": result.get("status"),
        "mode": "select",
        "request_sha256": request["request_sha256"],
        "result_path": str(out / "SELECTION_RESULT.json"),
        "stage_reused": result.get("stage_reused", False),
        "raw_reused": result.get("raw_reused", False),
        "validation_errors": result.get("validation_errors") or [],
        "model_visible_projection": projection_check,
    }
    _dump(out / "RUN_REPORT.json", report)
    return report


def prepare_on_demand(args: argparse.Namespace) -> dict[str, Any]:
    payload = _payload(_load(Path(args.input)))
    catalog = on_demand.build_material_catalog(payload)
    access_profile_name, owner_profile_name = _on_demand_profiles(args)
    access_profile = load_quality_profile(access_profile_name)
    owner_profile = load_quality_profile(owner_profile_name)
    policy = _on_demand_execution_policy(args)
    counter, metering = _on_demand_meter(args)
    access_request = on_demand.access_messages(payload, catalog)
    access_estimate = strengthening.estimate_strengthening_request(
        access_request, profile=access_profile, token_counter=counter, stream=policy["stream"],
    )
    # Do not construct/send an all-catalog owner upper bound here.  The access
    # response is the first paid step; the production run resolves the actual
    # requested records and estimates that concrete owner request immediately
    # before reserving it.  Keep an empty-read preview only for offline sizing.
    initial_trace = {
        "status": "initial_no_materials",
        "material_requests": [],
        "selected_materials": [],
        "trace": [],
        "resolved_count": 0,
        "access_ids": [],
        "catalog_sha256": catalog.get("catalog_sha256"),
    }
    owner_request = on_demand.owner_messages(payload, catalog, initial_trace)
    owner_estimate = strengthening.estimate_strengthening_request(
        owner_request, profile=owner_profile, token_counter=counter, stream=policy["stream"],
    )
    request = {
        "mode": "on_demand",
        "payload": payload,
        "catalog": on_demand.public_catalog(catalog),
        "access_profile_name": access_profile_name,
        "access_profile": access_profile,
        "owner_profile_name": owner_profile_name,
        "owner_profile": owner_profile,
        "execution_policy": policy,
        "metering": metering,
        "access_messages": access_request,
        "access_request_sha256": _hash(access_request),
        "owner_initial_messages": owner_request,
        "owner_initial_request_sha256": _hash(owner_request),
        "access_estimate": access_estimate,
        # This is an empty-read preview only.  It is deliberately not called
        # an upper bound: resolved material and any continuation are estimated
        # at the actual paid step after the access response.
        "owner_initial_estimate": owner_estimate,
        "initial_estimated_cost_cny": float(access_estimate["estimated_cost_cny"]) + 2.0 * float(owner_estimate["estimated_cost_cny"]),
        "owner_upper_bound_calls": None,
        "max_continuation_reads": 1,
        "owner_initial_trace": initial_trace,
        "combined_uppergate_removed": True,
        "owner_upper_bound_status": "removed_from_launch",
        "owner_upper_bound_messages": None,
        "owner_upper_bound_request_sha256": None,
        "owner_upper_bound_estimate": None,
        "worst_case_estimated_cost_cny": None,
    }
    out = Path(args.output)
    _dump(out / "ACCESS_REQUEST.json", {"messages": access_request, "request_sha256": request["access_request_sha256"],
                                       "profile": access_profile, "execution_policy": policy, "metering": metering})
    _dump(out / "OWNER_INITIAL_REQUEST.json", {"messages": owner_request, "request_sha256": request["owner_initial_request_sha256"],
                                              "profile": owner_profile, "trace": initial_trace,
                                              "execution_policy": policy, "metering": metering})
    _dump(out / "REQUEST.json", request)
    report = {
        "status": "prepared_no_paid_calls",
        "mode": "on_demand",
        "request_path": str(out / "REQUEST.json"),
        "access_request_path": str(out / "ACCESS_REQUEST.json"),
        "owner_initial_request_path": str(out / "OWNER_INITIAL_REQUEST.json"),
        "access_request_sha256": request["access_request_sha256"],
        "owner_initial_request_sha256": request["owner_initial_request_sha256"],
        "access_profile_name": access_profile_name,
        "owner_profile_name": owner_profile_name,
        "execution_policy": policy,
        "metering": metering,
        "access_estimate": access_estimate,
        "owner_initial_estimate": owner_estimate,
        "initial_estimated_cost_cny": request["initial_estimated_cost_cny"],
        "owner_upper_bound_status": "removed_from_launch; actual_resolved_trace_only",
        "payload_contract": {
            "modifiable_unit_ids": payload.get("modifiable_unit_ids") or [],
            "read_only_unit_ids": payload.get("read_only_unit_ids") or [],
            "catalog_coverage": catalog.get("coverage") or {},
            "catalog_sha256": catalog.get("catalog_sha256"),
            "source_material_count": len(payload.get("source_materials") or []),
            "tool_material_count": len(payload.get("tool_materials") or []),
            "neighbor_role_count": len(payload.get("readonly_neighbor_unit_roles") or []),
            "manual_feedback_empty": not any(payload.get(key) for key in ("chapter_feedback", "editorial_feedback_for_chapter", "case_suggestions")),
        },
    }
    _dump(out / "PREPARE_REPORT.json", report)
    return report


def prepare(args: argparse.Namespace) -> dict[str, Any]:
    payload = _payload(_load(Path(args.input)))
    mode = str(args.mode or "single").casefold()
    deduplicate_materials = bool(args.deduplicate_materials)
    if deduplicate_materials and mode != "single":
        raise SystemExit("deduplicate_materials_requires_single_mode")
    profile_name = args.reviewer_profile if mode == "reviewed" else args.profile
    material_projection = None
    model_payload = None
    if deduplicate_materials:
        material_projection = strengthening.model_material_projection(payload)
        model_payload = material_projection["payload"]
    messages = (
        strengthening.reviewer_messages(payload)
        if mode == "reviewed"
        else strengthening.strengthening_messages(payload, model_payload=model_payload)
    )
    profile = load_quality_profile(profile_name)
    tokenizer = Path(args.tokenizer) if args.tokenizer else planning.DEFAULT_TOKENIZER_PATH
    counter = planning.qwen_local_token_counter(tokenizer) if tokenizer.is_file() else None
    estimate = strengthening.estimate_strengthening_request(messages, profile=profile, token_counter=counter)
    request = {
        "role": profile_name,
        "stage": strengthening.STAGE,
        "mode": mode,
        "model": profile["model"],
        "thinking": profile["thinking"],
        "thinking_budget": profile["thinking_budget"],
        "max_output_tokens": profile["max_output_tokens"],
        "expected_max_completion_tokens": profile["thinking_budget"] + profile["max_output_tokens"],
        "request_sha256": _hash(messages),
        "messages": messages,
        "payload": payload,
        "deduplicate_materials": deduplicate_materials,
        "material_projection": {
            key: value for key, value in (material_projection or {}).items() if key != "payload"
        },
        "model_payload": model_payload,
    }
    out = Path(args.output)
    request_name = "REVIEWER_REQUEST.json" if mode == "reviewed" else "REQUEST.json"
    estimate_name = "REVIEWER_ESTIMATE.json" if mode == "reviewed" else "ESTIMATE.json"
    _dump(out / request_name, request)
    _dump(out / estimate_name, estimate)
    report = {
        "status": "prepared_no_paid_calls",
        "mode": mode,
        "request_path": str(out / request_name),
        "request_sha256": request["request_sha256"],
        "profile_name": profile_name,
        "profile": profile,
        "estimate": estimate,
        "payload_contract": {
            "modifiable_unit_ids": payload.get("modifiable_unit_ids") or [],
            "read_only_unit_ids": payload.get("read_only_unit_ids") or [],
            "source_material_count": len(payload.get("source_materials") or []),
            "neighbor_role_count": len(payload.get("readonly_neighbor_unit_roles") or []),
            "manual_feedback_empty": not any(payload.get(key) for key in ("chapter_feedback", "editorial_feedback_for_chapter", "case_suggestions")),
        },
    }
    if deduplicate_materials:
        before_messages = strengthening.strengthening_messages(payload)
        before_estimate = strengthening.estimate_strengthening_request(
            before_messages, profile=profile, token_counter=counter,
        )
        report["deduplication"] = {
            **{key: value for key, value in (material_projection or {}).items() if key != "payload"},
            "before_estimate": before_estimate,
            "after_estimate": estimate,
            "estimated_cost_cny_sum_before_after": (
                float(before_estimate["estimated_cost_cny"]) + float(estimate["estimated_cost_cny"])
            ),
        }
    if mode == "reviewed":
        owner_profile = load_quality_profile(args.owner_profile)
        owner_estimate = strengthening.estimate_strengthening_request(
            strengthening.strengthening_messages(payload), profile=owner_profile, token_counter=counter,
        )
        report["owner_profile_name"] = args.owner_profile
        report["owner_profile"] = owner_profile
        report["owner_estimate_before_review"] = owner_estimate
        report["owner_estimate_deferred_until_reviewer_output"] = True
    _dump(out / "PREPARE_REPORT.json", report)
    return report


def run_on_demand_cli(args: argparse.Namespace) -> dict[str, Any]:
    out = Path(args.output)
    request = _load(out / "REQUEST.json")
    if request.get("mode") != "on_demand":
        raise SystemExit("prepared_mode_mismatch:rerun_prepare")
    payload = request.get("payload") or {}
    catalog = on_demand.build_material_catalog(payload)
    current_access = on_demand.access_messages(payload, catalog)
    if _hash(current_access) != request.get("access_request_sha256"):
        raise SystemExit("prepared_access_messages_changed:rerun_prepare")
    current_owner = on_demand.owner_messages(payload, catalog, request.get("owner_initial_trace") or {})
    if _hash(current_owner) != request.get("owner_initial_request_sha256"):
        raise SystemExit("prepared_owner_messages_changed:rerun_prepare")
    access_profile_name, owner_profile_name = _on_demand_profiles(args)
    access_profile = load_quality_profile(access_profile_name)
    owner_profile = load_quality_profile(owner_profile_name)
    if request.get("access_profile_name") != access_profile_name or request.get("owner_profile_name") != owner_profile_name:
        raise SystemExit("prepared_profile_changed:rerun_prepare")
    for saved, current in ((request.get("access_profile") or {}, access_profile), (request.get("owner_profile") or {}, owner_profile)):
        if saved != current:
            raise SystemExit("prepared_profile_changed:rerun_prepare")
    policy = _on_demand_execution_policy(args)
    if request.get("execution_policy") != policy:
        raise SystemExit("prepared_execution_policy_changed:rerun_prepare")
    counter, metering = _on_demand_meter(args)
    if request.get("metering") != metering:
        raise SystemExit("prepared_metering_changed:rerun_prepare")
    ledger_path = Path(args.budget_ledger) if args.budget_ledger else None
    key_path = Path(args.key_file) if args.key_file else None
    if ledger_path is None or key_path is None:
        raise SystemExit("run_requires_budget_ledger_and_key_file")
    ledger = runtime.GlobalBudgetLedger(path=ledger_path, limit_cny=args.budget_limit)
    access_client = strengthening.make_strengthening_client(
        role=access_profile_name, key_file=key_path, budget_ledger=ledger,
        raw_response_dir=out / "access_raw_responses", prompt_token_counter=counter,
    )
    owner_client = strengthening.make_strengthening_client(
        role=owner_profile_name, key_file=key_path, budget_ledger=ledger,
        raw_response_dir=out / "owner_raw_responses", prompt_token_counter=counter,
    )
    result = on_demand.run_on_demand_strengthening(
        payload,
        access_client=access_client, access_model=access_profile["model"],
        access_thinking_budget=access_profile["thinking_budget"],
        access_max_output_tokens=access_profile["max_output_tokens"],
        owner_client=owner_client, owner_model=owner_profile["model"],
        owner_thinking_budget=owner_profile["thinking_budget"],
        owner_max_output_tokens=owner_profile["max_output_tokens"],
        access_call_id=str(payload.get("call_id") or "outline-on-demand") + ":access",
        owner_call_id=str(payload.get("call_id") or "outline-on-demand") + ":owner",
        checkpoint_dir=out / "stages",
        resume=True,
        access_profile=access_profile,
        owner_profile=owner_profile,
        access_stream=policy["stream"], owner_stream=policy["stream"],
        access_timeout_seconds=policy["timeout_seconds"], owner_timeout_seconds=policy["timeout_seconds"],
        access_stream_overall_timeout_seconds=policy["stream_overall_timeout_seconds"],
        owner_stream_overall_timeout_seconds=policy["stream_overall_timeout_seconds"],
        retry_unresolved=bool(getattr(args, "retry_unresolved", False)),
    )
    _dump(out / "RESULT.json", result)
    arrangement_path = None
    packet = None
    if result.get("status") in {"updated", "no_change"}:
        packet = strengthening.project_plan_for_arrangement(payload, result)
        _dump(out / "ARRANGEMENT_PACKET.json", packet)
        view = arranging.build_chapter_view(out / "ARRANGEMENT_PACKET.json", id_map_path=out / "ID_MAP.json")
        arrangement_path = arranging.write_view(view, out / "ARRANGEMENT_INPUT.json")
    report = {
        "status": result["status"],
        "mode": "on_demand",
        "request_sha256": request["access_request_sha256"],
        "result_path": str(out / "RESULT.json"),
        "arrangement_input": str(arrangement_path) if arrangement_path else None,
        "accepted_source_material_count": len(packet.get("source_materials") or []) if packet else 0,
        "owner_call_count": (result.get("on_demand_strengthening") or {}).get("owner_call_count"),
        "access_reused": (result.get("on_demand_strengthening") or {}).get("access_reused"),
        "access_raw_reused": (result.get("on_demand_strengthening") or {}).get("access_raw_reused"),
        "owner_reused": (result.get("on_demand_strengthening") or {}).get("owner_reused"),
        "owner_raw_reused": (result.get("on_demand_strengthening") or {}).get("owner_raw_reused"),
        "owner_continuation_reused": (result.get("on_demand_strengthening") or {}).get("owner_continuation_reused"),
        "checkpoint_dir": (result.get("on_demand_strengthening") or {}).get("checkpoint_dir"),
        "execution_policy": policy,
        "execution_provenance": (result.get("on_demand_strengthening") or {}).get("execution_provenance"),
        "metering": metering,
    }
    _dump(out / "RUN_REPORT.json", report)
    return report


def run(args: argparse.Namespace) -> dict[str, Any]:
    out = Path(args.output)
    mode = str(args.mode or "single").casefold()
    request = _load(out / ("REVIEWER_REQUEST.json" if mode == "reviewed" else "REQUEST.json"))
    payload = request.get("payload") or {}
    prepared_dedup = bool(request.get("deduplicate_materials"))
    if prepared_dedup != bool(args.deduplicate_materials):
        raise SystemExit("prepared_material_projection_mode_changed:rerun_prepare")
    model_payload = request.get("model_payload") if prepared_dedup else None
    if prepared_dedup:
        current_projection = strengthening.model_material_projection(payload)
        if current_projection.get("projected_payload_sha256") != (request.get("material_projection") or {}).get("projected_payload_sha256"):
            raise SystemExit("prepared_material_projection_changed:rerun_prepare")
        if model_payload != current_projection.get("payload"):
            raise SystemExit("prepared_model_payload_changed:rerun_prepare")
        if strengthening.expand_material_projection(model_payload) != payload:
            raise SystemExit("prepared_material_projection_not_lossless")
    profile_name = args.reviewer_profile if mode == "reviewed" else args.profile
    profile = load_quality_profile(profile_name)
    current_messages = (
        strengthening.reviewer_messages(payload)
        if mode == "reviewed"
        else strengthening.strengthening_messages(payload, model_payload=model_payload)
    )
    if request.get("request_sha256") != _hash(request.get("messages") or []):
        raise SystemExit("request_fingerprint_mismatch")
    if _hash(current_messages) != request.get("request_sha256"):
        raise SystemExit("prepared_payload_messages_changed:rerun_prepare")
    expected_profile = {
        "model": request.get("model"),
        "thinking": request.get("thinking"),
        "thinking_budget": request.get("thinking_budget"),
        "max_output_tokens": request.get("max_output_tokens"),
    }
    actual_profile = {key: profile.get(key) for key in expected_profile}
    if request.get("role") != profile_name or actual_profile != expected_profile:
        raise SystemExit("prepared_profile_changed:rerun_prepare")
    ledger_path = Path(args.budget_ledger) if args.budget_ledger else None
    key_path = Path(args.key_file) if args.key_file else None
    if ledger_path is None or key_path is None:
        raise SystemExit("run_requires_budget_ledger_and_key_file")
    tokenizer = Path(args.tokenizer) if args.tokenizer else planning.DEFAULT_TOKENIZER_PATH
    counter = planning.qwen_local_token_counter(tokenizer) if tokenizer.is_file() else None
    ledger = runtime.GlobalBudgetLedger(path=ledger_path, limit_cny=args.budget_limit)
    if mode == "reviewed":
        owner_profile = load_quality_profile(args.owner_profile)
        reviewer_client = strengthening.make_strengthening_client(
            role=args.reviewer_profile, key_file=key_path, budget_ledger=ledger,
            raw_response_dir=out / "reviewer_raw_responses", prompt_token_counter=counter,
        )
        owner_client = strengthening.make_strengthening_client(
            role=args.owner_profile, key_file=key_path, budget_ledger=ledger,
            raw_response_dir=out / "owner_raw_responses", prompt_token_counter=counter,
        )
        result = strengthening.run_reviewed_strengthening(
            payload,
            reviewer_client=reviewer_client, reviewer_model=profile["model"],
            reviewer_thinking_budget=profile["thinking_budget"], reviewer_max_output_tokens=profile["max_output_tokens"],
            owner_client=owner_client, owner_model=owner_profile["model"],
            owner_thinking_budget=owner_profile["thinking_budget"], owner_max_output_tokens=owner_profile["max_output_tokens"],
            reviewer_call_id=str(payload.get("call_id") or "outline-strengthening-cli") + ":reviewer",
            owner_call_id=str(payload.get("call_id") or "outline-strengthening-cli") + ":owner",
        )
    else:
        client = strengthening.make_strengthening_client(
            role=args.profile, key_file=key_path, budget_ledger=ledger,
            raw_response_dir=out / "raw_responses", prompt_token_counter=counter,
        )
        result = strengthening.run_strengthening(
            payload, client=client, model=profile["model"],
            thinking_budget=profile["thinking_budget"], max_output_tokens=profile["max_output_tokens"],
            call_id=str(payload.get("call_id") or "outline-strengthening-cli"),
            model_payload=model_payload,
        )
    _dump(out / "RESULT.json", result)
    arrangement_path = None
    packet = None
    if result.get("status") in {"updated", "no_change"}:
        packet = strengthening.project_plan_for_arrangement(payload, result)
        _dump(out / "ARRANGEMENT_PACKET.json", packet)
        view = arranging.build_chapter_view(out / "ARRANGEMENT_PACKET.json", id_map_path=out / "ID_MAP.json")
        arrangement_path = arranging.write_view(view, out / "ARRANGEMENT_INPUT.json")
    report = {
        "status": result["status"],
        "mode": mode,
        "deduplicate_materials": prepared_dedup,
        "request_sha256": request["request_sha256"],
        "result_path": str(out / "RESULT.json"),
        "arrangement_input": str(arrangement_path) if arrangement_path else None,
        "accepted_source_material_count": len(packet.get("source_materials") or []) if packet else 0,
    }
    _dump(out / "RUN_REPORT.json", report)
    return report


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True, help="Current generic chapter packet JSON")
    parser.add_argument("--output", required=True, help="New run output directory")
    parser.add_argument("--profile", default=DEFAULT_PROFILE)
    parser.add_argument("--mode", choices=("single", "reviewed", "on_demand", "select"), default="single")
    parser.add_argument("--deduplicate-materials", action="store_true")
    parser.add_argument("--reviewer-profile", default=DEFAULT_REVIEWER_PROFILE)
    parser.add_argument("--owner-profile", default=DEFAULT_OWNER_PROFILE)
    parser.add_argument("--access-profile", default=DEFAULT_ACCESS_PROFILE)
    parser.add_argument("--selection-profile", default=DEFAULT_SELECTION_PROFILE)
    parser.add_argument("--on-demand-stream", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--on-demand-request-timeout", type=float, default=1800.0)
    parser.add_argument("--on-demand-stream-overall-timeout", type=float, default=3600.0)
    parser.add_argument("--include-selection-material-index", action=argparse.BooleanOptionalAction, default=False,
                        help="Optional expanded material view; default uses full outlines and compact task-linked navigation")
    parser.add_argument("--tokenizer", default="")
    parser.add_argument("--run", action="store_true", help="Cross the explicit paid-call boundary")
    parser.add_argument("--retry-unresolved", action="store_true",
                        help="After inspection, retry one cached invalid selector or unresolved owner stage; retain valid work")
    parser.add_argument("--budget-ledger", default="")
    parser.add_argument("--budget-limit", type=float, default=30.0)
    parser.add_argument("--key-file", default="")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.mode == "select":
        report = run_selection_cli(args) if args.run else prepare_selection(args)
    elif args.mode == "on_demand":
        report = run_on_demand_cli(args) if args.run else prepare_on_demand(args)
    else:
        report = run(args) if args.run else prepare(args)
    print(json.dumps(report, ensure_ascii=False), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
