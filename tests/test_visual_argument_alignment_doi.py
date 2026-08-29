"""DOI provenance regression tests for visual chunk retrieval."""

from __future__ import annotations

from optomind_research.visual_argument_alignment import (
    VisualArgumentAligner,
)


def _record(
    chunk_id: str,
    *,
    doi: str = "",
) -> dict:
    return {
        "chunk_id": chunk_id,
        "paper_id": "paper-1",
        "doi": doi,
        "title": "Resonant paper",
        "caption": "Optical resonance mechanism with field confinement.",
        "search_text": "optical resonance mechanism field confinement",
        "visual_argument_type": "mechanism_anchor",
        "visual_argument_status": "ok",
        "visual_argument_confidence": "high",
        "local_image_path": "unused.png",
    }


def _index_for(record: dict) -> list[dict]:
    return VisualArgumentAligner().build_visual_retrieval_index([record])


def test_unit_namespace_doi_is_preserved() -> None:
    index = _index_for(
        _record("unit:visual:abc123", doi="10.1000/real-doi")
    )
    assert index[0]["doi"] == "10.1000/real-doi"


def test_malformed_prefixes_never_override_explicit_doi() -> None:
    for chunk_id in ("unit:visual:x", "foo:bar", "random:thing"):
        index = _index_for(_record(chunk_id, doi="10.1/real-doi"))
        assert index[0]["doi"] == "10.1/real-doi"


def test_legacy_doi_chunk_ids_remain_supported() -> None:
    cases = (
        ("doi-10.1000/journal:type:s001", "10.1000/journal"),
        ("10.1000/journal:type:s001", "10.1000/journal"),
    )
    for chunk_id, expected in cases:
        index = _index_for(_record(chunk_id))
        assert index[0]["doi"] == expected


def test_missing_or_non_doi_prefix_falls_back_to_empty() -> None:
    assert _index_for(_record("unit:visual:abc"))[0]["doi"] == ""
    assert _index_for(_record("plain-id"))[0]["doi"] == ""
    assert _index_for(_record("doi-not-a-doi:x"))[0]["doi"] == ""


def test_recommendation_preserves_candidate_doi() -> None:
    aligner = VisualArgumentAligner()
    index = aligner.build_visual_retrieval_index(
        [_record("unit:visual:abc", doi="10.1000/real-doi")]
    )
    section = {
        "section_id": "S01",
        "title": "Optical resonance mechanism",
        "argument_role": "Explain the optical resonance mechanism.",
    }
    recommendations = aligner.recommend_visuals_for_section(
        section,
        index,
        top_k=1,
    )
    assert recommendations
    assert recommendations[0]["doi"] == "10.1000/real-doi"
