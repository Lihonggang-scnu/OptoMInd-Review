from __future__ import annotations
import hashlib
import json
import re
import sys
from pathlib import Path

WORKTREE = Path(r"F:\OptoMind-Review-2\outputs\guided_body_frozen_max_guide_plus_20261009\worktree")
ROOT = Path(r"F:\OptoMind-Review-2\outputs\guided_body_frozen_max_guide_plus_20261009")
MANIFEST = Path(r"F:\OptoMind-Review-2\outputs\evidence_body_round2_20261008_60cny\PREPARED_MANIFEST.json")
GUIDE = ROOT / "inputs" / "GUIDE_RECOVERED_FOR_REVIEW.json"
BASELINE_GUIDE = ROOT / "inputs" / "BASELINE_GUIDE.json"
CONFIG = WORKTREE / "config" / "guided_body_writer" / "plus_first.json"
TOKENIZER = Path(r"F:\OptoMind-Review-2\data\tokenizers\qwen3_5_9b\tokenizer.json")
OUT = ROOT / "PREVIEW_ALL7"
sys.path.insert(0, str(WORKTREE))

from scripts.upgrade3.evidence_body_writer import load_body_manifest, read_json, sha256_file
from optomind_research.runtime.upgrade3.guided_body_contracts import (
    validate_guide, compile_guided_materials, build_author_payload,
)
from optomind_research.runtime.upgrade3.guided_body_writer import (
    validate_config, _source_file_hashes,
)
from optomind_research.runtime.upgrade3.fullbody_writer import (
    _stage, _messages, _text_hash, PROMPT_ROOT,
)
from scripts.upgrade3.writer_candidates import tokenizer_counter
from optomind_research.runtime.upgrade3.writer_candidates import _hash

def recursive_key_count(value, key):
    if isinstance(value, dict):
        return (1 if key in value else 0) + sum(recursive_key_count(v, key) for v in value.values())
    if isinstance(value, list):
        return sum(recursive_key_count(v, key) for v in value)
    return 0

def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()

def json_bytes(value) -> bytes:
    return json.dumps(value, ensure_ascii=False, indent=2).encode("utf-8")

def write_json(path: Path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(json_bytes(value))

book, prepared = load_body_manifest(MANIFEST)
guide_raw = read_json(GUIDE)
baseline = read_json(BASELINE_GUIDE)
guide = validate_guide(guide_raw, book)
assert guide == validate_guide(baseline, book), "automatic guide differs from baseline after validation"
config = validate_config(read_json(CONFIG))
counter, meter = tokenizer_counter(str(TOKENIZER))
pack = compile_guided_materials(book)
input_hash = _hash(book)
guide_hash = _hash(guide)
source_hashes = _source_file_hashes()
code_hash = _hash(source_hashes)
prompt = (PROMPT_ROOT / "writer.md").read_text(encoding="utf-8")
profile = config["writer"]

if OUT.exists():
    for child in OUT.iterdir():
        if child.is_dir():
            import shutil
            shutil.rmtree(child)
        else:
            child.unlink()
OUT.mkdir(parents=True, exist_ok=True)

rows = []
for index, chapter in enumerate(guide["chapters"], 1):
    cid = chapter["chapter_id"]
    payload = build_author_payload(pack, guide, chapter, "", reread_atoms=[])
    assert set(payload) == {"manuscript_guide", "chapter_assignment", "materials", "accepted_body_markdown"}
    assert payload["accepted_body_markdown"] == ""
    assert payload["chapter_assignment"]["chapter_id"] == cid
    assert payload["chapter_assignment"] == next(row for row in guide["chapters"] if row["chapter_id"] == cid)
    materials = payload["materials"]
    assert all(handle in pack["source_identities"] for handle in materials["source_identities"])
    assert all(alias in pack["source_aliases"] for alias in materials["source_aliases"])
    assert recursive_key_count(payload, "outline_action") == 0
    messages = _messages(prompt, payload)
    stage = _stage(
        f"author_{index:03d}", "writer", messages, profile, config, counter,
        chapter_id=cid, guide_sha256=guide_hash, full_prefix_sha256=_text_hash(""),
    )
    stage_dir = OUT / "runs" / "all7" / "plans" / stage["stage_id"]
    stage_dir.mkdir(parents=True, exist_ok=True)
    messages_path = stage_dir / "MESSAGES.json"
    messages_path.write_bytes(json_bytes(messages))
    signature = {
        "schema_version": "optomind.guided_body_writer.v1",
        "stage_id": stage["stage_id"],
        "route": "guided_body",
        "execution_mode": "preview",
        "recording_fixture_sha256": None,
        "messages": messages,
        "effective_profile": profile,
        "wire_request_sha256": stage["estimate"]["wire_request_sha256"],
        "code_hash": code_hash,
        "dependencies": [{"input_hash": input_hash, "guide_sha256": guide_hash}],
    }
    cache_key = _hash(signature)
    public = {k: v for k, v in stage.items() if k != "messages"}
    public.update(
        cache_key=cache_key,
        dependencies=signature["dependencies"],
        messages_path=str(messages_path),
        model_calls=0,
        client_invocations=0,
        paid_dispatch_count=0,
    )
    request_path = stage_dir / "REQUEST_PREVIEW.json"
    write_json(request_path, {**public, "signature": {k: v for k, v in signature.items() if k != "messages"}})
    text = json.dumps(payload, ensure_ascii=False, indent=2)
    row = {
        "stage_id": stage["stage_id"],
        "chapter_id": cid,
        "title": chapter["title"],
        "messages_path": str(messages_path),
        "messages_sha256": sha256_file(messages_path),
        "messages_utf8_bytes": messages_path.stat().st_size,
        "request_preview_path": str(request_path),
        "cache_key": cache_key,
        "wire_request_sha256": stage["estimate"]["wire_request_sha256"],
        "effective_profile": profile,
        "estimate": stage["estimate"],
        "accepted_body_markdown_empty": payload["accepted_body_markdown"] == "",
        "top_level_payload_keys": sorted(payload),
        "material_schema_version": materials["schema_version"],
        "evidence_atom_count": len(materials["evidence_atoms"]),
        "source_identity_count": len(materials["source_identities"]),
        "source_alias_count": len(materials["source_aliases"]),
        "tool_material_count": len(materials["tool_materials"]),
        "source_navigation_count": len(materials["source_navigation"]),
        "read_protocol_keys": sorted(materials["read_protocol"]),
        "source_identity_handles": sorted(materials["source_identities"]),
        "source_identity_content_hash": _hash(materials["source_identities"]),
        "evidence_content_hash": _hash(materials["evidence_atoms"]),
        "tool_material_content_hash": _hash(materials["tool_materials"]),
        "chapter_assignment_hash": _hash(payload["chapter_assignment"]),
        "manuscript_guide_hash": _hash(payload["manuscript_guide"]),
        "outline_action_recursive_count": recursive_key_count(payload, "outline_action"),
        "payload_utf8_bytes": len(text.encode("utf-8")),
        "official_builder": "optomind_research.runtime.upgrade3.guided_body_contracts.build_author_payload",
        "official_meter": "optomind_research.runtime.upgrade3.writer_candidates._estimate via fullbody_writer._stage",
    }
    assert row["estimate"]["fits"], f"{cid} does not fit"
    rows.append(row)

audit = {
    "schema_version": "optomind.guided_body_preview_all7.v1",
    "execution_mode": "preview",
    "model_calls": 0,
    "paid_dispatch_count": 0,
    "provider_requests": 0,
    "no_ledger_mutation": True,
    "manifest_path": str(MANIFEST),
    "manifest_sha256": sha256_file(MANIFEST),
    "input_hash": input_hash,
    "guide_path": str(GUIDE),
    "guide_file_sha256": sha256_file(GUIDE),
    "baseline_guide_path": str(BASELINE_GUIDE),
    "baseline_guide_file_sha256": sha256_file(BASELINE_GUIDE),
    "validated_guide_hash": guide_hash,
    "guide_parsed_equal_to_baseline": guide == validate_guide(baseline, book),
    "config_path": str(CONFIG),
    "config": config,
    "tokenizer_path": str(TOKENIZER),
    "meter": meter,
    "source_manifest_hash": _hash(prepared),
    "code_hash": code_hash,
    "source_file_hashes": source_hashes,
    "chapter_count": len(rows),
    "chapter_ids": [row["chapter_id"] for row in rows],
    "all_fit": all(row["estimate"]["fits"] for row in rows),
    "max_estimated_cost_cny": max(row["estimate"]["estimated_max_cost_cny"] for row in rows),
    "sum_estimated_max_cost_cny": sum(row["estimate"]["estimated_max_cost_cny"] for row in rows),
    "rows": rows,
}
write_json(ROOT / "records" / "PREVIEW_ALL7_AUDIT.json", audit)
write_json(OUT / "RUN_MANIFEST.json", {
    "schema_version": "optomind.guided_body_preview_all7.v1",
    "execution_mode": "preview",
    "run_id": "all7",
    "input_hash": input_hash,
    "guide_sha256": guide_hash,
    "source_manifest_sha256": _hash(prepared),
    "source_file_hashes": source_hashes,
    "code_hash": code_hash,
    "stages": rows,
    "model_calls": 0,
    "paid_dispatch_count": 0,
    "material_preserved": True,
})
print(json.dumps({
    "audit_path": str(ROOT / "records" / "PREVIEW_ALL7_AUDIT.json"),
    "chapter_count": len(rows),
    "chapter_ids": [row["chapter_id"] for row in rows],
    "all_fit": audit["all_fit"],
    "sum_estimated_max_cost_cny": audit["sum_estimated_max_cost_cny"],
    "rows": [
        {
            "stage_id": row["stage_id"],
            "chapter_id": row["chapter_id"],
            "bytes": row["messages_utf8_bytes"],
            "tokens": row["estimate"]["prompt_tokens"],
            "reserved_input_tokens": row["estimate"]["reserved_input_tokens"],
            "limit": row["estimate"]["input_limit_tokens"],
            "max_cost_cny": row["estimate"]["estimated_max_cost_cny"],
            "atoms": row["evidence_atom_count"],
            "sources": row["source_identity_count"],
            "aliases": row["source_alias_count"],
            "outline_action": row["outline_action_recursive_count"],
        } for row in rows
    ],
}, ensure_ascii=False, indent=2))

