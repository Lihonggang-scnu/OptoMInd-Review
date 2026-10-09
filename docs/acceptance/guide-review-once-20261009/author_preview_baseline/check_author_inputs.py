from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / "worktree"
sys.path.insert(0, str(ROOT))

from scripts.upgrade3.evidence_body_writer import load_body_manifest
from scripts.upgrade3.writer_candidates import tokenizer_counter
from optomind_research.runtime.upgrade3.guided_body_contracts import (
    build_author_payload,
    compile_guided_materials,
    validate_guide,
)
from optomind_research.runtime.upgrade3.guided_body_writer import validate_config
from optomind_research.runtime.upgrade3.fullbody_writer import _messages, _stage


OUT = Path(__file__).resolve().parent
MANIFEST = Path(r"F:\OptoMind-Review-2\outputs\evidence_body_round2_20261008_60cny\PREPARED_MANIFEST.json")
TOKENIZER = Path(r"F:\OptoMind-Review-2\data\tokenizers\qwen3_5_9b\tokenizer.json")
GUIDE = Path(r"F:\OptoMind-Review-2\outputs\guide_review_acceptance_20261009_30cny\live_once\BASELINE_GUIDE.json")
CONFIG = ROOT / "config" / "guided_body_writer" / "plus_first.json"


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def sha(value: object) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def main() -> int:
    book, prepared = load_body_manifest(MANIFEST)
    guide = validate_guide(json.loads(GUIDE.read_text(encoding="utf-8")), book)
    pack = compile_guided_materials(book)
    config = validate_config(json.loads(CONFIG.read_text(encoding="utf-8")))
    counter, meter = tokenizer_counter(TOKENIZER)
    prompt = (ROOT / "prompts" / "guided_body_writer" / "writer.md").read_text(encoding="utf-8")

    expected_ids = list(prepared["expected_chapter_ids"])
    actual_ids = list(prepared["actual_chapter_ids"])
    guide_ids = [row["chapter_id"] for row in guide["chapters"]]
    rows = []
    errors = []
    for index, chapter in enumerate(guide["chapters"], 1):
        cid = chapter["chapter_id"]
        payload = build_author_payload(pack, guide, chapter, "")
        materials = payload["materials"]
        atom_handles = {atom["source_handle"] for atom in materials["evidence_atoms"]}
        identity_handles = set(materials["source_identities"])
        nav_handles = {row["source_handle"] for row in materials["source_navigation"]}
        expected_handles = set(pack["chapter_source_handles"][cid])
        arrangement = chapter["writing_arrangement"]
        guide_has_all_arrangements = all(row["writing_arrangement"] in payload["manuscript_guide"] for row in guide["chapters"])
        checks = {
            "chapter_id": cid,
            "chapter_order": index,
            "manuscript_guide_all_7_arrangements": guide_has_all_arrangements,
            "assignment_matches_validated_guide": payload["chapter_assignment"] == chapter,
            "assignment_arrangement_present": arrangement in payload["manuscript_guide"],
            "required_content_is_current": payload["chapter_assignment"].get("required_content", []) == chapter.get("required_content", []),
            "required_content_nonempty": bool(chapter.get("required_content")),
            "material_atoms_present": bool(materials["evidence_atoms"]),
            "atom_handles_have_identities": atom_handles <= identity_handles,
            "source_navigation_matches_identities": nav_handles == set(pack["source_identities"]),
            "chapter_handles_have_atoms": expected_handles <= atom_handles,
            "material_source_identity_scope_agrees": identity_handles == atom_handles,
            "accepted_prefix_empty_preview": payload["accepted_body_markdown"] == "",
        }
        if not all(checks.values()):
            errors.append({"chapter_id": cid, "failed": [key for key, value in checks.items() if not value]})
        messages = _messages(prompt, payload)
        stage = _stage(
            f"author_{index:03d}", "writer", messages, config["writer"], config, counter,
            chapter_id=cid, guide_sha256=sha(guide), full_prefix_sha256=hashlib.sha256(b"").hexdigest(),
            material_preserved=True,
        )
        chapter_dir = OUT / f"chapter_{index:03d}_{cid}"
        write_json(chapter_dir / "PAYLOAD.json", payload)
        write_json(chapter_dir / "MESSAGES.json", messages)
        write_json(chapter_dir / "REQUEST_PREVIEW.json", {key: value for key, value in stage.items() if key != "messages"})
        write_json(chapter_dir / "CHECK.json", {"checks": checks, "payload_sha256": sha(payload), "messages_sha256": sha(messages)})
        rows.append({
            "chapter_id": cid,
            "payload_path": str(chapter_dir / "PAYLOAD.json"),
            "messages_path": str(chapter_dir / "MESSAGES.json"),
            "request_preview_path": str(chapter_dir / "REQUEST_PREVIEW.json"),
            "payload_sha256": sha(payload),
            "messages_sha256": sha(messages),
            "estimate": stage["estimate"],
            "checks": checks,
        })

    result = {
        "schema_version": "optomind.root_author_input_check.v1",
        "guide_path": str(GUIDE),
        "manifest_path": str(MANIFEST),
        "tokenizer_path": str(TOKENIZER),
        "config_path": str(CONFIG),
        "input_mode": "live_source_manifest",
        "source_sha256": hashlib.sha256(MANIFEST.read_bytes()).hexdigest(),
        "guide_sha256": sha(guide),
        "book_sha256": book["book_sha256"],
        "meter": meter,
        "expected_chapter_ids": expected_ids,
        "actual_chapter_ids": actual_ids,
        "guide_chapter_ids": guide_ids,
        "chapter_count": len(rows),
        "all_7_chapter_payloads_saved": len(rows) == 7,
        "all_checks_pass": not errors and len(rows) == 7,
        "errors": errors,
        "preview_only_no_model_or_ledger_calls": True,
        "prefix_policy": "empty accepted_body_markdown for each independent payload preview",
        "chapters": rows,
    }
    write_json(OUT / "ROOT_AUTHOR_INPUT_CHECK.json", result)
    print(json.dumps({"all_checks_pass": result["all_checks_pass"], "chapter_count": len(rows), "output": str(OUT / "ROOT_AUTHOR_INPUT_CHECK.json")}, ensure_ascii=False))
    return 0 if result["all_checks_pass"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
