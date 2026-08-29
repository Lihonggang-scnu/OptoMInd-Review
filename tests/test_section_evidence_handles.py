from __future__ import annotations

import json

import pytest

from optomind_research.review_writer import (
    COMPACT_EVIDENCE_HANDLES_MODE,
    EvidencePacket,
    SectionMaterialPacket,
    _compact_paragraph_payload,
)
from optomind_research.section_evidence_handles import EvidenceHandleRegistry


def _evidence_pair() -> list[EvidencePacket]:
    return [
        EvidencePacket(
            claim_id="C1",
            paper_id="paper-one",
            chunk_id="paper-one:0010",
            exact_spans=["Verified span one for mechanism A."],
            source_title="Mechanism A study",
            limitations=["Covers only mechanism A."],
            evidence_level="fulltext",
        ),
        EvidencePacket(
            claim_id="C2",
            paper_id="paper-two",
            chunk_id="paper-two:0068",
            exact_spans=["Measured response B in the target system."],
            source_title="Response B measurement",
            limitations=["Single experimental condition."],
            evidence_level="fulltext",
        ),
    ]


def _packet() -> SectionMaterialPacket:
    return SectionMaterialPacket(
        section_id="S01",
        section_contract={
            "writing_mode": COMPACT_EVIDENCE_HANDLES_MODE,
            "title": "Mechanisms",
            "word_budget": 120,
            "paragraph_functions": [{"paragraph_index": 1, "claim_ids": ["C1"]}],
        },
        evidence_packets=_evidence_pair(),
    )


def test_handle_registry_deterministic_assignment_and_resolution() -> None:
    registry = EvidenceHandleRegistry(_evidence_pair())

    assert registry.handles == ("E01", "E02")
    assert registry.allowed_markers() == {"E01", "E02"}
    assert registry.entry("E01")["paper_id"] == "paper-one"
    assert registry.entry("E01")["canonical_marker"] == "paper-one"
    assert registry.entry("E01")["chunk_id"] == "paper-one:0010"
    assert registry.entry("E01")["exact_spans"] == [
        "Verified span one for mechanism A."
    ]
    assert registry.entry("E01")["semantic_label"].startswith("Mechanism A study")
    assert " | " in registry.entry("E01")["semantic_label"]
    assert registry.entry("E01")["semantic_label"].isascii()
    assert registry.entry("E02")["paper_id"] == "paper-two"

    text, resolved, unknown = registry.resolve_text(
        "One source [REF:E01] and another [REF:E02] in the same sentence."
    )
    assert text == (
        "One source [REF:paper-one] and another [REF:paper-two] in the same sentence."
    )
    assert resolved == ["E01", "E02"]
    assert unknown == []


def test_unknown_handles_stay_rejected_and_unresolved() -> None:
    registry = EvidenceHandleRegistry(_evidence_pair())

    text, resolved, unknown = registry.resolve_text(
        "Unknown [REF:E99] and [REF:E03] plus canonical [REF:paper-one]."
    )
    assert "[REF:E99]" in text
    assert "[REF:E03]" in text
    assert "[REF:paper-one]" in text
    assert resolved == []
    assert unknown == ["E03", "E99"]

    # A handle never maps to more than one canonical source.
    assert registry.entry("E01")["paper_id"] != registry.entry("E02")["paper_id"]


def test_bare_and_ref_handle_forms_resolve_to_canonical() -> None:
    registry = EvidenceHandleRegistry(_evidence_pair())

    text, resolved, unknown = registry.resolve_text(
        "Bare [E01] and ref [REF:E02] with unknown [E99] and [REF:E99]."
    )
    assert text == (
        "Bare [REF:paper-one] and ref [REF:paper-two] with unknown [E99] "
        "and [REF:E99]."
    )
    assert resolved == ["E01", "E02"]
    assert unknown == ["E99"]


def test_handle_markers_detect_bare_ref_and_malformed_forms() -> None:
    registry = EvidenceHandleRegistry(_evidence_pair())

    markers = registry.handle_markers(
        "Bare [E01] ref [REF:E02] unknown [E99] [REF:E99] lower [e01]."
    )
    assert markers == {"E01", "E02", "E99", "e01"}

    text, resolved, unknown = registry.resolve_text("Lower [e01].")
    assert "[e01]" in text
    assert resolved == []
    assert unknown == ["e01"]


def test_malformed_mixed_handle_forms_fail_closed() -> None:
    registry = EvidenceHandleRegistry(_evidence_pair())

    malformed = registry.malformed_handle_markers(
        "Mixed [E01:0010] and [REF:E02:0068]."
    )
    assert malformed == ["[E01:0010]", "[REF:E02:0068]"]

    text, _resolved, unknown = registry.resolve_text(
        "Mixed [E01:0010] stays unresolved."
    )
    assert "[E01:0010]" in text
    assert unknown == []


def test_compound_handle_lists_resolve_to_canonical_markers() -> None:
    registry = EvidenceHandleRegistry([
        EvidencePacket(
            claim_id="C1",
            paper_id="paper-one",
            chunk_id="paper-one:0010",
            exact_spans=["One."],
        ),
        EvidencePacket(
            claim_id="C2",
            paper_id="paper-two",
            chunk_id="paper-two:0020",
            exact_spans=["Two."],
        ),
        EvidencePacket(
            claim_id="C3",
            paper_id="paper-one",
            chunk_id="paper-one:0030",
            exact_spans=["One again."],
        ),
    ])

    text, resolved, unknown = registry.resolve_text("[E01, E02]")
    assert text == "[REF:paper-one] [REF:paper-two]"
    assert resolved == ["E01", "E02"]
    assert unknown == []

    text, resolved, unknown = registry.resolve_text("[E03, E01]")
    assert text == "[REF:paper-one]"
    assert resolved == ["E03", "E01"]
    assert unknown == []

    text, _resolved, _unknown = registry.resolve_text("[REF:E01; REF:E02]")
    assert text == "[REF:paper-one] [REF:paper-two]"

    text, _resolved, _unknown = registry.resolve_text("[E01, REF:E02]")
    assert text == "[REF:paper-one] [REF:paper-two]"


def test_live_compound_pattern_e21_e46_e47_resolves() -> None:
    registry = EvidenceHandleRegistry([
        EvidencePacket(
            claim_id=f"C{index}",
            paper_id=f"paper-{index:02d}",
            chunk_id=f"paper-{index:02d}:0010",
            exact_spans=[f"Span {index}."],
        )
        for index in range(1, 48)
    ])

    text, resolved, unknown = registry.resolve_text("[E21, E46, E47]")
    assert text == "[REF:paper-21] [REF:paper-46] [REF:paper-47]"
    assert resolved == ["E21", "E46", "E47"]
    assert unknown == []


def test_compound_with_unknown_item_fails_closed_no_partial_rewrite() -> None:
    import optomind_research.review_writer as module

    registry = EvidenceHandleRegistry(_evidence_pair())
    for source in (
        "[E01, E99]",
        "[REF:E01, REF:E99]",
        "[e01, E02]",
    ):
        text, resolved, unknown = registry.resolve_text(source)
        assert text == source
        assert resolved == []
        assert unknown

    assert registry.non_handle_reference_markers(
        "[REF:E01, REF:E02]"
    ) == set()

    packet = _packet()
    _payload, packet_registry = module._compact_packet_payload(packet)
    text, failures = module._resolve_evidence_handle_text(
        "Compound [E01, E99] stays unresolved.",
        packet_registry,
        packet,
    )
    assert "[E01, E99]" in text
    assert any(
        "unknown_handle_markers=['E99']" in failure
        for failure in failures
    )


@pytest.mark.parametrize("source", [
    "[E01, paper-x]",
    "[E01,]",
    "[E01 / E02]",
    "[E01, E99]",
    "[e01]",
    "[REF:E99]",
])
def test_unresolved_handle_brackets_fail_closed(source: str) -> None:
    import optomind_research.review_writer as module

    packet = _packet()
    _payload, registry = module._compact_packet_payload(packet)
    text, failures = module._resolve_evidence_handle_text(
        source, registry, packet
    )

    assert text == source
    assert any(
        "unresolved_handle_brackets=" in failure
        for failure in failures
    )


@pytest.mark.parametrize("source, expected", [
    ("[E01]", "[REF:paper-one]"),
    ("[REF:E01]", "[REF:paper-one]"),
    ("[E01, E02]", "[REF:paper-one] [REF:paper-two]"),
    ("[REF:E01; REF:E02]", "[REF:paper-one] [REF:paper-two]"),
])
def test_valid_handle_forms_resolve_without_residual(
    source: str,
    expected: str,
) -> None:
    registry = EvidenceHandleRegistry(_evidence_pair())

    text, _resolved, _unknown = registry.resolve_text(source)
    assert text == expected
    assert registry.residual_handle_brackets(text) == []


def test_compact_rows_do_not_expose_long_identifiers() -> None:
    registry = EvidenceHandleRegistry(_evidence_pair())
    rows = registry.compact_rows()

    assert [row["handle"] for row in rows] == ["E01", "E02"]
    assert rows[0]["semantic_label"].startswith("Mechanism A study")
    assert " | " in rows[0]["semantic_label"]
    assert rows[0]["semantic_label"].isascii()
    assert rows[0]["exact_text"] == "Verified span one for mechanism A."
    assert rows[0]["limitations"] == ["Covers only mechanism A."]
    assert rows[0]["evidence_level"] == "fulltext"
    for row in rows:
        assert "chunk_id" not in row
        assert "paper_id" not in row
        assert "canonical_marker" not in row

    serialized = json.dumps(registry.to_dict())
    assert "paper-one:0010" in serialized
    assert "paper-two:0068" in serialized


def test_compact_paragraph_payload_uses_handles_only() -> None:
    packet = _packet()
    evidence = _evidence_pair()
    registry = EvidenceHandleRegistry(evidence)
    payload = _compact_paragraph_payload(
        packet,
        paragraph_index=0,
        paragraph_function=packet.section_contract["paragraph_functions"][0],
        assigned_claims=[],
        evidence_packets=evidence,
        allowed_markers={"paper-one", "paper-two"},
        evidence_handles=registry,
        previous_paragraph_text="",
        previous_paragraph_tail="",
        word_targets=(100, 120, 200),
    )

    assert payload["writing_mode"] == COMPACT_EVIDENCE_HANDLES_MODE
    assert payload["allowed_reference_markers"] == ["E01", "E02"]
    assert "evidence_packets" not in payload
    assert payload["evidence_handles"][0]["handle"] == "E01"
    serialized = json.dumps(payload)
    assert "paper-one:0010" not in serialized
    assert "paper-two:0068" not in serialized
    assert "chunk_id" not in serialized
    assert "paper_id" not in serialized


def test_legacy_payload_mode_unchanged() -> None:
    packet = _packet()
    evidence = _evidence_pair()
    payload = _compact_paragraph_payload(
        packet,
        paragraph_index=0,
        paragraph_function=packet.section_contract["paragraph_functions"][0],
        assigned_claims=[],
        evidence_packets=evidence,
        allowed_markers={"paper-one", "paper-two"},
        previous_paragraph_text="",
        previous_paragraph_tail="",
        word_targets=(100, 120, 200),
    )

    assert "writing_mode" not in payload
    assert payload["evidence_packets"][0]["chunk_id"] == "paper-one:0010"
    assert payload["evidence_packets"][0]["paper_id"] == "paper-one"
    assert payload["allowed_reference_markers"] == ["paper-one", "paper-two"]


def test_retry_reencoding_uses_lowest_valid_handle() -> None:
    registry = EvidenceHandleRegistry([
        EvidencePacket(
            claim_id="C1",
            paper_id="paper-one",
            chunk_id="paper-one:0010",
            exact_spans=["First chunk."],
        ),
        EvidencePacket(
            claim_id="C2",
            paper_id="paper-one",
            chunk_id="paper-one:0015",
            exact_spans=["Second chunk."],
        ),
    ])

    encoded = registry.encode_markers_to_handles(
        "Both [REF:paper-one] and [REF:paper-one] plus unknown [REF:E99]."
    )
    assert encoded == (
        "Both [REF:E01] and [REF:E01] plus unknown [REF:E99]."
    )


def test_packet_level_compact_payload_hides_citation_identifiers() -> None:
    import optomind_research.review_writer as module

    packet = _packet()
    packet.literature_coverage = {
        "sources": [
            {
                "paper_id": "coverage-paper",
                "title": "Landscape study",
                "role": "foundation",
                "source_kind": "s2_body",
                "content_depth": "fulltext",
                "use_permission": "factual_support",
                "allowed_claim_kinds": ["core"],
                "doi": "10.1000/coverage-doi",
                "representative_chunks": [
                    {
                        "chunk_id": "coverage-paper:0055",
                        "text_preview": "Coverage span.",
                    }
                ],
            }
        ]
    }
    payload, registry = module._compact_packet_payload(packet)

    assert registry.handles == ("E01", "E02", "E03")
    assert payload["allowed_reference_markers"] == ["E01", "E02", "E03"]
    assert "evidence_packets" not in payload
    assert payload["literature_coverage"]["mode"] == (
        module.COMPACT_EVIDENCE_HANDLES_MODE
    )
    assert payload["literature_coverage"]["sources"][0]["evidence_handles"] == [
        "E03"
    ]
    coverage_source = payload["literature_coverage"]["sources"][0]
    assert coverage_source["source_kind"] == "s2_body"
    assert coverage_source["content_depth"] == "fulltext"
    assert coverage_source["use_permission"] == "factual_support"
    assert coverage_source["allowed_claim_kinds"] == ["core"]
    serialized = json.dumps(payload)
    assert "paper-one:0010" not in serialized
    assert "coverage-paper:0055" not in serialized
    assert "chunk_id" not in serialized
    assert "paper_id" not in serialized
    assert "10.1000/coverage-doi" not in serialized


def test_packet_level_resolution_rejects_non_handle_markers() -> None:
    import optomind_research.review_writer as module

    packet = _packet()
    _payload, registry = module._compact_packet_payload(packet)
    text, failures = module._resolve_evidence_handle_text(
        "Canonical [REF:paper-one] and unknown [REF:E99].",
        registry,
        packet,
    )

    assert "[REF:paper-one]" in text
    assert "[REF:E99]" in text
    assert failures == [
        "non_handle_reference_markers=['paper-one']",
        "unknown_handle_markers=['E99']",
        "unresolved_handle_brackets=['[REF:E99]']",
    ]


def test_packet_level_payload_sanitizes_nested_identifier_fields() -> None:
    import optomind_research.review_writer as module

    long_paper = "5b3673c6625834df7a84a464bdb6950ae7e55b7c"
    long_chunk = f"s2-body:{long_paper}:0010"
    claim = {
        "claim_id": "C1",
        "statement": "Claim statement.",
        "statement_for_writing": "Claim writing statement.",
        "role": "core",
        "caveats": ["Caveat."],
        "claim_components": [
            {
                "bindings": [
                    {
                        "chunk_id": long_chunk,
                        "verbatim_quote": "exact binding quote",
                    }
                ],
                "reason": (
                    f"The claim is supported by Chunk {long_chunk} "
                    f"from paper {long_paper}. A typo locator mentions Chunk "
                    "s2-body:5db739ebe01f8718845e80bd0f23d3c17a167e95:0003."
                ),
            }
        ],
        "verified_quotes": [
            {"chunk_id": long_chunk, "quote": "exact verified quote"}
        ],
        "claim_scope_contract": {
            "source_envelope": {
                "paper_ids": [long_paper],
                "chunk_ids": [long_chunk],
                "independent_source_count": 1,
                "source_kinds": ["s2_body"],
                "content_depths": ["fulltext"],
                "permissions": ["factual_support"],
                "attribution_required": False,
                "sources": [
                    {
                        "chunk_id": long_chunk,
                        "paper_id": long_paper,
                        "title": "Source title",
                        "doi": "10.1088/1361-6463/acbec3",
                        "source_kind": "s2_body",
                        "content_depth": "fulltext",
                        "use_permission": "factual_support",
                    }
                ],
                "verified_quotes": [
                    {"chunk_id": long_chunk, "quote": "exact verified quote"}
                ],
            }
        },
        "source_contexts": [
            {
                "chunk_id": long_chunk,
                "paper_id": long_paper,
                "title": "Source title",
                "doi": "10.1088/1361-6463/acbec3",
                "source_kind": "s2_body",
                "content_depth": "fulltext",
                "use_permission": "factual_support",
            }
        ],
        "_rejected_chunk_ids": [long_chunk],
        "writing_permission": "factual_assertion",
    }
    packet = SectionMaterialPacket(
        section_id="S01",
        section_contract={
            "writing_mode": COMPACT_EVIDENCE_HANDLES_MODE,
            "title": "Mechanisms",
            "evidence_provenance": {
                long_chunk: {
                    "paper_id": long_paper,
                    "provenance_type": "real_paper_id",
                    "source": {"title": "Source title"},
                }
            },
        },
        claims=[claim],
        evidence_packets=[
            EvidencePacket(
                claim_id="C1",
                paper_id=long_paper,
                chunk_id=long_chunk,
                exact_spans=["exact verified quote"],
                source_title="Source title",
            )
        ],
        manuscript_context={
            "evidence_provenance": {
                long_chunk: {"paper_id": long_paper}
            }
        },
        literature_coverage={
            "paper_ids": [long_paper],
            "evidence_chunk_ids": [long_chunk],
            "sources": [
                {
                    "paper_id": long_paper,
                    "title": "Coverage source title",
                    "role": "foundation",
                    "representative_chunks": [
                        {
                            "chunk_id": long_chunk,
                            "text_preview": "coverage exact text",
                        }
                    ],
                }
            ],
        },
    )

    payload, registry = module._compact_packet_payload(packet)
    serialized = json.dumps(payload)

    for forbidden in (
        "chunk_id",
        "chunk_ids",
        "paper_id",
        "paper_ids",
        "evidence_provenance",
        "_rejected_chunk_ids",
        "provenance_type",
        "doi",
    ):
        assert forbidden not in serialized
    assert long_chunk not in serialized
    assert long_paper not in serialized
    assert "5db739ebe01f8718845e80bd0f23d3c17a167e95" not in serialized
    assert "10.1088/1361-6463/acbec3" not in serialized
    # Scientific content and exact text survive.
    assert "exact verified quote" in serialized
    assert "exact binding quote" in serialized
    assert "[source locator omitted]" in serialized
    assert "Source title" in serialized
    assert "coverage exact text" in serialized
    assert any(
        row["exact_text"] == "exact verified quote"
        for row in payload["evidence_handles"]
    )
    # Local registry keeps the canonical identity mapping.
    registry_json = json.dumps(registry.to_dict())
    assert long_chunk in registry_json
    assert long_paper in registry_json


def test_router_routes_all_ready_claims_unique_primary_and_balanced() -> None:
    from optomind_research.section_claim_router import route_section_claims

    claims = [
        {
            "claim_id": f"C{index:02d}",
            "statement": f"Statement {index} about mechanism {index}.",
            "statement_for_writing": (
                f"Claim {index} is supported by evidence {index}."
            ),
            "role": "core",
            "supported_components": [f"component {index}"],
            "caveats": [f"caveat {index}"],
            "writing_permission": "factual_assertion",
        }
        for index in range(1, 42)
    ]
    paragraph_functions = [
        {
            "paragraph_index": paragraph + 1,
            "title": f"Paragraph {paragraph + 1}",
            "purpose": f"Purpose {paragraph + 1}",
            "claim_ids": [
                f"C{index:02d}"
                for index in range(paragraph * 4 + 1, paragraph * 4 + 5)
            ],
        }
        for paragraph in range(4)
    ]
    argument_sequence = [
        {"step": f"Step {paragraph + 1}", "claim_ids": [f"C{paragraph + 1:02d}"]}
        for paragraph in range(4)
    ]
    ready_ids = {f"C{index:02d}" for index in range(1, 42)}
    evidence_text = {
        claim_id: f"evidence text for {claim_id}"
        for claim_id in ready_ids
    }
    kwargs = {
        "paragraph_functions": paragraph_functions,
        "argument_sequence": argument_sequence,
        "claims": claims,
        "ready_claim_ids": ready_ids,
        "evidence_text_by_claim": evidence_text,
        "expected_paragraphs": 4,
        "section_key_questions": ["Which mechanism controls the response?"],
    }

    result = route_section_claims(**kwargs)
    primary_ids = [
        str(claim.get("claim_id") or "")
        for paragraph in result.primary_by_paragraph
        for claim in paragraph
    ]

    assert len(primary_ids) == 41
    assert len(set(primary_ids)) == 41
    assert set(primary_ids) == ready_ids
    assert result.unassigned_claim_ids == []
    assert result.unsupported_claim_ids == []
    assert result.diagnostics["explicit_primary_count"] == 16
    assert result.diagnostics["residual_primary_count"] == 25
    residual_per_paragraph = [
        sum(1 for source in sources if source != "paragraph_functions_explicit")
        for sources in result.primary_sources
    ]
    assert max(residual_per_paragraph) <= 9
    # Deterministic: identical routing on a second call.
    assert route_section_claims(**kwargs).to_dict() == result.to_dict()


def test_source_locator_redaction_generic_patterns() -> None:
    from optomind_research.review_writer import _redact_source_locators

    text = (
        "See Chunk s2-body:5db739ebe01f8718845e80bd0f23d3c17a167e95:0003 "
        "and Chunk m3gap:10.1007-s10444-023-10065-9:0068 and hash "
        "5db739ebe01f8718845e80bd0f23d3c17a167e95. Normal numbers 58% and "
        "0.052 and 10^4 remain."
    )
    redacted = _redact_source_locators(text)

    assert "5db739ebe01f8718845e80bd0f23d3c17a167e95" not in redacted
    assert "10.1007-s10444-023-10065-9" not in redacted
    assert redacted.count("[source locator omitted]") == 3
    assert "58%" in redacted
    assert "0.052" in redacted
    assert "10^4" in redacted
