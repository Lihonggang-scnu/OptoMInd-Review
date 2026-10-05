from __future__ import annotations

import hashlib
import json
import shutil
import sys
from pathlib import Path

ROOT = Path(r"F:\OptoMind-Review-2\outputs\body_full_staged_acceptance_20261004_40cny")
WORKTREE = ROOT / "worktree"
sys.path.insert(0, str(WORKTREE))
from optomind_research.runtime.upgrade3 import review_unit_writer as writer

ARRANGEMENT = ROOT / "arrangement_repaired" / "Ch2" / "CHAPTER_ARRANGEMENT.json"
VIEW = ROOT / "arrangement_repaired" / "Ch2" / "ARRANGEMENT_INPUT.json"
ORIGINAL = ROOT / "writer_live" / "Ch2"
OUTPUT = ROOT / "writer_repaired" / "Ch2"
UNITS = ["Ch2_U01", "Ch2_U02", "Ch2_U03", "Ch2_U04"]


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> None:
    OUTPUT.mkdir(parents=True, exist_ok=True)
    entries = []
    total_repairs = 0
    for unit_id in UNITS:
        original_dir = ORIGINAL / f"Ch2_{unit_id}"
        original_result_path = original_dir / "UNIT_RESULT.json"
        original_result = json.loads(original_result_path.read_text(encoding="utf-8"))
        original_body_path = Path(original_result["body_path"])
        original_body = original_body_path.read_text(encoding="utf-8")
        view = writer.build_unit_view(
            ARRANGEMENT,
            unit_id,
            view_path=VIEW,
            max_material_chars_per_source=0,
        )
        known_handles = writer._known_unit_handles(view)
        citation_map = writer._local_numeric_citation_map(known_handles)
        repaired_body, repairs = writer._repair_numeric_citations(
            original_body, citation_map, known_handles
        )
        target = OUTPUT / f"Ch2_{unit_id}"
        target.mkdir(parents=True, exist_ok=True)
        for filename in ("UNIT_INPUT.json", "UNIT_MESSAGES.json"):
            source = original_dir / filename
            if source.is_file():
                shutil.copy2(source, target / filename)
        if repaired_body == original_body and unit_id in {"Ch2_U03", "Ch2_U04"}:
            raise AssertionError(f"expected local numeric repairs for {unit_id}")
        result = writer.write_unit_output(
            view,
            repaired_body,
            target,
            model=original_result.get("model") or "qwen3.7-flash",
            language=original_result.get("language") or "zh",
            mode="reprocess",
            used_messages=json.loads((original_dir / "UNIT_MESSAGES.json").read_text(encoding="utf-8")),
            estimate=original_result.get("estimate") or {},
            usage=original_result.get("usage") or {},
            response_path=original_result.get("raw_response") or "",
            finish_reason=original_result.get("finish_reason") or "",
            complete=bool(original_result.get("complete", True)),
            partial_error=original_result.get("partial_error") or "",
            issues=original_result.get("issues") or [],
        )
        result_path = target / "UNIT_RESULT.json"
        result.update({
            "mode": "zero_call_reprocess",
            "simulated": False,
            "new_paid_calls": 0,
            "new_cost_cny": 0.0,
            "model_calls": 0,
            "reprocess_reason": "caller-declared unique local P-handle aliases; existing repair helper only",
            "reprocessed_from_body": str(original_body_path),
            "reprocessed_from_result": str(original_result_path),
            "original_body_sha256": sha256(original_body_path),
            "numeric_citation_map": citation_map,
            "numeric_citation_repairs": repairs,
            "original_diagnostics": {
                key: original_result.get(key)
                for key in ("used_source_handles", "unknown_citations", "unresolved_numeric_citations", "citation_problems")
            },
            "usage_origin": "copied from original paid writer call; no new call or charge",
            "note": "Zero-call deterministic reprocess of the original writer body; only explicit unique local numeric citation aliases were normalized.",
        })
        result_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        report = {
            "unit_id": unit_id,
            "original_body": str(original_body_path),
            "repaired_body": str(target / "UNIT_BODY.md"),
            "original_result": str(original_result_path),
            "repaired_result": str(result_path),
            "original_body_sha256": result["original_body_sha256"],
            "known_unit_handles": known_handles,
            "local_numeric_aliases": citation_map,
            "numeric_citation_repairs": repairs,
            "repair_count": len(repairs),
            "used_source_handles_after": result["used_source_handles"],
            "unknown_citations_after": result["unknown_citations"],
            "unresolved_numeric_citations_after": result["unresolved_numeric_citations"],
            "material_summary": view.material_summary(),
            "warnings": view.warnings,
            "new_paid_calls": 0,
            "new_cost_cny": 0.0,
        }
        (target / "ZERO_CALL_REPROCESS.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        entries.append(report)
        total_repairs += len(repairs)
    aggregate = {
        "schema_version": "optomind.review_unit_writer.zero_call_reprocess.v1",
        "mode": "zero_call_reprocess",
        "chapter_id": "Ch2",
        "arrangement": str(ARRANGEMENT),
        "view": str(VIEW),
        "original_root": str(ORIGINAL),
        "output_root": str(OUTPUT),
        "units": entries,
        "unit_count": len(entries),
        "total_numeric_citation_repairs": total_repairs,
        "model_calls": 0,
        "new_paid_calls": 0,
        "new_cost_cny": 0.0,
        "ledger_impact": "none",
        "original_outputs_preserved": True,
        "raw_responses_unchanged": True,
    }
    (OUTPUT / "UNIT_WRITING_REPROCESS_RUN.json").write_text(
        json.dumps(aggregate, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps({
        "output_root": str(OUTPUT),
        "unit_count": len(entries),
        "total_repairs": total_repairs,
        "model_calls": 0,
        "new_cost_cny": 0.0,
        "units": [
            {
                "unit_id": row["unit_id"],
                "repairs": row["repair_count"],
                "used": len(row["used_source_handles_after"]),
                "unknown": row["unknown_citations_after"],
                "unresolved_numeric": row["unresolved_numeric_citations_after"],
            }
            for row in entries
        ],
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
