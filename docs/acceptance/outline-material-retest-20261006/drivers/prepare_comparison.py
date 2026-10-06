"""Prepare the two material-visibility comparison requests without model calls.

This driver deliberately imports the production outline and on-demand
constructors.  It only writes offline request, projection, provenance,
semantic-visibility, and conservative-budget records.  A saved access result
is not fabricated: the Max owner request is exported as an unresolved template
and as a separately labelled all-catalog upper-bound estimate.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import sys
from pathlib import Path
from typing import Any, Mapping


SOURCE_ROOT = Path(r"F:\OptoMind-branch-backups\20261005-final-consolidation-204632\source")
OUT = Path(r"F:\OptoMind-Review-2\outputs\outline_directory_repair_comparison_20261006")
BASELINE_REQUEST = Path(
    r"F:\OptoMind-Review-2\outputs\outline_autonomous_cost_20261006\max_dedup_attempt1\REQUEST.json"
)
LEDGER_PATH = Path(
    r"F:\OptoMind-Review-2\outputs\post_body_revision_local_experiment_20261005\live\budget.sqlite"
)
TOKENIZER = Path(r"F:\OptoMind-Review-2\data\tokenizers\qwen3_5_9b\tokenizer.json")
TARGET_SHA = "4d81a772bdecc4da18a84a8bc90298fb1a38f625"


def _copy(value: Any) -> Any:
    return json.loads(json.dumps(value, ensure_ascii=False, default=str))


def _hash(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    ).hexdigest()


def _file_hash(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _dump(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")


def _imports():
    if str(SOURCE_ROOT) not in sys.path:
        sys.path.insert(0, str(SOURCE_ROOT))
    from optomind_research.runtime.upgrade3 import outline_on_demand as on_demand
    from optomind_research.runtime.upgrade3 import outline_strengthening as strengthening
    from optomind_research.runtime.upgrade3 import progressive_review_plan as planning
    from optomind_research.runtime.upgrade3.quality_capacity import load_quality_profile

    return on_demand, strengthening, planning, load_quality_profile


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _message_json(messages: list[dict[str, Any]]) -> dict[str, Any] | None:
    for message in reversed(messages):
        if str(message.get("role") or "") != "user":
            continue
        content = message.get("content")
        if not isinstance(content, str):
            continue
        decoder = json.JSONDecoder()
        start = content.find("{")
        while start >= 0:
            try:
                value, _ = decoder.raw_decode(content[start:])
            except json.JSONDecodeError:
                start = content.find("{", start + 1)
                continue
            if isinstance(value, dict):
                return value
            start = content.find("{", start + 1)
    return None


def _identity(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        key: _copy(row[key])
        for key in ("source_handle", "paper_id", "original_source_handle", "original_paper_id", "need_id", "title", "doi", "study_id")
        if key in row
    }


def _stable_identity(row: Mapping[str, Any]) -> tuple[str, str]:
    for key in ("source_handle", "paper_id", "original_source_handle", "original_paper_id", "need_id", "study_id", "doi"):
        value = str(row.get(key) or "").strip()
        if value:
            return key, value
    return "record_sha256", _hash(row)


def _source_fingerprints(payload: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "chapter_plan": _hash(payload.get("chapter_plan") or {}),
        "source_materials": _hash(payload.get("source_materials") or []),
        "tool_materials": _hash(payload.get("tool_materials") or []),
        "readonly_neighbor_unit_roles": _hash(payload.get("readonly_neighbor_unit_roles") or []),
        "modifiable_unit_ids": _hash(payload.get("modifiable_unit_ids") or []),
        "read_only_unit_ids": _hash(payload.get("read_only_unit_ids") or []),
        "source_identity_map": _hash(payload.get("source_identity_map") or {}),
        "actual_local_body": hashlib.sha256(str(payload.get("actual_local_body") or "").encode("utf-8")).hexdigest(),
    }


def _parse_json_text(value: Any) -> Any:
    if not isinstance(value, str):
        return None
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return None


def _state(raw: Any, visible: Any, locator: Mapping[str, Any] | None) -> str:
    if locator is None:
        return "unavailable"
    # A parent locator may omit a sibling while retaining this complete child;
    # compare the concrete child first instead of inheriting the parent's flag.
    if visible == raw:
        return "complete"
    if visible is None or visible == {} or visible == [] or visible == "":
        return "only_path"
    return "partial"


def _lookup_nested(value: Any, path: list[str]) -> Any:
    current = value
    for part in path:
        if isinstance(current, Mapping):
            if part not in current:
                return None
            current = current[part]
        elif isinstance(current, list) and str(part).isdigit():
            index = int(part)
            if index >= len(current):
                return None
            current = current[index]
        else:
            return None
    return current


def _relevant_paths(record: Mapping[str, Any], category: str) -> list[list[str]]:
    result: list[list[str]] = []

    if category == "a_key_findings":
        values = ((record.get("study_summary_A") or {}).get("key_findings")) if isinstance(record.get("study_summary_A"), Mapping) else None
        if isinstance(values, list):
            return [["study_summary_A", "key_findings", str(index)] for index in range(len(values))]
        if values is not None:
            return [["study_summary_A", "key_findings"]]
        return []
    if category == "b_planning_summary":
        values = ((record.get("review_planning_B") or {}).get("planning_summary")) if isinstance(record.get("review_planning_B"), Mapping) else None
        return [["review_planning_B", "planning_summary"]] if values is not None else []
    if category == "b_scope_limits":
        values = ((record.get("review_planning_B") or {}).get("scope_interpretation_cautions")) if isinstance(record.get("review_planning_B"), Mapping) else None
        if isinstance(values, list):
            return [["review_planning_B", "scope_interpretation_cautions", str(index)] for index in range(len(values))]
        return [["review_planning_B", "scope_interpretation_cautions"]] if values is not None else []

    def walk(value: Any, path: list[str]) -> None:
        if isinstance(value, Mapping):
            for key, child in value.items():
                key_text = str(key)
                folded = key_text.casefold()
                child_path = [*path, key_text]
                if category == "planning_summary" and "planning_summary" in folded:
                    result.append(child_path)
                elif category == "a_findings" and (
                    folded in {"key_findings", "key_results", "finding", "findings", "claim", "claims", "results", "result", "contribution"}
                    or (path and path[-1] == "study_summary_A" and folded in {"work_summary", "approach", "problem_or_question"})
                ):
                    result.append(child_path)
                elif category == "conditions_limits" and any(word in folded for word in ("condition", "boundary", "limit", "limitation", "caution")):
                    result.append(child_path)
                walk(child, child_path)
        elif isinstance(value, list):
            for index, child in enumerate(value):
                walk(child, [*path, str(index)])

    walk(record, [])
    return result


def _visibility_audit(catalog: Mapping[str, Any], payload: Mapping[str, Any], on_demand: Any) -> dict[str, Any]:
    """Audit semantic locators by item, retaining stable location examples."""
    lookup = catalog.get("_lookup") or {}
    categories = ("a_key_findings", "b_planning_summary", "b_scope_limits", "conditions_limits")
    output: dict[str, Any] = {
        "catalog_schema_version": catalog.get("schema_version"),
        "catalog_sha256": catalog.get("catalog_sha256"),
        "source_material_roots": len(payload.get("source_materials") or []),
        "tool_material_roots": len(payload.get("tool_materials") or []),
        "entries_total": len(catalog.get("entries") or []),
        "categories": {},
        "locator_policy": _copy(catalog.get("locator_policy") or {}),
    }
    catalog_stable_ids = {
        _stable_identity(info.get("record"))
        for info in lookup.values()
        if isinstance(info, Mapping) and isinstance(info.get("record"), Mapping)
    }
    for category in categories:
        items: list[dict[str, Any]] = []
        for access_id, info in lookup.items():
            if not isinstance(info, Mapping):
                continue
            record = info.get("record")
            entry = info.get("entry") or {}
            if not isinstance(record, Mapping):
                continue
            for path_parts in _relevant_paths(record, category):
                if not path_parts:
                    continue
                root_path = path_parts[0]
                raw_root = record.get(root_path)
                locator = next(
                    (item for item in (entry.get("source_supplied_locators") or []) if str(item.get("path")) == root_path),
                    None,
                )
                visible_root = _parse_json_text(locator.get("source_excerpt")) if locator else None
                raw_item = _lookup_nested(record, path_parts)
                visible_item = _lookup_nested(visible_root, path_parts[1:]) if path_parts[1:] else visible_root
                state = _state(raw_item, visible_item, locator)
                duplicate_record = None
                if path_parts[0] in {"tool_materials", "tool_supplement_materials"} and len(path_parts) >= 2:
                    nested_rows = record.get(path_parts[0])
                    if isinstance(nested_rows, list) and str(path_parts[1]).isdigit():
                        index = int(path_parts[1])
                        if index < len(nested_rows) and isinstance(nested_rows[index], Mapping):
                            duplicate_record = nested_rows[index]
                if locator is None and duplicate_record is not None:
                    if _stable_identity(duplicate_record) in catalog_stable_ids:
                        state = "duplicated_elsewhere"
                if locator is None and state == "unavailable" and root_path in set(entry.get("available_material_paths") or []):
                    state = "only_path"
                items.append({
                    "access_id": str(access_id),
                    "channel": entry.get("channel"),
                    "index_path": entry.get("index_path"),
                    "identity": _identity(record),
                    "path": ".".join(path_parts),
                    "state": state,
                    "raw_chars": len(json.dumps(raw_item, ensure_ascii=False, default=str)),
                    "visible_chars": len(json.dumps(visible_item, ensure_ascii=False, default=str)) if visible_item is not None else 0,
                    "locator_path": locator.get("path") if locator else None,
                    "locator_excerpt": bool(locator and locator.get("excerpt")),
                    "omitted_material_paths": _copy(locator.get("omitted_material_paths") or []) if locator else [],
                })
        counts = {state: sum(1 for item in items if item["state"] == state) for state in ("complete", "partial", "only_path", "unavailable", "duplicated_elsewhere")}
        output["categories"][category] = {
            "item_count": len(items),
            "state_counts": counts,
            "items": items,
            "examples_by_state": {
                state: [item for item in items if item["state"] == state][:5]
                for state in counts
            },
        }
    # A source card can carry the same tool result nested under the card while
    # the payload also exposes it through the top-level tool channel.  Report
    # that relationship explicitly so the audit does not call a duplicated
    # identity "unavailable".
    catalog_hashes = {
        _stable_identity(info.get("record"))
        for info in lookup.values()
        if isinstance(info, Mapping) and isinstance(info.get("record"), Mapping)
    }
    nested_tool_rows = []
    for source_index, row in enumerate(payload.get("source_materials") or []):
        if not isinstance(row, Mapping):
            continue
        for nested_key in ("tool_materials", "tool_supplement_materials"):
            for nested_index, nested in enumerate(row.get(nested_key) or []):
                if not isinstance(nested, Mapping):
                    continue
                nested_tool_rows.append({
                    "path": f"source_materials[{source_index}].{nested_key}[{nested_index}]",
                    "identity": _identity(nested),
                    "record_sha256": _hash(nested),
                    "stable_identity": _stable_identity(nested),
                    "represented_by_catalog_entry": _stable_identity(nested) in catalog_hashes,
                })
    output["nested_tool_materials"] = {
        "count": len(nested_tool_rows),
        "rows": nested_tool_rows,
        "unique_unrepresented_count": sum(1 for row in nested_tool_rows if not row["represented_by_catalog_entry"]),
        "note": "Nested tool records are retained when their exact record is represented in the top-level or tool-supplement catalog; this is a transport-location duplicate check.",
    }
    duplicate_read_paths = []
    for access_id, info in lookup.items():
        record = info.get("record") if isinstance(info, Mapping) else None
        entry = info.get("entry") if isinstance(info, Mapping) else {}
        deep = record.get("deep_read_material") if isinstance(record, Mapping) else None
        if not isinstance(deep, Mapping):
            continue
        current = deep.get("current_question_material")
        for sibling_key in ("prior_question_material", "question_material"):
            sibling = deep.get(sibling_key)
            if sibling is not None and sibling == current:
                duplicate_read_paths.append({
                    "access_id": str(access_id),
                    "index_path": entry.get("index_path"),
                    "identity": _identity(record),
                    "duplicated_path": f"deep_read_material.{sibling_key}",
                    "duplicate_of": "deep_read_material.current_question_material",
                    "state": "duplicated_elsewhere",
                })
    output["duplicated_read_paths"] = duplicate_read_paths
    return output


def _ledger_snapshot() -> dict[str, Any]:
    if not LEDGER_PATH.is_file():
        return {"path": str(LEDGER_PATH), "exists": False}
    uri = f"file:{LEDGER_PATH.as_posix()}?mode=ro"
    with sqlite3.connect(uri, uri=True) as con:
        rows = con.execute(
            "select reservation_id, call_id, amount_cny, actual_cny, status, returned_model, finish_reason from reservations order by rowid"
        ).fetchall()
    statuses: dict[str, int] = {}
    for row in rows:
        statuses[str(row[4])] = statuses.get(str(row[4]), 0) + 1
    return {
        "path": str(LEDGER_PATH),
        "exists": True,
        "row_count": len(rows),
        "status_counts": statuses,
        "settled_actual_cny": sum(float(row[3] or 0) for row in rows),
        "reserved_cny": sum(float(row[2] or 0) for row in rows if row[4] == "reserved"),
        "uncertain_cny": sum(float(row[2] or 0) for row in rows if row[4] == "uncertain"),
        "read_only": True,
        "shared_limit_before_raise_cny": 45.0,
        "authorized_new_round_cny": 40.0,
        "shared_limit_after_explicit_raise_cny": 85.0,
        "raise_applied_by_this_driver": False,
    }


def _profile_record(profile_name: str, profile: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "name": profile_name,
        "model": profile.get("model"),
        "thinking": profile.get("thinking"),
        "thinking_budget": profile.get("thinking_budget"),
        "max_output_tokens": profile.get("max_output_tokens"),
        "wire_completion_capacity": int(profile.get("thinking_budget") or 0) + int(profile.get("max_output_tokens") or 0),
    }


def _request_record(*, group: str, role: str, profile: Mapping[str, Any], messages: list[dict[str, Any]], payload: Mapping[str, Any], estimate: Mapping[str, Any], model_payload: Mapping[str, Any] | None = None, trace: Mapping[str, Any] | None = None) -> dict[str, Any]:
    return {
        "schema_version": "outline_directory_repair_comparison.v1",
        "group": group,
        "role": role,
        "status": "prepared_no_paid_calls",
        "model": profile.get("model"),
        "thinking": profile.get("thinking"),
        "thinking_budget": profile.get("thinking_budget"),
        "max_output_tokens": profile.get("max_output_tokens"),
        "wire_completion_capacity": int(profile.get("thinking_budget") or 0) + int(profile.get("max_output_tokens") or 0),
        "request_sha256": _hash(messages),
        "messages": _copy(messages),
        "payload_fingerprints": _source_fingerprints(payload),
        "estimate": _copy(estimate),
        "model_payload": _copy(model_payload) if model_payload is not None else None,
        "material_access_trace": _copy(trace) if trace is not None else None,
        "no_root_scientific_feedback_input": True,
        "no_prior_candidate_answer_input": True,
    }


def _message_payload_equivalence(messages: list[dict[str, Any]], payload: Mapping[str, Any], keys: tuple[str, ...]) -> dict[str, Any]:
    parsed = _message_json(messages)
    fields = {}
    for key in keys:
        original = payload.get(key)
        visible = parsed.get(key) if isinstance(parsed, Mapping) else None
        fields[key] = {
            "equal": visible == original,
            "original_sha256": _hash(original),
            "message_json_sha256": _hash(visible),
        }
    return {
        "parsed_user_json_present": isinstance(parsed, Mapping),
        "fields": fields,
        "all_equal": all(item["equal"] for item in fields.values()),
    }


def prepare() -> dict[str, Any]:
    on_demand, strengthening, planning, load_profile = _imports()
    raw_request = _read_json(BASELINE_REQUEST)
    payload = raw_request.get("payload")
    if not isinstance(payload, Mapping):
        raise RuntimeError("baseline_request_payload_missing")
    payload = _copy(payload)
    # This is the old request's input payload only.  No RESULT/raw response is read.
    fingerprints = _source_fingerprints(payload)
    if len(payload.get("source_materials") or []) != 29 or len(payload.get("tool_materials") or []) != 2:
        raise RuntimeError("unexpected_material_counts")
    if len(payload.get("modifiable_unit_ids") or []) != 2:
        raise RuntimeError("unexpected_modifiable_units")

    tokenizer = TOKENIZER if TOKENIZER.is_file() else None
    counter = planning.qwen_local_token_counter(tokenizer) if tokenizer else None
    plus_name = "autonomous_outline"
    max_name = "strong_outline"
    plus = load_profile(plus_name)
    max_profile = load_profile(max_name)

    group1_messages = strengthening.strengthening_messages(payload)
    group1_estimate = strengthening.estimate_strengthening_request(group1_messages, profile=plus, token_counter=counter)
    group1 = _request_record(
        group="full_material_plus",
        role=plus_name,
        profile=plus,
        messages=group1_messages,
        payload=payload,
        estimate=group1_estimate,
        model_payload=None,
    )
    group1["messages_user_json_equivalence"] = _message_payload_equivalence(
        group1_messages,
        payload,
        ("chapter_plan", "source_materials", "tool_materials", "readonly_neighbor_unit_roles", "actual_local_body", "source_identity_map"),
    )

    catalog = on_demand.build_material_catalog(payload)
    visibility = _visibility_audit(catalog, payload, on_demand)
    access_messages = on_demand.access_messages(payload, catalog)
    access_estimate = strengthening.estimate_strengthening_request(access_messages, profile=plus, token_counter=counter)
    empty_trace = {
        "status": "access_not_run",
        "material_requests": [],
        "selected_materials": [],
        "trace": [],
        "resolved_count": 0,
        "access_ids": [],
        "catalog_sha256": catalog.get("catalog_sha256"),
    }
    owner_template_messages = on_demand.owner_messages(payload, catalog, empty_trace)
    owner_template_estimate = strengthening.estimate_strengthening_request(
        owner_template_messages, profile=max_profile, token_counter=counter,
    )
    upper_trace = on_demand.full_catalog_trace(catalog)
    owner_upper_messages = on_demand.owner_messages(payload, catalog, upper_trace)
    owner_upper_estimate = strengthening.estimate_strengthening_request(
        owner_upper_messages, profile=max_profile, token_counter=counter,
    )
    group2_access = _request_record(
        group="semantic_catalog_access_plus",
        role=plus_name,
        profile=plus,
        messages=access_messages,
        payload=payload,
        estimate=access_estimate,
        model_payload=_message_json(access_messages),
    )
    group2_owner_template = _request_record(
        group="semantic_catalog_owner_max_unresolved_template",
        role=max_name,
        profile=max_profile,
        messages=owner_template_messages,
        payload=payload,
        estimate=owner_template_estimate,
        model_payload=_message_json(owner_template_messages),
        trace=empty_trace,
    )
    group2_owner_upper = _request_record(
        group="semantic_catalog_owner_max_all_catalog_upper_bound",
        role=max_name,
        profile=max_profile,
        messages=owner_upper_messages,
        payload=payload,
        estimate=owner_upper_estimate,
        model_payload=_message_json(owner_upper_messages),
        trace=upper_trace,
    )

    run_input = {
        "source_path": str(BASELINE_REQUEST),
        "source_sha256": _file_hash(BASELINE_REQUEST),
        "source_request_mode": raw_request.get("mode"),
        "source_payload_fingerprints": fingerprints,
        "source_material_counts": {
            "source_materials": len(payload.get("source_materials") or []),
            "tool_materials": len(payload.get("tool_materials") or []),
            "catalog_entries": len(catalog.get("entries") or []),
        },
        "unit_ids": {
            "modifiable": _copy(payload.get("modifiable_unit_ids") or []),
            "read_only": _copy(payload.get("read_only_unit_ids") or []),
        },
        "catalog_sha256": catalog.get("catalog_sha256"),
        "source_head_locked": TARGET_SHA,
        "input_is_original_payload_only": True,
        "result_or_quality_review_read": False,
    }

    _dump(OUT / "INPUT_PROVENANCE.json", run_input)
    _dump(OUT / "INPUT_PAYLOAD.json", payload)
    _dump(OUT / "MATERIAL_CATALOG.json", on_demand.public_catalog(catalog))
    _dump(OUT / "SEMANTIC_VISIBILITY_AUDIT.json", visibility)
    _dump(OUT / "full_material_plus" / "REQUEST.json", group1)
    _dump(OUT / "semantic_catalog_max" / "ACCESS_REQUEST.json", group2_access)
    _dump(OUT / "semantic_catalog_max" / "OWNER_REQUEST_TEMPLATE_UNRESOLVED.json", group2_owner_template)
    _dump(OUT / "semantic_catalog_max" / "OWNER_UPPER_BOUND_REQUEST_ALL_CATALOG.json", group2_owner_upper)
    _dump(OUT / "semantic_catalog_max" / "ACCESS_PROFILE.json", _profile_record(plus_name, plus))
    _dump(OUT / "semantic_catalog_max" / "OWNER_PROFILE.json", _profile_record(max_name, max_profile))
    _dump(OUT / "full_material_plus" / "PROFILE.json", _profile_record(plus_name, plus))
    _dump(OUT / "BUDGET_READONLY_SNAPSHOT.json", _ledger_snapshot())
    report = {
        "status": "prepared_no_paid_calls",
        "source_head_locked": TARGET_SHA,
        "input_provenance": run_input,
        "group1": {
            "request_path": str(OUT / "full_material_plus" / "REQUEST.json"),
            "request_sha256": group1["request_sha256"],
            "profile": _profile_record(plus_name, plus),
            "estimate": group1_estimate,
            "message_material_mode": "full original payload in production strengthening messages",
        },
        "group2": {
            "access_request_path": str(OUT / "semantic_catalog_max" / "ACCESS_REQUEST.json"),
            "access_request_sha256": group2_access["request_sha256"],
            "owner_template_path": str(OUT / "semantic_catalog_max" / "OWNER_REQUEST_TEMPLATE_UNRESOLVED.json"),
            "owner_template_sha256": group2_owner_template["request_sha256"],
            "owner_upper_bound_path": str(OUT / "semantic_catalog_max" / "OWNER_UPPER_BOUND_REQUEST_ALL_CATALOG.json"),
            "owner_upper_bound_sha256": group2_owner_upper["request_sha256"],
            "access_profile": _profile_record(plus_name, plus),
            "owner_profile": _profile_record(max_name, max_profile),
            "access_estimate": access_estimate,
            "owner_template_estimate": owner_template_estimate,
            "owner_upper_bound_estimate": owner_upper_estimate,
            "owner_template_status": "not_final_until_access_response",
            "owner_upper_bound_status": "conservative_estimate_only_not_sent",
        },
        "semantic_visibility_audit_path": str(OUT / "SEMANTIC_VISIBILITY_AUDIT.json"),
        "budget_snapshot_path": str(OUT / "BUDGET_READONLY_SNAPSHOT.json"),
        "paid_calls": 0,
        "live_ledger_mutated": False,
        "root_review_required_before_live": True,
    }
    _dump(OUT / "PREPARE_REPORT.json", report)
    return report


if __name__ == "__main__":
    print(json.dumps(prepare(), ensure_ascii=False, indent=2), flush=True)
