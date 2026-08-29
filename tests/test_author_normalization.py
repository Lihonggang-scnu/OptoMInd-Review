"""Regression tests for author-string normalization and BibTeX author output.

Covers:
- _strip_affiliation_marker: trailing digits and symbols
- _normalize_author_list: all common bibliographic formats
- _reference_bibtex: emits and-joined authors, no BibTeX comma error
- _identify_dropped_references: uses normalized authors for contract check
- _drop_citation_tokens: adjacent citation handling
"""
from __future__ import annotations

from optomind_research.runtime.latex_publication_renderer import (
    _citation_key,
    _drop_citation_tokens,
    _identify_dropped_references,
    _normalize_author_list,
    _reference_bibtex,
    _strip_affiliation_marker,
)


# ── _strip_affiliation_marker ────────────────────────────────────────────────


def test_strip_trailing_digit() -> None:
    assert _strip_affiliation_marker("Xijun Gao1") == "Xijun Gao"


def test_strip_trailing_multi_digit() -> None:
    assert _strip_affiliation_marker("Smith12") == "Smith"


def test_strip_trailing_superscript_unicode() -> None:
    # U+00B9 = ¹
    assert _strip_affiliation_marker("Jones¹") == "Jones"


def test_strip_trailing_star() -> None:
    assert _strip_affiliation_marker("Brown*") == "Brown"


def test_no_strip_plain_name() -> None:
    assert _strip_affiliation_marker("Alice Smith") == "Alice Smith"


def test_no_strip_last_first() -> None:
    # "Jr." and Roman suffixes must not be stripped
    assert _strip_affiliation_marker("Smith, Alice B.") == "Smith, Alice B."


# ── _normalize_author_list ───────────────────────────────────────────────────


def test_structured_list_returned_as_is() -> None:
    authors = ["Alice Smith", "Bob Jones"]
    assert _normalize_author_list(authors) == ["Alice Smith", "Bob Jones"]


def test_structured_list_strips_affiliation_markers() -> None:
    assert _normalize_author_list(["Xijun Gao1", "Wenxuan Gong1"]) == [
        "Xijun Gao",
        "Wenxuan Gong",
    ]


def test_one_element_list_with_comma_separated_string() -> None:
    """Regression: real metadata stores authors as a one-element list whose
    sole item is the full comma-separated author string.  The list branch
    must normalise the element through the string path and flatten.
    """
    raw = ["Xijun Gao1, Wenxuan Gong1, Jiaqi Li1, Dairan Li1, Yutong Li1"]
    result = _normalize_author_list(raw)
    assert result == [
        "Xijun Gao",
        "Wenxuan Gong",
        "Jiaqi Li",
        "Dairan Li",
        "Yutong Li",
    ], f"Got: {result}"


def test_one_element_list_last_first_preserved() -> None:
    """A single-element list containing a valid 'Surname, Given' name must
    not be split into two spurious authors."""
    result = _normalize_author_list(["Einstein, Albert"])
    assert result == ["Einstein, Albert"], f"Got: {result}"


def test_multi_element_list_with_mixed_items() -> None:
    """Multi-element list where some elements are already clean names and
    one element is a delimited sub-string must be flattened correctly."""
    raw = ["Alice Smith", "Gao1, Wenxuan Gong1", "Carol Lee"]
    result = _normalize_author_list(raw)
    # "Gao1, Wenxuan Gong1": two comma-parts, first ("Gao1") has no spaces
    # but will have affiliation stripped → "Gao" — preserved as Last, First.
    assert "Alice Smith" in result
    assert "Carol Lee" in result
    assert len(result) == 3


def test_and_delimited_string() -> None:
    result = _normalize_author_list("A. Smith and B. Jones")
    assert result == ["A. Smith", "B. Jones"]


def test_semicolon_delimited_string() -> None:
    result = _normalize_author_list("A. Smith; B. Jones; C. Lee")
    assert result == ["A. Smith", "B. Jones", "C. Lee"]


def test_comma_separated_given_surname() -> None:
    """The exact bug: five given-surname authors separated by commas."""
    raw = "Xijun Gao1, Wenxuan Gong1, Jiaqi Li1, Dairan Li1, Yutong Li1"
    result = _normalize_author_list(raw)
    assert result == [
        "Xijun Gao",
        "Wenxuan Gong",
        "Jiaqi Li",
        "Dairan Li",
        "Yutong Li",
    ]


def test_last_first_single_author_preserved() -> None:
    """Single author in BibTeX Last, First form must keep its comma."""
    result = _normalize_author_list("Smith, Alice B.")
    assert result == ["Smith, Alice B."]


def test_and_with_last_first_pairs() -> None:
    """'and'-joined Last-First pairs must keep their commas."""
    result = _normalize_author_list("Smith, A. and Jones, B.")
    assert result == ["Smith, A.", "Jones, B."]


def test_empty_string_returns_empty() -> None:
    assert _normalize_author_list("") == []


def test_none_returns_empty() -> None:
    assert _normalize_author_list(None) == []


def test_empty_list_returns_empty() -> None:
    assert _normalize_author_list([]) == []


# ── _reference_bibtex ────────────────────────────────────────────────────────

def _make_key_by_id(paper_id: str) -> dict[str, str]:
    return {paper_id: _citation_key(paper_id)}


def test_reference_bibtex_comma_separated_authors_no_bibtex_error() -> None:
    """_reference_bibtex must not emit a single malformed author string.

    The BibTeX error 'Too many commas in name' occurs when the author field
    contains commas that BibTeX cannot interpret as Last, First delimiters
    (e.g. 'Gao, Gong, Li').  After normalization the field must be
    and-joined individual names.
    """
    paper_id = "doi:10.25236/ijfet.2026.080104"
    record = {
        "authors": "Xijun Gao1, Wenxuan Gong1, Jiaqi Li1, Dairan Li1, Yutong Li1",
        "title": "A Study on Photonic Lattices",
        "year": "2026",
        "venue": "IJFET",
        "reference_kind": "article",
    }
    key_by_id = _make_key_by_id(paper_id)
    bib = _reference_bibtex([paper_id], key_by_id, {paper_id: record})

    # The author field must use " and " as the only author delimiter.
    # Extract the author line from the .bib string.
    author_line = next(
        line for line in bib.splitlines() if line.strip().startswith("author")
    )
    author_value = author_line.split("=", 1)[1].strip().strip("{},")
    assert " and " in author_value, "Expected 'and'-joined authors in BibTeX"
    # Commas inside the author field must only appear inside Last, First pairs
    # (i.e. never between two distinct author names).  The simplest check:
    # the author value must not contain ', ' between two full names.
    parts = [p.strip() for p in author_value.split(" and ")]
    assert len(parts) == 5, f"Expected 5 authors, got {len(parts)}: {parts}"
    for part in parts:
        # Each individual author segment must not itself contain commas
        # unless it is a valid Last, First form (first token has no spaces).
        if "," in part:
            tokens = [t.strip() for t in part.split(",")]
            assert len(tokens) == 2 and " " not in tokens[0], (
                f"Unexpected comma structure in author segment: {part!r}"
            )


def test_reference_bibtex_structured_list_unchanged() -> None:
    """A pre-structured author list must pass through without modification."""
    paper_id = "doi:10.1000/structured"
    record = {
        "authors": ["Alice Smith", "Bob Jones"],
        "title": "Structured Authors",
        "year": "2024",
        "venue": "Journal of Tests",
        "reference_kind": "article",
    }
    key_by_id = _make_key_by_id(paper_id)
    bib = _reference_bibtex([paper_id], key_by_id, {paper_id: record})
    assert "Alice Smith and Bob Jones" in bib


def test_reference_bibtex_corporate_sentinel_not_dropped() -> None:
    """The 'Authors not recovered' sentinel must become a corporate token."""
    paper_id = "doi:10.1000/noauthors"
    record = {
        "authors": "Authors not recovered in chapter snapshot",
        "title": "Orphan Paper",
        "year": "2023",
        "venue": "Some Journal",
        "reference_kind": "article",
    }
    key_by_id = _make_key_by_id(paper_id)
    bib = _reference_bibtex([paper_id], key_by_id, {paper_id: record})
    assert "OptoMind source metadata pending" in bib


def test_reference_bibtex_complete_record_unchanged_behavior() -> None:
    """A complete record with no affiliation markers must be emitted as-is."""
    paper_id = "doi:10.1234/complete"
    record = {
        "authors": ["Alex Example", "Bailey Sample"],
        "title": "A Q-factor study",
        "year": "2025",
        "venue": "Journal of Optical Tests",
        "reference_kind": "article",
    }
    key_by_id = _make_key_by_id(paper_id)
    bib = _reference_bibtex([paper_id], key_by_id, {paper_id: record})
    assert "Alex Example and Bailey Sample" in bib
    assert "@article" in bib


# ── _identify_dropped_references ─────────────────────────────────────────────


def test_identify_dropped_uses_normalized_authors() -> None:
    """A comma-separated author string that is non-empty after normalization
    must NOT cause the reference to be flagged as dropped."""
    paper_id = "doi:10.1000/normcheck"
    records = {
        paper_id: {
            "authors": "Xijun Gao1, Wenxuan Gong1",
            "title": "Photonic paper",
            "year": "2026",
            "venue": "Optics Letters",
            "reference_kind": "article",
        }
    }
    dropped = _identify_dropped_references([paper_id], records)
    assert dropped == [], f"Should not be dropped, got: {dropped}"


def test_identify_dropped_flags_truly_empty_authors() -> None:
    """A record with no authors and no title should be flagged."""
    paper_id = "title:unknown paper with nothing"
    records = {
        paper_id: {
            "authors": "",
            "title": "",
            "year": "",
            "reference_kind": "misc",
        }
    }
    dropped = _identify_dropped_references([paper_id], records)
    assert len(dropped) == 1
    assert "author" in dropped[0]["missing_fields"]
    assert "title" in dropped[0]["missing_fields"]


# ── _drop_citation_tokens ─────────────────────────────────────────────────────


def test_drop_single_citation() -> None:
    pid = "title:bad paper"
    key = _citation_key(pid)
    md = f"Some text [@{key}] more text."
    result = _drop_citation_tokens(md, {key})
    assert f"[@{key}]" not in result
    assert "Some text" in result
    assert "more text." in result


def test_drop_adjacent_preserves_valid() -> None:
    """[@good][@bad][@good2] → [@good][@good2]"""
    good1 = _citation_key("doi:10.1/good1")
    bad = _citation_key("title:unresolved paper")
    good2 = _citation_key("doi:10.1/good2")
    md = f"Text [@{good1}][@{bad}][@{good2}] end."
    result = _drop_citation_tokens(md, {bad})
    assert f"[@{good1}]" in result
    assert f"[@{good2}]" in result
    assert f"[@{bad}]" not in result
    assert "Text" in result
    assert "end." in result


def test_drop_no_keys_returns_unchanged() -> None:
    md = "Text [@somecite] more."
    assert _drop_citation_tokens(md, set()) == md


def test_no_fabricated_bibtex_for_dropped_reference() -> None:
    """_reference_bibtex must not be called with dropped ids.

    Simulate the contract: if dropped ids are filtered from ordered_ids
    before calling _reference_bibtex, no entry for the dropped id appears.
    """
    good_id = "doi:10.1/good"
    bad_id = "title:emission dynamics of a qubit"
    records = {
        good_id: {
            "authors": ["Alice Smith"],
            "title": "Good Paper",
            "year": "2024",
            "venue": "Journal of Tests",
            "reference_kind": "article",
        },
        bad_id: {
            "authors": "",
            "title": "emission dynamics of a qubit",
            "year": "",
            "reference_kind": "misc",
        },
    }
    # Simulate the fail-open filter
    dropped = _identify_dropped_references([good_id, bad_id], records)
    dropped_ids = {e["paper_id"] for e in dropped}
    filtered_ids = [pid for pid in [good_id, bad_id] if pid not in dropped_ids]
    key_by_id = {pid: _citation_key(pid) for pid in filtered_ids}

    assert bad_id in dropped_ids, "bad_id should have been dropped"
    assert bad_id not in filtered_ids

    bib = _reference_bibtex(filtered_ids, key_by_id, records)
    assert "emission dynamics" not in bib, "Dropped reference must not appear in BibTeX"
    assert "Good Paper" in bib
