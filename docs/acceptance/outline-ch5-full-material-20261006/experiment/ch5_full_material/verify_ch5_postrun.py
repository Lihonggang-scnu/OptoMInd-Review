"""Offline provenance and semantic-diff checks for the completed Ch5 call."""
from __future__ import annotations

import hashlib
import json
import sys
import difflib
from pathlib import Path
from typing import Any

SOURCE = Path(r"F:\OptoMind-branch-backups\20261005-final-consolidation-204632\source")
ROOT = Path(r"F:\OptoMind-Review-2\outputs\outline_full_strengthening_20261006_40cny\ch5_full_material")
PRIOR = Path(r"F:\OptoMind-Review-2\outputs\outline_autonomous_cost_20261006\max_dedup_attempt1\REQUEST.json")


def load(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")).hexdigest()


def message_payload(message: dict[str, Any]) -> tuple[dict[str, Any], str, str]:
    content = str(message.get("content") or "")
    start = content.find("{")
    if start < 0:
        raise ValueError("message_payload_json_missing")
    payload, consumed = json.JSONDecoder().raw_decode(content[start:])
    return payload, content[:start], content[start + consumed:]


def append_diff(out: list[dict[str, Any]], kind: str, path: str, old: Any = None, new: Any = None) -> None:
    row = {"kind": kind, "path": path}
    if kind in {"removed", "changed"}:
        row["old"] = old
    if kind in {"added", "changed"}:
        row["new"] = new
    out.append(row)


def recursive_diff(old: Any, new: Any, path: str = "$") -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    if isinstance(old, dict) and isinstance(new, dict):
        for key in old.keys() - new.keys():
            append_diff(out, "removed", f"{path}.{key}", old[key])
        for key in new.keys() - old.keys():
            append_diff(out, "added", f"{path}.{key}", new[key])
        for key in old.keys() & new.keys():
            out.extend(recursive_diff(old[key], new[key], f"{path}.{key}"))
        return out
    if isinstance(old, list) and isinstance(new, list):
        keyed = all(isinstance(row, dict) and (row.get("unit_id") or row.get("paragraph_id")) for row in [*old, *new]) if old or new else False
        if keyed:
            def key(row: dict[str, Any]) -> str:
                return str(row.get("unit_id") or row.get("paragraph_id"))
            old_map, new_map = {key(row): row for row in old}, {key(row): row for row in new}
            for item_id in old_map.keys() - new_map.keys():
                append_diff(out, "removed", f"{path}[{item_id}]", old_map[item_id])
            for item_id in new_map.keys() - old_map.keys():
                append_diff(out, "added", f"{path}[{item_id}]", new_map[item_id])
            for item_id in old_map.keys() & new_map.keys():
                out.extend(recursive_diff(old_map[item_id], new_map[item_id], f"{path}[{item_id}]"))
            return out
        if len(old) != len(new):
            append_diff(out, "changed", f"{path}.length", len(old), len(new))
        for index in range(min(len(old), len(new))):
            out.extend(recursive_diff(old[index], new[index], f"{path}[{index}]"))
        for index in range(len(new), len(old)):
            append_diff(out, "removed", f"{path}[{index}]", old[index])
        for index in range(len(old), len(new)):
            append_diff(out, "added", f"{path}[{index}]", new[index])
        return out
    if old != new:
        append_diff(out, "changed", path, old, new)
    return out


def main() -> int:
    if str(SOURCE) not in sys.path:
        sys.path.insert(0, str(SOURCE))
    from optomind_research.runtime.upgrade3 import progressive_review_plan as planning

    payload = load(ROOT / "INPUT_PAYLOAD.json")
    prepared = load(ROOT / "FULL_MAX_MESSAGES.json")
    result = load(ROOT / "RESULT.json")
    raw_file = load(ROOT / "RAW_RESPONSE.json")
    wire = load(ROOT / "WIRE_REQUEST.json")
    messages = prepared["messages"]
    actual_messages = wire["messages"]
    actual_payload, current_prefix, current_suffix = message_payload(messages[1])
    raw_embedded = result.get("raw_response") or {}
    parsed_from_raw, telemetry_from_raw = planning._parse_planner_response(raw_file)
    parsed_embedded = result.get("parsed_response") or {}
    parsed_plan = ((parsed_from_raw.get("chapter_updates") or [{}])[0]).get("updated_plan")
    result_plan = result.get("updated_plan")

    key_names = ["chapter_plan", "source_materials", "candidate_materials", "tool_materials", "actual_local_body", "readonly_neighbor_unit_roles", "source_identity_map"]
    payload_equality = {key: actual_payload.get(key) == payload.get(key) for key in key_names}
    material_counts = {key: len(actual_payload.get(key) or []) for key in ["source_materials", "candidate_materials", "tool_materials"]}
    result_raw_equal = raw_embedded == raw_file
    parsed_equal = parsed_embedded == parsed_from_raw
    plan_equal = parsed_plan == result_plan
    message_equal = actual_messages == messages

    prior_comparison = {"available": PRIOR.is_file()}
    if PRIOR.is_file():
        prior_request = load(PRIOR)
        prior_messages = prior_request.get("messages") or []
        prior_system = str(prior_messages[0].get("content") or "") if prior_messages else ""
        prior_payload, prior_prefix, prior_suffix = message_payload(prior_messages[1]) if len(prior_messages) > 1 else ({}, "", "")
        prior_comparison.update({
            "path": str(PRIOR),
            "system_equal": str(messages[0].get("content") or "") == prior_system,
            "current_system_sha256": digest(str(messages[0].get("content") or "")),
            "prior_system_sha256": digest(prior_system),
            "current_system_chars": len(str(messages[0].get("content") or "")),
            "prior_system_chars": len(prior_system),
            "user_prefix_equal": current_prefix == prior_prefix,
            "user_suffix_equal": current_suffix == prior_suffix,
            "current_user_prefix_sha256": digest(current_prefix),
            "prior_user_prefix_sha256": digest(prior_prefix),
            "current_user_suffix_sha256": digest(current_suffix),
            "prior_user_suffix_sha256": digest(prior_suffix),
            "current_request_sha256": digest(messages),
            "prior_request_sha256": str(prior_request.get("request_sha256") or digest(prior_messages)),
            "current_profile": prepared.get("profile"),
            "prior_profile": prior_request.get("profile"),
            "prior_payload_material_counts": {key: len(prior_payload.get(key) or []) for key in ["source_materials", "candidate_materials", "tool_materials"]},
            "contract_marker_counts": {
                marker: {"current": sum(str(m.get("content") or "").count(marker) for m in messages), "prior": sum(str(m.get("content") or "").count(marker) for m in prior_messages)}
                for marker in ["自主范围与返回合同", "按需材料读取合同", "updated_plan", "review_targets", "expected_answers"]
            },
        })
        interface_diff = []
        interface_diff.extend(difflib.unified_diff(
            prior_system.splitlines(True), str(messages[0].get("content") or "").splitlines(True),
            fromfile="prior_system", tofile="current_system",
        ))
        interface_diff.extend(difflib.unified_diff(
            prior_suffix.splitlines(True), current_suffix.splitlines(True),
            fromfile="prior_user_suffix", tofile="current_user_suffix",
        ))
        (ROOT / "CH5_INTERFACE_TEXT_DIFF.txt").write_text("".join(interface_diff), encoding="utf-8")
        prior_comparison["text_diff_path"] = str(ROOT / "CH5_INTERFACE_TEXT_DIFF.txt")

    diff = recursive_diff(payload.get("chapter_plan") or {}, result_plan or {})
    semantic = {
        "source_input_plan_sha256": digest(payload.get("chapter_plan") or {}),
        "returned_updated_plan_sha256": digest(result_plan or {}),
        "diff_count": len(diff),
        "removed_count": sum(row["kind"] == "removed" for row in diff),
        "added_count": sum(row["kind"] == "added" for row in diff),
        "changed_count": sum(row["kind"] == "changed" for row in diff),
        "diff": diff,
    }
    provenance = {
        "status": "verified_offline",
        "request_sha256": prepared.get("sha256"),
        "wire_messages_equal_prepared": message_equal,
        "actual_user_payload_key_equality": payload_equality,
        "actual_material_counts": material_counts,
        "actual_full_material_payload": {
            "source_materials": len(actual_payload.get("source_materials") or []),
            "candidate_materials": len(actual_payload.get("candidate_materials") or []),
            "tool_materials": len(actual_payload.get("tool_materials") or []),
            "source_identity_map": len(actual_payload.get("source_identity_map") or {}),
            "actual_local_body_chars": len(str(actual_payload.get("actual_local_body") or "")),
        },
        "result_raw_equals_independent_raw_file": result_raw_equal,
        "result_parsed_equals_reparsed_raw": parsed_equal,
        "result_updated_plan_equals_reparsed_plan": plan_equal,
        "raw_effective_request": raw_file.get("effective_request"),
        "raw_usage": raw_file.get("usage"),
        "raw_cached_tokens": ((raw_file.get("usage") or {}).get("prompt_tokens_details") or {}).get("cached_tokens", 0),
        "no_postprocess_replacement": result_raw_equal and parsed_equal and plan_equal,
        "semantic_diff_path": str(ROOT / "CH5_PLAN_SEMANTIC_DIFF.json"),
        "interface_comparison_path": str(ROOT / "CH5_INTERFACE_COMPARISON.json"),
    }
    (ROOT / "CH5_PLAN_SEMANTIC_DIFF.json").write_text(json.dumps(semantic, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (ROOT / "CH5_INTERFACE_COMPARISON.json").write_text(json.dumps(prior_comparison, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (ROOT / "CH5_RESULT_PROVENANCE.json").write_text(json.dumps(provenance, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"provenance": provenance, "semantic_counts": {key: semantic[key] for key in ["diff_count", "removed_count", "added_count", "changed_count"]}, "interface": prior_comparison}, ensure_ascii=False))


if __name__ == "__main__":
    main()
