"""Offline tests for the dominant-review full-reference expansion stage."""

from __future__ import annotations

import json
from typing import Any

from optomind_research.dominant_review_expansion import (
    ACQUISITION_PRIORITY_ORDER,
    build_acquisition_requests,
    build_dominant_review_input,
    build_reference_cards,
    build_screening_batch_prompt,
    build_screening_batches,
    collect_mention_contexts,
    evidence_precedence_contract,
    extract_reference_identity,
    merge_enriched_metadata,
    merge_screening_decisions,
    run_dominant_review_expansion,
    screening_coverage_audit,
)
from optomind_research.review_source_unpacking import (
    parse_numbered_bibliography,
)


def _bibliography_text(count: int = 255) -> str:
    lines = ["References"]
    for number in range(1, count + 1):
        lines.append(
            f"[{number}] Synthetic Author {number}, \"Synthetic Title {number},"
            f"\" Journal of Studies, vol. {number % 10 + 1}, pp. 1-9, 2024."
        )
    return "\n".join(lines)


def _parsed_bibliography(count: int = 255):
    return parse_numbered_bibliography(
        _bibliography_text(count), mode="whole_document"
    )


def _expansion_input(
    *,
    count: int = 255,
    body: str | None = None,
    claim_local: dict[int, list[dict[str, Any]]] | None = None,
):
    return build_dominant_review_input(
        user_question="Which original studies support the reviewed claims?",
        dynamic_axes=["mechanism", "comparison", "boundary"],
        section_workplan={
            "section_id": "S02",
            "argument_role": "core",
            "must_cover": ["mechanism"],
        },
        current_section_tasks=["map original evidence", "resolve conflicts"],
        review_identity={
            "paper_id": "paperS02",
            "unit_id": "unit:review:s02",
            "title": "Beyond Data-Driven: A Roadmap",
        },
        review_body=(
            body
            if body is not None
            else "The roadmap summarizes the field.\n"
            "The survey method is described in [85] and later validated.\n"
            "Earlier work [42] established the mechanism."
        ),
        bibliography=_parsed_bibliography(count),
        claim_local_marker_associations=claim_local or {},
        enriched_metadata={},
    )


def _decision(
    number: int,
    *,
    keep: bool = True,
    score: float = 80.0,
    priority: str = "high",
    reason: str = "Directly relevant to the mechanism claim.",
) -> dict[str, Any]:
    return {
        "reference_number": number,
        "relevance_score": score,
        "keep": keep,
        "useful_axes": ["mechanism"],
        "useful_sections": ["S02"],
        "likely_evidence_roles": ["central_fact"],
        "acquisition_priority": priority,
        "reason": reason,
    }


def test_255_entries_full_coverage_across_bounded_batches() -> None:
    expansion_input = _expansion_input()
    cards = build_reference_cards(
        expansion_input.bibliography,
        review_identity=expansion_input.review_identity,
        review_body=expansion_input.review_body,
    )
    assert len(cards) == 255
    batches = build_screening_batches(cards)
    coverage = screening_coverage_audit(batches, cards)
    assert coverage["complete"] is True
    assert coverage["expected_count"] == 255
    assert coverage["covered_count"] == 255
    assert coverage["missing_reference_numbers"] == []
    assert coverage["duplicate_reference_numbers"] == []
    assert coverage["batch_count"] == 16
    assert max(coverage["batch_sizes"]) <= 16
    covered = {
        number
        for batch in batches
        for number in batch["reference_numbers"]
    }
    assert covered == set(range(1, 256))


def test_permissive_no_top_n_quota() -> None:
    expansion_input = _expansion_input()

    def screen_all(batch):
        return [
            _decision(number)
            for number in batch["reference_numbers"]
        ]

    result = run_dominant_review_expansion(
        expansion_input, screen_decisions_call=screen_all
    )
    assert result["screening_audit"]["status_counts"]["kept"] == 255
    assert result["screening_audit"]["preserved_record_count"] == 255
    assert result["acquisition"]["audit"]["kept_request_count"] == 255
    assert result["coverage_audit"]["complete"] is True


def test_doi_arxiv_extraction() -> None:
    entries = parse_numbered_bibliography(
        "References\n"
        '[10] A. Author, "DOI entry," Journal, doi: 10.1000/abc123, 2023.\n'
        '[11] B. Author, "arXiv entry," Journal, arXiv:2401.01234, 2024.\n',
        mode="whole_document",
    )
    doi_identity = extract_reference_identity(entries[10])
    assert doi_identity["doi"] == "10.1000/abc123"
    assert doi_identity["batch_lookup_ids"] == ["DOI:10.1000/abc123"]
    arxiv_identity = extract_reference_identity(entries[11])
    assert arxiv_identity["arxiv_id"] == "2401.01234"
    assert arxiv_identity["batch_lookup_ids"] == ["ARXIV:2401.01234"]
    assert arxiv_identity["title"] == "arXiv entry"
    assert arxiv_identity["year"] == "2024"


def test_mention_context_collection_with_section_heading() -> None:
    body = (
        "3.2 Results\n"
        "The method is described in [42] and improves speed.\n"
        "A later sentence repeats the mention [42].\n"
        "References\n"
        "[42] An entry that must not be treated as a body mention.\n"
    )
    contexts = collect_mention_contexts(body)
    assert 42 in contexts
    assert len(contexts[42]) == 2
    assert all(
        context["section_heading"] == "3.2 Results"
        for context in contexts[42]
    )
    assert all("[42]" in context["sentence"] for context in contexts[42])
    assert 99 not in contexts


def test_malformed_model_output_preserved_as_pending_review() -> None:
    expansion_input = _expansion_input(count=5)
    cards = build_reference_cards(
        expansion_input.bibliography,
        review_identity=expansion_input.review_identity,
        review_body=expansion_input.review_body,
    )
    decisions = [
        _decision(1, keep=True),
        {
            "reference_number": 2,
            "relevance_score": "not-a-number",
            "keep": True,
            "acquisition_priority": "high",
            "reason": "malformed score",
        },
        {
            "reference_number": 3,
            "relevance_score": 50,
            "keep": True,
            "acquisition_priority": "unknown",
            "reason": "bad priority",
        },
        _decision(1, keep=False, reason="duplicate row"),
    ]
    merged = merge_screening_decisions(cards, decisions)
    audit = merged["audit"]
    assert audit["preserved_record_count"] == 5
    assert audit["all_reference_numbers"] == [1, 2, 3, 4, 5]
    assert audit["status_counts"]["kept"] == 1
    assert audit["status_counts"]["pending_review"] == 4
    assert audit["pending_reference_numbers"] == [2, 3, 4, 5]
    assert audit["duplicate_decision_numbers"] == [1]
    status_by_number = {
        int(card["reference_number"]): card["screen"]["status"]
        for card in merged["cards"]
    }
    assert status_by_number == {
        1: "kept",
        2: "pending_review",
        3: "pending_review",
        4: "pending_review",
        5: "pending_review",
    }


def test_acquisition_priority_order_and_conflict_policy() -> None:
    expansion_input = _expansion_input(count=3)
    cards = build_reference_cards(
        expansion_input.bibliography,
        review_identity=expansion_input.review_identity,
        review_body=expansion_input.review_body,
    )
    merged = merge_screening_decisions(
        cards,
        [
            _decision(1, keep=True, priority="high"),
            _decision(2, keep=True, priority="medium"),
            _decision(3, keep=False, reason="Unrelated to the question."),
        ],
    )
    acquisition = build_acquisition_requests(merged["cards"])
    requests = {row["reference_number"]: row for row in acquisition["requests"]}
    assert list(requests) == [1, 2]
    assert requests[1]["acquisition_priority_order"] == list(
        ACQUISITION_PRIORITY_ORDER
    )
    assert requests[1]["conflict_status"] == "pending_primary_check"
    assert requests[1]["evidence_precedence"] == "original_primary"
    assert requests[1]["review_secondary"]["paper_id"] == "paperS02"
    assert requests[1]["query_text"].startswith("[1] Synthetic Author 1")
    contract = evidence_precedence_contract()
    assert contract["original_paper_controls"] == [
        "factual claims",
        "method claims",
        "measurement claims",
    ]
    assert contract["review_secondary_roles"] == [
        "synthesis",
        "history",
        "context",
    ]
    assert contract["never_overwrite"] is True
    assert contract["conflict_status"] == "pending_primary_check"


def test_claim_local_signals_do_not_limit_screening() -> None:
    expansion_input = _expansion_input(
        count=100,
        claim_local={
            85: [{
                "claim_id": "c1",
                "relation": "exact_quote",
                "strength": "strong",
            }]
        },
    )
    cards = build_reference_cards(
        expansion_input.bibliography,
        review_identity=expansion_input.review_identity,
        review_body=expansion_input.review_body,
        claim_local_marker_associations=(
            expansion_input.claim_local_marker_associations
        ),
    )
    assert len(cards) == 100
    by_number = {
        int(card["reference_number"]): card for card in cards
    }
    assert by_number[85]["claim_local_priority_signals"][0]["strength"] == "strong"
    assert by_number[1]["claim_local_priority_signals"] == []
    assert all(card["screen"]["status"] == "pending_review" for card in cards)


def test_screening_batch_prompt_contract_and_enriched_merge() -> None:
    expansion_input = _expansion_input(count=5)
    cards = build_reference_cards(
        expansion_input.bibliography,
        review_identity=expansion_input.review_identity,
        review_body=expansion_input.review_body,
    )
    cards = merge_enriched_metadata(
        cards,
        {
            "DOI:10.1000/abc123": {
                "title": "Enriched Title",
                "abstract": "Enriched abstract.",
                "s2_paper_id": "S2:1",
            },
            "3": {
                "title": "Enriched Three",
                "abstract": "Enriched abstract three.",
                "s2_paper_id": "S2:3",
            },
        },
    )
    assert len(cards) == 5
    enriched_three = next(
        card for card in cards
        if int(card["reference_number"]) == 3
    )
    assert enriched_three["enriched"]["title"] == "Enriched Three"
    assert enriched_three["identity"]["s2_paper_id"] == "S2:3"
    batches = build_screening_batches(cards, batch_size=3)
    messages = build_screening_batch_prompt(
        batches[0],
        user_question=expansion_input.user_question,
        dynamic_axes=expansion_input.dynamic_axes,
        section_workplan=expansion_input.section_workplan,
        current_section_tasks=expansion_input.current_section_tasks,
        review_identity=expansion_input.review_identity,
    )
    assert "DOMINANT-REVIEW REFERENCE SCREENER" in messages[0]["content"]
    payload = json.loads(messages[1]["content"])
    assert payload["screening_contract_version"].startswith(
        "dominant_review_reference_screening"
    )
    assert len(payload["reference_cards"]) == 3
    assert payload["policy"]["permissive"] is True
    assert payload["policy"]["no_top_n_quota"] is True
    card_payloads = {
        int(card["reference_number"]): card
        for card in payload["reference_cards"]
    }
    assert card_payloads[3]["enriched"]["title"] == "Enriched Three"
    assert card_payloads[3]["enriched"]["abstract"] == (
        "Enriched abstract three."
    )
    assert set(payload["required_output"]["decisions"][0]) == {
        "reference_number",
        "relevance_score",
        "keep",
        "useful_axes",
        "useful_sections",
        "likely_evidence_roles",
        "acquisition_priority",
        "reason",
    }


def test_structured_eight_section_workplan_survives_prompt_payload() -> None:
    sections = [
        {
            "section_id": f"S{index:02d}",
            "title": f"Chapter {index}",
            "argument_role": (
                "core" if index % 2 else "supporting"
            ),
            "must_cover": [f"cover-{index}"],
            "must_not_cover": [f"exclude-{index}"],
            "key_questions": [f"question-{index}"],
        }
        for index in range(1, 9)
    ]
    expansion_input = build_dominant_review_input(
        user_question="Which original studies support the review?",
        dynamic_axes=["mechanism"],
        section_workplan=sections,
        current_section_tasks=[
            {"task_id": "t1", "section_id": "S01", "task": "map evidence"},
            "plain string task",
        ],
        review_identity={"paper_id": "paperS02", "title": "A Roadmap"},
        review_body="The roadmap summarizes the field.\n"
        "The survey method is described in [85].",
        bibliography=_parsed_bibliography(5),
    )
    assert isinstance(expansion_input.section_workplan, list)
    assert len(expansion_input.section_workplan) == 8
    cards = build_reference_cards(
        expansion_input.bibliography,
        review_identity=expansion_input.review_identity,
        review_body=expansion_input.review_body,
    )
    batch = build_screening_batches(cards)[0]
    messages = build_screening_batch_prompt(
        batch,
        user_question=expansion_input.user_question,
        dynamic_axes=expansion_input.dynamic_axes,
        section_workplan=expansion_input.section_workplan,
        current_section_tasks=expansion_input.current_section_tasks,
        review_identity=expansion_input.review_identity,
    )
    payload = json.loads(messages[1]["content"])
    sections_payload = payload["section_workplan"]["sections"]
    assert payload["section_workplan"]["section_count"] == 8
    assert len(sections_payload) == 8
    for index, row in enumerate(sections_payload, start=1):
        assert row["section_id"] == f"S{index:02d}"
        assert row["title"] == f"Chapter {index}"
        assert row["argument_role"] in {"core", "supporting"}
        assert row["must_cover"] == [f"cover-{index}"]
        assert row["must_not_cover"] == [f"exclude-{index}"]
        assert row["key_questions"] == [f"question-{index}"]
    assert payload["current_section_tasks"][0]["task_id"] == "t1"
    assert payload["current_section_tasks"][1] == "plain string task"
    round_trip = expansion_input.to_dict()
    assert len(round_trip["section_workplan"]) == 8
    assert round_trip["current_section_tasks"][0]["task_id"] == "t1"
