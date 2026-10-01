"""Build a Plus-only assembler batch from already completed writer results.

This is a task-local adapter: it reuses RUN_BODY's plan, arrangement loader,
and manifest builder, but intentionally does not apply the BODY driver's
writer-issue gate. Scientific issue objects are retained for the assembler
report; structural missing/unknown/truncation conditions still fail here.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parent
REPO = ROOT.parent / "worktree"
RUN_BODY_PATH = ROOT.parent / "RUN_BODY.py"
PLAN_ROOT = ROOT / "plan"
ARRANGEMENT_ROOT = ROOT / "body" / "arrangement"
OUTPUT_ROOT = ROOT / "body_plus"
WRITER_ROOT = OUTPUT_ROOT / "writer"
BATCH_ROOT = OUTPUT_ROOT / "batch"
ASSEMBLY_ROOT = OUTPUT_ROOT / "assembly"


def _load_run_body():
    spec = importlib.util.spec_from_file_location("acceptance_run_body", RUN_BODY_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {RUN_BODY_PATH}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def main() -> None:
    run_body = _load_run_body()
    plan_path = run_body._plan_path(PLAN_ROOT)
    info = run_body._validate_plan(plan_path)
    args = type(
        "PathsArgs",
        (),
        {
            "output_root": str(OUTPUT_ROOT),
            "arrangement_root": str(ARRANGEMENT_ROOT),
            "writer_root": str(WRITER_ROOT),
            "batch_root": str(BATCH_ROOT),
            "assembly_root": str(ASSEMBLY_ROOT),
        },
    )()
    paths = run_body._paths(args)
    arrangements = run_body._load_arrangements(info, paths["arrangement"])
    expected = sorted(arrangements)
    records = []
    used = set()
    issue_units = []
    for (chapter_id, unit_id), item in arrangements.items():
        result_path = paths["writer"] / f"{chapter_id}_{unit_id}" / "UNIT_RESULT.json"
        if not result_path.is_file():
            raise RuntimeError(f"missing Plus result: {result_path}")
        result = json.loads(result_path.read_text(encoding="utf-8"))
        if result.get("chapter_id") != chapter_id or result.get("unit_id") != unit_id:
            raise RuntimeError(f"result identity mismatch: {result_path}")
        if result.get("model") != "qwen3.5-plus":
            raise RuntimeError(f"non-Plus result: {result_path}:{result.get('model')}")
        if result.get("complete") is not True or result.get("finish_reason") == "length":
            raise RuntimeError(f"incomplete/truncated Plus result: {result_path}")
        if result.get("unknown_citations"):
            raise RuntimeError(f"unknown citations in Plus result: {result_path}")
        body_path = Path(str(result.get("body_path") or result_path.parent / "UNIT_BODY.md"))
        if not body_path.is_absolute():
            body_path = result_path.parent / body_path
        if not body_path.is_file() or not body_path.read_text(encoding="utf-8").strip():
            raise RuntimeError(f"missing Plus body: {body_path}")
        used.update(result.get("used_source_handles") or [])
        issues = result.get("issues") or []
        if issues:
            issue_units.append({"chapter_id": chapter_id, "unit_id": unit_id, "issues": issues})
        records.append({
            "chapter_id": chapter_id,
            "unit_id": unit_id,
            "model": result.get("model"),
            "result_path": str(result_path),
            "body_path": str(body_path),
            "complete": result.get("complete"),
            "finish_reason": result.get("finish_reason"),
            "issue_count": len(issues),
        })
    if len(records) != 20 or len(expected) != 20:
        raise RuntimeError(f"expected 20 Plus units, got jobs={len(expected)}, results={len(records)}")

    manifest_path, jobs_path = run_body._manifest(info, paths, arrangements)
    preflight = {
        "status": "complete",
        "calls": 0,
        "model": "qwen3.5-plus",
        "plan": str(plan_path),
        "arrangement_root": str(ARRANGEMENT_ROOT),
        "writer_root": str(WRITER_ROOT),
        "batch_root": str(BATCH_ROOT),
        "assembly_root": str(ASSEMBLY_ROOT),
        "manifest": str(manifest_path),
        "batch_jobs": str(jobs_path),
        "unit_count": len(records),
        "unique_used_source_handles": len(used),
        "issue_units": issue_units,
        "records": records,
        "planning_context_preserved": True,
        "scientific_issue_objects_retained": True,
    }
    (BATCH_ROOT / "PLUS_BATCH_PREFLIGHT.json").write_text(
        json.dumps(preflight, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps({k: v for k, v in preflight.items() if k not in {"records", "issue_units"}}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
