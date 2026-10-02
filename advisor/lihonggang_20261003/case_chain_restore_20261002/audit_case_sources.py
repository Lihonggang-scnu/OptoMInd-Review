from __future__ import annotations

import json
from pathlib import Path

root = Path(__file__).resolve().parent
plan = json.loads((root / "DETAILED_REVIEW_PLAN.json").read_text(encoding="utf-8-sig"))
results = []
for chapter in (plan.get("case_enrichment") or {}).get("chapter_results") or []:
    chapter_id = str(chapter.get("chapter_id") or "")
    packet = json.loads((root / "writer_packets" / f"{chapter_id}.json").read_text(encoding="utf-8-sig"))
    rows = [row for row in packet.get("source_materials") or [] if isinstance(row, dict)]
    candidate_rows = [row for row in packet.get("candidate_materials") or [] if isinstance(row, dict)]
    by_handle = {str(row.get("source_handle")): row for row in [*rows, *candidate_rows] if row.get("source_handle")}
    additions = []
    missing = []
    unreadable = []
    for addition in chapter.get("additions") or []:
        if not isinstance(addition, dict):
            continue
        unit_key = str(addition.get("unit_key") or "")
        for study in addition.get("studies") or []:
            if not isinstance(study, dict):
                continue
            handle = str(study.get("source_handle") or "")
            material = by_handle.get(handle)
            content_keys = (
                "study_summary_A", "review_planning_B", "deep_read_material",
                "supplement_gap_material", "supplement_gap_materials",
            )
            readable = bool(material and any(material.get(key) for key in content_keys))
            row = {
                "unit_key": unit_key,
                "source_handle": handle,
                "contribution_present": bool(str(study.get("contribution") or "").strip()),
                "material_present": material is not None,
                "material_readable": readable,
                "title": str((material or {}).get("title") or ""),
            }
            additions.append(row)
            if material is None:
                missing.append(handle)
            elif not readable:
                unreadable.append(handle)

    results.append({
        "chapter_id": chapter_id,
        "status": chapter.get("status"),
        "route_count": chapter.get("route_count"),
        "route_batches": chapter.get("route_batches"),
        "addition_rows": len(additions),
        "unique_addition_handles": len({row["source_handle"] for row in additions if row["source_handle"]}),
        "missing_handles": sorted(set(missing)),
        "unreadable_handles": sorted(set(unreadable)),
        "all_additions_have_packet_material": not missing,
        "all_additions_have_readable_material": not missing and not unreadable,
        "additions": additions,
    })

ch01 = json.loads((root / "writer_packets" / "CH01.json").read_text(encoding="utf-8-sig"))
u02 = next(
    (unit for unit in (ch01.get("chapter_plan") or {}).get("units") or []
     if str(unit.get("unit_id") or unit.get("id") or "") in {"U02", "CH01:2"}),
    {},
)
u02_handles = set(str(value) for value in u02.get("source_handles") or [])
u02_studies = [study for study in u02.get("supporting_studies") or [] if isinstance(study, dict)]
ch01_materials = {
    str(row.get("source_handle")): row
    for row in ch01.get("source_materials") or []
    if isinstance(row, dict) and row.get("source_handle")
}
u02_original_candidates = []
for study in u02_studies:
    handle = str(study.get("source_handle") or "")
    material = ch01_materials.get(handle) or {}
    a = material.get("study_summary_A") or {}
    u02_original_candidates.append({
        "source_handle": handle,
        "title": material.get("title"),
        "year": material.get("year"),
        "paper_kind": a.get("paper_kind"),
        "approach": a.get("approach"),
        "finding": study.get("contribution") or study.get("finding"),
    })

report = {
    "status": "offline_case_source_audit",
    "plan_status": plan.get("status"),
    "case_enrichment_status": (plan.get("case_enrichment") or {}).get("status"),
    "chapters": results,
    "CH01_U02": {
        "explicit_source_handles": sorted(u02_handles),
        "supporting_study_rows": len(u02_studies),
        "supporting_study_unique_handles": len({row.get("source_handle") for row in u02_studies if row.get("source_handle")}),
        "material_identity_rows": u02_original_candidates,
        "original_or_longitudinal_candidates": [
            row for row in u02_original_candidates
            if any(term in str(row.get("paper_kind") or "") + str(row.get("approach") or "")
                   for term in ("前瞻", "纵向", "prospective", "longitudinal", "trial"))
        ],
        "note": "This is an identity/material audit; it does not assert scientific correctness of model contributions.",
    },
}
(root / "CASE_SOURCE_AUDIT.json").write_text(
    json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
)
print(json.dumps({
    "status": report["status"],
    "chapters": [(row["chapter_id"], row["addition_rows"], row["all_additions_have_packet_material"], row["all_additions_have_readable_material"]) for row in results],
    "CH01_U02_original_candidates": len(report["CH01_U02"]["original_or_longitudinal_candidates"]),
}, ensure_ascii=False, indent=2))
