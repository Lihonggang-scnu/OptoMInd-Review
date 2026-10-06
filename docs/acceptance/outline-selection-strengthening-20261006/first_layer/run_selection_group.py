"""Generic selector-group on-demand runner.

The default action prepares one selector group offline.  ``--run`` is the
explicit paid boundary.  Chapters in a cross-chapter group run in selector
order; each completed owner result is merged into the complete current
seven-chapter payload before the next chapter is projected.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sqlite3
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Mapping


SOURCE = Path(r"F:\OptoMind-branch-backups\20261005-final-consolidation-204632\source")
ROOT = Path(r"F:\OptoMind-Review-2\outputs\outline_selection_20261006")
SELECTOR_RESULT = ROOT / "live_attempt1" / "SELECTION_RESULT.json"
FULL_PAYLOAD = ROOT / "FULL_CHAPTER_PAYLOADS.json"
PREVIOUS_CURRENT = ROOT / "on_demand_group1_ch3" / "CURRENT_CHAPTER_PAYLOADS.json"
LEDGER = Path(r"F:\OptoMind-Review-2\outputs\post_body_revision_local_experiment_20261005\live\budget.sqlite")
KEY = Path(r"F:\OptoMind-Review-2\api_keys\qwen-api-key.txt")
TOKENIZER = Path(r"F:\OptoMind-Review-2\data\tokenizers\qwen3_5_9b\tokenizer.json")
ROUND_BASELINE = Path(r"F:\OptoMind-Review-2\outputs\outline_directory_repair_comparison_20261006\LIVE_BUDGET_BASELINE.json")
ROUND_LIMIT = 40.0
SHARED_LIMIT = 85.0
EXPECTED_HEAD = "a0b645e096a148382196b5aadd3dcf36c43c69d6"
BASE_PAYLOAD_OVERRIDE: Path | None = None


def clone(value: Any) -> Any:
    return json.loads(json.dumps(value, ensure_ascii=False, default=str))


def digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")).hexdigest()


def dump(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")


def load(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def source_head() -> str:
    return subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=SOURCE, text=True).strip()


def source_diff_sha() -> str:
    return hashlib.sha256(subprocess.check_output(["git", "diff", "--no-ext-diff", "--binary"], cwd=SOURCE)).hexdigest()


def ledger_rows() -> list[dict[str, Any]]:
    with sqlite3.connect(LEDGER) as con:
        rows = con.execute(
            "select reservation_id,call_id,amount_cny,actual_cny,status,returned_model,finish_reason,request_id,raw_response_sha256,usage_json,request_metadata_json from reservations order by rowid"
        ).fetchall()
    names = ("reservation_id", "call_id", "reserved_cny", "actual_cny", "status", "returned_model", "finish_reason", "request_id", "raw_response_sha256", "usage_json", "request_metadata_json")
    return [dict(zip(names, row)) for row in rows]


def spend(rows: list[dict[str, Any]], baseline_ids: set[str]) -> dict[str, Any]:
    selected = [row for row in rows if str(row.get("reservation_id")) not in baseline_ids]
    actual = sum(float(row.get("actual_cny") or 0) for row in selected)
    held = sum(float(row.get("reserved_cny") or 0) for row in selected if row.get("status") in {"reserved", "uncertain"})
    return {"rows": selected, "actual_cny": actual, "held_cny": held, "occupied_cny": actual + held}


class AuditedClient:
    """Write the exact request and round guard before every real invoke."""

    def __init__(self, inner: Any, *, role: str, profile: dict[str, Any], counter: Any, round_ids: set[str], out: Path):
        self.inner = inner
        self.role = role
        self.profile = profile
        self.counter = counter
        self.round_ids = round_ids
        self.out = out
        self.sequence = 0

    def complete(self, messages: Any, **kwargs: Any) -> dict[str, Any]:
        from optomind_research.runtime.upgrade3 import outline_strengthening as strengthening

        estimate = strengthening.estimate_strengthening_request(messages, profile=self.profile, token_counter=self.counter)
        before = spend(ledger_rows(), self.round_ids)
        projected = before["occupied_cny"] + float(estimate.get("estimated_cost_cny") or 0)
        if projected > ROUND_LIMIT + 1e-9:
            raise RuntimeError(f"round_budget_guard:{self.role}:{projected}")
        self.sequence += 1
        stem = f"{self.sequence:02d}_{self.role}"
        guard_dir = self.out / "CALL_GUARDS"
        precall = {
            "state": "precall", "role": self.role, "messages": clone(messages),
            "messages_sha256": digest(messages), "profile": clone(self.profile),
            "invoke_kwargs": {str(k): v for k, v in kwargs.items() if k not in {"client", "key_file"}},
            "estimate": estimate, "round_before": before, "round_limit_cny": ROUND_LIMIT,
            "source_head": source_head(), "source_diff_sha256": source_diff_sha(), "created_at_epoch": time.time(),
        }
        dump(guard_dir / f"{stem}_PRECALL.json", precall)
        try:
            result = self.inner(messages, **kwargs)
        except Exception as exc:
            dump(guard_dir / f"{stem}_EXCEPTION.json", {"role": self.role, "error_type": type(exc).__name__, "error": str(exc), "precall_path": str(guard_dir / f"{stem}_PRECALL.json")})
            raise
        after = spend(ledger_rows(), self.round_ids)
        dump(guard_dir / f"{stem}_POSTCALL.json", {
            "state": "postcall", "role": self.role, "messages_sha256": digest(messages),
            "estimate": estimate, "round_after": after,
            "returned_model": result.get("returned_model") if isinstance(result, dict) else None,
            "finish_reason": result.get("finish_reason") if isinstance(result, dict) else None,
            "usage": result.get("usage") if isinstance(result, dict) else None, "created_at_epoch": time.time(),
        })
        return result


def snapshot_source(out: Path) -> dict[str, Any]:
    allowlist = [
        "optomind_research/runtime/upgrade3/outline_selection.py",
        "optomind_research/runtime/upgrade3/outline_on_demand.py",
        "optomind_research/runtime/upgrade3/outline_strengthening.py",
        "scripts/upgrade3/outline_strengthening.py",
        "config/outline_revision/quality.json",
        "tests/upgrade3/test_outline_selection.py",
        "tests/upgrade3/test_outline_strengthening.py",
    ]
    entries = []
    for relative in allowlist:
        source = SOURCE / relative
        if not source.is_file():
            raise SystemExit(f"source_snapshot_file_missing:{relative}")
        target = out / "source_snapshot" / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        entries.append({"path": relative, "sha256": hashlib.sha256(source.read_bytes()).hexdigest(), "bytes": source.stat().st_size})
    manifest = {"source_head": source_head(), "source_diff_sha256": source_diff_sha(), "allowlist": allowlist, "files": entries, "includes_untracked_selection_module": True, "no_credentials_or_budget_db": True}
    dump(out / "SOURCE_SNAPSHOT_MANIFEST.json", manifest)
    return manifest


def selected_group(group_id: str) -> tuple[dict[str, Any], dict[str, Any]]:
    result = load(SELECTOR_RESULT)
    for group in result.get("groups") or []:
        if str(group.get("group_id")) == group_id:
            return result, group
    raise SystemExit(f"selector_group_not_found:{group_id}")


def selected_group_projection(group_ids: list[str], chapter_id: str | None = None) -> tuple[dict[str, Any], dict[str, Any], list[dict[str, Any]]]:
    """Return one group projection, optionally merging selector groups locally."""

    if not group_ids:
        raise SystemExit("selector_group_ids_required")
    results = []
    groups = []
    for group_id in group_ids:
        result, group = selected_group(group_id)
        results.append(result)
        groups.append(group)
    if len(groups) == 1:
        group = clone(groups[0])
        if chapter_id:
            if str(chapter_id) not in {str(value) for value in group.get("chapter_ids") or []}:
                raise SystemExit(f"selector_group_chapter_not_found:{group_ids[0]}:{chapter_id}")
            # Keep all stable IDs from the source group; the projection uses
            # the complete payload to retain only this chapter's editable IDs.
            group["chapter_ids"] = [str(chapter_id)]
        return results[0], group, []
    if not chapter_id:
        raise SystemExit("merged_selector_groups_require_chapter")
    merged_units = []
    related = []
    contexts = []
    for group_id, group in zip(group_ids, groups):
        contexts.append({
            "group_id": str(group.get("group_id") or group_id),
            "model_group_id": str(group.get("model_group_id") or ""),
            "chapter_ids": clone(group.get("chapter_ids") or []),
            "unit_ids": clone(group.get("unit_ids") or []),
            "selection_reason": str(group.get("selection_reason") or ""),
            "improvement_focus": clone(group.get("improvement_focus") or []),
            "related_read_only_unit_ids": clone(group.get("related_read_only_unit_ids") or []),
        })
        for unit_id in group.get("unit_ids") or []:
            # Keep all stable IDs from both source groups.  The complete
            # payload projection resolves the target chapter and drops the
            # other chapter-local IDs without guessing from ID spelling.
            if str(unit_id) not in merged_units:
                merged_units.append(str(unit_id))
        related.extend(str(value) for value in group.get("related_read_only_unit_ids") or [])
    merged_id = "selection-merged-" + "-".join(str(group.get("group_id") or group_id) for group_id, group in zip(group_ids, groups))
    merged = {
        "group_id": merged_id,
        "model_group_id": "merged:" + "+".join(str(group.get("model_group_id") or group_id) for group_id, group in zip(group_ids, groups)),
        "chapter_ids": [str(chapter_id)],
        "unit_ids": list(dict.fromkeys(merged_units)),
        "selection_reason": "\n\n".join(item["selection_reason"] for item in contexts if item["selection_reason"]),
        "improvement_focus": list(dict.fromkeys(item for context in contexts for item in context["improvement_focus"])),
        "related_read_only_unit_ids": list(dict.fromkeys(related)),
        "source_group_contexts": contexts,
    }
    merged_result = {
        "schema_version": "merged_selector_projection.v1",
        "status": "selected",
        "source_group_ids": list(group_ids),
        "groups": contexts,
    }
    return merged_result, merged, contexts


def base_payloads() -> tuple[dict[str, Any], str]:
    path = BASE_PAYLOAD_OVERRIDE or (PREVIOUS_CURRENT if PREVIOUS_CURRENT.is_file() else FULL_PAYLOAD)
    if not path.is_file():
        raise SystemExit(f"base_payload_missing:{path}")
    return load(path), str(path)


def projected_payloads(current: dict[str, Any], group: dict[str, Any]) -> list[dict[str, Any]]:
    from optomind_research.runtime.upgrade3 import outline_selection as selection
    rows = selection.project_selection_group(current, group)
    contexts = group.get("source_group_contexts") or []
    if contexts:
        for row in rows:
            selection_context = row["payload"].setdefault("selection_context", {})
            selection_context["source_group_contexts"] = clone(contexts)
            selection_context["merged_group"] = True
    return rows


def profiles_and_counter() -> tuple[dict[str, Any], dict[str, Any], Any]:
    from optomind_research.runtime.upgrade3.quality_capacity import load_quality_profile
    from optomind_research.runtime.upgrade3 import progressive_review_plan as planning
    access = load_quality_profile("autonomous_outline")
    owner = load_quality_profile("owner_revision")
    counter = planning.qwen_local_token_counter(TOKENIZER) if TOKENIZER.is_file() else None
    return access, owner, counter


def prepare(group_ids: list[str], out: Path, merge_chapter: str | None = None) -> dict[str, Any]:
    if source_head() != EXPECTED_HEAD:
        raise SystemExit("source_head_changed_since_selector")
    for path in (KEY, LEDGER, ROUND_BASELINE, FULL_PAYLOAD, SELECTOR_RESULT):
        if not path.is_file():
            raise SystemExit(f"required_file_missing:{path}")
    if str(SOURCE) not in sys.path:
        sys.path.insert(0, str(SOURCE))
    from optomind_research.runtime.upgrade3 import outline_on_demand as on_demand
    from optomind_research.runtime.upgrade3 import outline_strengthening as strengthening

    result, group, source_group_contexts = selected_group_projection(group_ids, merge_chapter)
    current, base_path = base_payloads()
    projected = projected_payloads(current, group)
    if not projected:
        raise SystemExit("selector_group_has_no_projected_chapters")
    access_profile, owner_profile, counter = profiles_and_counter()
    chapter_reports = []
    for row in projected:
        chapter_id = str(row["chapter_id"])
        chapter_out = out / chapter_id
        payload = row["payload"]
        catalog = on_demand.build_material_catalog(payload)
        access_messages = on_demand.access_messages(payload, catalog)
        upper_trace = on_demand.full_catalog_trace(catalog)
        owner_messages = on_demand.owner_messages(payload, catalog, upper_trace)
        access_estimate = strengthening.estimate_strengthening_request(access_messages, profile=access_profile, token_counter=counter)
        owner_estimate = strengthening.estimate_strengthening_request(owner_messages, profile=owner_profile, token_counter=counter)
        dump(chapter_out / "INPUT_PAYLOAD.json", payload)
        dump(chapter_out / "CATALOG.json", on_demand.public_catalog(catalog))
        dump(chapter_out / "ACCESS_MESSAGES.json", {"messages": access_messages, "sha256": digest(access_messages), "profile": access_profile})
        dump(chapter_out / "OWNER_UPPER_BOUND_MESSAGES.json", {"messages": owner_messages, "sha256": digest(owner_messages), "profile": owner_profile, "trace": upper_trace})
        chapter_reports.append({
            "chapter_id": chapter_id, "modifiable_unit_ids": row["modifiable_unit_ids"],
            "read_only_unit_count": len(payload.get("read_only_unit_ids") or []),
            "payload_sha256": digest(payload), "access_messages_sha256": digest(access_messages),
            "owner_upper_bound_messages_sha256": digest(owner_messages), "access_estimate": access_estimate,
            "owner_upper_bound_estimate": owner_estimate, "owner_upper_bound_record_count": len(upper_trace.get("selected_materials") or []),
            "full_unit_order": (payload.get("full_chapter_context") or {}).get("full_unit_order"),
        })
    out.mkdir(parents=True, exist_ok=True)
    snapshot_source(out)
    report = {
        "status": "prepared_no_paid_calls", "group_id": str(group.get("group_id")),
        "source_group_ids": list(group_ids), "merge_chapter": merge_chapter,
        "logical_group_count": 4, "projected_request_count": len(projected),
        "chapter_reports": chapter_reports, "base_payload_path": base_path,
        "selector_result_sha256": digest(result), "source_head": source_head(), "source_diff_sha256": source_diff_sha(),
        "round_baseline_path": str(ROUND_BASELINE), "round_limit_cny": ROUND_LIMIT, "shared_limit_cny": SHARED_LIMIT,
        "no_root_quality_review_in_input": True, "no_prior_owner_answer_in_input": True, "downstream_owner_calls": 0,
    }
    dump(out / "PREPARE_REPORT.json", report)
    print(json.dumps(report, ensure_ascii=False))
    return report


def merge_result(current: dict[str, Any], payload: dict[str, Any], result: dict[str, Any], out: Path) -> dict[str, Any]:
    from optomind_research.runtime.upgrade3 import outline_strengthening as strengthening
    if result.get("status") not in {"updated", "no_change"}:
        return current
    latest = strengthening.merge_owner_result_into_chapter_payloads(current, payload, result)
    original_ids = [str(row.get("unit_id") or row.get("id")) for chapter in current.values() for row in (chapter.get("chapter_plan") or {}).get("units") or [] if isinstance(row, dict)]
    merged_ids = [str(row.get("unit_id") or row.get("id")) for chapter in latest.values() for row in (chapter.get("chapter_plan") or {}).get("units") or [] if isinstance(row, dict)]
    if len(merged_ids) != len(set(merged_ids)) or set(merged_ids) < set(original_ids) - set(payload.get("modifiable_unit_ids") or []):
        raise SystemExit("merged_total_plan_unit_integrity_failed")
    dump(out / "CURRENT_CHAPTER_PAYLOADS.json", latest)
    dump(out / "MERGE_INTEGRITY.json", {"status": "verified", "chapter_count": len(latest), "unit_count": len(merged_ids), "base_unit_count": len(original_ids), "complete_total_plan_preserved": True, "editable_unit_ids": payload.get("modifiable_unit_ids") or []})
    return latest


def compatible_saved_result(chapter_out: Path, payload: Mapping[str, Any], chapter_id: str, request_context: Mapping[str, Any]) -> dict[str, Any] | None:
    """Reuse only a result bound to this exact prepared payload.

    An orphaned RESULT is never treated as a cache hit.  The on-demand stage
    checkpoints remain the recovery source, so an incompatible result can be
    audited without silently substituting an older model response.
    """

    result_path = chapter_out / "RESULT.json"
    report_path = chapter_out / "RUN_REPORT.json"
    if not result_path.is_file():
        return None
    if not report_path.is_file():
        raise SystemExit(f"incompatible_saved_result_missing_run_report:{result_path}")
    context_path = chapter_out / "REQUEST_CONTEXT.json"
    if not context_path.is_file():
        raise SystemExit(f"incompatible_saved_result_missing_context:{result_path}")
    result = load(result_path)
    report = load(report_path)
    saved_context = load(context_path)
    context_keys = ("payload_sha256", "access_messages_sha256", "owner_upper_bound_messages_sha256", "access_profile_sha256", "owner_profile_sha256", "source_head", "source_diff_sha256", "base_payload_path")
    if any(saved_context.get(key) != request_context.get(key) for key in context_keys):
        raise SystemExit(f"incompatible_saved_result_context:{result_path}")
    if str(report.get("payload_sha256") or "") != digest(payload):
        raise SystemExit(f"incompatible_saved_result_payload:{result_path}")
    if str(result.get("chapter_id") or "") != chapter_id:
        raise SystemExit(f"incompatible_saved_result_chapter:{result_path}")
    messages = result.get("messages")
    request_sha = str(result.get("request_sha256") or "")
    if not isinstance(messages, list) or request_sha != digest(messages):
        raise SystemExit(f"incompatible_saved_result_request:{result_path}")
    return result


def run(group_ids: list[str], out: Path, merge_chapter: str | None = None) -> dict[str, Any]:
    if not (out / "PREPARE_REPORT.json").is_file():
        prepare(group_ids, out, merge_chapter)
    if str(SOURCE) not in sys.path:
        sys.path.insert(0, str(SOURCE))
    from optomind_research.runtime.upgrade3 import outline_on_demand as on_demand
    from optomind_research.runtime.upgrade3 import outline_strengthening as strengthening
    from optomind_research.runtime.upgrade3.module4 import runtime

    selector_result, group, source_group_contexts = selected_group_projection(group_ids, merge_chapter)
    current, base_path = base_payloads()
    access_profile, owner_profile, counter = profiles_and_counter()
    old_baseline = load(ROUND_BASELINE)
    baseline_ids = {str(value) for value in old_baseline.get("reservation_ids_before") or []}
    with sqlite3.connect(LEDGER) as con:
        metadata = {str(row[0]): str(row[1]) for row in con.execute("select key,value from budget_meta")}
    if float(metadata.get("limit_cny", "nan")) != SHARED_LIMIT:
        raise SystemExit("shared_budget_limit_changed")
    out.mkdir(parents=True, exist_ok=True)
    snapshot_source(out)
    dump(out / "LEDGER_BASELINE.json", {"reservation_ids_before": sorted({str(row.get("reservation_id")) for row in ledger_rows()}), "round_baseline_ids": sorted(baseline_ids), "round_before": spend(ledger_rows(), baseline_ids), "metadata": metadata})
    ledger = runtime.GlobalBudgetLedger(path=LEDGER, limit_cny=SHARED_LIMIT)
    records = []
    chapter_ids = [str(value) for value in group.get("chapter_ids") or []]
    for chapter_id in chapter_ids:
        rows_for_current = projected_payloads(current, group)
        row = next((item for item in rows_for_current if str(item.get("chapter_id")) == chapter_id), None)
        if row is None:
            raise SystemExit(f"selector_group_chapter_projection_missing:{chapter_id}")
        chapter_id = str(row["chapter_id"])
        chapter_out = out / chapter_id
        payload = row["payload"]
        catalog = on_demand.build_material_catalog(payload)
        access_messages = on_demand.access_messages(payload, catalog)
        upper_trace = on_demand.full_catalog_trace(catalog)
        owner_messages = on_demand.owner_messages(payload, catalog, upper_trace)
        access_estimate = strengthening.estimate_strengthening_request(access_messages, profile=access_profile, token_counter=counter)
        owner_estimate = strengthening.estimate_strengthening_request(owner_messages, profile=owner_profile, token_counter=counter)
        request_context = {"group_id": str(group.get("group_id")), "source_group_ids": list(group_ids), "merge_chapter": merge_chapter, "chapter_id": chapter_id, "payload_sha256": digest(payload), "access_messages_sha256": digest(access_messages), "owner_upper_bound_messages_sha256": digest(owner_messages), "access_profile_sha256": digest(access_profile), "owner_profile_sha256": digest(owner_profile), "source_head": source_head(), "source_diff_sha256": source_diff_sha(), "base_payload_path": base_path}
        dump(chapter_out / "REQUEST_CONTEXT.json", request_context)
        dump(chapter_out / "WIRE_REQUEST.json", {"group_id": str(group.get("group_id")), "source_group_ids": list(group_ids), "merge_chapter": merge_chapter, "chapter_id": chapter_id, "access_messages": access_messages, "owner_upper_bound_messages": owner_messages, "access_profile": access_profile, "owner_profile": owner_profile, "access_estimate": access_estimate, "owner_upper_bound_estimate": owner_estimate, "round_before": spend(ledger_rows(), baseline_ids), "max_retries": 0, "source_head": source_head(), "source_diff_sha256": source_diff_sha(), "payload_sha256": digest(payload)})
        saved = compatible_saved_result(chapter_out, payload, chapter_id, request_context)
        if saved is not None:
            result = saved
        else:
            access_client = AuditedClient(strengthening.make_strengthening_client(role="autonomous_outline", key_file=KEY, budget_ledger=ledger, raw_response_dir=chapter_out / "access_raw_responses", prompt_token_counter=counter), role="autonomous_outline", profile=access_profile, counter=counter, round_ids=baseline_ids, out=chapter_out)
            owner_client = AuditedClient(strengthening.make_strengthening_client(role="owner_revision", key_file=KEY, budget_ledger=ledger, raw_response_dir=chapter_out / "owner_raw_responses", prompt_token_counter=counter), role="owner_revision", profile=owner_profile, counter=counter, round_ids=baseline_ids, out=chapter_out)
            result = on_demand.run_on_demand_strengthening(
                payload, access_client=access_client, access_model=access_profile["model"], access_thinking_budget=access_profile["thinking_budget"], access_max_output_tokens=access_profile["max_output_tokens"],
                owner_client=owner_client, owner_model=owner_profile["model"], owner_thinking_budget=owner_profile["thinking_budget"], owner_max_output_tokens=owner_profile["max_output_tokens"],
                access_call_id=f"outline-selection:{group.get('group_id')}:{chapter_id}:access", owner_call_id=f"outline-selection:{group.get('group_id')}:{chapter_id}:owner", checkpoint_dir=chapter_out / "stages", resume=True, access_profile=access_profile, owner_profile=owner_profile,
            )
            dump(chapter_out / "RESULT.json", result)
            detail = result.get("on_demand_strengthening") or {}
            dump(chapter_out / "EFFECTIVE_REQUESTS.json", {"access": detail.get("access"), "owner_initial_messages": (detail.get("owner_initial") or {}).get("messages"), "owner_continuation_messages": (detail.get("owner_continuation") or {}).get("messages") if detail.get("owner_continuation") else None})
        current = merge_result(current, payload, result, out)
        after = spend(ledger_rows(), baseline_ids)
        chapter_report = {"chapter_id": chapter_id, "status": result.get("status"), "result_path": str(chapter_out / "RESULT.json"), "request_context_path": str(chapter_out / "REQUEST_CONTEXT.json"), "round_after": after, "new_round_actual_cny": after["actual_cny"], "new_round_held_cny": after["held_cny"], "owner_call_count": (result.get("on_demand_strengthening") or {}).get("owner_call_count"), "payload_sha256": digest(payload), "access_messages_sha256": digest(access_messages), "owner_upper_bound_messages_sha256": digest(owner_messages), "access_profile_sha256": digest(access_profile), "owner_profile_sha256": digest(owner_profile), "source_head": source_head(), "source_diff_sha256": source_diff_sha()}
        dump(chapter_out / "RUN_REPORT.json", chapter_report)
        records.append(chapter_report)
    final_spend = spend(ledger_rows(), baseline_ids)
    report = {"status": "completed_group", "group_id": str(group.get("group_id")), "source_group_ids": list(group_ids), "merge_chapter": merge_chapter, "base_payload_path": base_path, "chapter_records": records, "round_after": final_spend, "round_remaining_cny": ROUND_LIMIT - final_spend["occupied_cny"], "selector_result_sha256": digest(selector_result), "source_head": source_head(), "source_diff_sha256": source_diff_sha(), "no_next_group_started": True}
    dump(out / "RUN_REPORT.json", report)
    print(json.dumps(report, ensure_ascii=False))
    return report


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--group-id", action="append", required=True)
    parser.add_argument("--merge-chapter")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--base-payload", type=Path)
    parser.add_argument("--run", action="store_true")
    args = parser.parse_args()
    global BASE_PAYLOAD_OVERRIDE
    BASE_PAYLOAD_OVERRIDE = args.base_payload
    output_suffix = "_".join(args.group_id)
    if args.merge_chapter:
        output_suffix += f"_merged_{args.merge_chapter}"
    out = args.output or (ROOT / f"on_demand_{output_suffix}")
    if str(SOURCE) not in sys.path:
        sys.path.insert(0, str(SOURCE))
    if not (out / "PREPARE_REPORT.json").is_file():
        prepare(args.group_id, out, args.merge_chapter)
    if args.run:
        run(args.group_id, out, args.merge_chapter)


if __name__ == "__main__":
    main()
