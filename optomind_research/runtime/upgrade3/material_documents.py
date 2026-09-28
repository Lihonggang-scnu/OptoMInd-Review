"""Held material can back a document (SM10 reopen).

The central material cache stores the parsed full text of 39,295 units.  None of
the papers the role-targeted acquisition found has its publisher PDF on this
machine, so under the old rule every one of them was unusable: the resolver looked
for a *.pdf under a downloads directory and refused anything else.  A generation
that has read a paper but not downloaded its PDF could therefore never cite it.

This module makes the text it actually holds citable, and labels it so nobody can
mistake it for a publisher PDF:

* one file per paper under the acquisition's own document root, named from the
  DOI when there is one and from the chunk identity when there is not;
* the declaration carries the file's sha256, the parser, the chunk identity and
  the character range inside the held text, so a consumer can verify the bytes
  before it registers anything;
* the parser is never a pdftotext parser, so every downstream receipt says
  "material_unit_text" where a downloaded PDF would say "pdftotext-layout".

Nothing here grants permission or decides domain.  It only says which bytes back
a record, and lets a consumer refuse when they do not.
"""

from __future__ import annotations

import hashlib
import os
import re
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

MATERIAL_DOCUMENT_PARSER = "material_unit_text"
MATERIAL_DOCUMENT_KIND = "material_unit_text"
DECLARATION_KEY = "material_document"


def _text(value: Any) -> str:
    return "" if value is None else str(value).strip()


def sha256_file(path: str | os.PathLike[str]) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _slug(value: str, fallback: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9.]+", "-", _text(value)).strip("-.")
    return cleaned or fallback


def materialise_document(
    *,
    root: str | os.PathLike[str],
    chunk_id: str,
    paper_id: str,
    doi: str,
    title: str,
    text: str,
    locator: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Write the held text of one chunk and declare what backs it."""

    base = Path(root)
    relative = Path("documents") / (
        _slug(doi, "") or _slug(paper_id, "paper") + "-" + _slug(chunk_id, "chunk")
    )
    relative = relative.with_suffix(".txt")
    target = base / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    body = _text(text)
    if not target.is_file() or target.read_text(encoding="utf-8") != body:
        temporary = target.with_name(target.name + ".tmp-%d" % os.getpid())
        temporary.write_text(body, encoding="utf-8", newline="")
        os.replace(temporary, target)
    return {
        "schema_version": "optomind.upgrade3.material_document.v1",
        "relative_path": relative.as_posix(),
        "path": str(target),
        "sha256": sha256_file(target),
        "bytes": target.stat().st_size,
        "parser": MATERIAL_DOCUMENT_PARSER,
        "source_kind": MATERIAL_DOCUMENT_KIND,
        "legal_basis": "central_material_cache_unit_text",
        "chunk_id": _text(chunk_id),
        "paper_id": _text(paper_id),
        "doi": _text(doi),
        "title": _text(title),
        "locator": dict(locator or {}),
    }


def declared_document(record: Mapping[str, Any]) -> dict[str, Any] | None:
    locator = record.get("source_locator")
    if not isinstance(locator, Mapping):
        return None
    declaration = locator.get(DECLARATION_KEY)
    if not isinstance(declaration, Mapping):
        return None
    return dict(declaration) if declaration else None


def _candidate_paths(declaration: Mapping[str, Any],
                     source_roots: Sequence[str | os.PathLike[str]]) -> list[Path]:
    relative = _text(declaration.get("relative_path"))
    declared = _text(declaration.get("path"))
    if not relative and not declared:
        return []
    if relative and not declared:
        declared = relative
    path = Path(declared)
    candidates: list[Path] = []
    if path.is_absolute():
        candidates.append(path)
    for root in source_roots:
        base = Path(root)
        if relative:
            candidates.append(base / relative)
        if not path.is_absolute():
            candidates.append(base / path)
    unique: list[Path] = []
    seen: set[str] = set()
    for item in candidates:
        key = str(item)
        if key not in seen:
            seen.add(key)
            unique.append(item)
    return unique


def resolve_declared_document(
    record: Mapping[str, Any],
    *,
    source_roots: Iterable[str | os.PathLike[str]] = (),
) -> tuple[dict[str, Any] | None, str]:
    """The held text that backs this record, or why it cannot be trusted."""

    declaration = declared_document(record)
    if declaration is None:
        return None, "no_declared_document"
    roots = [Path(item).expanduser().resolve(strict=False) for item in source_roots]
    chunk_id = _text(record.get("chunk_id"))
    declared_chunk = _text(declaration.get("chunk_id"))
    if declared_chunk and chunk_id and declared_chunk != chunk_id:
        return None, "declared_document_chunk_mismatch"
    expected = _text(declaration.get("sha256"))
    if not expected:
        return None, "declared_document_without_hash"
    inside_mismatch = False
    outside_match = False
    for candidate in _candidate_paths(declaration, source_roots):
        if not candidate.is_file():
            continue
        resolved = candidate.resolve(strict=False)
        within = any(_is_within(resolved, root) for root in roots)
        if sha256_file(candidate) != expected:
            if within:
                inside_mismatch = True
            continue
        if not within:
            # The right bytes, but not from a place this run may read.
            outside_match = True
            continue
        return {
            "path": str(candidate),
            "sha256": expected,
            "parser": _text(declaration.get("parser")) or MATERIAL_DOCUMENT_PARSER,
            "source_kind": _text(declaration.get("source_kind"))
            or MATERIAL_DOCUMENT_KIND,
            "legal_basis": _text(declaration.get("legal_basis"))
            or "central_material_cache_unit_text",
            "chunk_id": declared_chunk or chunk_id,
            "locator": dict(declaration.get("locator") or {}),
            "declaration": declaration,
        }, "resolved"
    if inside_mismatch:
        return None, "declared_document_hash_mismatch"
    if outside_match:
        return None, "declared_document_outside_source_roots"
    return None, "declared_document_missing"


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False
