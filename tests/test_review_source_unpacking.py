"""Offline tests for the review-source unpacking core."""

from __future__ import annotations

import json
from typing import Any

from optomind_research.review_source_unpacking import (
    build_review_bibliography_skeleton,
    build_review_source_ranker_prompt,
    build_review_trace_tasks,
    detect_review_bound_claims,
    extract_citation_markers,
    is_review_source,
    original_source_material_unit,
    parse_numbered_bibliography,
    review_source_signal,
    unpack_review_sources,
)
from optomind_research.runtime.material_unit_store import (
    material_unit_from_text_chunk,
)


def _unit(
    chunk_id: str,
    title: str,
    text: str,
    *,
    paper_id: str = "p_review",
    source_kind: str = "",
    use_permission: str = "factual_support",
) -> dict[str, Any]:
    return material_unit_from_text_chunk({
        "paper_id": paper_id,
        "chunk_id": chunk_id,
        "doi": f"10.0/{chunk_id}",
        "title": title,
        "text": text,
        "source_kind": source_kind,
        "content_depth": "fulltext",
        "use_permission": use_permission,
    })


def _claim(
    claim_id: str,
    statement: str,
    chunk_id: str,
    quote: str,
) -> dict[str, Any]:
    return {
        "claim_id": claim_id,
        "statement": statement,
        "role": "load_bearing",
        "component_verification": [{
            "component_id": f"{claim_id}.1",
            "statement": statement,
            "bindings": [{
                "chunk_id": chunk_id,
                "verbatim_quote": quote,
                "quote_exact": True,
            }],
        }],
    }


def _units_map(*units: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {
        unit["unit_id"]: unit
        for unit in units
    } | {
        (unit.get("identity") or {}).get("chunk_id", ""): unit
        for unit in units
    }


def test_numeric_marker_extraction_forms_and_guards() -> None:
    assert [m["numbers"] for m in extract_citation_markers("[12]")] == [[12]]
    assert [m["numbers"] for m in extract_citation_markers("[12-14]")] == [
        [12, 13, 14]
    ]
    assert [m["numbers"] for m in extract_citation_markers("[12, 15]")] == [
        [12, 15]
    ]
    assert [m["numbers"] for m in extract_citation_markers("(12, 15)")] == [
        [12, 15]
    ]
    assert [m["numbers"] for m in extract_citation_markers("(12-14)")] == [
        [12, 13, 14]
    ]
    assert extract_citation_markers("[0,1]") == []
    assert extract_citation_markers("[2019]") == []
    assert extract_citation_markers("(2021)") == []
    assert extract_citation_markers("arr[12]") == []
    assert extract_citation_markers("f(12,15)") == []
    assert extract_citation_markers("(12)") == []


def test_review_detection_ignores_empirical_sources() -> None:
    review = _unit(
        "review1",
        "A perspective on the field",
        "Several studies report improved accuracy [12, 15].",
    )
    empirical = _unit(
        "emp1",
        "Improved accuracy with a new method",
        "The method improves accuracy by three percent.",
        paper_id="p_emp",
    )
    assert is_review_source(review) is True
    assert is_review_source(empirical) is False
    claims = [
        _claim(
            "c1",
            "Studies report improved accuracy.",
            "review1",
            "Several studies report improved accuracy [12, 15].",
        ),
        _claim(
            "c2",
            "The method improves accuracy.",
            "emp1",
            "The method improves accuracy by three percent.",
        ),
    ]
    detected = detect_review_bound_claims(claims, _units_map(review, empirical))
    assert [row["claim_id"] for row in detected] == ["c1"]
    assert detected[0]["review_source_signal"] == r"title:\bperspective\b"
    assert detected[0]["review_fallback"]["role"] == "review_secondary"


def test_opaque_title_detected_via_publication_types_metadata() -> None:
    opaque_title = (
        "Beyond Data-Driven: How Physics-Informed Neural Networks are "
        "Reshaping Multi-Physics Design and Discovery"
    )
    unit = {
        "unit_id": "unit:text:review_opaque",
        "work_id": "work:opaque",
        "unit_kind": "text_chunk",
        "identity": {
            "chunk_id": "review_opaque",
            "paper_id": "p_opaque",
            "doi": "10.0/opaque",
            "title": opaque_title,
        },
        "durable_content": {
            "raw_text": "Several studies report improved accuracy [12, 15].",
        },
        "durable_content_card": {
            "content_quality": {"source_kind": "fulltext"},
        },
        "publicationTypes": ["Review"],
    }
    assert is_review_source(unit) is True
    detected = detect_review_bound_claims(
        [_claim(
            "c1",
            "Studies report improved accuracy.",
            "review_opaque",
            "Several studies report improved accuracy [12, 15].",
        )],
        {"review_opaque": unit},
    )
    assert detected[0]["review_source_signal"] == "publication_types:review"

    raw_metadata_unit = dict(unit)
    raw_metadata_unit.pop("publicationTypes")
    raw_metadata_unit["raw_metadata"] = {
        "publication_types": ["Perspective", "roadmap"]
    }
    assert is_review_source(raw_metadata_unit) is True
    assert (
        review_source_signal(raw_metadata_unit)
        == "publication_types:perspective"
    )


def test_explicit_review_paper_identity_injection() -> None:
    opaque_title = (
        "Beyond Data-Driven: How Physics-Informed Neural Networks are "
        "Reshaping Multi-Physics Design and Discovery"
    )
    review = _unit(
        "review_opaque",
        opaque_title,
        "Several studies report improved accuracy [12, 15].",
    )
    claim = _claim(
        "c1",
        "Studies report improved accuracy.",
        "review_opaque",
        "Several studies report improved accuracy [12, 15].",
    )
    units = _units_map(review)
    assert detect_review_bound_claims([claim], units) == []
    detected = detect_review_bound_claims(
        [claim], units, review_paper_ids={"p_review"}
    )
    assert detected[0]["review_source_signal"] == "explicit_review_paper_id"
    result = unpack_review_sources(
        [claim], units, review_paper_ids={"p_review"}
    )
    assert result["tasks"][0]["review_chunk_id"] == "review_opaque"


def test_stable_deduplication_and_task_shape() -> None:
    review_text = (
        "Several studies report improved accuracy [12, 15]. "
        "Earlier work [12-14] established the mechanism."
    )
    review = _unit("review1", "A review of methods", review_text)
    quote = "Several studies report improved accuracy [12, 15]."
    claim = _claim(
        "c1",
        "Studies report improved accuracy.",
        "review1",
        quote,
    )
    units = _units_map(review)
    detected = detect_review_bound_claims([claim, claim], units)
    tasks = build_review_trace_tasks(detected, units)
    assert len(tasks) == 1
    task = tasks[0]
    assert task["task_id"].startswith("review_trace:")
    assert task["claim_id"] == "c1"
    assert task["review_chunk_id"] == "review1"
    assert task["exact_quote"] == quote
    assert task["citation_markers"] == ["[12, 15]"]
    assert task["citation_numbers"] == [12, 15]
    assert task["nearby_citation_markers"] == ["[12-14]"]
    assert task["nearby_citation_numbers"] == [12, 13, 14]
    associations_by_raw = {
        marker["raw"]: marker
        for marker in task["citation_marker_associations"]
    }
    assert associations_by_raw["[12, 15]"]["relation"] == "exact_quote"
    assert associations_by_raw["[12, 15]"]["strength"] == "strong"
    assert associations_by_raw["[12-14]"]["relation"] == "nearby_context"
    assert associations_by_raw["[12-14]"]["strength"] == "weak"
    assert task["outcome"] == "unresolved_review_reference"
    assert task["review_fallback"]["role"] == "review_secondary"
    # Stable task id across runs.
    second = build_review_trace_tasks(detected[:1], units)[0]
    assert second["task_id"] == task["task_id"]


def test_exact_index_resolution_materializes_and_metadata_only() -> None:
    review = _unit(
        "review1",
        "A review of methods",
        "Several studies report improved accuracy [12, 15].",
    )
    claim = _claim(
        "c1",
        "Studies report improved accuracy.",
        "review1",
        "Several studies report improved accuracy [12, 15].",
    )
    candidate_a = {
        "paper_id": "orig_a",
        "title": "Original study A",
        "doi": "10.0/a",
        "text": "Original text A.",
        "source_kind": "s2_body",
        "content_depth": "fulltext",
    }
    candidate_b = {
        "paper_id": "orig_b",
        "title": "Original study B",
        "doi": "10.0/b",
    }
    result = unpack_review_sources(
        [claim],
        _units_map(review),
        bibliography_index={12: candidate_a, 15: candidate_b},
    )
    task = result["tasks"][0]
    assert task["outcome"] == "original_source_materialized"
    assert task["why"].startswith("Resolved 2 original source(s) via ")
    assert "exact_bibliography_number_match" in task["why"]
    assert len(task["selected_original_sources"]) == 2
    assert len(task["materialized_unit_ids"]) == 1
    assert task["review_fallback"]["unit_id"] == review["unit_id"]

    metadata_result = unpack_review_sources(
        [claim],
        _units_map(review),
        bibliography_index={12: candidate_b},
    )
    metadata_task = metadata_result["tasks"][0]
    assert metadata_task["outcome"] == "original_source_found_metadata_only"
    assert metadata_task["materialized_unit_ids"] == []


def test_semantic_ranker_fallback() -> None:
    review = _unit(
        "review1",
        "A review of methods",
        "One study reported the key result [7].",
    )
    claim = _claim(
        "c1",
        "A key result was reported.",
        "review1",
        "One study reported the key result [7].",
    )
    candidate_pool = [
        {
            "paper_id": "orig_x",
            "title": "Unrelated paper",
            "doi": "10.0/x",
            "text": "Unrelated text.",
        },
        {
            "paper_id": "orig_y",
            "title": "Original cited study",
            "doi": "10.0/y",
            "text": "Original text Y.",
            "source_kind": "oa_fulltext",
            "content_depth": "fulltext",
        },
    ]

    def ranker(task, candidates):
        return [{
            "task_id": task["task_id"],
            "candidate_id": candidates[1]["paper_id"],
            "reason": "Title and topic align with the cited result.",
        }]

    result = unpack_review_sources(
        [claim],
        _units_map(review),
        candidate_pool=candidate_pool,
        ranker=ranker,
    )
    task = result["tasks"][0]
    assert task["outcome"] == "original_source_materialized"
    assert task["why"].startswith("Resolved 1 original source(s) via ")
    assert "semantic_ranker_selection" in task["why"]
    assert task["selected_original_sources"][0]["paper_id"] == "orig_y"
    assert task["selected_original_sources"][0]["selection_reason"] == (
        "Title and topic align with the cited result."
    )


def test_per_review_bibliography_index_resolves_distinct_originals() -> None:
    review_a = _unit(
        "review_a",
        "Review A",
        "One study reported the key result [1].",
        paper_id="paperA",
    )
    review_b = _unit(
        "review_b",
        "Review B",
        "Another study reported the key result [1].",
        paper_id="paperB",
    )
    claim_a = _claim(
        "ca",
        "A key result was reported.",
        "review_a",
        "One study reported the key result [1].",
    )
    claim_b = _claim(
        "cb",
        "Another key result was reported.",
        "review_b",
        "Another study reported the key result [1].",
    )
    bibliography_index = {
        "paperA": {
            1: {
                "paper_id": "orig_a",
                "title": "Original A",
                "doi": "10.0/a",
                "text": "Original text A.",
            }
        },
        "paperB": {
            1: {
                "paper_id": "orig_b",
                "title": "Original B",
                "doi": "10.0/b",
                "text": "Original text B.",
            }
        },
    }
    result = unpack_review_sources(
        [claim_a, claim_b],
        _units_map(review_a, review_b),
        bibliography_index=bibliography_index,
    )
    by_claim = {task["claim_id"]: task for task in result["tasks"]}
    assert by_claim["ca"]["selected_original_sources"][0]["paper_id"] == "orig_a"
    assert by_claim["cb"]["selected_original_sources"][0]["paper_id"] == "orig_b"
    assert by_claim["ca"]["outcome"] == "original_source_materialized"
    assert by_claim["cb"]["outcome"] == "original_source_materialized"
    assert by_claim["ca"]["review_fallback"]["chunk_id"] == "review_a"
    assert by_claim["cb"]["review_fallback"]["chunk_id"] == "review_b"


def test_reference_provider_resolves_per_review() -> None:
    review_a = _unit(
        "review_a",
        "Review A",
        "One study reported the key result [1].",
        paper_id="paperA",
    )
    review_b = _unit(
        "review_b",
        "Review B",
        "Another study reported the key result [1].",
        paper_id="paperB",
    )
    claims = [
        _claim(
            "ca",
            "A key result was reported.",
            "review_a",
            "One study reported the key result [1].",
        ),
        _claim(
            "cb",
            "Another key result was reported.",
            "review_b",
            "Another study reported the key result [1].",
        ),
    ]

    def reference_provider(task):
        if task["review_paper_id"] == "paperA":
            return {
                1: {
                    "paper_id": "orig_a",
                    "title": "Original A",
                    "doi": "10.0/a",
                    "text": "Original text A.",
                }
            }
        return {
            1: {
                "paper_id": "orig_b",
                "title": "Original B",
                "doi": "10.0/b",
                "text": "Original text B.",
            }
        }

    result = unpack_review_sources(
        claims,
        _units_map(review_a, review_b),
        reference_provider=reference_provider,
    )
    by_claim = {task["claim_id"]: task for task in result["tasks"]}
    assert by_claim["ca"]["selected_original_sources"][0]["paper_id"] == "orig_a"
    assert by_claim["cb"]["selected_original_sources"][0]["paper_id"] == "orig_b"


def test_ambiguous_global_index_not_reused_across_reviews() -> None:
    review_a = _unit(
        "review_a",
        "Review A",
        "One study reported the key result [1].",
        paper_id="paperA",
    )
    review_b = _unit(
        "review_b",
        "Review B",
        "Another study reported the key result [1].",
        paper_id="paperB",
    )
    claims = [
        _claim(
            "ca",
            "A key result was reported.",
            "review_a",
            "One study reported the key result [1].",
        ),
        _claim(
            "cb",
            "Another key result was reported.",
            "review_b",
            "Another study reported the key result [1].",
        ),
    ]
    result = unpack_review_sources(
        claims,
        _units_map(review_a, review_b),
        bibliography_index={
            1: {
                "paper_id": "ambiguous",
                "title": "Ambiguous original",
                "doi": "10.0/x",
                "text": "Ambiguous text.",
            }
        },
    )
    assert all(
        task["outcome"] == "unresolved_review_reference"
        for task in result["tasks"]
    )
    assert all(
        "not reused across multiple review identities" in task["why"]
        for task in result["tasks"]
    )
    assert all(
        task["selected_original_sources"] == [] for task in result["tasks"]
    )


def test_review_specific_candidate_pool_and_ranker_identity() -> None:
    review_a = _unit(
        "review_a",
        "Review A",
        "One study reported the key result [1].",
        paper_id="paperA",
    )
    review_b = _unit(
        "review_b",
        "Review B",
        "Another study reported the key result [1].",
        paper_id="paperB",
    )
    claims = [
        _claim(
            "ca",
            "A key result was reported.",
            "review_a",
            "One study reported the key result [1].",
        ),
        _claim(
            "cb",
            "Another key result was reported.",
            "review_b",
            "Another study reported the key result [1].",
        ),
    ]
    candidate_pool = {
        "paperA": [
            {
                "paper_id": "orig_a",
                "title": "Original A",
                "doi": "10.0/a",
                "text": "Original text A.",
            },
            {
                "paper_id": "noise_a",
                "title": "Noise A",
                "doi": "10.0/na",
                "text": "Noise A text.",
            },
        ],
        "paperB": [
            {
                "paper_id": "orig_b",
                "title": "Original B",
                "doi": "10.0/b",
                "text": "Original text B.",
            }
        ],
    }

    def ranker(task, candidates):
        seen_pool = {
            candidate["paper_id"] for candidate in candidates
        }
        if task["review_paper_id"] == "paperA":
            assert seen_pool == {"orig_a", "noise_a"}
            return [{
                "task_id": task["task_id"],
                "candidate_id": "orig_a",
                "reason": "Matches review A citation.",
            }]
        assert seen_pool == {"orig_b"}
        return [{
            "task_id": task["task_id"],
            "candidate_id": "orig_b",
            "reason": "Matches review B citation.",
        }]

    result = unpack_review_sources(
        claims,
        _units_map(review_a, review_b),
        candidate_pool=candidate_pool,
        ranker=ranker,
    )
    by_claim = {task["claim_id"]: task for task in result["tasks"]}
    assert by_claim["ca"]["selected_original_sources"][0]["paper_id"] == "orig_a"
    assert by_claim["cb"]["selected_original_sources"][0]["paper_id"] == "orig_b"

    task_a = by_claim["ca"]
    messages = build_review_source_ranker_prompt(
        task_a,
        candidate_pool["paperA"],
    )
    payload = json.loads(messages[1]["content"])
    assert payload["task"]["review_paper_id"] == "paperA"
    assert payload["task"]["review_unit_id"] == review_a["unit_id"]
    assert {row["candidate_id"] for row in payload["candidates"]} == {
        "orig_a",
        "noise_a",
    }


def test_materialization_inherits_existing_trust_semantics() -> None:
    review = _unit(
        "review1",
        "A roadmap for the field",
        "The roadmap summarizes the result [3].",
    )
    claim = _claim(
        "c1",
        "The roadmap summarizes the result.",
        "review1",
        "The roadmap summarizes the result [3].",
    )
    candidate = {
        "paper_id": "orig_abstract",
        "title": "Original abstract",
        "doi": "10.0/abs",
        "text": "Abstract text.",
        "source_kind": "true_abstract",
        "content_depth": "abstract",
        "use_permission": "contextual_or_qualified_support",
    }
    unit = original_source_material_unit(
        candidate,
        task={
            "review_chunk_id": "review1",
            "review_unit_id": review["unit_id"],
            "citation_markers": ["[3]"],
            "citation_numbers": [3],
            "review_fallback": review["identity"],
        },
    )
    quality = unit["durable_content_card"]["content_quality"]
    assert quality["evidence_ceiling"] == "contextual_or_qualified_support"
    assert quality["source_kind"] == "true_abstract"
    assert unit["durable_content"]["content_depth"] == "abstract"
    provenance = (unit.get("audit") or {}).get("source_provenance") or {}
    assert provenance.get("unpacked_from_review_chunk_id") == "review1"
    assert provenance.get("citation_numbers") == [3]

    fulltext = original_source_material_unit(
        {
            "paper_id": "orig_ft",
            "title": "Original full text",
            "doi": "10.0/ft",
            "text": "Full text.",
            "source_kind": "s2_body",
            "content_depth": "fulltext",
            "use_permission": "factual_support",
        },
        task={
            "review_chunk_id": "review1",
            "citation_markers": ["[3]"],
            "citation_numbers": [3],
        },
    )
    ft_quality = fulltext["durable_content_card"]["content_quality"]
    assert ft_quality["evidence_ceiling"] == "factual_support"
    assert ft_quality["source_kind"] == "s2_body"
    assert fulltext["durable_content"]["content_depth"] == "fulltext"


def test_unresolved_review_reference_preserves_review_fallback() -> None:
    review = _unit(
        "review1",
        "A survey of methods",
        "An early study reported the effect [42].",
    )
    claim = _claim(
        "c1",
        "An early study reported the effect.",
        "review1",
        "An early study reported the effect [42].",
    )
    result = unpack_review_sources([claim], _units_map(review))
    task = result["tasks"][0]
    assert task["outcome"] == "unresolved_review_reference"
    assert task["citation_numbers"] == [42]
    assert task["selected_original_sources"] == []
    assert task["review_fallback"]["chunk_id"] == "review1"
    assert task["review_fallback"]["role"] == "review_secondary"
    assert result["outcome_counts"]["unresolved_review_reference"] == 1


def test_no_inline_reference_outcome() -> None:
    review = _unit(
        "review1",
        "A review of methods",
        "The field is developing rapidly without inline numeric citations.",
    )
    claim = _claim(
        "c1",
        "The field is developing rapidly.",
        "review1",
        "The field is developing rapidly.",
    )
    result = unpack_review_sources([claim], _units_map(review))
    task = result["tasks"][0]
    assert task["outcome"] == "no_inline_reference"
    assert task["citation_numbers"] == []
    assert task["review_fallback"]["role"] == "review_secondary"


def test_evidence_preservation_no_mutation() -> None:
    review = _unit(
        "review1",
        "A perspective on the field",
        "Several studies report improved accuracy [12, 15].",
    )
    claim = _claim(
        "c1",
        "Studies report improved accuracy.",
        "review1",
        "Several studies report improved accuracy [12, 15].",
    )
    claims = [claim]
    units = _units_map(review)
    claims_before = json.dumps(claims, ensure_ascii=False, sort_keys=True)
    units_before = json.dumps(units, ensure_ascii=False, sort_keys=True)
    result = unpack_review_sources(
        claims,
        units,
        bibliography_index={
            12: {
                "paper_id": "orig_a",
                "title": "Original A",
                "doi": "10.0/a",
                "text": "Original text A.",
            }
        },
    )
    assert json.dumps(claims, ensure_ascii=False, sort_keys=True) == claims_before
    assert json.dumps(units, ensure_ascii=False, sort_keys=True) == units_before
    assert result["tasks"][0]["review_fallback"]["unit_id"] == review["unit_id"]


def test_ranker_prompt_is_fill_in_contract_only() -> None:
    review = _unit(
        "review1",
        "A review of methods",
        "One study reported the key result [7].",
    )
    task = build_review_trace_tasks(
        [{
            "claim_id": "c1",
            "claim_statement": "A key result was reported.",
            "review_chunk_id": "review1",
            "review_unit_id": review["unit_id"],
            "exact_quote": "One study reported the key result [7].",
            "citation_markers": ["[7]"],
            "citation_numbers": [7],
            "review_fallback": {"chunk_id": "review1", "role": "review_secondary"},
        }],
        _units_map(review),
    )[0]
    messages = build_review_source_ranker_prompt(task, [{
        "paper_id": "orig_y",
        "title": "Original cited study",
        "doi": "10.0/y",
    }])
    assert "REVIEW-SOURCE UNPACKER" in messages[0]["content"]
    payload = json.loads(messages[1]["content"])
    assert payload["task"]["task_id"] == task["task_id"]
    assert payload["candidates"][0]["candidate_id"] == "orig_y"
    assert set(payload["required_output"]) == {"selections"}


def test_ranker_prompt_distinguishes_nearby_clues_from_direct_markers() -> None:
    review = _unit(
        "review1",
        "A review of methods",
        "The previous section ends with a result [254].\n\n"
        "New subsection: the quoted result is here.",
    )
    claim = _claim(
        "c1",
        "The quoted result is here.",
        "review1",
        "the quoted result is here",
    )
    detected = detect_review_bound_claims([claim], _units_map(review))
    task = build_review_trace_tasks(
        detected, _units_map(review), window_chars=500
    )[0]
    messages = build_review_source_ranker_prompt(task, [{
        "paper_id": "candidate_x",
        "title": "Candidate X",
        "doi": "10.0/x",
    }])
    assert "established attribution" in messages[0]["content"]
    payload = json.loads(messages[1]["content"])
    assert payload["task"]["citation_markers"] == []
    assert payload["task"]["nearby_citation_markers"] == ["[254]"]
    assert payload["task"]["nearby_citation_numbers"] == [254]
    assert payload["task"]["citation_marker_associations"][0]["relation"] == (
        "nearby_context"
    )


def test_prior_section_marker_is_nearby_not_direct() -> None:
    review = _unit(
        "review1",
        "A review of methods",
        "The previous section ends with a result [254].\n\n"
        "New subsection: the quoted result is here.",
    )
    claim = _claim(
        "c1",
        "The quoted result is here.",
        "review1",
        "the quoted result is here",
    )
    detected = detect_review_bound_claims([claim], _units_map(review))
    task = build_review_trace_tasks(
        detected, _units_map(review), window_chars=500
    )[0]
    assert task["citation_numbers"] == []
    assert task["citation_markers"] == []
    assert task["nearby_citation_numbers"] == [254]
    assert task["nearby_citation_markers"] == ["[254]"]
    association = task["citation_marker_associations"][0]
    assert association["relation"] == "nearby_context"
    assert association["strength"] == "weak"
    assert task["outcome"] == "no_inline_reference"
    assert "not used for automatic resolution" in task["why"]
    assert task["review_fallback"]["role"] == "review_secondary"


def test_prior_sentence_marker_is_nearby_not_direct() -> None:
    review = _unit(
        "review1",
        "A review of methods",
        "The prior sentence ends with a number [14]. "
        "The quoted result is Speed improves by ten percent.",
    )
    claim = _claim(
        "c1",
        "Speed improves by ten percent.",
        "review1",
        "Speed improves by ten percent",
    )
    detected = detect_review_bound_claims([claim], _units_map(review))
    task = build_review_trace_tasks(
        detected, _units_map(review), window_chars=500
    )[0]
    assert task["citation_numbers"] == []
    assert task["nearby_citation_numbers"] == [14]
    assert task["citation_marker_associations"][0]["relation"] == (
        "nearby_context"
    )
    assert task["outcome"] == "no_inline_reference"


def test_same_sentence_trailing_markers_are_direct() -> None:
    review = _unit(
        "review1",
        "A review of methods",
        'The method achieves faster convergence" [177]-[179] and '
        "[154],[176] in the tested setting.",
    )
    claim = _claim(
        "c1",
        "The method achieves faster convergence.",
        "review1",
        "The method achieves faster convergence",
    )
    detected = detect_review_bound_claims([claim], _units_map(review))
    task = build_review_trace_tasks(
        detected, _units_map(review), window_chars=500
    )[0]
    assert task["citation_numbers"] == [154, 176, 177, 179]
    assert task["nearby_citation_numbers"] == []
    relations = {
        marker["raw"]: marker["relation"]
        for marker in task["citation_marker_associations"]
    }
    assert set(relations) == {"[177]", "[179]", "[154]", "[176]"}
    assert all(
        relation == "same_sentence" for relation in relations.values()
    )
    assert all(
        marker["strength"] == "medium"
        for marker in task["citation_marker_associations"]
    )
    assert task["outcome"] == "unresolved_review_reference"


def test_exact_quote_marker_is_strong_direct() -> None:
    review = _unit(
        "review1",
        "A review of methods",
        "The survey method is described in [85]. It was later validated.",
    )
    claim = _claim(
        "c1",
        "The survey method is described in [85].",
        "review1",
        "The survey method is described in [85].",
    )
    detected = detect_review_bound_claims([claim], _units_map(review))
    task = build_review_trace_tasks(
        detected, _units_map(review), window_chars=500
    )[0]
    assert task["citation_numbers"] == [85]
    association = task["citation_marker_associations"][0]
    assert association["relation"] == "exact_quote"
    assert association["strength"] == "strong"
    assert task["nearby_citation_numbers"] == []


def test_quote_not_found_only_inside_quote_counts_as_direct() -> None:
    review = _unit(
        "review1",
        "A review of methods",
        "A nearby marker [9] appears in the excerpt but not with the quote.",
    )
    claim = _claim(
        "c1",
        "The missing quote contains [85].",
        "review1",
        "The missing quote contains [85].",
    )
    detected = detect_review_bound_claims([claim], _units_map(review))
    task = build_review_trace_tasks(
        detected, _units_map(review), window_chars=500
    )[0]
    assert task["quote_found_in_unit"] is False
    assert task["citation_numbers"] == [85]
    assert task["nearby_citation_numbers"] == [9]
    associations_by_raw = {
        marker["raw"]: marker
        for marker in task["citation_marker_associations"]
    }
    assert associations_by_raw["[85]"]["relation"] == "exact_quote"
    assert associations_by_raw["[9]"]["relation"] == "nearby_context"
    assert not any(
        marker["relation"] == "same_sentence"
        for marker in task["citation_marker_associations"]
    )


def test_real_shaped_c10_c11_markers_are_same_sentence_direct() -> None:
    review_a = _unit(
        "review_a",
        "Review A",
        'The comparison shows improved speed [154],[176] in the tested regime.',
        paper_id="paperA",
    )
    review_b = _unit(
        "review_b",
        "Review B",
        "The survey summarizes the results [177]-[179].",
        paper_id="paperB",
    )
    claims = [
        _claim(
            "c10",
            "The comparison shows improved speed.",
            "review_a",
            "The comparison shows improved speed",
        ),
        _claim(
            "c11",
            "The survey summarizes the results.",
            "review_b",
            "The survey summarizes the results",
        ),
    ]
    detected = detect_review_bound_claims(
        claims, _units_map(review_a, review_b)
    )
    tasks = build_review_trace_tasks(
        detected, _units_map(review_a, review_b), window_chars=500
    )
    by_claim = {task["claim_id"]: task for task in tasks}
    assert by_claim["c10"]["citation_numbers"] == [154, 176]
    assert by_claim["c11"]["citation_numbers"] == [177, 179]
    assert all(
        marker["relation"] == "same_sentence"
        for task in tasks
        for marker in task["citation_marker_associations"]
    )
    assert by_claim["c10"]["nearby_citation_numbers"] == []
    assert by_claim["c11"]["nearby_citation_numbers"] == []


def test_post_sentence_citation_cluster_is_nearby_real_shape() -> None:
    review = _unit(
        "review1",
        "A review of methods",
        "... convergence to the global minimum is not guaranteed.[14] "
        "From a performance perspective, solving a single Poisson equation "
        "is faster than the alternative.",
    )
    claim = _claim(
        "c1",
        "Solving a single Poisson equation is faster.",
        "review1",
        "From a performance perspective, solving a single Poisson equation "
        "is faster than the alternative",
    )
    detected = detect_review_bound_claims([claim], _units_map(review))
    task = build_review_trace_tasks(
        detected, _units_map(review), window_chars=500
    )[0]
    assert task["citation_numbers"] == []
    assert task["citation_markers"] == []
    assert task["nearby_citation_numbers"] == [14]
    assert task["nearby_citation_markers"] == ["[14]"]
    association = task["citation_marker_associations"][0]
    assert association["relation"] == "nearby_context"
    assert association["strength"] == "weak"
    assert task["outcome"] == "no_inline_reference"


def test_post_sentence_citation_cluster_variants_own_preceding_sentence() -> None:
    cases = [
        (
            "Sentence ends.[14], [15] Next sentence has the quote here.",
            "Next sentence has the quote here",
            [14, 15],
        ),
        (
            "Sentence ends [14]. Next sentence has the quote here.",
            "Next sentence has the quote here",
            [14],
        ),
        (
            "The claim is supported [177], [178].",
            "The claim is supported",
            [],
        ),
    ]
    for text, quote, nearby_expected in cases:
        review = _unit("review1", "A review of methods", text)
        claim = _claim("c1", quote, "review1", quote)
        detected = detect_review_bound_claims([claim], _units_map(review))
        task = build_review_trace_tasks(
            detected, _units_map(review), window_chars=500
        )[0]
        assert task["nearby_citation_numbers"] == nearby_expected
        if nearby_expected:
            assert task["citation_numbers"] == []
            assert all(
                marker["relation"] == "nearby_context"
                for marker in task["citation_marker_associations"]
            )
        else:
            assert task["citation_numbers"] == [177, 178]
            assert all(
                marker["relation"] == "same_sentence"
                for marker in task["citation_marker_associations"]
            )


def test_parse_realistic_multiline_ieee_entry_fragment() -> None:
    bibliography_text = """\
[83] A. Author, "Title A," Journal of Methods, vol. 1, pp. 1-9, 2020.
[84] B. Author, "Title B," Proceedings of the Conference, pp. 10-19, 2021.
[85] S. Authors, "Beyond Data-Driven: How Physics-Informed Neural Networks are
Reshaping Multi-Physics Design and Discovery," Journal of Design, vol. 7,
pp. 12-34, 2024.
128
[86] C. Author, "Title C," Journal of Design, vol. 8, pp. 35-40, 2022.
[87] D. Author, "Title D," Journal of Methods, vol. 2, pp. 1-8, 2023.
[88] E. Author, "Title E," Workshop Notes, 2023.
[89] F. Author, "Title F," Journal of Design, vol. 9, pp. 41-50, 2024.
"""
    parsed = parse_numbered_bibliography(
        bibliography_text, mode="bibliography_only"
    )
    assert sorted(parsed) == [83, 84, 85, 86, 87, 88, 89]
    assert parsed.audit["entry_count"] == 7
    assert parsed.audit["duplicate_numbers"] == []
    entry_85 = parsed[85]
    assert entry_85["reference_number"] == 85
    assert "Beyond Data-Driven: How Physics-Informed Neural Networks are" in (
        entry_85["raw_text"]
    )
    assert "Reshaping Multi-Physics Design and Discovery" in (
        entry_85["candidate_text"]
    )
    assert "Journal of Design" in entry_85["candidate_text"]
    assert entry_85["entry_start"] < entry_85["entry_end"]
    assert entry_85["entry_end"] == parsed[86]["entry_start"]
    assert "128" not in entry_85["candidate_text"]
    assert "pp. 12-34, 2024." in entry_85["raw_text"]
    assert "128\n" in entry_85["raw_text"]
    assert entry_85["first_line"].startswith(
        'S. Authors, "Beyond Data-Driven'
    )
    assert parsed.audit["removed_page_number_lines"][0]["text"] == "128"


def test_whole_document_mode_prefers_bibliography_entry_over_body_citation() -> None:
    document = """\
The method is described in [85] and improves the workflow.
Another body sentence cites [85] as well.
References
[85] The Actual Bibliography Entry for the Original Study, Journal, 2024.
"""
    parsed = parse_numbered_bibliography(document, mode="whole_document")
    assert parsed.audit["heading_found"] is True
    assert sorted(parsed) == [85]
    assert parsed[85]["candidate_text"].startswith(
        "[85] The Actual Bibliography Entry"
    )
    assert "improves the workflow" not in parsed[85]["candidate_text"]


def test_whole_document_no_heading_returns_auditable_empty() -> None:
    document = (
        "The method is described in [85] and improves the workflow. "
        "No bibliography section follows."
    )
    parsed = parse_numbered_bibliography(document, mode="whole_document")
    assert parsed == {}
    assert parsed.audit["heading_found"] is False
    assert parsed.audit["reason"] == "no_references_heading_found"
    assert parsed.audit["entry_count"] == 0


def test_duplicate_reference_numbers_audited_first_wins() -> None:
    bibliography_text = """\
[7] First Author, "First entry," Journal, 2020.
[8] Second Author, "Second entry," Journal, 2021.
[7] Unrelated Author, "Unrelated duplicate entry," Journal, 2022.
"""
    parsed = parse_numbered_bibliography(
        bibliography_text, mode="bibliography_only"
    )
    assert sorted(parsed) == [7, 8]
    assert parsed[7]["first_line"].startswith("First Author")
    assert "Unrelated duplicate" not in parsed[7]["raw_text"]
    assert parsed[7]["entry_end"] == parsed[8]["entry_start"]
    assert len(parsed.audit["duplicate_numbers"]) == 1
    duplicate = parsed.audit["duplicate_numbers"][0]
    assert duplicate["reference_number"] == 7
    assert duplicate["resolution"] == "first_wins_dropped"
    assert "Unrelated duplicate entry" in duplicate["raw_preview"]


def test_parsed_bibliography_feeds_unpack_resolution() -> None:
    bibliography_text = """\
References
[84] A. Author, "Other entry," Journal, 2023.
[85] S. Authors, "Beyond Data-Driven: How Physics-Informed Neural Networks are
Reshaping Multi-Physics Design and Discovery," Journal of Design, 2024.
"""
    parsed = parse_numbered_bibliography(
        bibliography_text, mode="whole_document"
    )
    skeleton = build_review_bibliography_skeleton(
        parsed, review_paper_id="paperS02"
    )
    assert sorted(skeleton) == ["paperS02"]
    assert skeleton["paperS02"][85]["reference_number"] == 85
    assert skeleton["paperS02"][85]["review_paper_id"] == "paperS02"
    assert "Beyond Data-Driven" in skeleton["paperS02"][85]["candidate_text"]
    assert skeleton["paperS02"][85]["candidate_id"] == "bib:paperS02:85"

    skeleton["paperS02"][85].update({
        "paper_id": "orig85",
        "title": "Beyond Data-Driven: How Physics-Informed Neural Networks "
        "are Reshaping Multi-Physics Design and Discovery",
        "doi": "10.0/85",
        "text": "Original full text for reference 85.",
        "source_kind": "oa_fulltext",
        "content_depth": "fulltext",
    })
    review = _unit(
        "review_s02",
        "Beyond Data-Driven: How Physics-Informed Neural Networks are "
        "Reshaping Multi-Physics Design and Discovery",
        "The survey method is described in [85].",
        paper_id="paperS02",
    )
    claim = _claim(
        "c1",
        "The survey method is described.",
        "review_s02",
        "The survey method is described in [85].",
    )
    result = unpack_review_sources(
        [claim],
        _units_map(review),
        bibliography_index=skeleton,
        review_paper_ids={"paperS02"},
    )
    task = result["tasks"][0]
    assert task["citation_numbers"] == [85]
    assert task["outcome"] == "original_source_materialized"
    assert task["selected_original_sources"][0]["paper_id"] == "orig85"
    assert task["selected_original_sources"][0]["materialized_unit_id"]
    assert task["review_fallback"]["chunk_id"] == "review_s02"
