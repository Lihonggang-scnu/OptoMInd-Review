"""Render a separate numbered reader copy with the locked formatter."""

from __future__ import annotations

import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
WORKTREE = ROOT / "worktree"
LIVE = ROOT / "LIVE"
RECORDS = ROOT / "records"
STAGE = RECORDS / "NUMBERED_READER_STAGE"
# FINAL_CITATION_AUDIT counts the immutable FULL_BODY manuscript.  Use that
# source for the numbered compatibility check; DELIVERY_BODY remains a
# separately preserved formatter output and is not overwritten.
HANDLE_DRAFT = LIVE / "FULL_BODY.md"
DELIVERY_BODY = LIVE / "DELIVERY_BODY.md"
INPUT_PATH = next((LIVE / "inputs").glob("*/FULL_BODY_INPUT.json"))

sys.path.insert(0, str(WORKTREE))
from optomind_research.runtime.upgrade3 import delivery_citations  # noqa: E402


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_json(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def catalog_from_input() -> dict[str, Any]:
    book = json.loads(INPUT_PATH.read_text(encoding="utf-8"))
    aliases = book.get("source_aliases") or {}
    rows = []
    for handle, raw in (book.get("source_identities") or {}).items():
        row = dict(raw)
        row["handles"] = [handle] + sorted(alias for alias, canonical in aliases.items() if canonical == handle)
        rows.append(row)
    return {"namespace": "compact_final", "references": rows}


def mask_handle(text: str, mapped: set[str]) -> str:
    def sub(match: re.Match[str]) -> str:
        tokens = re.findall(r"(?:[A-Za-z0-9_-]+::)?P\d{3,}", match.group(1))
        return "[[CITATION]]" if tokens and any(token in mapped for token in tokens) else match.group(0)
    return delivery_citations.BRACKET_RE.sub(sub, text)


def mask_numbered(text: str) -> str:
    return re.sub(r"\[\d+\]", "[[CITATION]]", text)


def core(text: str) -> str:
    marker = "\n## 参考文献\n"
    return text.split(marker, 1)[0] if marker in text else text


def table_hash(text: str) -> str:
    rows = "\n".join(line for line in text.splitlines() if "|" in line)
    return hashlib.sha256(rows.encode("utf-8")).hexdigest()


def main() -> int:
    STAGE.mkdir(parents=True, exist_ok=True)
    handle_text = HANDLE_DRAFT.read_text(encoding="utf-8")
    delivery_text = DELIVERY_BODY.read_text(encoding="utf-8")
    result = delivery_citations.run_figures_citations_stage(
        final_draft_path=HANDLE_DRAFT,
        identity_catalogs=[catalog_from_input()],
        out_dir=STAGE,
    )
    stage_handles = STAGE / "MANUSCRIPT_HANDLES.md"
    stage_reader = STAGE / "MANUSCRIPT_READER.md"
    stage_mapping = STAGE / "REFERENCES.json"
    reader = RECORDS / "NUMBERED_READER_BODY.md"
    mapping = RECORDS / "NUMBERED_READER_MAPPING.json"
    reader.write_bytes(stage_reader.read_bytes())
    mapping.write_bytes(stage_mapping.read_bytes())

    map_data = json.loads(stage_mapping.read_text(encoding="utf-8"))
    handle_core = core(handle_text)
    reader_core = core(stage_reader.read_text(encoding="utf-8"))
    mapped_tokens = set(map_data.get("token_details", {}))
    handle_tokens = []
    for match in delivery_citations.BRACKET_RE.finditer(handle_core):
        handle_tokens.extend(re.findall(r"(?:[A-Za-z0-9_-]+::)?P\d{3,}", match.group(1)))
    masked_handle = mask_handle(handle_core, mapped_tokens)
    masked_reader = mask_numbered(reader_core)
    numbered_occurrences = re.findall(r"\[\d+\]", reader_core)
    delivery_tokens = []
    for match in delivery_citations.BRACKET_RE.finditer(delivery_text):
        delivery_tokens.extend(re.findall(r"(?:[A-Za-z0-9_-]+::)?P\d{3,}", match.group(1)))
    audit = {
        "schema_version": "guided_body_compact_auto_metadata.numbered_reader_audit.v1",
        "formatter": "optomind_research.runtime.upgrade3.delivery_citations.run_figures_citations_stage",
        "numbering_source": "LIVE/FULL_BODY.md, matching FINAL_CITATION_AUDIT.body_path; LIVE/DELIVERY_BODY.md preserved separately",
        "model_calls": result.get("model_calls", 0),
        "external_requests": result.get("external_requests", 0),
        "original_handle_delivery_path": str(HANDLE_DRAFT.resolve()),
        "original_handle_delivery_sha256": sha(HANDLE_DRAFT),
        "preserved_delivery_body_path": str(DELIVERY_BODY.resolve()),
        "preserved_delivery_body_sha256": sha(DELIVERY_BODY),
        "preserved_delivery_body_citation_positions": len(delivery_tokens),
        "preserved_delivery_body_distinct_raw_handles": len(set(delivery_tokens)),
        "stage_handle_copy_path": str(stage_handles.resolve()),
        "stage_handle_copy_sha256": sha(stage_handles),
        "stage_handle_copy_byte_identical": stage_handles.read_bytes() == HANDLE_DRAFT.read_bytes(),
        "numbered_reader_path": str(reader.resolve()),
        "numbered_reader_sha256": sha(reader),
        "mapping_path": str(mapping.resolve()),
        "mapping_sha256": sha(mapping),
        "citation_positions": len(handle_tokens),
        "distinct_raw_handles": len(set(handle_tokens)),
        "unique_stable_identity_count": len(map_data.get("references", [])),
        "numbered_body_positions": len(numbered_occurrences),
        "unknown_citation_tokens": result.get("unknown_citation_tokens", []),
        "unknown_identity_handles": result.get("unknown_identity_handles", []),
        "scientific_text_and_markdown_structure_unchanged": masked_handle == masked_reader,
        "table_structure_unchanged": table_hash(masked_handle) == table_hash(masked_reader),
        "masked_handle_core_sha256": hashlib.sha256(masked_handle.encode("utf-8")).hexdigest(),
        "masked_numbered_core_sha256": hashlib.sha256(masked_reader.encode("utf-8")).hexdigest(),
        "references_section_generated": "## 参考文献" in stage_reader.read_text(encoding="utf-8"),
        "body_audit_expected_positions": 409,
        "body_audit_expected_unique_doi_identities": 117,
        "input_unbracketed_p4119_is_not_citation": "P4119" in handle_core and "[P4119]" not in handle_core,
    }
    write_json(RECORDS / "NUMBERED_READER_AUDIT.json", audit)
    print(json.dumps({
        "status": result.get("status"),
        "citation_positions": audit["citation_positions"],
        "distinct_raw_handles": audit["distinct_raw_handles"],
        "unique_stable_identity_count": audit["unique_stable_identity_count"],
        "numbered_body_positions": audit["numbered_body_positions"],
        "unknown": audit["unknown_citation_tokens"],
        "scientific_text_and_markdown_structure_unchanged": audit["scientific_text_and_markdown_structure_unchanged"],
        "table_structure_unchanged": audit["table_structure_unchanged"],
        "reader": str(reader),
        "mapping": str(mapping),
    }, ensure_ascii=False, indent=2))
    return 0 if result.get("status") == "complete" and not result.get("unknown_citation_tokens") else 2


if __name__ == "__main__":
    raise SystemExit(main())
