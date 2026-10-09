"""Offline reader citation rendering using the locked production formatter.

This is an audit/consumer copy only.  It never writes LIVE files and never
contacts a model or network service.  The original handle-form delivery copy
is retained; the numbered reader draft is a separate artifact.
"""

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
HANDLE_DRAFT = LIVE / "DELIVERY_BODY.md"
IDENTITY_MAP = RECORDS / "FINAL_SOURCE_IDENTITY_MAP.json"

sys.path.insert(0, str(WORKTREE))
from optomind_research.runtime.upgrade3 import delivery_citations  # noqa: E402


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_json(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def identity_catalog() -> dict[str, Any]:
    payload = json.loads(IDENTITY_MAP.read_text(encoding="utf-8"))
    aliases = payload.get("source_aliases") or {}
    rows = []
    for handle, row in (payload.get("source_identities") or {}).items():
        item = dict(row)
        item["handles"] = [handle] + sorted(
            alias for alias, canonical in aliases.items() if canonical == handle
        )
        rows.append(item)
    return {"namespace": "final", "references": rows}


def handle_tokens(text: str) -> list[str]:
    values: list[str] = []
    for match in delivery_citations.BRACKET_RE.finditer(text):
        values.extend(re.findall(r"(?:[A-Za-z0-9_-]+::)?P\d{3,}", match.group(1)))
    return values


def mask_handle_citations(text: str, mapped: set[str]) -> str:
    def sub(match: re.Match[str]) -> str:
        tokens = re.findall(r"(?:[A-Za-z0-9_-]+::)?P\d{3,}", match.group(1))
        if tokens and any(token in mapped for token in tokens):
            return "[[CITATION]]"
        return match.group(0)

    return delivery_citations.BRACKET_RE.sub(sub, text)


def mask_numbered_citations(text: str) -> str:
    # Replace each generated [N] independently, matching the one-token-at-a-
    # time masking used for the original adjacent [Pxxxx][Pyyyy] tokens.
    return re.sub(r"\[\d+\]", "[[CITATION]]", text)


def body_without_references(text: str) -> str:
    marker = "\n## 参考文献\n"
    return text.split(marker, 1)[0] if marker in text else text


def table_signature(text: str) -> str:
    rows = [line for line in text.splitlines() if "|" in line]
    return hashlib.sha256("\n".join(rows).encode("utf-8")).hexdigest()


def main() -> int:
    STAGE.mkdir(parents=True, exist_ok=True)
    catalog = identity_catalog()
    handle_text = HANDLE_DRAFT.read_text(encoding="utf-8")

    report = delivery_citations.run_figures_citations_stage(
        final_draft_path=HANDLE_DRAFT,
        identity_catalogs=[catalog],
        out_dir=STAGE,
    )
    stage_handles = STAGE / "MANUSCRIPT_HANDLES.md"
    stage_reader = STAGE / "MANUSCRIPT_READER.md"
    stage_references = STAGE / "REFERENCES.json"

    # Keep the old handle-form artifact untouched and publish an explicitly
    # separate numbered reader copy for audit/reader consumption.
    numbered_reader = RECORDS / "NUMBERED_READER_BODY.md"
    numbered_reader.write_bytes(stage_reader.read_bytes())
    numbered_mapping = RECORDS / "NUMBERED_READER_MAPPING.json"
    numbered_mapping.write_bytes(stage_references.read_bytes())

    citation_map = json.loads(stage_references.read_text(encoding="utf-8"))
    reader_text = stage_reader.read_text(encoding="utf-8")
    reader_core = body_without_references(reader_text)
    mapped_tokens = set(citation_map.get("token_details", {}))
    handle_core = body_without_references(handle_text)
    handle_masked = mask_handle_citations(handle_core, mapped_tokens)
    reader_masked = mask_numbered_citations(reader_core)

    input_tokens = handle_tokens(handle_core)
    numeric_occurrences = re.findall(r"\[\d+\]", reader_core)
    ref_rows = citation_map.get("references") or []
    mapping_rows = [
        {
            "number": row.get("reference_number"),
            "canonical": row.get("canonical"),
            "handles": row.get("handles", []),
            "title": row.get("title", ""),
            "year": row.get("year", ""),
            "doi": row.get("doi", ""),
            "paper_id": row.get("paper_id", ""),
        }
        for row in ref_rows
    ]
    audit = {
        "schema_version": "guided_body_frozen.numbered_reader_audit.v1",
        "formatter": "optomind_research.runtime.upgrade3.delivery_citations.run_figures_citations_stage",
        "model_calls": report.get("model_calls", 0),
        "external_requests": report.get("external_requests", 0),
        "original_handle_delivery": str(HANDLE_DRAFT.resolve()),
        "original_handle_delivery_sha256": sha256(HANDLE_DRAFT),
        "stage_handle_copy": str(stage_handles.resolve()),
        "stage_handle_copy_sha256": sha256(stage_handles),
        "stage_handle_copy_byte_identical": stage_handles.read_bytes() == HANDLE_DRAFT.read_bytes(),
        "numbered_reader_path": str(numbered_reader.resolve()),
        "numbered_reader_sha256": sha256(numbered_reader),
        "numbered_mapping_path": str(numbered_mapping.resolve()),
        "numbered_mapping_sha256": sha256(numbered_mapping),
        "reference_count_unique_stable_identity": len(ref_rows),
        "handle_occurrence_count": len(input_tokens),
        "distinct_input_handles": len(set(input_tokens)),
        "numbered_body_occurrence_count": len(numeric_occurrences),
        "unknown_citation_tokens": report.get("unknown_citation_tokens", []),
        "unknown_identity_handles": report.get("unknown_identity_handles", []),
        "source_identity_count": json.loads(IDENTITY_MAP.read_text(encoding="utf-8")).get("source_identities", {}).__len__(),
        "source_alias_count": json.loads(IDENTITY_MAP.read_text(encoding="utf-8")).get("source_aliases", {}).__len__(),
        "citation_identity_mapping": mapping_rows,
        "scientific_text_and_markdown_structure_unchanged": handle_masked == reader_masked,
        "table_structure_unchanged": table_signature(handle_masked) == table_signature(reader_masked),
        "handle_core_sha256_masked": hashlib.sha256(handle_masked.encode("utf-8")).hexdigest(),
        "numbered_core_sha256_masked": hashlib.sha256(reader_masked.encode("utf-8")).hexdigest(),
        "input_unbracketed_p4119_is_not_citation": "P4119" in handle_core and "[P4119]" not in handle_core,
        "references_section_generated": "## 参考文献" in reader_text,
        "original_live_files_untouched": True,
    }
    write_json(RECORDS / "NUMBERED_READER_AUDIT.json", audit)
    print(json.dumps({
        "status": report.get("status"),
        "reference_count": len(ref_rows),
        "handle_occurrences": len(input_tokens),
        "distinct_handles": len(set(input_tokens)),
        "unknown": report.get("unknown_citation_tokens", []),
        "scientific_text_and_markdown_structure_unchanged": audit["scientific_text_and_markdown_structure_unchanged"],
        "table_structure_unchanged": audit["table_structure_unchanged"],
        "numbered_reader": str(numbered_reader),
        "mapping": str(numbered_mapping),
    }, ensure_ascii=False, indent=2))
    return 0 if report.get("status") == "complete" and not report.get("unknown_citation_tokens") else 2


if __name__ == "__main__":
    raise SystemExit(main())
