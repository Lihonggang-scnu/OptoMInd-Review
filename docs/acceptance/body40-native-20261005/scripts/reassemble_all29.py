"""Zero-call reassembly of the exact historical 29-job selection."""
from __future__ import annotations

import json
import sys
from pathlib import Path

WORKTREE = Path(r"F:/OptoMind-Review-2/outputs/body40_identity_citations_local_acceptance_20261005/worktree")
ACCEPTANCE = Path(r"F:/OptoMind-Review-2/outputs/body40_identity_citations_local_acceptance_20261005/native")
ORIGINAL_ROOT = Path(r"F:/OptoMind-Review-2/outputs/body_full_staged_acceptance_20261004_40cny")
MANIFEST = ORIGINAL_ROOT / "body_assembly_final/ASSEMBLY_MANIFEST.json"
ORIGINAL_BATCH = ORIGINAL_ROOT / "body_assembly_final/batch_input/BATCH_JOBS.json"
NEW_RESULTS = {
    "Ch6_U4": ACCEPTANCE / "replay/original_local_Ch6_U4/writer/Ch6_Ch6_U4/UNIT_RESULT.json",
    "Ch7_U02": ACCEPTANCE / "replay/original_local_Ch7_U02/writer/Ch7_Ch7_U02/UNIT_RESULT.json",
}

sys.path.insert(0, str(WORKTREE))
from scripts.upgrade3 import full_review_draft as assembly  # noqa: E402


def write_json(path: Path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main():
    batch_root = ACCEPTANCE / "all29_reassembly/batch_input"
    output_root = ACCEPTANCE / "all29_reassembly/output"
    raw = json.loads(ORIGINAL_BATCH.read_text(encoding="utf-8"))
    if len(raw) != 29:
        raise AssertionError(f"historical batch selection expected 29 jobs, found {len(raw)}")
    jobs = []
    for item in raw:
        row = dict(item)
        unit_id = str(row.get("unit_id"))
        if unit_id in NEW_RESULTS:
            row["reused_result"] = str(NEW_RESULTS[unit_id])
        jobs.append(row)
    write_json(batch_root / "BATCH_JOBS.json", jobs)
    rc = assembly.main([
        "--manifest", str(MANIFEST),
        "--batch-root", str(batch_root),
        "--output-root", str(output_root),
    ])
    if rc != 0:
        raise SystemExit(rc)
    summary = json.loads((output_root / "ASSEMBLY_SUMMARY.json").read_text(encoding="utf-8"))
    rows = summary.get("unit_rows") or []
    result = {
        "mode": "native_windows_zero_call_all29_reassembly",
        "source_batch_jobs": str(ORIGINAL_BATCH),
        "source_manifest": str(MANIFEST),
        "historical_selected_job_count": len(raw),
        "loaded_unit_count": len(rows),
        "all29_retained": len(rows) == 29 and {r.get("unit_id") for r in rows} == {r.get("unit_id") for r in raw},
        "overrides": {k: str(v) for k, v in NEW_RESULTS.items()},
        "paid_calls": 0,
        "network_calls": 0,
        "status": summary.get("status"),
        "problems_resolved": summary.get("problems_resolved"),
        "pending_problem_count": len(summary.get("pending_problems") or []),
        "used_papers": summary.get("used_papers"),
        "catalog_papers": summary.get("catalog_papers"),
        "reference_count": len((json.loads((output_root / "REFERENCES.json").read_text(encoding="utf-8")) or {}).get("references") or []),
        "unit_rows": [
            {
                "chapter_id": r.get("chapter_id"),
                "unit_id": r.get("unit_id"),
                "result_path": r.get("result_path"),
                "status": r.get("status"),
                "body_chars": r.get("body_chars"),
                "pending_problem": r.get("pending_problem"),
                "citation_problem_count": len(r.get("citation_problems") or []),
            }
            for r in rows
        ],
        "artifacts": {
            "batch_jobs": str(batch_root / "BATCH_JOBS.json"),
            "assembly_summary": str(output_root / "ASSEMBLY_SUMMARY.json"),
            "run_report": str(output_root / "RUN_REPORT.md"),
            "draft_handles": str(output_root / "REVIEW_DRAFT_HANDLES.md"),
            "references": str(output_root / "REFERENCES.json"),
        },
    }
    write_json(ACCEPTANCE / "all29_reassembly_report.json", result)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
