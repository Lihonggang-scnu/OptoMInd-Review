from pathlib import Path
import hashlib
import json

ROOT = Path(__file__).resolve().parent


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


records = {
    "new_legacy_plus": {
        "source_commit": "054f94a3e9659d5b6c4ffcc9bfafd9ae646d0512",
        "model": "qwen3.5-plus",
        "input_file_sha256": "6d6ae867ceae5c88f25d36f88087925341af5cf82f1672680157a3588d14e5f7",
        "canonical_book_sha256": "94590c2b7a3365b76af5e0fe4bad5ae5a677cd6d36f80e48a1e4ab90aa098aaa",
        "route": "legacy unit writer, no guide, no actual prior BODY",
    },
    "historical_173_flash": {
        "body_path": r"F:\OptoMind-Review-2\outputs\body_full_staged_acceptance_20261004_40cny\body_assembly_final\REVIEW_DRAFT_HANDLES.md",
        "source_commit": "7e293b63603157e741ec9b68f30565fcb4065f8a",
        "archive_commit": "e0615b0f83ed001042b12c539a08a316f3ef6ab6",
        "model": "qwen3.7-flash",
        "normalized_cited_identities": 173,
        "comparison_note": "Historical output only; outline/material version differs. The saved historical BODY contains known citation issues, including Q01 and the erroneous numeric-to-P0011 conversion subsequently repaired separately.",
    },
    "recent_117_guided_plus": {
        "body_path": r"F:\OptoMind-Review-2\outputs\guided_body_compact_auto_metadata_20261009_12cny\LIVE\FULL_BODY.md",
        "source_commit": "737ef95aff9f6ffb8061427489ca7e52fade5a5b",
        "archive_commit": "401a20b9ac44a4cc2a6d03b946cb0d53975bc760",
        "model": "qwen3.5-plus",
        "normalized_cited_identities": 117,
        "input_canonical_book_sha256": "94590c2b7a3365b76af5e0fe4bad5ae5a677cd6d36f80e48a1e4ab90aa098aaa",
        "comparison_note": "Same original FULL_BODY_INPUT, frozen historical Max guide, sequential Plus chapter authors receive actual prior BODY. Different grouping, writing budgets and prompt from new legacy unit run.",
    },
}
for value in records.values():
    if "body_path" in value:
        p = Path(value["body_path"])
        value.update(body_bytes=p.stat().st_size, body_sha256=sha(p))
(ROOT / "ROOT_COMPARISON_PROVENANCE.json").write_text(json.dumps(records, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

attempt = next(ROOT.glob("units/Ch3/U3_5/*/attempt_001"))
messages = json.loads((attempt / "UNIT_MESSAGES.json").read_text(encoding="utf-8"))
payload = json.loads(messages[1]["content"])
result = json.loads((attempt / "UNIT_RESULT.json").read_text(encoding="utf-8"))
trace = {
    "purpose": "Post-generation root evaluation only; never supplied to an author",
    "chapter_id": "Ch3",
    "unit_id": "U3_5",
    "actual_messages_relative_path": (attempt / "UNIT_MESSAGES.json").relative_to(ROOT).as_posix(),
    "actual_messages_file_sha256": sha(attempt / "UNIT_MESSAGES.json"),
    "actual_paragraph_tasks": payload["paragraph_tasks"],
    "body": (attempt / "UNIT_BODY.md").read_text(encoding="utf-8"),
    "result_complete": result.get("complete"),
    "finish_reason": result.get("finish_reason"),
    "issues": result.get("issues"),
    "effective_request": result.get("effective_request"),
    "usage": result.get("usage"),
    "root_observation": "P01 proxy/exposure limitation is written. P02 local threshold/delivery and P03 microdialysis/MALDI-MSI/paired sampling tasks are not developed. Normal stop plus completion metadata does not establish full task consumption. No paid repair or BODY edit performed in this round.",
}
(ROOT / "ROOT_TASK_CONSUMPTION_EXAMPLE.json").write_text(json.dumps(trace, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
print("comparison provenance and actual task-consumption trace saved")

attempt5 = next(ROOT.glob("units/Ch5/Ch5_U01/*/attempt_001"))
msg5 = json.loads((attempt5 / "UNIT_MESSAGES.json").read_text(encoding="utf-8"))
p5 = json.loads(msg5[1]["content"])
source = next(s for s in p5["sources"] if s.get("source_handle") == "P0593")
tools = p5.get("chapter_tool_materials", [])
identity_fields = ("source_handle", "paper_id", "canonical_paper_id", "title", "doi", "aliases", "identity_status", "original_source_handle")
matched_tools = []
for material in tools:
    serialized = json.dumps(material, ensure_ascii=False)
    if "TACITO" in serialized or "10.1038/s41591-025-04189-2" in serialized:
        matched_tools.append(material)
conflict = {
    "purpose": "Input-side identity conflict traced after generation; no input repair performed",
    "actual_messages_relative_path": (attempt5 / "UNIT_MESSAGES.json").relative_to(ROOT).as_posix(),
    "actual_messages_file_sha256": sha(attempt5 / "UNIT_MESSAGES.json"),
    "catalog_P0593": {k: source.get(k) for k in identity_fields if k in source},
    "tasks_mentioning_TACITO_or_P0593": [task for task in p5["paragraph_tasks"] if any(w in json.dumps(task, ensure_ascii=False) for w in ("TACITO", "P0593"))],
    "tool_records_mentioning_TACITO": matched_tools,
    "body_relative_path": (attempt5 / "UNIT_BODY.md").relative_to(ROOT).as_posix(),
    "body_sha256": sha(attempt5 / "UNIT_BODY.md"),
    "root_observation": "Task uses P0593 for TACITO while source catalog P0593 belongs to allopurinol paper DOI 10.1136/jitc-2025-014132. TACITO tool record carries DOI 10.1038/s41591-025-04189-2 and conflict marker, but repeats P0593 in usable content. New author accepts task P token, creating a semantic identity error although the token resolves. Recent guide Plus notices this same-input conflict. This is not a new fabricated P identity and unknown-token count cannot certify correctness.",
}
(ROOT / "ROOT_INPUT_IDENTITY_CONFLICT.json").write_text(json.dumps(conflict, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
print("input-side identity-conflict trace saved")
