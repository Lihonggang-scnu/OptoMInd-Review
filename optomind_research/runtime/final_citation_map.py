"""Build the one authoritative citation inventory from final manuscript text.

This module deliberately owns only final-text identity and de-duplication.  It
does not resolve DOI metadata or generate BibTeX; those remain the publication
metadata resolver's responsibilities.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any, Mapping

from .artifact_store import atomic_write_json

_REF = re.compile(r"\[REF:([^\]\s]+)\]")
_PANDOC = re.compile(r"\[@([^\]\s;,@]+)")
_LATEX = re.compile(r"\\cite[a-zA-Z*]*\{([^}]+)\}")


def _normalise_doi(value: Any) -> str:
    value = str(value or "").strip().lower()
    value = re.sub(r"^doi:\s*", "", value)
    return re.sub(r"^https?://(?:dx\.)?doi\.org/", "", value)


def _alias_keys(value: Any) -> set[str]:
    """Return explicit, lossless lookup keys for a citation alias."""
    raw = str(value or "").strip()
    if not raw:
        return set()
    lower = raw.lower()
    keys = {"raw:" + lower}
    doi = _normalise_doi(raw)
    if doi and (lower.startswith(("doi:", "http://", "https://")) or "/" in doi):
        keys.add("doi:" + doi)
    if lower.startswith("s2:"):
        keys.add("s2:" + lower[3:].strip())
    elif lower == "s2" or lower.startswith("s2_"):
        keys.add("s2:" + lower)
    # Publication LaTeX/BibTeX keys are generated from a DOI/S2 identity and
    # carry an eight-hex digest suffix to keep keys unique.  Decode only that
    # exact, documented form; arbitrary underscore strings are never guessed.
    bib_key = lower
    digest_match = re.match(r"^(doi_[a-z0-9_]+|s2_[a-f0-9]{40})_([a-f0-9]{8})$", bib_key)
    if digest_match:
        bib_key = digest_match.group(1)
    if bib_key.startswith("doi_"):
        encoded = bib_key[4:].strip("_")
        if encoded.startswith("10_"):
            # The resolver's BibTeX key encoder replaces DOI punctuation with
            # underscores.  The catalog supplies the authoritative DOI; this
            # key is only an exact join token, never an identity guess.
            keys.add("bibdoi:" + bib_key)
    elif bib_key.startswith("s2_") and re.fullmatch(r"s2_[a-f0-9]{40}", bib_key):
        keys.add("s2:" + bib_key[3:])
    return keys


def _metadata_aliases(row: Mapping[str, Any]) -> set[str]:
    keys: set[str] = set()
    for name in (
        "paper_id",
        "doi",
        "s2_id",
        "s2_paper_id",
        "semantic_scholar_id",
        "semantic_scholar_paper_id",
        "corpus_id",
        "marker_id",
        "identity",
        "canonical_identity",
    ):
        keys.update(_alias_keys(row.get(name)))
        if name in {
            "s2_id",
            "s2_paper_id",
            "semantic_scholar_id",
            "semantic_scholar_paper_id",
            "corpus_id",
        } and row.get(name):
            keys.add("s2:" + str(row[name]).strip().lower())
    for name in ("aliases", "paper_ids", "s2_ids", "x_markers", "markers"):
        value = row.get(name)
        if isinstance(value, (list, tuple, set)):
            for item in value:
                keys.update(_alias_keys(item))
    external = row.get("external_ids")
    if isinstance(external, Mapping):
        for value in external.values():
            keys.update(_alias_keys(value))
    external = row.get("externalIds")
    if isinstance(external, Mapping):
        for value in external.values():
            keys.update(_alias_keys(value))
    canonical = str(row.get("canonical_identity") or "").strip()
    if canonical:
        keys.update(_alias_keys(canonical))
    doi = _normalise_doi(row.get("doi"))
    if doi:
        encoded = "doi_" + re.sub(r"[^a-z0-9]+", "_", doi).strip("_")
        keys.add("bibdoi:" + encoded)
    return keys


_RECORD_CONTAINERS = frozenset({"citations", "entries", "records", "papers", "items"})


def _iter_metadata_records(value: Any, *, anchor: str = "") -> list[dict[str, Any]]:
    """Extract citation-shaped records from supported catalog containers.

    Catalogs in the harness have used both an ``entries`` list and a
    ``records`` alias map.  Nested provider payloads are handled by merging a
    record's explicit ``metadata`` object; the mapping key is retained as a
    paper-id anchor when a record omits one.  No title or string similarity is
    used for identity resolution.
    """
    records: list[dict[str, Any]] = []
    if isinstance(value, list):
        for item in value:
            records.extend(_iter_metadata_records(item, anchor=anchor))
        return records
    if not isinstance(value, Mapping):
        return records

    current = dict(value)
    if anchor and not current.get("paper_id"):
        current["paper_id"] = anchor
    nested = current.get("metadata")
    if isinstance(nested, Mapping):
        merged = dict(current)
        merged.update(nested)
        # Keep aliases on the parent row as well as those supplied by the
        # nested metadata payload.
        for name in ("aliases", "markers", "x_markers", "paper_ids", "s2_ids"):
            parent_values = current.get(name)
            nested_values = nested.get(name)
            if isinstance(parent_values, (list, tuple, set)) or isinstance(
                nested_values, (list, tuple, set)
            ):
                merged[name] = [
                    *(
                        list(parent_values)
                        if isinstance(parent_values, (list, tuple, set))
                        else []
                    ),
                    *(
                        list(nested_values)
                        if isinstance(nested_values, (list, tuple, set))
                        else []
                    ),
                ]
        current = merged
    if _metadata_aliases(current):
        records.append(current)

    for key, child in value.items():
        if key == "metadata":
            # Already merged above; recurse too for deeper provider envelopes.
            records.extend(_iter_metadata_records(child, anchor=anchor))
        elif key in _RECORD_CONTAINERS:
            if isinstance(child, Mapping):
                for child_anchor, item in child.items():
                    records.extend(
                        _iter_metadata_records(item, anchor=str(child_anchor))
                    )
            else:
                records.extend(_iter_metadata_records(child, anchor=anchor))
        elif isinstance(child, (Mapping, list)):
            records.extend(_iter_metadata_records(child, anchor=anchor))
    return records


def _identity(marker: str, metadata: Mapping[str, Any]) -> str:
    doi = _normalise_doi(metadata.get("doi"))
    if doi:
        return "doi:" + doi
    for name in (
        "s2_id",
        "s2_paper_id",
        "semantic_scholar_id",
        "semantic_scholar_paper_id",
        "corpus_id",
    ):
        value = str(metadata.get(name) or "").strip().lower()
        if value:
            return "s2:" + value
    marker_lower = marker.strip().lower()
    marker_doi = _normalise_doi(marker)
    if marker_doi and (
        marker_lower.startswith(("doi:", "http://", "https://"))
        or marker_doi.startswith("10.")
    ):
        return "doi:" + marker_doi
    if marker_lower.startswith("s2:") and marker_lower[3:].strip():
        return "s2:" + marker_lower[3:].strip()
    title = re.sub(r"\s+", " ", str(metadata.get("title") or "").strip().lower())
    year = str(metadata.get("year") or "").strip()
    if title and year:
        digest = hashlib.sha256((title + "\x1f" + year).encode("utf-8")).hexdigest()[:20]
        return "title_year:" + digest
    # A stable final marker remains distinct until the metadata resolver can
    # supply the DOI or title/year fallback.  It is never merged by a label.
    return "marker:" + marker


def _markers(text: str) -> list[str]:
    values = [*(_REF.findall(text)), *(_PANDOC.findall(text))]
    for group in _LATEX.findall(text):
        values.extend(part.strip() for part in group.split(",") if part.strip())
    return values


def build_final_citation_map(
    *,
    markdown_path: Path,
    output_path: Path,
    intermediate_map_path: Path | None = None,
    metadata_catalog_path: Path | None = None,
) -> dict[str, Any]:
    """Persist final cited identities in first-occurrence order.

    An authoring map may enrich trace status and metadata, but cannot add an
    identity absent from final text.  This prevents a pre-enhancement map from
    reporting a different reference count than the delivered manuscript.
    """

    text = Path(markdown_path).read_text(encoding="utf-8")
    intermediate: dict[str, dict[str, Any]] = {}
    for source_path in (intermediate_map_path, metadata_catalog_path):
        if not source_path or not Path(source_path).is_file():
            continue
        try:
            raw = json.loads(Path(source_path).read_text(encoding="utf-8"))
        except (OSError, ValueError, json.JSONDecodeError):
            raw = {}
        for row in _iter_metadata_records(raw):
            aliases = _metadata_aliases(row)
            existing_rows = [
                intermediate[key]
                for key in aliases
                if key in intermediate
            ]
            # A source record can be reached through either its hash, DOI,
            # S2 ID, X marker, or generated BibTeX key.  All aliases must
            # point at the same merged view: otherwise a lookup through the
            # new BibTeX-only alias sees catalog fields but loses authoring
            # trace_status, while the old hash sees the inverse.
            merged: dict[str, Any] = {}
            for existing in existing_rows:
                for field, value in existing.items():
                    if merged.get(field) in (None, "", [], {}):
                        merged[field] = value
            for field, value in row.items():
                if merged.get(field) in (None, "", [], {}):
                    merged[field] = value
            for key in aliases:
                intermediate[key] = merged

    citations: list[dict[str, Any]] = []
    seen: set[str] = set()
    for position, marker in enumerate(_markers(text)):
        raw = next(
            (
                intermediate[key]
                for key in sorted(_alias_keys(marker))
                if key in intermediate
            ),
            {},
        )
        identity = _identity(marker, raw)
        if identity in seen:
            continue
        seen.add(identity)
        citations.append(
            {
                "paper_id": marker,
                "citation_identity": identity,
                "trace_status": str(raw.get("trace_status") or "final_text_only"),
                "first_occurrence": position,
                **{
                    key: raw[key]
                    for key in ("doi", "s2_id", "title", "year")
                    if raw.get(key) not in (None, "")
                },
            }
        )
    result = {
        "schema_version": "research_harness.final_citation_map.v1",
        "source_markdown": str(markdown_path),
        "citation_count": len(citations),
        "citations": citations,
    }
    atomic_write_json(output_path, result)
    return result
