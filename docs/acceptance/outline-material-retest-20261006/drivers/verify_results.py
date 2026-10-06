"""Offline verification and cost snapshot for the two live stages."""
from __future__ import annotations

import hashlib
import json
import sqlite3
import subprocess
import sys
from pathlib import Path

ROOT = Path(r"F:\OptoMind-branch-backups\20261005-final-consolidation-204632\source")
OUT = Path(r"F:\OptoMind-Review-2\outputs\outline_directory_repair_comparison_20261006")
LEDGER = Path(r"F:\OptoMind-Review-2\outputs\post_body_revision_local_experiment_20261005\live\budget.sqlite")
sys.path.insert(0, str(ROOT))
from optomind_research.runtime.upgrade3 import outline_on_demand as ond  # noqa: E402


def load(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")).hexdigest()


def main() -> None:
    payload = load(OUT / "INPUT_PAYLOAD.json")
    provenance = load(OUT / "INPUT_PROVENANCE.json")
    group1 = load(OUT / "full_material_plus/REQUEST.json")
    group1_result = load(OUT / "full_material_plus/RESULT.json")
    access_request = load(OUT / "semantic_catalog_max/ACCESS_REQUEST.json")
    access = load(OUT / "semantic_catalog_max/ACCESS_RESULT.json")
    trace = load(OUT / "semantic_catalog_max/RESOLVED_TRACE.json")
    owner_request = load(OUT / "semantic_catalog_max/OWNER_REQUEST_FROM_ACCESS.json")
    owner = load(OUT / "semantic_catalog_max/OWNER_RESULT.json")
    arrangement = load(OUT / "semantic_catalog_max/ARRANGEMENT_INPUT.json")
    catalog = ond.build_material_catalog(payload)
    owner_messages = ond.owner_messages(payload, catalog, trace)
    lookup = catalog["_lookup"]
    selected_equal = [item.get("record") == lookup[item.get("access_id")]["record"] for item in trace.get("selected_materials") or []]
    visible = ond._model_visible_payload(ond._owner_payload(payload, catalog, trace), catalog, trace)
    visible_records = [*(visible.get("source_materials") or []), *(visible.get("candidate_materials") or []), *(visible.get("tool_materials") or [])]
    visible_equal = all(any(item.get("record") == record for record in visible_records) for item in trace.get("selected_materials") or [])

    baseline = set(load(OUT / "LIVE_BUDGET_BASELINE.json").get("reservation_ids_before") or [])
    with sqlite3.connect(LEDGER) as con:
        metadata = dict(con.execute("select key,value from budget_meta").fetchall())
        rows = con.execute("select reservation_id,call_id,amount_cny,actual_cny,status,returned_model,finish_reason,usage_json,request_metadata_json from reservations order by rowid").fetchall()
    new_rows = [row for row in rows if row[0] not in baseline]

    def row_record(row):
        return {
            "reservation_id": row[0], "call_id": row[1], "reserved_cny": row[2], "actual_cny": row[3],
            "status": row[4], "returned_model": row[5], "finish_reason": row[6],
            "usage": json.loads(row[7]) if row[7] else None,
            "request_metadata": json.loads(row[8]) if row[8] else None,
        }

    new_records = [row_record(row) for row in new_rows]
    actual = sum(float(row["actual_cny"] or 0) for row in new_records)
    held = sum(float(row["reserved_cny"] or 0) for row in new_records if row["status"] in {"reserved", "uncertain"})
    audit = load(OUT / "SEMANTIC_VISIBILITY_AUDIT.json")
    fingerprints_equal = (
        group1.get("payload_fingerprints") == provenance.get("source_payload_fingerprints")
        and access_request.get("payload_fingerprints") == provenance.get("source_payload_fingerprints")
    )
    files = []
    for path in (
        OUT / "full_material_plus/REQUEST.json", OUT / "full_material_plus/RESULT.json", OUT / "full_material_plus/ARRANGEMENT_INPUT.json",
        OUT / "semantic_catalog_max/ACCESS_WIRE_REQUEST.json", OUT / "semantic_catalog_max/ACCESS_RESULT.json", OUT / "semantic_catalog_max/RESOLVED_TRACE.json",
        OUT / "semantic_catalog_max/OWNER_REQUEST_FROM_ACCESS.json", OUT / "semantic_catalog_max/OWNER_RESULT.json", OUT / "semantic_catalog_max/ARRANGEMENT_INPUT.json",
        OUT / "SEMANTIC_VISIBILITY_AUDIT.json",
    ):
        files.append({"path": str(path), "exists": path.is_file(), "sha256": hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None})
    result = {
        "status": "offline_verified_after_paid_stages",
        "source_head_expected": "4d81a772bdecc4da18a84a8bc90298fb1a38f625",
        "source_head_actual": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
        "input_provenance_sha256": provenance.get("source_sha256"),
        "input_payload_counts": {"modifiable": payload.get("modifiable_unit_ids"), "readonly": payload.get("read_only_unit_ids"), "source_materials": len(payload.get("source_materials") or []), "tool_materials": len(payload.get("tool_materials") or [])},
        "input_fingerprints_unchanged": fingerprints_equal,
        "full_plus_message_payload_equivalence": group1.get("messages_user_json_equivalence"),
        "full_plus_result": {"status": group1_result.get("status"), "structural_errors": group1_result.get("structural_errors"), "returned_model": (group1_result.get("raw_response") or {}).get("returned_model"), "finish_reason": (group1_result.get("raw_response") or {}).get("finish_reason")},
        "access_result": {"status": access.get("parsed_response", {}).get("status"), "validation_errors": access.get("validation_errors"), "returned_model": (access.get("raw_response") or {}).get("returned_model"), "finish_reason": (access.get("raw_response") or {}).get("finish_reason"), "selected_count": trace.get("resolved_count"), "selected_access_ids": trace.get("access_ids"), "selected_records_equal_original": all(selected_equal)},
        "access_scope_observation": {"modifiable_unit_ids": payload.get("modifiable_unit_ids"), "readonly_unit_ids": payload.get("read_only_unit_ids"), "returned_request_unit_ids": [unit for row in access.get("parsed_response", {}).get("material_requests", []) for unit in row.get("unit_ids", [])], "manual_scope_correction": False},
        "owner_request_reconstructed_hash_equal": digest(owner_messages) == owner_request.get("request_sha256"),
        "owner_visible_records_equal_resolved_records": visible_equal,
        "owner_result": {"status": owner.get("status"), "structural_errors": owner.get("structural_errors"), "returned_model": (owner.get("raw_response") or {}).get("returned_model"), "finish_reason": (owner.get("raw_response") or {}).get("finish_reason"), "continuation_called": False},
        "arrangement_short_chain": {"unit_ids": [unit.get("unit_id") for unit in arrangement.get("units") or []], "modifiable_ids": payload.get("modifiable_unit_ids"), "unit_count": len(arrangement.get("units") or []), "has_sources": bool(arrangement.get("sources")), "has_source_uses": bool(arrangement.get("source_uses")), "preserves_chapter_tool_materials": bool(arrangement.get("chapter_tool_materials"))},
        "semantic_visibility_summary": {key: value["state_counts"] for key, value in audit["categories"].items()},
        "nested_tool_unique_unrepresented": audit["nested_tool_materials"]["unique_unrepresented_count"],
        "duplicated_read_paths": audit["duplicated_read_paths"],
        "paid_calls": len(new_records), "new_ledger_rows": new_records, "new_actual_cny": actual, "new_held_cny": held, "new_round_limit_cny": 40.0, "shared_limit_cny": float(metadata["limit_cny"]),
        "cache_tokens": {"full_plus": (group1_result.get("raw_response") or {}).get("usage", {}).get("prompt_tokens_details", {}).get("cached_tokens"), "access": (access.get("raw_response") or {}).get("usage", {}).get("prompt_tokens_details", {}).get("cached_tokens"), "max_owner": (owner.get("raw_response") or {}).get("usage", {}).get("prompt_tokens_details", {}).get("cached_tokens")},
        "no_max_continuation": True, "files": files,
    }
    (OUT / "FINAL_VERIFY.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (OUT / "BUDGET_FINAL_SNAPSHOT.json").write_text(json.dumps({"metadata": metadata, "baseline_reservation_count": len(baseline), "new_rows": new_records, "new_actual_cny": actual, "new_held_cny": held, "shared_limit_cny": metadata["limit_cny"], "round_limit_cny": 40.0}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({key: result[key] for key in ("status", "input_fingerprints_unchanged", "full_plus_message_payload_equivalence", "access_result", "owner_request_reconstructed_hash_equal", "owner_visible_records_equal_resolved_records", "owner_result", "arrangement_short_chain", "semantic_visibility_summary", "paid_calls", "new_actual_cny", "new_held_cny", "shared_limit_cny", "cache_tokens")}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
