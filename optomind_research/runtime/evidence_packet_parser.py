"""Canonical parser for SECTION_EVIDENCE_PACKET.json.

All modules must read evidence packets through this parser.
Formal schema: top-level key "items", item field "chunk_id" (singular).
Raises ValueError if old-format "chunk_ids" field is used without "chunk_id".
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional


@dataclass
class EvidenceItem:
    chunk_id: str
    paper_id: str
    claim_ids: List[str] = field(default_factory=list)
    exact_spans: List[str] = field(default_factory=list)
    writing_permission: str = ""
    literature_role: str = ""
    scope_fit: str = ""
    not_usable_for: List[str] = field(default_factory=list)


@dataclass
class CanonicalEvidencePacket:
    schema_version: str
    section_id: str
    items: List[EvidenceItem]
    uncovered_claim_ids: List[str] = field(default_factory=list)


def load_section_evidence_packet(path: Path) -> CanonicalEvidencePacket:
    """Parse SECTION_EVIDENCE_PACKET.json using the canonical v2 schema.

    Raises:
        FileNotFoundError: if path does not exist.
        ValueError: if an item uses old 'chunk_ids' field instead of 'chunk_id',
                    or if top-level 'items' key is missing.
    """
    raw = json.loads(path.read_text(encoding="utf-8"))

    if "items" not in raw:
        raise ValueError(
            f"Evidence packet at {path} missing top-level 'items' key "
            f"(found: {list(raw.keys())})"
        )

    items: List[EvidenceItem] = []
    for i, item in enumerate(raw["items"]):
        if "chunk_id" not in item:
            if "chunk_ids" in item:
                raise ValueError(
                    f"Evidence item[{i}] uses deprecated 'chunk_ids' field; "
                    "canonical schema requires singular 'chunk_id'"
                )
            raise ValueError(
                f"Evidence item[{i}] missing 'chunk_id' (found keys: {list(item.keys())})"
            )
        items.append(
            EvidenceItem(
                chunk_id=item["chunk_id"],
                paper_id=item.get("paper_id", ""),
                claim_ids=item.get("claim_ids", []),
                exact_spans=item.get("exact_spans", []),
                writing_permission=item.get("writing_permission", ""),
                literature_role=item.get("literature_role", ""),
                scope_fit=item.get("scope_fit", ""),
                not_usable_for=item.get("not_usable_for", []),
            )
        )

    return CanonicalEvidencePacket(
        schema_version=raw.get("schema_version", ""),
        section_id=raw.get("section_id", ""),
        items=items,
        uncovered_claim_ids=raw.get("uncovered_claim_ids", []),
    )
