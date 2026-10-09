"""Read-only audit for the legacy-unit-plus run.

This helper only reads the run records and writes audit JSON/Markdown beneath
the same run root.  It never contacts a model and never edits source files.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import re
import sqlite3
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any


REF_RE = re.compile(r"\[(P\d{4})\]")


def load(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def canon(handle: str, aliases: dict[str, str]) -> str:
    seen: set[str] = set()
    while handle in aliases and handle not in seen:
        seen.add(handle)
        handle = aliases[handle]
    return handle


def stable_identity(handle: str, identities: dict[str, Any], aliases: dict[str, str]) -> tuple[str, str]:
    ch = canon(handle, aliases)
    row = identities.get(ch)
    if not row:
        return ch, "UNKNOWN:" + ch
    doi = str(row.get("doi") or "").strip().lower()
    if doi:
        return ch, "DOI:" + doi
    return ch, "ID:" + str(row.get("source_id") or row.get("paper_id") or ch)


def message_parts(path: Path) -> tuple[str, dict[str, Any]]:
    msgs = load(path)
    system = next((m.get("content", "") for m in msgs if m.get("role") == "system"), "")
    user = next((m.get("content", "") for m in msgs if m.get("role") == "user"), "{}")
    return system, json.loads(user)


def deep_source_handles(obj: Any) -> set[str]:
    out: set[str] = set()
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k in {"source_handle", "source_id"} and isinstance(v, str) and re.fullmatch(r"P\d{4}", v):
                out.add(v)
            elif k in {"source_handles"} and isinstance(v, list):
                out.update(x for x in v if isinstance(x, str) and re.fullmatch(r"P\d{4}", x))
            else:
                out.update(deep_source_handles(v))
    elif isinstance(obj, list):
        for v in obj:
            out.update(deep_source_handles(v))
    return out


def normalize_for_compare(obj: Any) -> Any:
    """Keep JSON semantics, while making no ordering assumptions for maps."""
    if isinstance(obj, dict):
        return {k: normalize_for_compare(v) for k, v in sorted(obj.items())}
    if isinstance(obj, list):
        return [normalize_for_compare(v) for v in obj]
    return obj


def table_check(body: str, table_tasks: list[dict[str, Any]]) -> dict[str, Any]:
    lines = body.splitlines()
    separator = re.compile(r"^\s*\|?\s*:?-{3,}:?\s*(?:\|\s*:?-{3,}:?\s*)+\|?\s*$")
    tables: list[dict[str, Any]] = []
    for i, line in enumerate(lines):
        if "|" not in line or i + 1 >= len(lines) or not separator.match(lines[i + 1]):
            continue
        j = i + 2
        rows = []
        while j < len(lines) and "|" in lines[j] and lines[j].strip():
            rows.append(lines[j])
            j += 1
        tables.append({"header": line, "separator": lines[i + 1], "rows": rows, "data_rows": len(rows), "line": i + 1})
    return {
        "task_count": len(table_tasks),
        "task_row_counts": [len(t.get("row_tasks", [])) for t in table_tasks],
        "markdown_tables": tables,
        "has_table_for_task": bool(tables) if table_tasks else True,
        "body_bytes": len(body.encode("utf-8")),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run-root", required=True)
    args = ap.parse_args()
    root = Path(args.run_root)
    report = load(root / "RUN_REPORT.json")
    raw_input = next((root / "input").glob("*/FULL_BODY_INPUT.json"))
    book = load(raw_input)
    aliases = book.get("source_aliases", {})
    identities = book.get("source_identities", {})

    units: list[dict[str, Any]] = []
    all_cited: set[str] = set()
    all_supplied: set[str] = set()
    all_direct: set[str] = set()
    all_closure: set[str] = set()
    all_unknown: Counter[str] = Counter()
    chapter_cited: dict[str, set[str]] = defaultdict(set)
    chapter_supplied: dict[str, set[str]] = defaultdict(set)
    chapter_direct: dict[str, set[str]] = defaultdict(set)
    chapter_closure: dict[str, set[str]] = defaultdict(set)

    effective_values: list[dict[str, Any]] = []
    system_hashes: set[str] = set()
    message_diff_fields: Counter[str] = Counter()
    table_units: list[dict[str, Any]] = []
    expected_table_tasks = [
        {"chapter_id": c.get("chapter_id"), "unit_id": u.get("unit_id"), "task_ids": [t.get("table_id") or t.get("task_id") or t.get("id") for t in u.get("table_tasks", [])]}
        for c in book.get("chapters", []) for u in c.get("units", []) if u.get("table_tasks")
    ]

    for item in report.get("units", []):
        preview_path = Path(item["messages_path"])
        actual_path = preview_path.parent / "attempt_001" / "UNIT_MESSAGES.json"
        if not actual_path.exists() and preview_path.parent.name == "attempt_001":
            actual_path = preview_path
            preview_path = actual_path.parent.parent / "UNIT_MESSAGES.json"
        if not actual_path.exists():
            units.append({"chapter_id": item["chapter_id"], "unit_id": item["unit_id"], "status": item.get("status"), "missing": True})
            continue
        actual_system, actual_payload = message_parts(actual_path)
        preview_system, preview_payload = message_parts(preview_path) if preview_path.exists() else ("", {})
        system_hashes.add(hashlib.sha256(actual_system.encode("utf-8")).hexdigest())
        diffs = []
        if actual_system != preview_system:
            diffs.append("system")
        keys = sorted(set(actual_payload) | set(preview_payload))
        for key in keys:
            if normalize_for_compare(actual_payload.get(key)) != normalize_for_compare(preview_payload.get(key)):
                diffs.append(key)
                message_diff_fields[key] += 1

        direct_raw = set()
        for key in ("paragraph_tasks", "table_tasks"):
            direct_raw.update(deep_source_handles(actual_payload.get(key, [])))
        supplied_raw = set()
        input_candidates = list(actual_path.parent.glob("UNIT_INPUT.json"))
        if input_candidates:
            unit_input = load(input_candidates[0])
            supplied_raw.update(m.get("source_handle") for m in unit_input.get("materials", []) if m.get("source_handle"))
        supplied = {stable_identity(h, identities, aliases)[1] for h in supplied_raw}
        direct = {stable_identity(h, identities, aliases)[1] for h in direct_raw}
        closure = supplied - direct

        result_path = actual_path.parent / "UNIT_RESULT.json"
        result = load(result_path) if result_path.exists() else {}
        body = ""
        body_path = result.get("body_path")
        if body_path and Path(body_path).exists():
            body = Path(body_path).read_text(encoding="utf-8")
        citations = set()
        unknown = []
        for token in REF_RE.findall(body):
            ch, identity = stable_identity(token, identities, aliases)
            if identity.startswith("UNKNOWN:"):
                unknown.append(token)
            else:
                citations.add(identity)
        unknown.extend(x for x in result.get("unknown_citations", []) if isinstance(x, str))
        all_unknown.update(unknown)
        all_cited.update(citations)
        all_supplied.update(supplied)
        all_direct.update(direct)
        all_closure.update(closure)
        chapter = item["chapter_id"]
        chapter_cited[chapter].update(citations)
        chapter_supplied[chapter].update(supplied)
        chapter_direct[chapter].update(direct)
        chapter_closure[chapter].update(closure)

        er = result.get("effective_request") or {}
        if not er:
            error_path = actual_path.parent / "ERROR.json"
            if error_path.exists():
                er = (load(error_path).get("record") or {}).get("effective_request") or {}
        if er:
            effective_values.append(er)
        table_tasks = actual_payload.get("table_tasks") or []
        if table_tasks:
            table_units.append({"chapter_id": chapter, "unit_id": item["unit_id"], "check": table_check(body, table_tasks)})
        units.append({
            "chapter_id": chapter,
            "unit_id": item["unit_id"],
            "status": item.get("status"),
            "preview_path": str(preview_path),
            "actual_path": str(actual_path),
            "semantic_equal": not diffs,
            "different_fields": diffs,
            "actual_system_sha256": hashlib.sha256(actual_system.encode("utf-8")).hexdigest(),
            "direct_material_identities": sorted(direct),
            "closure_material_identities": sorted(closure),
            "cited_identities": sorted(citations),
            "unknown_citation_tokens": sorted(set(unknown)),
            "effective_request": er,
            "finish_reason": result.get("finish_reason"),
            "complete": result.get("complete"),
            "issues": result.get("issues", []),
        })

    def ident_summary(values: set[str]) -> list[str]:
        return sorted(values)

    chapter_summary = {}
    for chapter in sorted(chapter_supplied):
        chapter_summary[chapter] = {
            "cited_identities": ident_summary(chapter_cited[chapter]),
            "cited_count": len(chapter_cited[chapter]),
            "supplied_identities": ident_summary(chapter_supplied[chapter]),
            "supplied_count": len(chapter_supplied[chapter]),
            "supplied_but_uncited_identities": ident_summary(chapter_supplied[chapter] - chapter_cited[chapter]),
            "supplied_but_uncited_count": len(chapter_supplied[chapter] - chapter_cited[chapter]),
            "direct_task_material_identities": ident_summary(chapter_direct[chapter]),
            "direct_task_material_count": len(chapter_direct[chapter]),
            "citation_closure_material_identities": ident_summary(chapter_closure[chapter]),
            "citation_closure_material_count": len(chapter_closure[chapter]),
        }

    ledger_summary = {}
    ledger = root / "BUDGET.sqlite"
    if ledger.exists():
        con = sqlite3.connect(f"file:{ledger}?mode=ro", uri=True)
        ledger_summary["by_status"] = [list(row) for row in con.execute("select status,count(*),coalesce(sum(amount_cny),0),coalesce(sum(actual_cny),0) from reservations group by status")]
        ledger_summary["rows"] = con.execute("select reservation_id,call_id,status,amount_cny,actual_cny,returned_model,finish_reason,request_id,raw_response_sha256 from reservations order by rowid").fetchall()
        con.close()

    effective_keys = {json.dumps(normalize_for_compare(v), ensure_ascii=False, sort_keys=True) for v in effective_values}
    output = {
        "schema_version": "legacy_unit_plus.audit.v1",
        "read_only": True,
        "run_root": str(root),
        "units_total": len(units),
        "units_semantically_equal_to_preview": sum(1 for u in units if u.get("semantic_equal")),
        "units_with_message_differences": [u for u in units if u.get("different_fields")],
        "message_diff_field_counts": dict(message_diff_fields),
        "system_sha256_values": sorted(system_hashes),
        "effective_request_unique_count": len(effective_keys),
        "effective_request_values": [json.loads(x) for x in sorted(effective_keys)],
        "units_with_effective_request": len(effective_values),
        "units_with_actual_messages": sum(1 for u in units if not u.get("missing")),
        "units_missing_actual_messages": sum(1 for u in units if u.get("missing")),
        "chapters": chapter_summary,
        "whole_run": {
            "cited_identities": ident_summary(all_cited),
            "cited_count": len(all_cited),
            "supplied_identities": ident_summary(all_supplied),
            "supplied_count": len(all_supplied),
            "supplied_but_uncited_identities": ident_summary(all_supplied - all_cited),
            "supplied_but_uncited_count": len(all_supplied - all_cited),
            "direct_task_material_identities": ident_summary(all_direct),
            "direct_task_material_count": len(all_direct),
            "citation_closure_material_identities": ident_summary(all_closure),
            "citation_closure_material_count": len(all_closure),
            "unknown_citation_tokens": dict(all_unknown),
        },
        "table_units": table_units,
        "expected_table_tasks": expected_table_tasks,
        "missing_table_task_units": [
            {"chapter_id": x["chapter_id"], "unit_id": x["unit_id"]}
            for x in expected_table_tasks
            if not any(t["chapter_id"] == x["chapter_id"] and t["unit_id"] == x["unit_id"] for t in table_units)
        ],
        "ledger": ledger_summary,
        "units": units,
    }
    (root / "LIVE_AUDIT.json").write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8")
    (root / "EFFECTIVE_REQUEST_AUDIT.json").write_text(json.dumps({
        "unit_count_with_effective_request": len(effective_values),
        "unit_count_with_actual_messages": sum(1 for u in units if not u.get("missing")),
        "unit_count_missing_actual_messages": sum(1 for u in units if u.get("missing")),
        "unique_count": len(effective_keys),
        "values": [json.loads(x) for x in sorted(effective_keys)],
        "all_effective_requests_same": len(effective_keys) <= 1 and bool(effective_values),
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    (root / "CITATION_AUDIT.json").write_text(json.dumps({
        "chapters": chapter_summary,
        "whole_run": output["whole_run"],
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    (root / "TABLE_TASK_AUDIT.json").write_text(json.dumps({
        "expected_table_tasks": output["expected_table_tasks"],
        "completed_table_units": table_units,
        "missing_table_task_units": output["missing_table_task_units"],
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    (root / "LEDGER_SUMMARY.json").write_text(json.dumps(ledger_summary, ensure_ascii=False, indent=2), encoding="utf-8")
    lines = [
        "# Live run audit (read-only)",
        "",
        f"- Units observed: {len(units)}; semantically equal preview: {output['units_semantically_equal_to_preview']}",
        f"- Message-difference units: {len(output['units_with_message_differences'])}; effective_request unique values: {len(effective_keys)}",
        f"- Whole-run stable cited identities: {len(all_cited)}; supplied but uncited: {len(all_supplied - all_cited)}; unknown tokens: {sum(all_unknown.values())}",
        f"- Direct task material identities: {len(all_direct)}; closure identities: {len(all_closure)}",
        "",
        "## Chapter citation counts",
        "",
        "| Chapter | cited | supplied | supplied-but-uncited | direct task | closure |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    for ch, s in chapter_summary.items():
        lines.append(f"| {ch} | {s['cited_count']} | {s['supplied_count']} | {s['supplied_but_uncited_count']} | {s['direct_task_material_count']} | {s['citation_closure_material_count']} |")
    lines += ["", "## Table task checks", ""]
    for t in table_units:
        c = t["check"]
        lines.append(f"- {t['chapter_id']}/{t['unit_id']}: task_rows={c['task_row_counts']}; markdown_tables={len(c['markdown_tables'])}; has_table_for_task={c['has_table_for_task']}")
    if output["missing_table_task_units"]:
        lines.append("- Missing table-task units: " + ", ".join(f"{x['chapter_id']}/{x['unit_id']}" for x in output["missing_table_task_units"]))
    lines += ["", "## Semantic message differences", ""]
    if not output["units_with_message_differences"]:
        lines.append("- None.")
    else:
        for u in output["units_with_message_differences"]:
            lines.append(f"- {u['chapter_id']}/{u['unit_id']}: {', '.join(u['different_fields'])}")
    (root / "LIVE_AUDIT.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
