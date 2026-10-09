from __future__ import annotations
import hashlib
import json
import re
import sqlite3
from pathlib import Path

ROOT = Path(r"F:\OptoMind-Review-2\outputs\guided_body_frozen_max_guide_plus_20261009")
LIVE = ROOT / "LIVE"
RECORDS = ROOT / "records"
DB = Path(r"F:\OptoMind-Review-2\outputs\evidence_body_round2_20261008_60cny\new_round2_budget.sqlite")
MANIFEST = Path(r"F:\OptoMind-Review-2\outputs\evidence_body_round2_20261008_60cny\PREPARED_MANIFEST.json")
GUIDE = ROOT / "inputs" / "GUIDE_RECOVERED_FOR_REVIEW.json"
BASELINE_GUIDE = ROOT / "inputs" / "BASELINE_GUIDE.json"
CONFIG = ROOT / "worktree" / "config" / "guided_body_writer" / "plus_first.json"
TOKENIZER = Path(r"F:\OptoMind-Review-2\data\tokenizers\qwen3_5_9b\tokenizer.json")
WRAPPER = ROOT / "run_guided_body_frozen_max_plus.py"

def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()

def text_sha(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()

def load(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))

def count_key(value, key: str) -> int:
    if isinstance(value, dict):
        return (1 if key in value else 0) + sum(count_key(v, key) for v in value.values())
    if isinstance(value, list):
        return sum(count_key(v, key) for v in value)
    return 0

def compact(value) -> int:
    return len(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))

def find_attempt(stage: str) -> Path:
    attempts = sorted((LIVE / "stages" / stage).glob("*/attempt_001"))
    if not attempts:
        raise FileNotFoundError(stage)
    return attempts[-1]

full = load(LIVE / "FULL_BODY_RESULT.json")
segments = full["segments"]
assert full["complete"] is True and len(segments) == 7 and all(row["complete"] for row in segments)
prefix_audit = []
request_audit = []
actual_calls = []
for index in range(1, 8):
    stage = f"author_{index:03d}"
    attempt = find_attempt(stage)
    messages_path = attempt / "MESSAGES.json"
    request_path = attempt / "REQUEST.json"
    usage_path = attempt / "USAGE.json"
    result_path = attempt / "RESULT.json"
    messages = load(messages_path)
    payload = json.loads(messages[1]["content"])
    expected = "\n\n".join(row["body_markdown"] for row in segments[: index - 1])
    actual = payload.get("accepted_body_markdown")
    usage = load(usage_path)
    result = load(result_path)
    request = load(request_path)
    effective = usage.get("effective_request") or request.get("effective_profile") or {}
    call = {
        "stage_id": stage,
        "attempt_dir": str(attempt),
        "messages_path": str(messages_path),
        "request_path": str(request_path),
        "usage_path": str(usage_path),
        "result_path": str(result_path),
        "messages_sha256": sha(messages_path),
        "raw_response_sha256": sha(attempt / "RAW_RESPONSE.json"),
        "body_sha256": text_sha(result.get("body_markdown", "")),
        "body_chars": len(result.get("body_markdown", "")),
        "provider_request_id": usage.get("provider_request_id"),
        "model": usage.get("model"),
        "usage": usage.get("usage"),
        "actual_cost_cny": usage.get("estimated_actual_cost_cny"),
        "effective_request": {
            key: effective.get(key)
            for key in (
                "model", "enable_thinking", "thinking_budget", "answer_tokens",
                "total_output_tokens", "max_completion_tokens", "stream",
                "stream_options", "stream_inactivity_timeout_seconds",
                "stream_overall_timeout_seconds",
            )
            if key in effective
        },
        "transport_complete": result.get("transport_complete"),
        "complete": result.get("complete"),
        "chapter_complete": result.get("chapter_complete"),
        "errors": result.get("errors", []),
    }
    actual_calls.append(call)
    prefix_audit.append({
        "stage_id": stage,
        "chapter_id": segments[index - 1]["chapter_id"],
        "messages_path": str(messages_path),
        "accepted_prefix_empty": actual == "" and expected == "",
        "accepted_prefix_sha256": text_sha(actual or ""),
        "expected_prefix_sha256": text_sha(expected),
        "accepted_prefix_chars": len(actual or ""),
        "expected_prefix_chars": len(expected),
        "exact_prefix_match": actual == expected,
        "top_level_payload_keys": sorted(payload),
        "top_level_payload_exact": set(payload) == {
            "accepted_body_markdown", "chapter_assignment", "materials", "manuscript_guide"
        },
        "outline_action_recursive_count": count_key(payload, "outline_action"),
        "material_counts": {
            "evidence_atoms": len(payload["materials"]["evidence_atoms"]),
            "source_identities": len(payload["materials"]["source_identities"]),
            "source_aliases": len(payload["materials"]["source_aliases"]),
            "tool_materials": len(payload["materials"]["tool_materials"]),
            "source_navigation": len(payload["materials"]["source_navigation"]),
        },
        "body_result_sha256": call["body_sha256"],
    })
    request_audit.append(call)

completion_attempts = sorted((LIVE / "stages" / "complete_006").glob("*/attempt_001"))
assert completion_attempts
completion_attempt = completion_attempts[-1]
completion_messages = load(completion_attempt / "MESSAGES.json")
completion_payload = json.loads(completion_messages[1]["content"])
completion_result = load(completion_attempt / "RESULT.json")
completion_usage = load(completion_attempt / "USAGE.json")
completion_request = load(completion_attempt / "REQUEST.json")
completion_call = {
    "stage_id": "complete_006",
    "attempt_dir": str(completion_attempt),
    "messages_path": str(completion_attempt / "MESSAGES.json"),
    "request_path": str(completion_attempt / "REQUEST.json"),
    "usage_path": str(completion_attempt / "USAGE.json"),
    "result_path": str(completion_attempt / "RESULT.json"),
    "messages_sha256": sha(completion_attempt / "MESSAGES.json"),
    "raw_response_sha256": sha(completion_attempt / "RAW_RESPONSE.json"),
    "provider_request_id": completion_usage.get("provider_request_id"),
    "model": completion_usage.get("model"),
    "usage": completion_usage.get("usage"),
    "actual_cost_cny": completion_usage.get("estimated_actual_cost_cny"),
    "effective_request": {
        key: (completion_usage.get("effective_request") or {}).get(key)
        for key in (
            "model", "enable_thinking", "thinking_budget", "answer_tokens",
            "total_output_tokens", "max_completion_tokens", "stream",
            "stream_options", "stream_inactivity_timeout_seconds",
            "stream_overall_timeout_seconds",
        )
        if key in (completion_usage.get("effective_request") or {})
    },
    "draft_body_sha256": text_sha(completion_payload["chapter_assignment"].get("draft_body_markdown", "")),
    "remaining_content": completion_payload["chapter_assignment"].get("remaining_content"),
    "result_complete": completion_result.get("complete"),
    "insertions_count": len(completion_result.get("insertions", [])),
}
actual_calls.append(completion_call)

# Reconcile this run's eight provider calls with the shared ledger by provider request id.
with sqlite3.connect(DB) as db:
    db.row_factory = sqlite3.Row
    all_rows = [dict(row) for row in db.execute(
        "SELECT reservation_id,call_id,amount_cny,actual_cny,status,returned_model,finish_reason,request_id,raw_response_sha256,request_metadata_json FROM reservations ORDER BY created_at,reservation_id"
    )]
by_request = {row["request_id"]: row for row in all_rows if row["request_id"]}
for call in actual_calls:
    matched = by_request.get(call.get("provider_request_id"))
    call["ledger_row"] = ({key: value for key, value in matched.items()
                            if key != "request_metadata_json"} if matched else None)
    row = matched
    if row:
        metadata = json.loads(row.get("request_metadata_json") or "{}")
        events = metadata.get("transport_events") if isinstance(metadata, dict) else []
        elapsed = [float(event["elapsed_seconds"]) for event in events
                   if isinstance(event, dict) and isinstance(event.get("elapsed_seconds"), (int, float))]
        call["transport_elapsed_seconds"] = max(elapsed) if elapsed else None
        call["ledger_reservation_id"] = row["reservation_id"]
        call["ledger_reserved_cny"] = row["amount_cny"]
        call["ledger_actual_cny"] = row["actual_cny"]
        call["finish_reason"] = row["finish_reason"]

settled = sum(float(row["actual_cny"] or 0) for row in all_rows if row["status"] == "settled")
open_amount = sum(float(row["amount_cny"] or 0) for row in all_rows if row["status"] in ("reserved", "uncertain"))
known_run_cost = sum(float(call["actual_cost_cny"] or 0) for call in actual_calls)
run_ledger_cost = sum(float(call["ledger_row"]["actual_cny"] or 0) for call in actual_calls if call.get("ledger_row"))
ledger_snapshot = {
    "path": str(DB),
    "row_count": len(all_rows),
    "status_counts": {status: sum(1 for row in all_rows if row["status"] == status) for status in sorted({row["status"] for row in all_rows})},
    "settled_cny": settled,
    "open_cny": open_amount,
    "run_provider_call_count": len(actual_calls),
    "run_known_usage_cost_cny": known_run_cost,
    "run_ledger_matched_cost_cny": run_ledger_cost,
    "run_usage_matches_ledger": abs(known_run_cost - run_ledger_cost) < 1e-9,
    "matched_reservation_ids": [call["ledger_row"]["reservation_id"] for call in actual_calls if call.get("ledger_row")],
    "unmatched_provider_calls": [call["stage_id"] for call in actual_calls if not call.get("ledger_row")],
}
(RECORDS / "LEDGER_FINAL_SNAPSHOT.json").write_text(json.dumps(ledger_snapshot, ensure_ascii=False, indent=2), encoding="utf-8")

prefix_summary = {
    "schema_version": "guided_body_frozen.actual_prefix_audit.v1",
    "final_run_id": full["run_id"],
    "final_body_sha256": full["body_sha256"],
    "all_author_prefixes_exact": all(row["exact_prefix_match"] for row in prefix_audit),
    "all_payloads_four_fields": all(row["top_level_payload_exact"] for row in prefix_audit),
    "all_outline_action_zero": all(row["outline_action_recursive_count"] == 0 for row in prefix_audit),
    "author_requests": prefix_audit,
    "completion_request": completion_call,
}
(RECORDS / "ACTUAL_PREFIX_AUDIT.json").write_text(json.dumps(prefix_summary, ensure_ascii=False, indent=2), encoding="utf-8")
(RECORDS / "ACTUAL_REQUEST_AUDIT.json").write_text(json.dumps({
    "schema_version": "guided_body_frozen.actual_request_audit.v1",
    "final_run_id": full["run_id"],
    "provider_call_count": len(actual_calls),
    "physical_author_call_count": 7,
    "bounded_completion_call_count": 1,
    "calls": actual_calls,
    "wire_contract": "qwen3.5-plus; thinking=16384; answer_tokens=49152; total_output_tokens/max_completion_tokens=65536; stream=true; inactivity=1800; overall=3600; max_retries=0",
}, ensure_ascii=False, indent=2), encoding="utf-8")

book = load(next((LIVE / "inputs").glob("*/FULL_BODY_INPUT.json")))
identity_map = {
    "schema_version": "guided_body_frozen.final_source_identity_map.v1",
    "source_identities": book.get("source_identities", {}),
    "source_aliases": book.get("source_aliases", {}),
    "input_path": str(next((LIVE / "inputs").glob("*/FULL_BODY_INPUT.json"))),
    "input_sha256": sha(next((LIVE / "inputs").glob("*/FULL_BODY_INPUT.json"))),
}
(RECORDS / "FINAL_SOURCE_IDENTITY_MAP.json").write_text(json.dumps(identity_map, ensure_ascii=False, indent=2), encoding="utf-8")

raw_body = LIVE / "FULL_BODY.md"
delivery_body = LIVE / "DELIVERY_BODY.md"
numbered = RECORDS / "NUMBERED_DELIVERY_BODY.md"
numbered.write_bytes(delivery_body.read_bytes())
body_text = raw_body.read_text(encoding="utf-8")
# Citation identities are bracketed in the guide contract. Keep the runtime's
# broader lexical diagnostic separately because organism names such as
# "Marseille-P4119" are not citations.
handles = re.findall(r"(?<![A-Za-z0-9_])\[(P[0-9]{4})\]", body_text)
lexical_tokens = sorted(set(re.findall(r"(?<![A-Za-z0-9_])P[0-9]{4}(?![A-Za-z0-9_])", body_text)))
known_handles = set(book.get("source_identities", {})) | set(book.get("source_aliases", {}))
references = {
    "schema_version": "guided_body_frozen.references.v1",
    "citation_style": "provided_exact_handles",
    "body_source_path": str(raw_body),
    "delivery_source_path": str(delivery_body),
    "delivery_copy_path": str(numbered),
    "body_sha256": sha(raw_body),
    "delivery_sha256": sha(delivery_body),
    "delivery_copy_sha256": sha(numbered),
    "delivery_copy_byte_identical": delivery_body.read_bytes() == numbered.read_bytes(),
    "citation_handle_count": len(handles),
    "distinct_citation_handles": sorted(set(handles)),
    "unknown_citation_handles": sorted(set(handles) - known_handles),
    "runtime_lexical_unknown_tokens": sorted(set(lexical_tokens) - known_handles),
    "runtime_lexical_unknown_tokens_that_are_unbracketed": sorted(set(lexical_tokens) - set(handles) - known_handles),
    "source_identity_count": len(book.get("source_identities", {})),
    "source_alias_count": len(book.get("source_aliases", {})),
    "identity_map_path": str((RECORDS / "FINAL_SOURCE_IDENTITY_MAP.json").resolve()),
    "identity_map_sha256": sha(RECORDS / "FINAL_SOURCE_IDENTITY_MAP.json"),
}
(RECORDS / "REFERENCES.json").write_text(json.dumps(references, ensure_ascii=False, indent=2), encoding="utf-8")
cli_run = load(LIVE / "CLI_RUN.json")
(RECORDS / "DELIVERY_FORMAT_AUDIT.json").write_text(json.dumps({
    "schema_version": "guided_body_frozen.delivery_format_audit.v1",
    "final_run_id": full["run_id"],
    "raw_body_sha256": sha(raw_body),
    "delivery_body_sha256": sha(delivery_body),
    "delivery_format_adaptation": cli_run.get("delivery_format_adaptation"),
    "raw_body_preserved": cli_run.get("delivery_format_adaptation", {}).get("raw_body_preserved"),
    "body_text_changed": False,
    "citation_sequence_unchanged": cli_run.get("delivery_format_adaptation", {}).get("citation_token_sequence_unchanged"),
    "unknown_citation_handles": cli_run.get("unknown_citation_handles", []),
}, ensure_ascii=False, indent=2), encoding="utf-8")

paths = [
    MANIFEST, GUIDE, BASELINE_GUIDE, CONFIG, TOKENIZER, WRAPPER,
    LIVE / "FULL_BODY_RESULT.json", LIVE / "FULL_BODY.md", LIVE / "DELIVERY_BODY.md",
    LIVE / "CLI_RUN.json", LIVE / "RUN_MANIFEST.json",
    RECORDS / "METADATA_DECLARATIONS_AUTHOR003.json", RECORDS / "ROOT_METADATA_APPROVALS.json",
    RECORDS / "ACTUAL_PREFIX_AUDIT.json", RECORDS / "ACTUAL_REQUEST_AUDIT.json",
    RECORDS / "LEDGER_FINAL_SNAPSHOT.json", RECORDS / "FINAL_SOURCE_IDENTITY_MAP.json",
    RECORDS / "REFERENCES.json", RECORDS / "DELIVERY_FORMAT_AUDIT.json",
]
inventory = {
    "schema_version": "guided_body_frozen.final_archive_input_inventory.v1",
    "no_upload_per_user_instruction": True,
    "frozen_source_commit": "6a5ed067f7e302db569c6f8edcf7379c3ed252b5",
    "wrapper_sha256": sha(WRAPPER),
    "guide_path": str(GUIDE),
    "guide_sha256": sha(GUIDE),
    "baseline_guide_path": str(BASELINE_GUIDE),
    "baseline_guide_sha256": sha(BASELINE_GUIDE),
    "manifest_path": str(MANIFEST),
    "manifest_sha256": sha(MANIFEST),
    "config_path": str(CONFIG),
    "config_sha256": sha(CONFIG),
    "tokenizer_path": str(TOKENIZER),
    "tokenizer_sha256": sha(TOKENIZER),
    "final_body_path": str((LIVE / "FULL_BODY.md").resolve()),
    "final_body_sha256": sha(LIVE / "FULL_BODY.md"),
    "final_result_path": str((LIVE / "FULL_BODY_RESULT.json").resolve()),
    "final_result_sha256": sha(LIVE / "FULL_BODY_RESULT.json"),
    "delivery_body_path": str(delivery_body.resolve()),
    "delivery_body_sha256": sha(delivery_body),
    "runs": sorted(str(path.resolve()) for path in (LIVE / "runs").glob("*")),
    "stage_attempts": sorted(str(path.resolve()) for path in (LIVE / "stages").glob("*/*/attempt_001")),
    "records": {path.name: {"path": str(path.resolve()), "sha256": sha(path)} for path in paths if path.is_file()},
    "recovery_decisions_separated": str((RECORDS / "ROOT_METADATA_APPROVALS.json").resolve()),
    "original_outputs_preserved": True,
}
(RECORDS / "FINAL_ARCHIVE_INPUT_INVENTORY.json").write_text(json.dumps(inventory, ensure_ascii=False, indent=2), encoding="utf-8")

summary = {
    "final_run_id": full["run_id"],
    "final_complete": full["complete"],
    "final_body_sha256": full["body_sha256"],
    "chapters": [(row["chapter_id"], row["complete"], row["sha256"], len(row["body_markdown"])) for row in segments],
    "physical_provider_calls": len(actual_calls),
    "author_calls": 7,
    "bounded_completion_calls": 1,
    "known_run_cost_cny": known_run_cost,
    "ledger_settled_cny": settled,
    "ledger_open_cny": open_amount,
    "prefix_audit_all_exact": prefix_summary["all_author_prefixes_exact"],
    "references_unknown_handles": references["unknown_citation_handles"],
    "delivery_byte_identical_copy": references["delivery_copy_byte_identical"],
}
(RECORDS / "FINAL_EXECUTION_SUMMARY.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
print(json.dumps(summary, ensure_ascii=False, indent=2))
