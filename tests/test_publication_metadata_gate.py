"""Regression tests for the final-publication metadata gate.

Covers:
- A title-only reference that can be recovered via Crossref bibliographic
  search (_recover_incomplete_final_references).
- A title-only reference enriched from a local central-cache record (resolver
  level: LocalMetadataIndex.find_by_title + _resolve_identity title lookup).
- A title-only reference that cannot be recovered produces a clear,
  actionable ValueError that names the paper_id and title.
- Recovery is skipped when enrich_crossref=False or budget=0.
- Already-complete records are not re-queried.
- Existing _reference_bibtex contracts (complete article, misc locator note)
  are unaffected.

No test makes a live network call.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

import pytest

from optomind_research.runtime.latex_publication_renderer import (
    _citation_key,
    _recover_incomplete_final_references,
    _reference_bibtex,
)
from optomind_research.runtime.publication_metadata_resolver import (
    LocalMetadataIndex,
    PublicationMetadataResolver,
    ResolverOptions,
    normalize_title,
)


# ── helpers ────────────────────────────────────────────────────────────────


def _make_record(
    *,
    paper_id: str = "test_paper",
    title: str = "Test Paper Title",
    authors: list[str] | None = None,
    year: int | str | None = None,
    venue: str = "",
    doi: str = "",
    url: str = "",
) -> dict[str, Any]:
    return {
        "paper_id": paper_id,
        "title": title,
        "authors": authors if authors is not None else [],
        "year": year if year is not None else "",
        "venue": venue,
        "doi": doi,
        "url": url,
        "reference_kind": "article" if venue else "misc",
    }


def _keys(ids: list[str]) -> dict[str, str]:
    return {pid: _citation_key(pid) for pid in ids}


_CROSSREF_MODULE = (
    "optomind_research.runtime.latex_publication_renderer"
    "._crossref_metadata_by_bibliographic_record"
)

_TITLE_ONLY = (
    "emission dynamics of a qubit in photonic lattices"
    " with synthetic potentials"
)

_CROSSREF_HIT: dict[str, Any] = {
    "authors": ["A. Author", "B. Coauthor"],
    "year": 2022,
    "venue": "Physical Review Letters",
    "doi": "10.1103/PhysRevLett.129.123456",
    "title": (
        "Emission Dynamics of a Qubit in Photonic Lattices"
        " with Synthetic Potentials"
    ),
    "metadata_source": "crossref_bibliographic_match",
}


# ── _recover_incomplete_final_references ───────────────────────────────────


def test_title_only_reference_recovered_via_crossref() -> None:
    """A title-only record without authors/year is enriched when Crossref
    returns a high-confidence bibliographic match."""

    records = {_TITLE_ONLY: _make_record(paper_id=_TITLE_ONLY, title=_TITLE_ONLY)}

    with patch(_CROSSREF_MODULE, return_value=_CROSSREF_HIT):
        updated, audit = _recover_incomplete_final_references(
            [_TITLE_ONLY],
            records,
            enrich_crossref=True,
        )

    assert updated[_TITLE_ONLY]["authors"] == ["A. Author", "B. Coauthor"]
    assert str(updated[_TITLE_ONLY]["year"]) == "2022"
    assert updated[_TITLE_ONLY]["venue"] == "Physical Review Letters"
    assert len(audit) == 1
    assert set(audit[0]["recovered_fields"]) >= {"authors", "year"}
    assert audit[0]["crossref_matched"] is True
    assert audit[0]["missing_before_recovery"] == ["authors", "year"]


def test_recovery_does_not_overwrite_existing_fields() -> None:
    """Recovery fills only genuinely absent fields; existing values survive."""

    existing_authors = ["Existing Author"]
    records = {
        _TITLE_ONLY: _make_record(
            paper_id=_TITLE_ONLY,
            title=_TITLE_ONLY,
            authors=existing_authors,
            year="",  # only year is missing
        )
    }

    with patch(_CROSSREF_MODULE, return_value=_CROSSREF_HIT):
        updated, audit = _recover_incomplete_final_references(
            [_TITLE_ONLY],
            records,
            enrich_crossref=True,
        )

    # existing authors must not be replaced
    assert updated[_TITLE_ONLY]["authors"] == existing_authors
    # year was missing and should be filled
    assert str(updated[_TITLE_ONLY]["year"]) == "2022"
    assert "year" in audit[0]["recovered_fields"]
    assert "authors" not in audit[0]["recovered_fields"]


def test_recovery_skipped_when_enrich_crossref_is_false() -> None:
    """Recovery is a no-op when enrich_crossref=False."""

    records = {_TITLE_ONLY: _make_record(paper_id=_TITLE_ONLY, title=_TITLE_ONLY)}

    with patch(_CROSSREF_MODULE) as mock_crossref:
        updated, audit = _recover_incomplete_final_references(
            [_TITLE_ONLY],
            records,
            enrich_crossref=False,
        )

    mock_crossref.assert_not_called()
    assert audit == []
    assert updated[_TITLE_ONLY]["authors"] == []


def test_recovery_budget_respected_at_zero() -> None:
    """With max_crossref_requests=0 recovery is skipped with an audit reason."""

    records = {_TITLE_ONLY: _make_record(paper_id=_TITLE_ONLY, title=_TITLE_ONLY)}

    with patch(_CROSSREF_MODULE) as mock_crossref:
        updated, audit = _recover_incomplete_final_references(
            [_TITLE_ONLY],
            records,
            enrich_crossref=True,
            max_crossref_requests=0,
        )

    mock_crossref.assert_not_called()
    assert len(audit) == 1
    assert audit[0]["skipped_reason"] == "crossref_budget_exhausted"
    assert audit[0]["recovered_fields"] == []


def test_recovery_budget_limits_calls_across_multiple_candidates() -> None:
    """Only the first max_crossref_requests candidates are searched; the rest
    receive a skipped_reason audit entry."""

    ids = [f"title_only_{i}" for i in range(5)]
    records = {
        pid: _make_record(paper_id=pid, title=f"Title only paper {i}")
        for i, pid in enumerate(ids)
    }

    with patch(_CROSSREF_MODULE, return_value=_CROSSREF_HIT) as mock_crossref:
        _, audit = _recover_incomplete_final_references(
            ids,
            records,
            enrich_crossref=True,
            max_crossref_requests=3,
        )

    assert mock_crossref.call_count == 3
    skipped = [row for row in audit if row.get("skipped_reason")]
    assert len(skipped) == 2


def test_already_complete_records_are_not_requeried() -> None:
    """References that already have both authors and year skip Crossref."""

    pid = "complete_ref"
    records = {
        pid: _make_record(
            paper_id=pid,
            title="Complete reference title",
            authors=["Known Author"],
            year=2021,
            venue="Nature",
        )
    }

    with patch(_CROSSREF_MODULE) as mock_crossref:
        updated, audit = _recover_incomplete_final_references(
            [pid],
            records,
            enrich_crossref=True,
        )

    mock_crossref.assert_not_called()
    assert audit == []


def test_unrecoverable_title_only_reference_leaves_record_unchanged() -> None:
    """When Crossref returns no match the record is left unchanged."""

    records = {_TITLE_ONLY: _make_record(paper_id=_TITLE_ONLY, title=_TITLE_ONLY)}

    with patch(_CROSSREF_MODULE, return_value={}):
        updated, audit = _recover_incomplete_final_references(
            [_TITLE_ONLY],
            records,
            enrich_crossref=True,
        )

    assert updated[_TITLE_ONLY]["authors"] == []
    assert updated[_TITLE_ONLY]["year"] == ""
    assert audit[0]["recovered_fields"] == []
    assert audit[0]["crossref_matched"] is False


def test_no_title_record_is_not_queried() -> None:
    """A record with no title is not sent to Crossref (nothing to search on)."""

    pid = "notitle"
    records = {
        pid: _make_record(paper_id=pid, title="", authors=[], year="")
    }

    with patch(_CROSSREF_MODULE) as mock_crossref:
        _, audit = _recover_incomplete_final_references(
            [pid],
            records,
            enrich_crossref=True,
        )

    mock_crossref.assert_not_called()
    assert audit == []


# ── _reference_bibtex error diagnostics ────────────────────────────────────


def test_bibtex_raises_clear_error_for_unrecoverable_title_only_reference() -> None:
    """_reference_bibtex raises ValueError with paper_id, title, and an
    actionable hint when recovery was impossible."""

    pid = _TITLE_ONLY
    records = {
        pid: _make_record(
            paper_id=pid,
            title=pid,
            authors=[],
            year="",
            venue="",
            doi="",
            url="https://example.com/paper",
        )
    }

    with pytest.raises(ValueError) as exc_info:
        _reference_bibtex([pid], _keys([pid]), records)

    message = str(exc_info.value)
    # names the paper (via paper_id or title fragment)
    assert "emission dynamics" in message.lower() or pid in message
    # lists what is missing
    assert "author" in message.lower() or "missing" in message.lower()
    # carries an actionable hint
    assert any(
        hint in message.lower()
        for hint in ("supplemental", "knowledge base", "re-running")
    )


def test_bibtex_error_names_paper_id_for_fully_empty_record() -> None:
    """_reference_bibtex names the paper_id even when title is also absent."""

    pid = "orphan_corpus_id_9999"
    records = {
        pid: _make_record(paper_id=pid, title="", authors=[], year="", doi="")
    }

    with pytest.raises(ValueError) as exc_info:
        _reference_bibtex([pid], _keys([pid]), records)

    assert pid in str(exc_info.value)


def test_bibtex_complete_article_record_produces_valid_entry() -> None:
    """A fully populated article record generates a valid BibTeX entry."""

    pid = "doi:10.9999/example.complete"
    records = {
        pid: {
            "paper_id": pid,
            "title": "Complete Article",
            "authors": ["First Author", "Second Author"],
            "year": 2024,
            "venue": "Journal of Completeness",
            "doi": "10.9999/example.complete",
            "url": "https://doi.org/10.9999/example.complete",
            "reference_kind": "article",
        }
    }
    bibtex = _reference_bibtex([pid], _keys([pid]), records)

    assert "@article{" in bibtex
    assert "Complete Article" in bibtex
    assert "First Author" in bibtex
    assert "Journal of Completeness" in bibtex


def test_bibtex_misc_record_without_locator_gets_pending_note() -> None:
    """A misc record without DOI or URL receives a 'Stable locator pending' note."""

    pid = "misc_no_locator"
    records = {
        pid: {
            "paper_id": pid,
            "title": "Unpublished Preprint",
            "authors": ["Anon Author"],
            "year": 2025,
            "venue": "",
            "doi": "",
            "url": "",
            "reference_kind": "misc",
        }
    }
    bibtex = _reference_bibtex([pid], _keys([pid]), records)

    assert "@misc{" in bibtex
    assert "Stable locator pending" in bibtex


def test_bibtex_existing_fields_not_clobbered_by_recovery_partial() -> None:
    """A record that has only year missing but has authors does not raise after
    recovery fills year in."""

    pid = "partial_year_missing"
    # simulate what the record looks like after successful recovery
    records = {
        pid: {
            "paper_id": pid,
            "title": "Partial Record With Year Recovered",
            "authors": ["Recovered Author"],
            "year": 2023,   # already set by recovery
            "venue": "",
            "doi": "",
            "url": "https://example.com/partial",
            "reference_kind": "misc",
        }
    }
    # should not raise
    bibtex = _reference_bibtex([pid], _keys([pid]), records)

    assert "Recovered Author" in bibtex
    assert "2023" in bibtex


# ── resolver-level local-index title lookup ────────────────────────────────


class _StubIndex:
    """Minimal LocalMetadataIndex stand-in that holds injected records."""

    def __init__(
        self,
        by_identity: dict[str, list[dict]] | None = None,
        by_title: dict[str, list[dict]] | None = None,
        supplemental_by_identity: dict[str, list[dict]] | None = None,
        supplemental_by_title: dict[str, list[dict]] | None = None,
    ) -> None:
        self.by_identity = by_identity or {}
        self.by_title = by_title or {}
        self.supplemental_by_identity = supplemental_by_identity or {}
        self.supplemental_by_title = supplemental_by_title or {}
        self.supplemental_records: list[dict] = []
        self.supplemental_files: list[object] = []

    # These mirror LocalMetadataIndex's public interface used by the resolver.
    def find(self, identity: object) -> list[dict]:
        from optomind_research.runtime.publication_metadata_resolver import (
            identity_lookup_keys,
        )
        results: list[dict] = []
        seen: set[int] = set()
        for key in identity_lookup_keys(identity):  # type: ignore[arg-type]
            for rec in self.by_identity.get(key, []):
                if id(rec) not in seen:
                    seen.add(id(rec))
                    results.append(rec)
        return results

    def find_by_title(self, title: object) -> list[dict]:
        key = normalize_title(title)
        return list(self.by_title.get(key, [])) if key else []

    def find_supplemental(self, identity: object) -> list[dict]:
        from optomind_research.runtime.publication_metadata_resolver import (
            identity_lookup_keys,
        )
        results: list[dict] = []
        seen: set[int] = set()
        for key in identity_lookup_keys(identity):  # type: ignore[arg-type]
            for rec in self.supplemental_by_identity.get(key, []):
                if id(rec) not in seen:
                    seen.add(id(rec))
                    results.append(rec)
        return results

    def find_supplemental_by_title(self, title: object) -> list[dict]:
        key = normalize_title(title)
        return list(self.supplemental_by_title.get(key, [])) if key else []


def _make_input_packet_record(title: str) -> dict:
    """Minimal input_packet record: title present, authors/year absent."""
    from optomind_research.runtime.publication_metadata_resolver import (
        normalize_title,
    )
    title_token = f"title:{normalize_title(title)}"
    return {
        "identities": [title_token],
        "doi": "",
        "s2_id": "",
        "title": title,
        "authors": [],
        "year": "",
        "venue": "",
        "url": "",
        "abstract": "",
        "source": "input_packet",
        "source_path": "sections/ch1/input_packet.json",
        "section_id": "ch1",
        "trust_type": "core_evidence",
        "retrieval_origin": "",
    }


def _make_s2_cache_record(title: str, paper_id: str, authors: list[str], year: str) -> dict:
    """S2-cache record keyed under paper_id hash with full bibliographic data."""
    return {
        "identities": [paper_id, f"s2:{paper_id}"],
        "doi": "",
        "s2_id": paper_id,
        "title": title,
        "authors": authors,
        "year": year,
        "venue": "Physical Review Letters",
        "url": f"https://www.semanticscholar.org/paper/{paper_id}",
        "abstract": "",
        "source": "s2_cache",
        "source_path": "database/s2_cache/s2_online_cache.sqlite",
        "section_id": None,
        "trust_type": "local_s2_cache",
        "retrieval_origin": "/paper/search",
    }


def test_title_only_selected_record_enriched_from_local_cache() -> None:
    """Regression: a title-only input_packet reference is enriched from a
    locally-cached S2 record keyed by paper_id hash (not by title token).

    The resolver must find the S2 cache record via the by_title secondary
    lookup, add it as a candidate, and emit a catalog entry with authors, year,
    and venue — without any live network call.  Provenance must identify
    s2_cache as the source for authors/year.
    """

    title = "emission dynamics of a qubit in photonic lattices with synthetic potentials"
    paper_id = "a" * 40  # fake 40-hex S2 paper id
    authors = ["A. Mirhosseini", "B. Vrajitoarea"]
    year = "2022"

    input_packet_rec = _make_input_packet_record(title)
    s2_cache_rec = _make_s2_cache_record(title, paper_id, authors, year)

    title_key = normalize_title(title)
    title_token = f"title:{title_key}"

    # The title token finds the input_packet record (identity lookup).
    # The S2 cache record is keyed only under paper_id — title lookup is needed.
    stub_index = _StubIndex(
        by_identity={title_token: [input_packet_rec]},
        by_title={title_key: [input_packet_rec, s2_cache_rec]},
    )

    resolver = PublicationMetadataResolver(
        project_root=".",
        index=stub_index,  # type: ignore[arg-type]
        options=ResolverOptions(),  # all providers disabled — local only
    )

    manuscript = f"[REF:{title_token}]"
    result = resolver.resolve(manuscript)

    entries = result["entries"]
    assert len(entries) == 1, f"expected 1 entry, got {len(entries)}"
    entry = entries[0]

    assert entry["authors"] == authors, (
        f"authors should come from s2_cache; got {entry['authors']!r}"
    )
    assert entry["year"] == year, f"year should be {year!r}; got {entry['year']!r}"
    assert entry["venue"] == "Physical Review Letters"

    # Provenance must credit a local cache source, not just input_packet.
    authors_prov = entry["provenance"].get("authors", {})
    assert authors_prov.get("source") == "s2_cache", (
        f"authors provenance should be s2_cache; got {authors_prov!r}"
    )
    year_prov = entry["provenance"].get("year", {})
    assert year_prov.get("source") == "s2_cache", (
        f"year provenance should be s2_cache; got {year_prov!r}"
    )
    assert entry["resolution_status"] == "resolved"


def test_title_lookup_does_not_admit_empty_records() -> None:
    """The local-index title lookup must not re-admit records that contribute
    no authors or year — the gate must filter them out silently."""

    title = "A Paper With No Metadata Anywhere"
    title_key = normalize_title(title)
    title_token = f"title:{title_key}"

    input_packet_rec = _make_input_packet_record(title)
    # A second local record also has no authors/year (e.g. material_cache stub).
    material_rec = dict(input_packet_rec)
    material_rec = {**input_packet_rec, "source": "material_cache"}

    stub_index = _StubIndex(
        by_identity={title_token: [input_packet_rec]},
        by_title={title_key: [input_packet_rec, material_rec]},
    )

    resolver = PublicationMetadataResolver(
        project_root=".",
        index=stub_index,  # type: ignore[arg-type]
        options=ResolverOptions(),
    )

    result = resolver.resolve(f"[REF:{title_token}]")
    entry = result["entries"][0]

    # Both title-lookup records have no authors/year — entry stays partial.
    assert entry["authors"] == []
    assert entry["year"] == ""
    assert entry["resolution_status"] in ("partial", "unresolved")


def test_title_lookup_uses_highest_trust_source() -> None:
    """When both explanatory_ledger and s2_cache records match by title,
    the ledger's data wins (trust rank 90 > 60) for fields it supplies."""

    title = "Photonic Band-Gap Materials"
    title_key = normalize_title(title)
    title_token = f"title:{title_key}"
    paper_id = "b" * 40

    input_rec = _make_input_packet_record(title)
    s2_rec = _make_s2_cache_record(title, paper_id, ["S2 Author"], "2019")
    ledger_rec = {
        **s2_rec,
        "identities": ["ledger_marker"],
        "s2_id": "",
        "authors": ["Ledger Author"],
        "year": "2020",
        "source": "explanatory_ledger",
        "trust_type": "background_explanation_only",
        "source_path": "sections/ch2/ledger.json",
    }

    stub_index = _StubIndex(
        by_identity={title_token: [input_rec]},
        by_title={title_key: [input_rec, s2_rec, ledger_rec]},
    )

    resolver = PublicationMetadataResolver(
        project_root=".",
        index=stub_index,  # type: ignore[arg-type]
        options=ResolverOptions(),
    )

    result = resolver.resolve(f"[REF:{title_token}]")
    entry = result["entries"][0]

    # explanatory_ledger rank=90 > s2_cache rank=60; ledger values win.
    assert entry["authors"] == ["Ledger Author"]
    assert entry["year"] == "2020"
