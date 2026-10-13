"""Preview the real planning/arrangement CLI; explicit --run enables the gates.

Do not launch until root has reviewed this command and A/B is ready. This
driver has no automatic gate approval and never invokes BODY.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys

from planning_transport import PlanningTransport, DEFAULT_BINDING, load_binding


def _hash_file(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _bridge_receipt(report_path):
    if not report_path or not Path(report_path).is_file():
        raise ValueError("ab_bridge_receipt_required")
    report = json.loads(Path(report_path).read_text(encoding="utf-8"))
    if not isinstance(report, dict):
        raise ValueError("ab_bridge_receipt_shape")
    # run_ab persists an outer run report; bridge_batch_to_pool can also
    # persist the actual receipt directly. Neither shape changes readiness.
    receipt = report.get("bridge", report)
    if not isinstance(receipt, dict):
        raise ValueError("ab_bridge_receipt_shape")
    return receipt


def _metadata_exclusion(missing, items, plan_path):
    """Only the native terminal status plus a valid metadata-only snapshot."""
    from optomind_research.runtime.upgrade3.local_materials import PreparedSnapshotProvider
    key = missing.get("work_key")
    item = items.get(key)
    if (not key or not isinstance(item, dict)
            or item.get("work_key") != key
            or missing.get("paper_id") != item.get("paper_id")
            or missing.get("status") != "insufficient_material"
            or item.get("status") != "insufficient_material"
            or missing.get("category") != "metadata_only_or_insufficient_material"
            or missing.get("reason") != "batch_item_not_in_success_index"):
        raise ValueError("ab_unresolved_item:" + str(key))
    snapshot_dir = Path(item["snapshot_dir"])
    if (not item.get("manifest_sha256")
            or _hash_file(snapshot_dir / "DOCUMENT_MANIFEST.json") != item["manifest_sha256"]
            or Path(item["plan_path"]).resolve() != Path(plan_path).resolve()
            or not item.get("plan_sha256") or _hash_file(item["plan_path"]) != item["plan_sha256"]):
        raise ValueError("ab_excluded_input_changed:" + key)
    snapshot = PreparedSnapshotProvider(snapshot_dir).load()
    if (snapshot.manifest.get("canonical_paper_id") != item.get("paper_id")
            or snapshot.build_reading_packet().get("material_scope") != "metadata_only"):
        raise ValueError("ab_exclusion_not_metadata_only:" + key)
    return {"work_key": key, "paper_id": item["paper_id"],
            "status": "insufficient_material", "material_scope": "metadata_only",
            "snapshot_dir": str(snapshot_dir.resolve())}


def _verify_bound_file(receipt, path_field, hash_field, expected_path, error_prefix):
    """Bind a local scope receipt to one exact immutable input file."""
    value = receipt.get(path_field)
    expected = Path(expected_path).resolve()
    if not value or Path(value).resolve() != expected:
        raise ValueError(error_prefix + "_path")
    if not expected.is_file() or _hash_file(expected) != receipt.get(hash_field):
        raise ValueError(error_prefix + "_hash")
    return expected


def _failed_exclusion_scope(receipt_path, report_path, report, items, pool_path):
    """Validate an explicit root-approved failed gap without changing state.

    Failed batch items are deliberately separate from metadata-only exclusions.
    The caller must opt in with ``--failed-exclusions``; omitting it keeps the
    historical strict behavior and rejects a bridge that contains failures.
    """
    path = Path(receipt_path).resolve()
    if not path.is_file():
        raise ValueError("ab_failed_exclusions_required")
    try:
        receipt = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError("ab_failed_exclusions_shape") from exc
    if (not isinstance(receipt, dict)
            or receipt.get("schema") != "optomind.optics_ab_failed_exclusions.v1"
            or receipt.get("status") != "approved"):
        raise ValueError("ab_failed_exclusions_not_approved")
    _verify_bound_file(receipt, "bridge_report", "bridge_report_sha256", report_path,
                       "ab_failed_bridge")
    inputs = report.get("input_files") or {}
    state_path = inputs.get("state_path")
    cards_index_path = inputs.get("cards_index_path")
    plan_path = inputs.get("plan_path")
    _verify_bound_file(receipt, "source_state", "source_state_sha256", state_path,
                       "ab_failed_state")
    _verify_bound_file(receipt, "cards_index", "cards_index_sha256", cards_index_path,
                       "ab_failed_cards_index")
    _verify_bound_file(receipt, "plan_path", "plan_sha256", plan_path,
                       "ab_failed_plan")
    _verify_bound_file(receipt, "pool_path", "pool_sha256", pool_path,
                       "ab_failed_pool")
    decision_path = receipt.get("decision_source")
    if (not decision_path or not Path(decision_path).is_file()
            or _hash_file(decision_path) != receipt.get("decision_source_sha256")):
        raise ValueError("ab_failed_decision_hash")
    counts = receipt.get("counts") or {}
    if (counts.get("accepted") != report.get("counts", {}).get("accepted")
            or counts.get("state_items") != report.get("counts", {}).get("state_items")
            or counts.get("pool_rows") != report.get("counts", {}).get("accepted")):
        raise ValueError("ab_failed_counts_changed")
    rows = receipt.get("rows")
    if not isinstance(rows, list) or counts.get("failed_exclusions") != len(rows):
        raise ValueError("ab_failed_rows_shape")
    report_failed = {
        row.get("work_key") for row in report.get("missing", [])
        if row.get("status") == "failed" or row.get("category") == "batch_failure"
    }
    row_keys = [row.get("work_key") for row in rows]
    if (len(set(row_keys)) != len(row_keys)
            or set(row_keys) != report_failed
            or len(report_failed) != counts.get("failed_exclusions")):
        raise ValueError("ab_failed_rows_not_exact")
    normalized = []
    for row in rows:
        key = row.get("work_key")
        item = items.get(key)
        bridge_row = next(item for item in report["missing"] if item.get("work_key") == key)
        if (not isinstance(item, dict)
                or row.get("paper_id") != item.get("paper_id")
                or row.get("paper_id") != bridge_row.get("paper_id")
                or row.get("status") != "failed"
                or row.get("source_batch_status") != "failed"
                or item.get("status") != "failed"
                or row.get("category") != "batch_failure"
                or row.get("reason") != "batch_item_not_in_success_index"
                or row.get("error") != item.get("last_error")
                or row.get("plan_sha256") != item.get("plan_sha256")
                or row.get("manifest_sha256") != item.get("manifest_sha256")):
            raise ValueError("ab_failed_item_changed:" + str(key))
        if item.get("plan_path") and Path(row.get("plan_path", "")).resolve() != Path(item["plan_path"]).resolve():
            raise ValueError("ab_failed_item_plan_changed:" + str(key))
        normalized.append({
            "work_key": key,
            "paper_id": item["paper_id"],
            "status": "failed",
            "error": item.get("last_error"),
        })
    metadata_rows = [
        row for row in report.get("missing", [])
        if row.get("work_key") not in report_failed
    ]
    if counts.get("metadata_exclusions") != len(metadata_rows):
        raise ValueError("ab_metadata_counts_changed")
    return {"path": str(path), "rows": normalized,
            "keys": set(report_failed), "metadata_rows": metadata_rows}


def _completed_pool(report_path, pool_path=None, failed_exclusions_path=None):
    """Validate readiness without changing the bridge's partial/complete claim."""
    report = _bridge_receipt(report_path)
    if (report.get("status") not in {"complete", "partial"} or not report.get("pool_written")
            or report.get("rejected") or report.get("identity_conflicts")
            or report.get("counts", {}).get("accepted", 0) < 1):
        raise ValueError("ab_bridge_not_ready")
    pool_path = pool_path or report["pool_written"]
    if not Path(pool_path).is_file():
        raise ValueError("ab_pool_required")
    if Path(report["pool_written"]).resolve() != Path(pool_path).resolve():
        raise ValueError("ab_pool_identity_mismatch")
    for key in ("state", "cards_index", "plan"):
        inputs = report.get("input_files") or {}
        if not inputs.get(key + "_path") or _hash_file(inputs[key + "_path"]) != inputs.get(key + "_sha256"):
            raise ValueError("ab_bridge_input_changed:" + key)
    state = json.loads(Path(report["input_files"]["state_path"]).read_text(encoding="utf-8"))
    if state.get("manifest_finalized") is not True:
        raise ValueError("ab_manifest_not_finalized")
    items = state.get("items")
    if not isinstance(items, dict) or len(items) != report["counts"]["state_items"]:
        raise ValueError("ab_state_items_changed")
    failed_scope = None
    report_failed = {
        row.get("work_key") for row in report.get("missing", [])
        if row.get("status") == "failed" or row.get("category") == "batch_failure"
    }
    if report_failed:
        if not failed_exclusions_path:
            # Preserve the pre-existing safe default: a failed item can never
            # silently become metadata-only.
            exclusions = [_metadata_exclusion(row, items, report["input_files"]["plan_path"])
                          for row in report.get("missing", [])]
        else:
            failed_scope = _failed_exclusion_scope(
                failed_exclusions_path, report_path, report, items, pool_path)
            exclusions = [_metadata_exclusion(row, items, report["input_files"]["plan_path"])
                          for row in failed_scope["metadata_rows"]]
    else:
        if failed_exclusions_path:
            raise ValueError("ab_failed_exclusions_unexpected")
        exclusions = [_metadata_exclusion(row, items, report["input_files"]["plan_path"])
                      for row in report.get("missing", [])]
    accepted = report["accepted"]
    failed_keys = failed_scope["keys"] if failed_scope else set()
    if (len(accepted) != report["counts"]["accepted"]
            or len(exclusions) + len(failed_keys) != report["counts"]["missing"]):
        raise ValueError("ab_receipt_counts_changed")
    accepted_keys = {row["source_batch_work_key"] for row in accepted}
    excluded_keys = {row["work_key"] for row in exclusions}
    if (len(accepted_keys) != len(accepted) or len(excluded_keys) != len(exclusions)
            or accepted_keys & excluded_keys or accepted_keys & failed_keys
            or excluded_keys & failed_keys
            or accepted_keys | excluded_keys | failed_keys != set(items)):
        raise ValueError("ab_unaccounted_state_items")
    for row in accepted:
        item = items[row["source_batch_work_key"]]
        if (item.get("status") not in {"success", "reused"}
                or item.get("status") != row["source_batch_status"]
                or item.get("paper_id") != row["paper_id"]):
            raise ValueError("ab_accepted_state_changed")
    pool = [json.loads(line) for line in Path(pool_path).read_text(encoding="utf-8").splitlines() if line.strip()]
    if len(pool) != report["counts"]["accepted"]:
        raise ValueError("ab_pool_count_changed")
    expected = {(row["paper_id"], str(Path(row["card_path"]).resolve())) for row in report["accepted"]}
    actual = {(row["paper_id"], str(Path(row["card_path"]).resolve())) for row in pool}
    if actual != expected:
        raise ValueError("ab_pool_members_changed")
    return {**report, "planning_readiness": {"ready": True,
        "accepted_count": len(accepted), "excluded_metadata_only": exclusions,
        "excluded_failed": (failed_scope["rows"] if failed_scope else []),
        "failed_exclusions_path": (failed_scope["path"] if failed_scope else None)}}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("phase", choices=("plan", "arrange"))
    parser.add_argument("--binding", type=Path, default=DEFAULT_BINDING)
    parser.add_argument("--ab-report", type=Path)
    parser.add_argument("--failed-exclusions", type=Path,
                        help="Explicit root-approved receipt for failed batch gaps; omitted means fail closed")
    parser.add_argument("--allow-planning-max", action="store_true")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--preview", action="store_true", help="Default: read-only command preview; never construct clients")
    mode.add_argument("--run", action="store_true", help="Root-reviewed execution, still stops at every request/response gate")
    args, native = parser.parse_known_args(argv)
    if native and native[0] == "--":
        native = native[1:]
    if "--run" in native:
        raise ValueError("native_run_flag_owned_by_wrapper")
    binding, _ = load_binding(args.binding)
    source = str(Path(binding["source_worktree"]).resolve())
    if source not in sys.path:
        sys.path.insert(0, source)
    readiness = None
    readiness_error = None
    if args.phase == "plan":
        from scripts.upgrade3 import progressive_review_plan as cli
        parsed = cli.parser().parse_args(native)
        if parsed.key_file and parsed.key_file.resolve() != Path(binding["key_file"]).resolve():
            raise ValueError("native_key_route_conflict")
        if parsed.budget_ledger and parsed.budget_ledger.resolve() != Path(binding["project_ledger"]).resolve():
            raise ValueError("native_project_ledger_conflict")
        if parsed.budget_limit_cny not in (None, 80.0):
            raise ValueError("native_project_cap_conflict")
        # Existing source parser retains every algorithm parameter and default.
        command = [*native, "--key-file", binding["key_file"], "--budget-ledger", binding["project_ledger"], "--budget-limit-cny", "80"]
        if parsed.recognition_run:
            if parsed.account_ledger and parsed.account_ledger.resolve() != Path(binding["account_ledger"]).resolve():
                raise ValueError("native_account_ledger_conflict")
            if parsed.account_budget_limit not in (None, 300.0) or parsed.key_index not in (None, 1):
                raise ValueError("native_regional_policy_conflict")
            command.extend(["--account-ledger", binding["account_ledger"], "--account-budget-limit", "300", "--key-index", "1"])
        try:
            readiness = _completed_pool(args.ab_report, parsed.pool, args.failed_exclusions)
        except (ValueError, OSError, KeyError):
            if args.run:
                raise
            readiness_error = str(sys.exc_info()[1])
    else:
        from scripts.upgrade3 import chapter_arrangement as cli
        parsed = cli._parser().parse_args(native)
        provided = set(native)
        if "--key-file" in provided and Path(parsed.key_file).resolve() != Path(binding["key_file"]).resolve():
            raise ValueError("native_key_route_conflict")
        if "--budget-ledger" in provided and Path(parsed.budget_ledger).resolve() != Path(binding["project_ledger"]).resolve():
            raise ValueError("native_project_ledger_conflict")
        if parsed.round_cap_cny != 10.0:
            raise ValueError("preserve_authorized_arrangement_round_cap_10")
        command = [*native, "--key-file", binding["key_file"], "--budget-ledger", binding["project_ledger"], "--no-retry"]
        try:
            readiness = _completed_pool(args.ab_report, failed_exclusions_path=args.failed_exclusions)
        except (ValueError, OSError, KeyError):
            if args.run:
                raise
            readiness_error = str(sys.exc_info()[1])
    preview = {"status": "preview_only", "phase": args.phase, "source_commit": binding["source_commit"],
        "native_module": cli.__name__, "native_argv_when_root_runs": [*command, "--run"],
        "allow_planning_max": args.allow_planning_max, "ab_ready": readiness is not None,
        "ab_complete": bool(readiness and readiness["status"] == "complete" and not readiness.get("missing")),
        "ab_bridge_status": readiness["status"] if readiness else None,
        "ab_accepted_count": readiness["planning_readiness"]["accepted_count"] if readiness else 0,
        "ab_excluded_metadata_only": readiness["planning_readiness"]["excluded_metadata_only"] if readiness else [],
        "ab_excluded_failed": readiness["planning_readiness"].get("excluded_failed", []) if readiness else [],
        "failed_exclusions_path": readiness["planning_readiness"].get("failed_exclusions_path") if readiness else None,
        "ab_readiness_error": readiness_error,
        "project_limit_cny": 80.0, "account_limit_cny": 300.0,
        "root_request_response_gates": True, "provider_calls": 0}
    if not args.run:
        # Preview deliberately does not run even the native preflight: it may
        # write products under the active S tree. This path is fully read-only.
        print(json.dumps(preview, ensure_ascii=False, indent=2))
        return 0
    adapter = PlanningTransport(binding_path=args.binding, allow_planning_max=args.allow_planning_max)
    with adapter.activate():
        if args.phase == "plan" and parsed.recognition_run:
            configured = cli.parser().parse_args([*command, "--run"])
            return cli.main([*command, "--run"], regional_client_constructor=adapter.regional_client_constructor(configured))
        return cli.main([*command, "--run"])


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ValueError, OSError, KeyError) as exc:
        print(json.dumps({"error_type": type(exc).__name__, "message": str(exc)}, ensure_ascii=False), file=sys.stderr)
        raise SystemExit(2)
