from __future__ import annotations

import collections
import hashlib
import json
import re
import sqlite3
from pathlib import Path


ROOT = Path(r"F:\OptoMind-Review-2\outputs\guided_body_compact_auto_metadata_20261009_12cny")
LIVE = ROOT / "LIVE"
RECORDS = ROOT / "records"
DB = Path(r"F:\OptoMind-Review-2\outputs\evidence_body_round2_20261008_60cny\new_round2_budget.sqlite")


def load(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def text_sha(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def attempt(stage: str) -> Path:
    return sorted((LIVE / "stages" / stage).glob("*/attempt_001"))[-1]


def stage_audit(stage: str) -> dict:
    path = attempt(stage)
    result = load(path / "RESULT.json")
    usage = load(path / "USAGE.json")
    request = load(path / "REQUEST.json")
    actual = load(path / "ACTUAL_REQUEST.json")
    effective = usage.get("effective_request") or {}
    profile = actual.get("profile") or request.get("effective_profile") or {}
    return {
        "stage_id": stage,
        "kind": result.get("kind"),
        "attempt_dir": str(path),
        "messages_path": str(path / "MESSAGES.json"),
        "request_path": str(path / "REQUEST.json"),
        "actual_request_path": str(path / "ACTUAL_REQUEST.json"),
        "raw_response_path": str(path / "RAW_RESPONSE.json"),
        "usage_path": str(path / "USAGE.json"),
        "result_path": str(path / "RESULT.json"),
        "messages_sha256": sha(path / "MESSAGES.json"),
        "raw_response_sha256": sha(path / "RAW_RESPONSE.json"),
        "result_sha256": sha(path / "RESULT.json"),
        "provider_request_id": usage.get("provider_request_id"),
        "model": usage.get("model"),
        "usage": usage.get("usage"),
        "actual_cost_cny": usage.get("estimated_actual_cost_cny"),
        "profile": {key: profile.get(key) for key in (
            "model", "thinking", "thinking_budget", "max_output_tokens",
            "json_mode", "timeout_seconds", "stream", "stream_overall_timeout_seconds"
        )},
        "effective_request": {key: effective.get(key) for key in (
            "model", "enable_thinking", "thinking_budget", "answer_tokens",
            "total_output_tokens", "max_completion_tokens", "max_tokens",
            "response_format", "stream", "stream_options",
            "stream_inactivity_timeout_seconds", "stream_overall_timeout_seconds"
        ) if key in effective},
        "complete": result.get("complete"),
        "chapter_complete": result.get("chapter_complete"),
        "remaining_count": len(result.get("remaining_content") or []),
        "errors": result.get("errors") or [],
        "transport_complete": result.get("transport_complete"),
    }


full = load(LIVE / "FULL_BODY_RESULT.json")
segments = full["segments"]
stage_calls = [stage_audit(f"author_{index:03d}") for index in range(1, 8)]
metadata_call = stage_audit("metadata_author_001")
completion_call = stage_audit("complete_001")

# The author messages carry the exact accepted prefix sent to each chapter.
prefixes = []
for index, segment in enumerate(segments, 1):
    path = attempt(f"author_{index:03d}")
    payload = json.loads(load(path / "MESSAGES.json")[1]["content"])
    accepted = payload["accepted_body_markdown"]
    expected = "\n\n".join(row["body_markdown"] for row in segments[: index - 1])
    prefixes.append({
        "stage_id": f"author_{index:03d}",
        "chapter_id": segment["chapter_id"],
        "messages_path": str(path / "MESSAGES.json"),
        "accepted_prefix_sha256": text_sha(accepted),
        "expected_prefix_sha256": text_sha(expected),
        "accepted_prefix_chars": len(accepted),
        "expected_prefix_chars": len(expected),
        "exact_prefix_match": accepted == expected,
        "payload_keys": sorted(payload),
        "payload_four_fields": set(payload) == {
            "accepted_body_markdown", "chapter_assignment", "manuscript_guide", "materials"
        },
        "outline_action_recursive_count": sum(
            1 for value in json.dumps(payload, ensure_ascii=False).split('"outline_action"')[:-1]
        ),
        "material_counts": {
            key: len(payload["materials"].get(key, []))
            for key in ("evidence_atoms", "source_identities", "source_aliases", "tool_materials", "source_navigation")
        },
    })

# Verify automatic metadata recovery kept the author body byte-for-byte and
# verify the bounded completion as exact anchor insertions into that body.
author_result = load(attempt("author_001") / "RESULT.json")
metadata_result = load(attempt("metadata_author_001") / "RESULT.json")
completion_result = load(attempt("complete_001") / "RESULT.json")
author_body = author_result["body_markdown"]
metadata_body = metadata_result["body_markdown"]
reconstructed = author_body
anchor_counts = []
for insertion in completion_result.get("insertions", []):
    anchor = insertion["after_anchor"]
    anchor_counts.append({"anchor": anchor, "count_before": reconstructed.count(anchor)})
    reconstructed = reconstructed.replace(anchor, anchor + insertion["text"], 1)
recovery_audit = {
    "author_result_body_sha256": text_sha(author_body),
    "metadata_result_body_sha256": text_sha(metadata_body),
    "metadata_body_byte_identical_to_author": metadata_body == author_body,
    "metadata_result_complete": metadata_result.get("complete"),
    "metadata_effective_profile": metadata_call["effective_request"],
    "metadata_actual_cost_cny": metadata_call["actual_cost_cny"],
    "completion_stage": "complete_001",
    "completion_result_complete": completion_result.get("complete"),
    "completion_untouched_prose_preserved": completion_result.get("untouched_prose_preserved"),
    "completion_insertions_count": len(completion_result.get("insertions", [])),
    "completion_anchor_counts": anchor_counts,
    "completion_reconstructed_body_sha256": text_sha(reconstructed),
    "completion_final_body_sha256": text_sha(completion_result.get("body_markdown", "")),
    "completion_exact_anchor_reconstruction": reconstructed == completion_result.get("body_markdown"),
}

# Source supply and citation identity audit from the actual prompt materials
# and final FULL_BODY.md.  The source_aliases map is the only canonicalization
# used for bracketed handles.
input_path = next((LIVE / "inputs").glob("*/FULL_BODY_INPUT.json"))
book = load(input_path)
aliases = book.get("source_aliases", {})
source_identities = book.get("source_identities", {})
supplied = set()
per_chapter_supply = []
for index in range(1, 8):
    path = attempt(f"author_{index:03d}")
    payload = json.loads(load(path / "MESSAGES.json")[1]["content"])
    materials = payload["materials"]
    handles = set(materials["source_identities"])
    supplied |= handles
    per_chapter_supply.append({
        "chapter_id": f"Ch{index}",
        "source_identities": len(handles),
        "source_navigation": len(materials["source_navigation"]),
        "evidence_atoms": len(materials["evidence_atoms"]),
        "source_aliases": len(materials["source_aliases"]),
        "tool_materials": len(materials["tool_materials"]),
    })

body = (LIVE / "FULL_BODY.md").read_text(encoding="utf-8")
raw_handles = re.findall(r"(?<![A-Za-z0-9_])\[(P[0-9]{4})\]", body)
raw_occurrences = collections.Counter(raw_handles)
canonical = lambda handle: aliases.get(handle, handle)
canonical_occurrences = collections.Counter(canonical(handle) for handle in raw_handles)
cited = set(canonical_occurrences)
known = set(source_identities) | set(aliases)
doi_by_canonical = {
    handle: (source_identities.get(handle, {}).get("doi") or "")
    for handle in source_identities
}
all_citation_rows = []
for handle in sorted(cited):
    row = source_identities.get(handle, {})
    all_citation_rows.append({
        "canonical_handle": handle,
        "doi": row.get("doi"),
        "aliases": row.get("aliases", []),
        "raw_handles": sorted(raw for raw in raw_occurrences if canonical(raw) == handle),
        "occurrences": canonical_occurrences[handle],
        "supplied_in_author_union": handle in supplied,
    })
doi_values = [row.get("doi") for row in source_identities.values() if row.get("doi")]
citation_audit = {
    "body_path": str(LIVE / "FULL_BODY.md"),
    "body_text_sha256": text_sha(body),
    "body_bytes_sha256": sha(LIVE / "FULL_BODY.md"),
    "canonical_source_identity_count": len(source_identities),
    "source_alias_count": len(aliases),
    "source_aliases": aliases,
    "doi_nonempty_count": len(doi_values),
    "unique_doi_count": len(set(doi_values)),
    "semantic_supply_union_count": len(supplied),
    "per_chapter_supply": per_chapter_supply,
    "citation_occurrence_count": len(raw_handles),
    "raw_distinct_citation_handle_count": len(raw_occurrences),
    "canonical_distinct_cited_identity_count": len(cited),
    "unknown_raw_citation_handles": sorted(set(raw_handles) - known),
    "cited_not_supplied": sorted(cited - supplied),
    "supplied_not_cited": sorted(supplied - cited),
    "known_never_supplied": sorted(set(source_identities) - supplied),
    "citation_mapping": all_citation_rows,
}

# Shared-ledger aggregate is read-only and intentionally excludes row detail.
with sqlite3.connect(DB) as db:
    status_rows = db.execute(
        "SELECT status, COUNT(*), COALESCE(SUM(amount_cny),0), COALESCE(SUM(actual_cny),0) "
        "FROM reservations GROUP BY status ORDER BY status"
    ).fetchall()
ledger = {
    "status_aggregate": [
        {"status": row[0], "count": row[1], "reserved_cny": row[2], "actual_cny": row[3]}
        for row in status_rows
    ],
    "settled_cny": sum(float(row[3]) for row in status_rows if row[0] == "settled"),
    "open_cny": sum(float(row[2]) for row in status_rows if row[0] in {"reserved", "uncertain"}),
    "run_actual_cost_cny": sum(float(call["actual_cost_cny"] or 0) for call in stage_calls + [metadata_call, completion_call]),
}

audit = {
    "schema_version": "guided_body_compact_auto_metadata_final_offline_audit.v1",
    "run_id": full["run_id"],
    "status": full["status"],
    "complete": full["complete"],
    "body_complete": full["body_complete"],
    "body_sha256": full["body_sha256"],
    "body_chars": len(body),
    "chapter_ids": full["completed_chapter_ids"],
    "chapter_count": len(segments),
    "paid_dispatch_count": full["paid_dispatch_count"],
    "model_calls": full["model_calls"],
    "author_calls": stage_calls,
    "metadata_call": metadata_call,
    "completion_call": completion_call,
    "prefix_audit": {
        "all_exact": all(row["exact_prefix_match"] for row in prefixes),
        "all_payloads_four_fields": all(row["payload_four_fields"] for row in prefixes),
        "all_outline_action_zero": all(row["outline_action_recursive_count"] == 0 for row in prefixes),
        "requests": prefixes,
    },
    "automatic_recovery_and_completion": recovery_audit,
    "citation_audit": citation_audit,
    "ledger": ledger,
    "semantic_quality_unreviewed": full["semantic_quality_unreviewed"],
    "issues": full["issues"],
}

(RECORDS / "FINAL_OFFLINE_AUDIT.json").write_text(
    json.dumps(audit, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
)
(RECORDS / "FINAL_PREFIX_AUDIT.json").write_text(
    json.dumps(audit["prefix_audit"], ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
)
(RECORDS / "FINAL_CITATION_AUDIT.json").write_text(
    json.dumps(citation_audit, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
)
print(json.dumps({
    "run_id": full["run_id"],
    "complete": full["complete"],
    "dispatches": full["paid_dispatch_count"],
    "actual_cost_cny": ledger["run_actual_cost_cny"],
    "settled_cny": ledger["settled_cny"],
    "open_cny": ledger["open_cny"],
    "prefixes_exact": audit["prefix_audit"]["all_exact"],
    "metadata_body_unchanged": recovery_audit["metadata_body_byte_identical_to_author"],
    "completion_reconstructed": recovery_audit["completion_exact_anchor_reconstruction"],
    "citation_occurrences": citation_audit["citation_occurrence_count"],
    "cited_canonical": citation_audit["canonical_distinct_cited_identity_count"],
    "supplied_union": citation_audit["semantic_supply_union_count"],
    "cited_not_supplied": citation_audit["cited_not_supplied"],
    "supplied_not_cited_count": len(citation_audit["supplied_not_cited"]),
}, ensure_ascii=False, indent=2))
