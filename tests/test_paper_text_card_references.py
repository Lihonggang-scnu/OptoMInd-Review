from __future__ import annotations

from optomind_research.paper_text_card_pipeline import (
    canonicalize_chunk_references,
    validate_card,
)


def _card(source_id: str) -> dict:
    return {
        "schema_version": "paper_text_card.v1",
        "paper_identity": {"doi": "10.1000/test"},
        "one_sentence_contribution": "A sufficiently specific contribution statement.",
        "high_density_summary": "A" * 160,
        "key_results": [{"claim": "Result", "source_chunk_ids": [source_id]}],
        "important_numbers": [],
        "evidence_map": [{"source_chunk_id": source_id, "why_it_matters": "Grounding"}],
        "directly_reusable_sentences": ["A grounded sentence."],
        "method_or_design": {},
        "limitations_and_open_questions": [],
        "useful_for_review_sections": [],
    }


def test_short_chunk_alias_is_mapped_to_canonical_id() -> None:
    valid = {"doi:10.1000/test:c0001", "doi:10.1000/test:c0002"}
    card = _card("c0001")
    audit = canonicalize_chunk_references(card, valid)
    assert audit["remapped_count"] == 1
    assert card["key_results"][0]["source_chunk_ids"] == ["doi:10.1000/test:c0001"]
    assert card["evidence_map"][0]["source_chunk_id"] == "doi:10.1000/test:c0001"


def test_unresolvable_chunk_id_fails_closed() -> None:
    valid = {"doi:10.1000/test:c0001"}
    card = _card("invented-chunk")
    canonicalize_chunk_references(card, valid)
    report = validate_card(card, {"doi": "10.1000/test"}, valid, 12000)
    assert not report["ok"]
    assert any(error.startswith("unknown_chunk_ids:") for error in report["errors"])

