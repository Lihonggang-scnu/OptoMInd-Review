from __future__ import annotations

import copy
import json
import re

import pytest

from optomind_research.review_writer import (
    EvidencePacket,
    SectionMaterialPacket,
    SectionWriter,
)


def _prose(word: str, count: int, marker: str | None = None) -> str:
    body = " ".join([word] * count)
    if marker:
        return f"{body} [REF:{marker}]."
    return f"{body}."


def _prose_multi(word: str, count: int, markers: list[str]) -> str:
    refs = " ".join(f"[REF:{marker}]" for marker in markers)
    return f"{' '.join([word] * count)} {refs}."


def _make_packet(
    *,
    word_budget: int = 1400,
    paragraph_count: int = 5,
    min_word_count: int | None = None,
    max_word_count: int | None = None,
):
    paragraph_functions = [
        {
            "paragraph_index": index + 1,
            "title": f"Paragraph {index + 1}",
            "purpose": f"Perform function {index + 1}.",
            "claim_ids": [f"C{index + 1}"],
            "transition_logic": f"Transition after paragraph {index + 1}.",
        }
        for index in range(paragraph_count)
    ]
    contract = {
        "title": "Mechanisms and limits",
        "section_purpose": "Establish the governing mechanism and its boundary conditions.",
        "central_thesis": "The optical mechanism must be interpreted with its boundary conditions.",
        "argument_role": "Explain the governing mechanism and its limits.",
        "argument_sequence": [
            {"step": f"Step {index + 1}: advance the section argument."}
            for index in range(paragraph_count)
        ],
        "paragraph_functions": paragraph_functions,
        "key_questions": ["Which mechanism controls the observed response?"],
        "novel_contribution_to_review": "A bounded comparison of competing mechanisms.",
        "forbidden_overclaims": ["Do not invent numerical limits."],
        "scope_guardrails": ["Stay inside the target material family."],
        "open_questions": ["Which boundary condition dominates in practice?"],
        "word_budget": word_budget,
    }
    if min_word_count is not None:
        contract["min_word_count"] = min_word_count
    if max_word_count is not None:
        contract["max_word_count"] = max_word_count
    claims = [
        {
            "claim_id": f"C{index + 1}",
            "statement": f"Claim {index + 1} statement.",
            "statement_for_writing": f"Claim {index + 1} is supported by direct evidence.",
            "writing_permission": "factual_assertion",
            "evidence_binding_status": "direct",
            "claim_state": "planned",
            "supported_components": ["supported component"],
            "missing_evidence_components": [],
        }
        for index in range(paragraph_count)
    ]
    evidence = [
        EvidencePacket(
            claim_id=f"C{index + 1}",
            paper_id=f"paper{index + 1}",
            chunk_id=f"chunk{index + 1}",
            exact_spans=[f"Verified span {index + 1}."],
        )
        for index in range(paragraph_count)
    ]
    # S2 body snippets must remain equal evidence with OA/fulltext packets.
    evidence.append(EvidencePacket(
        claim_id="C1",
        paper_id="paper1",
        chunk_id="chunk1-s2-body",
        exact_spans=["Verified S2 body span for claim 1."],
        evidence_level="fulltext",
        source_kind="s2_body",
    ))
    return SectionMaterialPacket(
        section_id="S01",
        section_contract=contract,
        claims=claims,
        evidence_packets=evidence,
        transition_contract={
            "transition_from_previous": "Continues from the prior section.",
            "transition_to_next": "Leads into the applications section.",
        },
        manuscript_context={
            "previous_section_tail": "The prior section closed on material synthesis."
        },
    )


def test_paragraph_recovery_assembles_1400_word_section_with_compact_payloads(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import optomind_research.review_writer as module

    packet = _make_packet()
    targets = module._paragraph_word_targets(1400, 5)
    paragraph_texts = [
        _prose(
            "paragraph",
            targets[index][1] - 1,
            f"paper{index + 1}:C{index + 1}",
        )
        for index in range(5)
    ]
    initial = _prose("short", 200) + "\n\n" + _prose("draft", 100)
    calls: list[dict] = []
    paragraph_call_count = 0

    def fake(agent_name: str, messages: list[dict], **_kwargs):
        nonlocal paragraph_call_count
        calls.append({
            "agent_name": agent_name,
            "payload": json.loads(messages[-1]["content"]),
        })
        if agent_name == "SectionWriterAgent":
            return {"content": json.dumps({"section_text": initial})}
        text = paragraph_texts[paragraph_call_count]
        paragraph_call_count += 1
        return {"content": json.dumps({"paragraph_text": text})}

    monkeypatch.setattr(module, "call_qwen_chat", fake)
    draft = SectionWriter(model_tier="c_model", real_llm=True).write(packet)

    assert draft.status == "draft"
    assert 1200 <= module._word_count(draft.english_text) <= 1600
    assert module._paragraph_count(draft.english_text) == 5

    paragraph_calls = [
        call for call in calls
        if call["agent_name"].startswith("SectionWriterParagraphRecovery")
    ]
    assert len(paragraph_calls) == 5
    for call in paragraph_calls:
        payload = call["payload"]
        assert set(payload) == {
            "section_identity",
            "guardrails",
            "paragraph",
            "assigned_claims",
            "evidence_packets",
            "allowed_reference_markers",
            "word_targets",
            "retry",
            "previous_attempt",
        }
        assert "material_packet" not in payload
        assert "section_text" not in payload
        assert payload["section_identity"]["section_id"] == "S01"
        assert payload["guardrails"]["forbidden_overclaims"]
        assert payload["paragraph"]["function"]
        assert payload["paragraph"]["function"]["claim_ids"]
        assert payload["word_targets"]["reference_words"] > 0
        assert payload["word_targets"]["target_words"] > 0
        assert payload["word_targets"]["max_words"] >= payload["word_targets"]["target_words"]
        assert payload["allowed_reference_markers"]
        assert payload["retry"] is False
        assert payload["previous_attempt"] is None

    for index, call in enumerate(paragraph_calls):
        payload = call["payload"]
        assert {c["claim_id"] for c in payload["assigned_claims"]} == {f"C{index + 1}"}
        assert {ep["claim_id"] for ep in payload["evidence_packets"]} == {f"C{index + 1}"}
        assert payload["paragraph"]["index"] == index
        assert payload["paragraph"]["function"]["claim_ids"] == [f"C{index + 1}"]
        assert set(payload["allowed_reference_markers"]) == {
            f"paper{index + 1}",
            f"paper{index + 1}:C{index + 1}",
        }
    assert {ep["chunk_id"] for ep in paragraph_calls[0]["payload"]["evidence_packets"]} == {
        "chunk1",
        "chunk1-s2-body",
    }

    record = draft.revision_history[-1]
    assert record["stage"] == "section_paragraph_recovery"
    assert record["accepted"] is True
    assert record["claim_assignment_mode"] == "contract_claim_ids"
    assert record["meets_80_percent_budget"] is True
    assert record["candidate_word_count"] == 1400
    assert len(record["per_paragraph"]) == 5
    assert all(row["call_attempts"] == 1 for row in record["per_paragraph"])
    assert all(row["retry_attempted"] is False for row in record["per_paragraph"])
    assert all(
        row["claim_ids_source"] == "paragraph_functions"
        for row in record["per_paragraph"]
    )
    assert all(
        row["assigned_claim_ids"] == [f"C{index + 1}"]
        for index, row in enumerate(record["per_paragraph"])
    )


def test_unknown_reference_paragraph_falls_back_to_whole_section_repair(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import optomind_research.review_writer as module

    packet = _make_packet(paragraph_count=2)
    targets = module._paragraph_word_targets(1400, 2)
    valid = _prose("valid", targets[0][1] - 1, "paper1:C1")
    bad = _prose("unknown", targets[1][1] - 1, "unknown-paper:C1")
    repaired = (
        _prose("repaired", 599, "paper1:C1") + "\n\n" + _prose("finish", 600)
    )
    initial = (
        _prose("short", 150, "unknown:C1")
        + "\n\n"
        + _prose("draft", 150)
    )
    responses = iter([
        {"section_text": initial},
        {"paragraph_text": valid},
        {"paragraph_text": bad},
        {"paragraph_text": bad},
        {"section_text": repaired},
    ])
    calls: list[str] = []

    def fake(agent_name: str, *_args, **_kwargs):
        calls.append(agent_name)
        return {"content": json.dumps(next(responses))}

    monkeypatch.setattr(module, "call_qwen_chat", fake)
    draft = SectionWriter(real_llm=True).write(packet)

    assert calls == [
        "SectionWriterAgent",
        "SectionWriterParagraphRecoveryAgent",
        "SectionWriterParagraphRecoveryAgent",
        "SectionWriterParagraphRecoveryRetryAgent",
        "SectionWriterContractRepairAgent",
    ]
    assert draft.english_text == repaired
    assert "unknown-paper" not in draft.english_text

    recovery = next(
        row for row in draft.revision_history
        if row["stage"] == "section_paragraph_recovery"
    )
    assert recovery["accepted"] is False
    assert recovery["claim_assignment_mode"] == "contract_claim_ids"
    assert recovery["failure_reason"] == "paragraph_1_hard_failure_after_retry"
    assert recovery["per_paragraph"][0]["retry_attempted"] is False
    assert recovery["per_paragraph"][1]["retry_attempted"] is True
    assert recovery["per_paragraph"][1]["call_attempts"] == 2
    assert recovery["per_paragraph"][1]["first_attempt_failures"] == [
        "unknown_reference_markers=['unknown-paper:C1']"
    ]

    repair = next(
        row for row in draft.revision_history
        if row["stage"] == "section_contract_repair"
    )
    assert repair["accepted"] is True
    assert repair["candidate_word_count"] == 1200


def test_hard_failure_paragraph_receives_bounded_local_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import optomind_research.review_writer as module

    packet = _make_packet(word_budget=900, paragraph_count=3)
    targets = module._paragraph_word_targets(900, 3)
    good_paragraphs = [
        _prose("good", targets[index][1] - 1, f"paper{index + 1}:C{index + 1}")
        for index in range(3)
    ]
    initial = _prose("short", 200)
    paragraph_responses = iter([
        _prose("bad", 298, "unknown:C9"),  # hard failure: unknown marker
        good_paragraphs[0],
        good_paragraphs[1],
        good_paragraphs[2],
    ])
    calls: list[str] = []

    def fake(agent_name: str, *_args, **_kwargs):
        calls.append(agent_name)
        if agent_name == "SectionWriterAgent":
            return {"content": json.dumps({"section_text": initial})}
        return {"content": json.dumps({"paragraph_text": next(paragraph_responses)})}

    monkeypatch.setattr(module, "call_qwen_chat", fake)
    draft = SectionWriter(real_llm=True).write(packet)

    assert calls == [
        "SectionWriterAgent",
        "SectionWriterParagraphRecoveryAgent",
        "SectionWriterParagraphRecoveryRetryAgent",
        "SectionWriterParagraphRecoveryAgent",
        "SectionWriterParagraphRecoveryAgent",
    ]
    assert draft.status == "draft"
    assert module._word_count(draft.english_text) == 900

    record = draft.revision_history[-1]
    assert record["stage"] == "section_paragraph_recovery"
    assert record["accepted"] is True
    assert record["total_paragraph_calls"] == 4
    assert [row["call_attempts"] for row in record["per_paragraph"]] == [2, 1, 1]
    assert [row["retry_attempted"] for row in record["per_paragraph"]] == [
        True, False, False,
    ]
    assert record["per_paragraph"][0]["first_attempt_word_count"] == 300
    assert record["per_paragraph"][0]["first_attempt_failures"] == [
        "unknown_reference_markers=['unknown:C9']"
    ]
    assert record["per_paragraph"][0]["retry_attempt_word_count"] == 300


def test_contract_claim_ids_order_overrides_claim_list_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import optomind_research.review_writer as module

    claims = [
        {
            "claim_id": f"C{index + 1}",
            "statement_for_writing": f"Claim {index + 1} statement.",
            "writing_permission": "factual_assertion",
            "evidence_binding_status": "direct",
            "claim_state": "planned",
            "supported_components": [],
            "missing_evidence_components": [],
        }
        for index in range(4)
    ]
    evidence = [
        EvidencePacket(
            claim_id=f"C{index + 1}",
            paper_id=f"paper{index + 1}",
            chunk_id=f"chunk{index + 1}",
            exact_spans=[f"Verified span {index + 1}."],
        )
        for index in range(4)
    ]
    contract = {
        "title": "Ordered mapping",
        "central_thesis": "Thesis.",
        "argument_sequence": [
            {"step": f"Step {index + 1}."}
            for index in range(2)
        ],
        "paragraph_functions": [
            {"paragraph_index": 1, "purpose": "First.", "claim_ids": ["C4", "C2"]},
            {"paragraph_index": 2, "purpose": "Second.", "claim_ids": ["C1", "C3"]},
        ],
        "forbidden_overclaims": [],
        "scope_guardrails": [],
        "open_questions": [],
        "word_budget": 800,
    }
    packet = SectionMaterialPacket(
        section_id="S01",
        section_contract=contract,
        claims=claims,
        evidence_packets=evidence,
    )
    targets = module._paragraph_word_targets(800, 2)
    responses = iter([
        {
            "section_text": _prose("short", 200, "unknown:C9")
            + "\n\n"
            + _prose("draft", 100),
        },
        {
            "paragraph_text": _prose_multi(
                "first", targets[0][1] - 2, ["paper4:C4", "paper2:C2"]
            )
        },
        {
            "paragraph_text": _prose_multi(
                "second", targets[1][1] - 2, ["paper1:C1", "paper3:C3"]
            )
        },
    ])
    payloads: list[dict] = []

    def fake(agent_name: str, messages: list[dict], **_kwargs):
        if agent_name.startswith("SectionWriterParagraphRecovery"):
            payloads.append(json.loads(messages[-1]["content"]))
        return {"content": json.dumps(next(responses))}

    monkeypatch.setattr(module, "call_qwen_chat", fake)
    draft = SectionWriter(real_llm=True).write(packet)

    assert draft.status == "draft"
    assert len(payloads) == 2
    first, second = payloads[0], payloads[1]
    assert [c["claim_id"] for c in first["assigned_claims"]] == ["C4", "C2"]
    assert [c["claim_id"] for c in second["assigned_claims"]] == ["C1", "C3"]
    assert {ep["claim_id"] for ep in first["evidence_packets"]} == {"C4", "C2"}
    assert {ep["claim_id"] for ep in second["evidence_packets"]} == {"C1", "C3"}
    assert set(first["allowed_reference_markers"]) == {
        "paper4",
        "paper4:C4",
        "paper2",
        "paper2:C2",
    }
    assert set(second["allowed_reference_markers"]) == {
        "paper1",
        "paper1:C1",
        "paper3",
        "paper3:C3",
    }
    assert "paper1:C1" not in first["allowed_reference_markers"]
    assert first["paragraph"]["function"]["claim_ids"] == ["C4", "C2"]
    assert second["paragraph"]["function"]["claim_ids"] == ["C1", "C3"]

    record = draft.revision_history[-1]
    assert record["claim_assignment_mode"] == "contract_claim_ids"
    assert [
        row["claim_ids_source"] for row in record["per_paragraph"]
    ] == ["paragraph_functions", "paragraph_functions"]
    assert [
        row["assigned_claim_ids"] for row in record["per_paragraph"]
    ] == [["C4", "C2"], ["C1", "C3"]]


def test_argument_sequence_claim_ids_used_when_paragraph_functions_are_legacy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import optomind_research.review_writer as module

    claims = [
        {
            "claim_id": f"C{index + 1}",
            "statement_for_writing": f"Claim {index + 1} statement.",
            "writing_permission": "factual_assertion",
            "evidence_binding_status": "direct",
            "claim_state": "planned",
            "supported_components": [],
            "missing_evidence_components": [],
        }
        for index in range(3)
    ]
    evidence = [
        EvidencePacket(
            claim_id=f"C{index + 1}",
            paper_id=f"paper{index + 1}",
            chunk_id=f"chunk{index + 1}",
            exact_spans=[f"Verified span {index + 1}."],
        )
        for index in range(3)
    ]
    contract = {
        "title": "Sequence mapping",
        "central_thesis": "Thesis.",
        "argument_sequence": [
            {"step": "Step 1.", "claim_ids": ["C2", "C1"]},
            {"step": "Step 2.", "claim_ids": ["C3"]},
        ],
        "paragraph_functions": ["legacy string one", "legacy string two"],
        "forbidden_overclaims": [],
        "scope_guardrails": [],
        "open_questions": [],
        "word_budget": 600,
    }
    packet = SectionMaterialPacket(
        section_id="S01",
        section_contract=contract,
        claims=claims,
        evidence_packets=evidence,
    )
    targets = module._paragraph_word_targets(600, 2)
    responses = iter([
        {"section_text": _prose("short", 150)},
        {
            "paragraph_text": _prose_multi(
                "first", targets[0][1] - 2, ["paper2:C2", "paper1:C1"]
            )
        },
        {"paragraph_text": _prose("second", targets[1][1] - 1, "paper3:C3")},
    ])

    def fake(agent_name: str, *_args, **_kwargs):
        return {"content": json.dumps(next(responses))}

    monkeypatch.setattr(module, "call_qwen_chat", fake)
    draft = SectionWriter(real_llm=True).write(packet)

    assert draft.status == "draft"
    record = draft.revision_history[-1]
    assert record["claim_assignment_mode"] == "contract_claim_ids"
    assert [
        row["claim_ids_source"] for row in record["per_paragraph"]
    ] == ["argument_sequence", "argument_sequence"]
    assert [
        row["assigned_claim_ids"] for row in record["per_paragraph"]
    ] == [["C2", "C1"], ["C3"]]


def test_legacy_string_contract_uses_recorded_round_robin_compat(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import optomind_research.review_writer as module

    claims = [
        {
            "claim_id": f"C{index + 1}",
            "statement_for_writing": f"Claim {index + 1} statement.",
            "writing_permission": "factual_assertion",
            "evidence_binding_status": "direct",
            "claim_state": "planned",
            "supported_components": [],
            "missing_evidence_components": [],
        }
        for index in range(3)
    ]
    evidence = [
        EvidencePacket(
            claim_id=f"C{index + 1}",
            paper_id=f"paper{index + 1}",
            chunk_id=f"chunk{index + 1}",
            exact_spans=[f"Verified span {index + 1}."],
        )
        for index in range(3)
    ]
    contract = {
        "title": "Legacy contract",
        "central_thesis": "Thesis.",
        "argument_sequence": ["Step 1.", "Step 2.", "Step 3."],
        "paragraph_functions": ["Legacy one.", "Legacy two.", "Legacy three."],
        "forbidden_overclaims": [],
        "scope_guardrails": [],
        "open_questions": [],
        "word_budget": 900,
    }
    packet = SectionMaterialPacket(
        section_id="S01",
        section_contract=contract,
        claims=claims,
        evidence_packets=evidence,
    )
    targets = module._paragraph_word_targets(900, 3)
    responses = iter([
        {"section_text": _prose("short", 200)},
        {"paragraph_text": _prose("one", targets[0][1] - 1, "paper1:C1")},
        {"paragraph_text": _prose("two", targets[1][1] - 1, "paper2:C2")},
        {"paragraph_text": _prose("three", targets[2][1] - 1, "paper3:C3")},
    ])

    def fake(agent_name: str, *_args, **_kwargs):
        return {"content": json.dumps(next(responses))}

    monkeypatch.setattr(module, "call_qwen_chat", fake)
    draft = SectionWriter(real_llm=True).write(packet)

    assert draft.status == "draft"
    record = draft.revision_history[-1]
    assert record["claim_assignment_mode"] == "legacy_round_robin_compat"
    assert [
        row["claim_ids_source"] for row in record["per_paragraph"]
    ] == ["compat_round_robin", "compat_round_robin", "compat_round_robin"]
    assert [
        row["assigned_claim_ids"] for row in record["per_paragraph"]
    ] == [["C1"], ["C2"], ["C3"]]


def test_paragraph_local_marker_scope_rejects_global_only_marker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import optomind_research.review_writer as module

    packet = _make_packet(paragraph_count=2)
    targets = module._paragraph_word_targets(1400, 2)
    valid = _prose("valid", targets[0][1] - 1, "paper1:C1")
    global_only = _prose("wrong", targets[1][1] - 1, "paper1:C1")
    repaired = (
        _prose("repaired", 599, "paper1:C1") + "\n\n" + _prose("finish", 600)
    )
    initial = (
        _prose("short", 150, "unknown:C1")
        + "\n\n"
        + _prose("draft", 150)
    )
    responses = iter([
        {"section_text": initial},
        {"paragraph_text": valid},
        {"paragraph_text": global_only},
        {"paragraph_text": global_only},
        {"section_text": repaired},
    ])
    payloads: list[dict] = []

    def fake(agent_name: str, messages: list[dict], **_kwargs):
        if agent_name.startswith("SectionWriterParagraphRecovery"):
            payloads.append(json.loads(messages[-1]["content"]))
        return {"content": json.dumps(next(responses))}

    monkeypatch.setattr(module, "call_qwen_chat", fake)
    draft = SectionWriter(real_llm=True).write(packet)

    assert len(payloads) == 3
    assert set(payloads[0]["allowed_reference_markers"]) == {
        "paper1",
        "paper1:C1",
    }
    assert set(payloads[1]["allowed_reference_markers"]) == {
        "paper2",
        "paper2:C2",
    }
    assert set(payloads[2]["allowed_reference_markers"]) == {
        "paper2",
        "paper2:C2",
    }

    recovery = next(
        row for row in draft.revision_history
        if row["stage"] == "section_paragraph_recovery"
    )
    assert recovery["accepted"] is False
    assert recovery["failure_reason"] == "paragraph_1_hard_failure_after_retry"
    assert recovery["per_paragraph"][1]["retry_attempted"] is True
    assert recovery["per_paragraph"][1]["first_attempt_failures"] == [
        "unknown_reference_markers=['paper1:C1']"
    ]
    repair = next(
        row for row in draft.revision_history
        if row["stage"] == "section_contract_repair"
    )
    assert repair["accepted"] is True
    assert draft.english_text == repaired


def test_main_writer_prompt_requests_single_output_field() -> None:
    import optomind_research.review_writer as module

    text = module._read_prompt(module.SECTION_WRITER_PROMPT)
    assert '"section_text"' in text
    for removed in (
        "claim_coverage",
        "literature_role_coverage",
        "author_synthesis_points",
        '"word_count"',
    ):
        assert removed not in text
    assert "1200" not in text
    assert "shorter evidence-limited chapter is safer" in (
        " ".join(text.lower().split())
    )
    assert "evidence_packets" in text
    assert "Citation policy" in text


def test_compact_recovery_prompt_uses_soft_reference_wording() -> None:
    import optomind_research.review_writer as module

    text = module._read_prompt(module.SECTION_PARAGRAPH_RECOVERY_PROMPT)
    assert "reference_words" in text
    assert "soft" in text
    assert "stop shorter" in text
    assert "never go below" not in text.lower()


def test_safe_short_5_paragraph_draft_below_reference_makes_no_recovery_calls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import optomind_research.review_writer as module

    packet = _make_packet(min_word_count=1200, max_word_count=1600)
    initial = "\n\n".join(_prose("initial", 190) for _ in range(5))
    calls: list[str] = []

    def fake(agent_name: str, *_args, **_kwargs):
        calls.append(agent_name)
        return {"content": json.dumps({"section_text": initial})}

    monkeypatch.setattr(module, "call_qwen_chat", fake)
    draft = SectionWriter(real_llm=True).write(packet)

    assert calls == ["SectionWriterAgent"]
    assert draft.english_text == initial
    assert draft.status == "draft"
    assert module._word_count(draft.english_text) == 950
    assert module._paragraph_count(draft.english_text) == 5

    record = draft.revision_history[-1]
    assert record["stage"] == "section_initial_soft_shortfall"
    assert record["accepted"] is True
    assert record["recovery_required"] is False
    assert record["hard_failures"] == []
    assert record["initial_word_count"] == 950
    assert record["reference_target_word_count"] == 1200
    assert record["hard_max_word_count"] == 1600
    assert record["below_reference_target"] is True


def test_safe_short_single_paragraph_draft_below_reference_makes_no_recovery_calls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import optomind_research.review_writer as module

    packet = _make_packet(
        paragraph_count=1,
        min_word_count=1200,
        max_word_count=1600,
    )
    initial = _prose("limited", 1149, "paper1:C1")
    calls: list[str] = []

    def fake(agent_name: str, *_args, **_kwargs):
        calls.append(agent_name)
        return {"content": json.dumps({"section_text": initial})}

    monkeypatch.setattr(module, "call_qwen_chat", fake)
    draft = SectionWriter(real_llm=True).write(packet)

    assert calls == ["SectionWriterAgent"]
    assert draft.english_text == initial
    assert draft.status == "draft"
    assert module._word_count(draft.english_text) == 1150

    record = draft.revision_history[-1]
    assert record["stage"] == "section_initial_soft_shortfall"
    assert record["accepted"] is True
    assert record["recovery_required"] is False
    assert record["hard_failures"] == []
    assert record["reference_target_word_count"] == 1200
    assert record["hard_max_word_count"] == 1600
    assert record["below_reference_target"] is True


def test_safe_855_word_draft_treats_1200_as_soft_target(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import optomind_research.review_writer as module

    packet = _make_packet(
        paragraph_count=1,
        min_word_count=1200,
        max_word_count=1600,
    )
    initial = _prose("bounded", 854, "paper1:C1")
    calls: list[str] = []

    def fake(agent_name: str, *_args, **_kwargs):
        calls.append(agent_name)
        return {"content": json.dumps({"section_text": initial})}

    monkeypatch.setattr(module, "call_qwen_chat", fake)
    draft = SectionWriter(real_llm=True).write(packet)

    assert calls == ["SectionWriterAgent"]
    assert draft.english_text == initial
    assert draft.status == "draft"
    assert module._word_count(draft.english_text) == 855
    record = draft.revision_history[-1]
    assert record["stage"] == "section_initial_soft_shortfall"
    assert record["accepted"] is True
    assert record["recovery_required"] is False
    assert record["hard_failures"] == []
    assert record["reference_target_word_count"] == 1200
    assert record["hard_max_word_count"] == 1600


def test_safe_json_repairs_only_a_trailing_root_field_fragment() -> None:
    import optomind_research.review_writer as module

    recovered = module._safe_json(
        '{"section_text":"kept","status":"draft"}'
        ', "submission_metadata":{"model":"qwen"}}'
    )
    assert recovered == {
        "section_text": "kept",
        "status": "draft",
        "submission_metadata": {"model": "qwen"},
    }

    assert module._safe_json('{"a":1} {"b":2}') == {"a": 1}
    assert module._safe_json('{"a":1} trailing prose') == {"a": 1}
    assert module._safe_json('{"a":1}, "a":2}') == {"a": 1}


def test_safe_short_paragraphs_are_not_retried_for_reference_shortfall(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import optomind_research.review_writer as module

    packet = _make_packet(min_word_count=1200, max_word_count=1600)
    paragraph_texts = [
        _prose("first", 230, "paper1:C1"),   # 231 words
        _prose("second", 241, "paper2:C2"),  # 242 words
        _prose("third", 250, "paper3:C3"),   # 251 words
        _prose("fourth", 248, "paper4:C4"),  # 249 words
        _prose("fifth", 235, "paper5:C5"),   # 236 words
    ]
    initial = _prose("short", 200)
    responses = iter([
        {"section_text": initial},
        *({"paragraph_text": text} for text in paragraph_texts),
    ])
    calls: list[str] = []

    def fake(agent_name: str, *_args, **_kwargs):
        calls.append(agent_name)
        return {"content": json.dumps(next(responses))}

    monkeypatch.setattr(module, "call_qwen_chat", fake)
    draft = SectionWriter(real_llm=True).write(packet)

    assert calls == [
        "SectionWriterAgent",
        "SectionWriterParagraphRecoveryAgent",
        "SectionWriterParagraphRecoveryAgent",
        "SectionWriterParagraphRecoveryAgent",
        "SectionWriterParagraphRecoveryAgent",
        "SectionWriterParagraphRecoveryAgent",
    ]
    assert "SectionWriterParagraphRecoveryRetryAgent" not in calls
    assert "SectionWriterContractRepairAgent" not in calls
    assert draft.status == "draft"
    assert module._word_count(draft.english_text) == 1209

    record = draft.revision_history[-1]
    assert record["stage"] == "section_paragraph_recovery"
    assert record["accepted"] is True
    assert record["candidate_word_count"] == 1209
    assert record["total_paragraph_calls"] == 5
    assert record["below_reference_target"] is False
    assert all(row["call_attempts"] == 1 for row in record["per_paragraph"])
    assert all(row["retry_attempted"] is False for row in record["per_paragraph"])
    assert all(row["first_attempt_below_reference"] for row in record["per_paragraph"])
    assert all(row["below_reference_target"] for row in record["per_paragraph"])


def test_runaway_paragraph_exceeding_section_cap_falls_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import optomind_research.review_writer as module

    packet = _make_packet(
        paragraph_count=2,
        min_word_count=1200,
        max_word_count=1600,
    )
    targets = module._paragraph_word_targets(1400, 2, 1200, 1600)
    assert targets[0] == (630, 700, 1600)
    runaway = _prose("long", 1699, "paper1:C1")
    repaired = (
        _prose("repaired", 649, "paper1:C1") + "\n\n" + _prose("finish", 600)
    )
    initial = (
        _prose("short", 150, "unknown:C1")
        + "\n\n"
        + _prose("draft", 150)
    )
    responses = iter([
        {"section_text": initial},
        {"paragraph_text": runaway},
        {"paragraph_text": runaway},
        {"section_text": repaired},
    ])
    payloads: list[dict] = []

    def fake(agent_name: str, messages: list[dict], **_kwargs):
        if agent_name.startswith("SectionWriterParagraphRecovery"):
            payloads.append(json.loads(messages[-1]["content"]))
        if agent_name == "SectionWriterContractRepairAgent":
            payloads.append(json.loads(messages[-1]["content"]))
        return {"content": json.dumps(next(responses))}

    monkeypatch.setattr(module, "call_qwen_chat", fake)
    draft = SectionWriter(real_llm=True).write(packet)

    assert payloads[0]["word_targets"] == {
        "reference_words": 630,
        "target_words": 700,
        "max_words": 1600,
    }
    repair_payload = payloads[-1]
    assert repair_payload["contract_failure"]["reference_target_word_count"] == 1200
    assert repair_payload["contract_failure"]["reference_words_per_paragraph"] == 630

    recovery = next(
        row for row in draft.revision_history
        if row["stage"] == "section_paragraph_recovery"
    )
    assert recovery["accepted"] is False
    assert recovery["reference_target_word_count"] == 1200
    assert recovery["hard_max_word_count"] == 1600
    assert recovery["per_paragraph"][0]["first_attempt_failures"] == [
        "word_count=1700>max=1600"
    ]
    assert recovery["per_paragraph"][0]["retry_attempted"] is True
    assert recovery["per_paragraph"][0]["call_attempts"] == 2

    repair = next(
        row for row in draft.revision_history
        if row["stage"] == "section_contract_repair"
    )
    assert repair["accepted"] is True
    assert repair["candidate_word_count"] == 1250
    assert repair["reference_target_word_count"] == 1200
    assert repair["hard_max_word_count"] == 1600


def test_generous_local_max_allows_uneven_rich_paragraph(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import optomind_research.review_writer as module

    packet = _make_packet(min_word_count=1200, max_word_count=1600)
    targets = module._paragraph_word_targets(1400, 5, 1200, 1600)
    assert targets[0] == (252, 280, 1600)
    rich = _prose("rich", 549, "paper1:C1")
    regular = [
        _prose("even", 254, f"paper{index + 1}:C{index + 1}")
        for index in range(1, 5)
    ]
    responses = iter([
        {"section_text": _prose("short", 200)},
        {"paragraph_text": rich},
        *({"paragraph_text": text} for text in regular),
    ])
    calls: list[str] = []
    payloads: list[dict] = []

    def fake(agent_name: str, messages: list[dict], **_kwargs):
        calls.append(agent_name)
        if agent_name.startswith("SectionWriterParagraphRecovery"):
            payloads.append(json.loads(messages[-1]["content"]))
        return {"content": json.dumps(next(responses))}

    monkeypatch.setattr(module, "call_qwen_chat", fake)
    draft = SectionWriter(real_llm=True).write(packet)

    assert draft.status == "draft"
    assert module._word_count(draft.english_text) == 1570
    assert calls[1:] == ["SectionWriterParagraphRecoveryAgent"] * 5
    assert all(
        payload["word_targets"]["max_words"] == 1600
        for payload in payloads
    )
    assert payloads[0]["word_targets"] == {
        "reference_words": 252,
        "target_words": 280,
        "max_words": 1600,
    }

    record = draft.revision_history[-1]
    assert record["accepted"] is True
    assert record["candidate_word_count"] == 1570
    assert record["below_reference_target"] is False
    assert all(row["call_attempts"] == 1 for row in record["per_paragraph"])
    assert all(row["retry_attempted"] is False for row in record["per_paragraph"])


def test_paragraph_recovery_call_contract_uses_named_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import optomind_research.review_writer as module

    packet = _make_packet(word_budget=600, paragraph_count=2)
    targets = module._paragraph_word_targets(600, 2)
    responses = iter([
        {"section_text": _prose("short", 150)},
        {"paragraph_text": _prose("one", targets[0][1] - 1, "paper1:C1")},
        {"paragraph_text": _prose("two", targets[1][1] - 1, "paper2:C2")},
    ])
    call_kwargs: list[tuple[str, dict]] = []

    def fake(agent_name: str, *_args, **kwargs):
        call_kwargs.append((agent_name, kwargs))
        return {"content": json.dumps(next(responses))}

    monkeypatch.setattr(module, "call_qwen_chat", fake)
    draft = SectionWriter(model_tier="c_model", real_llm=True).write(packet)

    assert draft.status == "draft"
    paragraph_calls = [
        kwargs
        for agent_name, kwargs in call_kwargs
        if agent_name.startswith("SectionWriterParagraphRecovery")
    ]
    assert len(paragraph_calls) == 2
    assert module.PARAGRAPH_RECOVERY_TIMEOUT_SECONDS == 300
    for kwargs in paragraph_calls:
        assert kwargs["timeout_seconds"] == 300
        assert kwargs["model_tier"] == "c_model"
        assert kwargs["stream"] is True
        assert kwargs["response_format"] == {"type": "json_object"}
        assert kwargs["max_retries"] == 0
        assert kwargs["allow_model_fallback"] is False


def test_assembled_recovery_rejects_exceeding_explicit_max(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import optomind_research.review_writer as module

    packet = _make_packet(
        paragraph_count=2,
        min_word_count=1200,
        max_word_count=1250,
    )
    first = _prose("first", 699, "paper1:C1")
    second = _prose("second", 699, "paper2:C2")
    repaired = (
        _prose("repaired", 649, "paper1:C1") + "\n\n" + _prose("finish", 600)
    )
    initial = (
        _prose("short", 150, "unknown:C1")
        + "\n\n"
        + _prose("draft", 150)
    )
    responses = iter([
        {"section_text": initial},
        {"paragraph_text": first},
        {"paragraph_text": second},
        {"section_text": repaired},
    ])
    calls: list[str] = []

    def fake(agent_name: str, *_args, **_kwargs):
        calls.append(agent_name)
        return {"content": json.dumps(next(responses))}

    monkeypatch.setattr(module, "call_qwen_chat", fake)
    draft = SectionWriter(real_llm=True).write(packet)

    assert calls == [
        "SectionWriterAgent",
        "SectionWriterParagraphRecoveryAgent",
        "SectionWriterParagraphRecoveryAgent",
        "SectionWriterContractRepairAgent",
    ]
    recovery = next(
        row for row in draft.revision_history
        if row["stage"] == "section_paragraph_recovery"
    )
    assert recovery["accepted"] is False
    assert recovery["failure_reason"] == "length_out_of_contract"
    assert recovery["candidate_word_count"] == 1400
    assert recovery["reference_target_word_count"] == 1200
    assert recovery["hard_max_word_count"] == 1250
    assert all(row["accepted"] is True for row in recovery["per_paragraph"])

    repair = next(
        row for row in draft.revision_history
        if row["stage"] == "section_contract_repair"
    )
    assert repair["accepted"] is True
    assert repair["candidate_word_count"] == 1250
    assert repair["reference_target_word_count"] == 1200
    assert repair["hard_max_word_count"] == 1250


def test_legacy_80_115_reference_fallback_when_explicit_bounds_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import optomind_research.review_writer as module

    assert module._section_word_guidance(1400) == (1120, 1609)
    assert module._section_word_guidance(1400, 1200, 1600) == (1200, 1600)
    assert module._section_word_guidance(1400, 2000, 1600) == (2000, 2000)
    assert module._section_word_guidance(1400, 0, 1600) == (1120, 1600)

    packet = _make_packet(word_budget=1000, paragraph_count=2)
    targets = module._paragraph_word_targets(1000, 2)
    responses = iter([
        {"section_text": _prose("short", 750)},
        {"paragraph_text": _prose("one", targets[0][1] - 1, "paper1:C1")},
        {"paragraph_text": _prose("two", targets[1][1] - 1, "paper2:C2")},
    ])

    def fake(agent_name: str, *_args, **_kwargs):
        return {"content": json.dumps(next(responses))}

    monkeypatch.setattr(module, "call_qwen_chat", fake)
    draft = SectionWriter(real_llm=True).write(packet)

    record = draft.revision_history[-1]
    assert record["stage"] == "section_paragraph_recovery"
    assert record["accepted"] is True
    assert record["candidate_word_count"] == 1000
    assert record["reference_target_word_count"] == 800
    assert record["hard_max_word_count"] == 1150
    assert record["below_reference_target"] is False
    assert record["meets_80_percent_budget"] is True

    calls: list[str] = []

    def fake_long(agent_name: str, *_args, **_kwargs):
        calls.append(agent_name)
        return {"content": json.dumps({
            "section_text": "\n\n".join(
                _prose("enough", 405) for _ in range(2)
            ),
        })}

    monkeypatch.setattr(module, "call_qwen_chat", fake_long)
    long_draft = SectionWriter(real_llm=True).write(
        _make_packet(word_budget=1000, paragraph_count=2)
    )
    assert calls == ["SectionWriterAgent"]
    assert long_draft.revision_history == []


def test_no_budget_skips_recovery_and_repair(monkeypatch: pytest.MonkeyPatch) -> None:
    import optomind_research.review_writer as module

    calls: list[str] = []

    def fake(agent_name: str, *_args, **_kwargs):
        calls.append(agent_name)
        return {"content": json.dumps({"section_text": "Short draft."})}

    monkeypatch.setattr(module, "call_qwen_chat", fake)
    draft = SectionWriter(real_llm=True).write(_make_packet(word_budget=0))

    assert calls == ["SectionWriterAgent"]
    assert draft.status == "draft"
    assert draft.revision_history == []


def test_already_long_draft_skips_recovery_and_repair(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import optomind_research.review_writer as module

    long_text = "\n\n".join(_prose("long", 250) for _ in range(5))
    calls: list[str] = []

    def fake(agent_name: str, *_args, **_kwargs):
        calls.append(agent_name)
        return {"content": json.dumps({"section_text": long_text})}

    monkeypatch.setattr(module, "call_qwen_chat", fake)
    draft = SectionWriter(real_llm=True).write(_make_packet())

    assert calls == ["SectionWriterAgent"]
    assert draft.english_text == long_text
    assert draft.revision_history == []


def test_dry_run_never_calls_model(monkeypatch: pytest.MonkeyPatch) -> None:
    import optomind_research.review_writer as module

    def boom(*_args, **_kwargs):
        raise AssertionError("dry run must not call the model")

    monkeypatch.setattr(module, "call_qwen_chat", boom)
    draft = SectionWriter(real_llm=False).write(_make_packet())

    assert draft.status == "draft"
    assert "Mechanisms and limits" in draft.english_text


def test_reference_marker_alias_normalization_packet_proven() -> None:
    import optomind_research.review_writer as module

    packet = SectionMaterialPacket(
        section_id="S01",
        evidence_packets=[
            EvidencePacket(
                claim_id="C1",
                paper_id="5b2f9e1c",
                chunk_id="5b2f9e1c:0010",
            ),
            EvidencePacket(
                claim_id="C2",
                paper_id="identity-fallback:abc123",
                chunk_id="identity-fallback:abc123:0068",
            ),
            EvidencePacket(
                claim_id="C3",
                paper_id="paper3",
                chunk_id="paper3:0042",
            ),
        ],
    )
    aliases = module.reference_marker_aliases(packet)
    assert aliases["5b2f9e1c:0010"] == "5b2f9e1c"
    assert (
        aliases["identity-fallback:abc123:0068"]
        == "identity-fallback:abc123"
    )
    assert aliases["paper3:0042"] == "paper3"
    assert "5b2f9e1c" not in aliases
    assert "5b2f9e1c:C1" not in aliases

    text = module.normalize_reference_markers(
        (
            "A [REF:5b2f9e1c:0010] "
            "B [REF:identity-fallback:abc123:0068] "
            "C [REF:paper3:0042] "
            "D [REF:5b2f9e1c] E [REF:5b2f9e1c:C1]."
        ),
        packet,
    )
    assert "[REF:5b2f9e1c]" in text
    assert "[REF:identity-fallback:abc123]" in text
    assert "[REF:paper3]" in text
    assert "[REF:5b2f9e1c:C1]" in text
    assert "5b2f9e1c:0010" not in text
    assert "identity-fallback:abc123:0068" not in text


def test_ambiguous_and_unknown_markers_stay_fail_closed() -> None:
    import optomind_research.review_writer as module

    packet = SectionMaterialPacket(
        section_id="S01",
        evidence_packets=[
            EvidencePacket(
                claim_id="A",
                paper_id="paperA",
                chunk_id="shared:0099",
            ),
            EvidencePacket(
                claim_id="B",
                paper_id="paperB",
                chunk_id="shared:0099",
            ),
            EvidencePacket(
                claim_id="C",
                paper_id="paperC",
                chunk_id="paperC:0011",
            ),
        ],
    )
    aliases = module.reference_marker_aliases(packet)
    assert "shared:0099" not in aliases
    assert "paperC:0011" in aliases

    text = module.normalize_reference_markers(
        (
            "[REF:shared:0099] [REF:ghost-paper:0011] "
            "[REF:paperC:9999] [REF:paperC:0011]"
        ),
        packet,
    )
    assert "[REF:shared:0099]" in text
    assert "[REF:ghost-paper:0011]" in text
    assert "[REF:paperC:9999]" in text
    assert "[REF:paperC]" in text
    allowed = module._allowed_reference_markers(packet)
    unknown = set(re.findall(r"\[REF:([^\]]+)\]", text)) - allowed
    assert "shared:0099" in unknown
    assert "ghost-paper:0011" in unknown
    assert "paperC:9999" in unknown


def test_pure_alias_correction_requires_no_model_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import optomind_research.review_writer as module

    packet = SectionMaterialPacket(
        section_id="S01",
        evidence_packets=[
            EvidencePacket(
                claim_id="C1",
                paper_id="5b2f9e1c",
                chunk_id="5b2f9e1c:0010",
            ),
        ],
    )
    responses = iter([{
        "content": json.dumps({
            "section_text": "Alias draft [REF:5b2f9e1c:0010]."
        }),
    }])
    calls: list[str] = []

    def fake(agent_name: str, messages: list[dict], **_kwargs):
        calls.append(agent_name)
        return next(responses)

    monkeypatch.setattr(module, "call_qwen_chat", fake)
    draft = SectionWriter(real_llm=True).write(packet)

    assert calls == ["SectionWriterAgent"]
    assert "[REF:5b2f9e1c]" in draft.english_text
    assert "5b2f9e1c:0010" not in draft.english_text
    assert any(
        entry.get("stage") == "reference_alias_normalization"
        for entry in draft.revision_history
    )


def test_contract_repair_alias_normalized_without_extra_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import optomind_research.review_writer as module

    packet = _make_packet(word_budget=1400, paragraph_count=0)
    packet.evidence_packets.append(EvidencePacket(
        claim_id="C9",
        paper_id="5b2f9e1c",
        chunk_id="5b2f9e1c:0010",
    ))
    repair_paragraphs = _prose("p", 800, "5b2f9e1c:0010")
    responses = iter([
        {"content": json.dumps({"section_text": "Too short."})},
        {"content": json.dumps({"section_text": repair_paragraphs})},
    ])
    calls: list[str] = []

    def fake(agent_name: str, messages: list[dict], **_kwargs):
        calls.append(agent_name)
        return next(responses)

    monkeypatch.setattr(module, "call_qwen_chat", fake)
    draft = SectionWriter(real_llm=True).write(packet)

    assert calls == [
        "SectionWriterAgent",
        "SectionWriterContractRepairAgent",
    ]
    assert "[REF:5b2f9e1c]" in draft.english_text
    assert "5b2f9e1c:0010" not in draft.english_text
    repair_record = next(
        entry for entry in draft.revision_history
        if entry.get("stage") == "section_contract_repair"
    )
    assert repair_record["accepted"] is True
    assert repair_record["unknown_reference_markers"] == []


def test_compact_evidence_handles_persist_canonical_without_extra_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import optomind_research.review_writer as module

    packet = _make_packet(word_budget=1400, paragraph_count=1)
    packet.section_contract["writing_mode"] = module.COMPACT_EVIDENCE_HANDLES_MODE
    packet.evidence_packets = [
        EvidencePacket(
            claim_id="C1",
            paper_id="5b2f9e1c",
            chunk_id="5b2f9e1c:0010",
            exact_spans=["Verified span for the mechanism."],
            source_title="Source title",
        )
    ]
    responses = iter([{
        "content": json.dumps({
            "section_text": ("Packet handle draft [REF:E01]. " * 140).strip()
        }),
    }])
    calls: list[str] = []
    payloads: dict[str, dict] = {}

    def fake(agent_name: str, messages: list[dict], **_kwargs):
        calls.append(agent_name)
        payloads[agent_name] = json.loads(messages[-1]["content"])
        return next(responses)

    monkeypatch.setattr(module, "call_qwen_chat", fake)
    draft = SectionWriter(real_llm=True).write(packet)

    assert calls == ["SectionWriterAgent"]
    assert "[REF:5b2f9e1c]" in draft.english_text
    assert "[REF:E01]" not in draft.english_text
    full_payload = payloads["SectionWriterAgent"]
    assert full_payload["writing_mode"] == module.COMPACT_EVIDENCE_HANDLES_MODE
    assert full_payload["allowed_reference_markers"] == ["E01"]
    assert full_payload["evidence_handles"][0]["handle"] == "E01"
    assert full_payload["evidence_handles"][0]["exact_text"] == (
        "Verified span for the mechanism."
    )
    assert "Claim 1 statement." in json.dumps(full_payload, ensure_ascii=False)
    assert "Source title" in json.dumps(full_payload, ensure_ascii=False)
    serialized_payload = json.dumps(full_payload)
    assert "chunk_id" not in serialized_payload
    assert "5b2f9e1c:0010" not in serialized_payload
    assert "5b2f9e1c" not in serialized_payload
    resolution = next(
        entry for entry in draft.revision_history
        if entry.get("stage") == "evidence_handle_resolution"
    )
    assert resolution["evidence_handle_registry"]["entries"]["E01"]["paper_id"] == (
        "5b2f9e1c"
    )
    assert not any(
        entry.get("stage") == "compact_evidence_handle_writing_retry"
        for entry in draft.revision_history
    )


def test_compact_evidence_handles_valid_handle_passes_without_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import optomind_research.review_writer as module

    packet = _make_packet(word_budget=1400, paragraph_count=1)
    packet.section_contract["writing_mode"] = module.COMPACT_EVIDENCE_HANDLES_MODE
    packet.evidence_packets = [
        EvidencePacket(
            claim_id="C1",
            paper_id="5b2f9e1c",
            chunk_id="5b2f9e1c:0010",
            exact_spans=["Verified span."],
        )
    ]
    registry = module.EvidenceHandleRegistry(packet.evidence_packets)
    payload = module._compact_paragraph_payload(
        packet,
        paragraph_index=0,
        paragraph_function=packet.section_contract["paragraph_functions"][0],
        assigned_claims=[packet.claims[0]],
        evidence_packets=packet.evidence_packets,
        allowed_markers=module._markers_for_evidence(packet.evidence_packets),
        evidence_handles=registry,
        previous_paragraph_text="",
        previous_paragraph_tail="",
        word_targets=(100, 120, 200),
    )
    calls: list[str] = []

    def fake(agent_name: str, messages: list[dict], **_kwargs):
        calls.append(agent_name)
        return {"content": json.dumps({
            "paragraph_text": "Valid handle [REF:E01]."
        })}

    monkeypatch.setattr(module, "call_qwen_chat", fake)
    writer = SectionWriter(real_llm=True)
    text, failures, _words, resolved_handles = writer._call_paragraph_recovery(
        agent_name="SectionWriterParagraphRecoveryAgent",
        payload=payload,
        recovery_system="",
        allowed_markers=module._markers_for_evidence(packet.evidence_packets),
        max_words=200,
        packet=packet,
        evidence_handles=registry,
    )

    assert calls == ["SectionWriterParagraphRecoveryAgent"]
    assert failures == []
    assert text == "Valid handle [REF:5b2f9e1c]."
    assert "[REF:E01]" not in text
    assert resolved_handles == ["E01"]


def test_compact_evidence_handles_unknown_handle_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import optomind_research.review_writer as module

    packet = _make_packet(word_budget=1400, paragraph_count=1)
    packet.section_contract["writing_mode"] = module.COMPACT_EVIDENCE_HANDLES_MODE
    packet.evidence_packets = [
        EvidencePacket(
            claim_id="C1",
            paper_id="5b2f9e1c",
            chunk_id="5b2f9e1c:0010",
            exact_spans=["Verified span."],
        )
    ]
    responses = iter([
        {"content": json.dumps({"section_text": "Bad [REF:E99]."})},
        {"content": json.dumps({"paragraph_text": "Bad handle [REF:E99]."})},
        {"content": json.dumps({"paragraph_text": "Bad handle again [REF:E99]."})},
        {
            "content": json.dumps({
                "section_text": (
                    "Repair text is longer and fully grounded [REF:E01]."
                )
            }),
        },
    ])
    calls: list[str] = []
    payloads: dict[str, dict] = {}

    def fake(agent_name: str, messages: list[dict], **_kwargs):
        calls.append(agent_name)
        payloads[agent_name] = json.loads(messages[-1]["content"])
        return next(responses)

    monkeypatch.setattr(module, "call_qwen_chat", fake)
    draft = SectionWriter(real_llm=True).write(packet)

    assert calls == [
        "SectionWriterAgent",
        "SectionWriterParagraphRecoveryAgent",
        "SectionWriterParagraphRecoveryRetryAgent",
        "SectionWriterContractRepairAgent",
    ]
    assert "[REF:5b2f9e1c]" in draft.english_text
    assert "[REF:E99]" not in draft.english_text
    recovery = next(
        entry for entry in draft.revision_history
        if entry.get("stage") == "compact_evidence_handle_writing_retry"
    )
    assert recovery["accepted"] is False
    assert recovery["failure_reason"] == "paragraph_0_hard_failure_after_retry"
    first_failures = recovery["per_paragraph"][0]["first_attempt_failures"]
    assert any("E99" in failure for failure in first_failures)
    full_payload = payloads["SectionWriterAgent"]
    assert full_payload["writing_mode"] == module.COMPACT_EVIDENCE_HANDLES_MODE
    assert full_payload["allowed_reference_markers"] == ["E01"]
    serialized_full = json.dumps(full_payload)
    assert "chunk_id" not in serialized_full
    assert "5b2f9e1c:0010" not in serialized_full
    assert "5b2f9e1c" not in serialized_full
    repair = next(
        entry for entry in draft.revision_history
        if entry.get("stage") == "section_contract_repair"
    )
    assert repair["accepted"] is True
    assert repair["unknown_reference_markers"] == []


def test_compact_whole_section_bare_and_ref_handles_resolve_without_rewrite(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import optomind_research.review_writer as module

    packet = _make_packet(word_budget=1400, paragraph_count=1)
    packet.section_contract["writing_mode"] = module.COMPACT_EVIDENCE_HANDLES_MODE
    packet.evidence_packets = [
        EvidencePacket(
            claim_id="C1",
            paper_id="5b2f9e1c",
            chunk_id="5b2f9e1c:0010",
            exact_spans=["Verified span."],
            source_title="Source title",
        )
    ]
    responses = iter([{
        "content": json.dumps({
            "section_text": (
                "Packet draft [E01] and [REF:E01]. " * 120
            ).strip()
        }),
    }])
    calls: list[str] = []

    def fake(agent_name: str, messages: list[dict], **_kwargs):
        calls.append(agent_name)
        return next(responses)

    monkeypatch.setattr(module, "call_qwen_chat", fake)
    draft = SectionWriter(real_llm=True).write(packet)

    assert calls == ["SectionWriterAgent"]
    assert "[REF:5b2f9e1c]" in draft.english_text
    assert "[E01]" not in draft.english_text
    assert "[REF:E01]" not in draft.english_text
    resolution = next(
        entry for entry in draft.revision_history
        if entry.get("stage") == "evidence_handle_resolution"
    )
    assert resolution["before_handles"] == ["E01"]
    assert resolution["resolved_handles"] == ["E01"]
    assert not any(
        entry.get("stage") == "compact_evidence_handle_writing_retry"
        for entry in draft.revision_history
    )


def test_compact_whole_section_unknown_bare_handle_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import optomind_research.review_writer as module

    packet = _make_packet(word_budget=1400, paragraph_count=1)
    packet.section_contract["writing_mode"] = module.COMPACT_EVIDENCE_HANDLES_MODE
    packet.evidence_packets = [
        EvidencePacket(
            claim_id="C1",
            paper_id="5b2f9e1c",
            chunk_id="5b2f9e1c:0010",
            exact_spans=["Verified span."],
        )
    ]
    responses = iter([
        {"content": json.dumps({"section_text": "Bad [E99]."})},
        {"content": json.dumps({"paragraph_text": "Bad handle [E99]."})},
        {"content": json.dumps({"paragraph_text": "Bad handle again [E99]."})},
        {
            "content": json.dumps({
                "section_text": (
                    "Repair text is longer and fully grounded [REF:E01]."
                )
            }),
        },
    ])
    calls: list[str] = []

    def fake(agent_name: str, messages: list[dict], **_kwargs):
        calls.append(agent_name)
        return next(responses)

    monkeypatch.setattr(module, "call_qwen_chat", fake)
    draft = SectionWriter(real_llm=True).write(packet)

    assert calls == [
        "SectionWriterAgent",
        "SectionWriterParagraphRecoveryAgent",
        "SectionWriterParagraphRecoveryRetryAgent",
        "SectionWriterContractRepairAgent",
    ]
    assert "[REF:5b2f9e1c]" in draft.english_text
    assert "[E99]" not in draft.english_text
    diagnostic = next(
        entry for entry in draft.revision_history
        if entry.get("stage") == "section_initial_contract_diagnostic"
    )
    assert any(
        "unknown_handle_markers=['E99']" in failure
        for failure in diagnostic["hard_failures"]
    )
    recovery = next(
        entry for entry in draft.revision_history
        if entry.get("stage") == "compact_evidence_handle_writing_retry"
    )
    first_failures = recovery["per_paragraph"][0]["first_attempt_failures"]
    assert any(
        "unknown_handle_markers=['E99']" in failure
        for failure in first_failures
    )


def test_compact_whole_section_live_compound_handles_resolve_without_rewrite(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import optomind_research.review_writer as module

    packet = _make_packet(word_budget=1400, paragraph_count=1)
    packet.section_contract["writing_mode"] = module.COMPACT_EVIDENCE_HANDLES_MODE
    packet.evidence_packets = [
        EvidencePacket(
            claim_id="C1",
            paper_id=f"paper-{index:02d}",
            chunk_id=f"paper-{index:02d}:0010",
            exact_spans=[f"Span {index}."],
            source_title=f"Source {index}",
        )
        for index in range(1, 48)
    ]
    responses = iter([{
        "content": json.dumps({
            "section_text": (
                "Live compound draft [E21, E46, E47]. " * 120
            ).strip()
        }),
    }])
    calls: list[str] = []

    def fake(agent_name: str, messages: list[dict], **_kwargs):
        calls.append(agent_name)
        return next(responses)

    monkeypatch.setattr(module, "call_qwen_chat", fake)
    draft = SectionWriter(real_llm=True).write(packet)

    assert calls == ["SectionWriterAgent"]
    assert "[REF:paper-21]" in draft.english_text
    assert "[REF:paper-46]" in draft.english_text
    assert "[REF:paper-47]" in draft.english_text
    assert "[E21, E46, E47]" not in draft.english_text
    assert "[E21" not in draft.english_text
    assert "[E46" not in draft.english_text
    assert "[E47" not in draft.english_text
    resolution = next(
        entry for entry in draft.revision_history
        if entry.get("stage") == "evidence_handle_resolution"
    )
    assert resolution["before_handles"] == ["E21", "E46", "E47"]
    assert resolution["resolved_handles"] == ["E21", "E46", "E47"]


def test_compact_whole_section_mixed_bracket_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import optomind_research.review_writer as module

    packet = _make_packet(word_budget=1400, paragraph_count=1)
    packet.section_contract["writing_mode"] = module.COMPACT_EVIDENCE_HANDLES_MODE
    packet.evidence_packets = [
        EvidencePacket(
            claim_id="C1",
            paper_id="5b2f9e1c",
            chunk_id="5b2f9e1c:0010",
            exact_spans=["Verified span."],
        )
    ]
    responses = iter([
        {"content": json.dumps({"section_text": "Bad [E01, paper-x]."})},
        {"content": json.dumps({"paragraph_text": "Bad [E01, paper-x]."})},
        {"content": json.dumps({"paragraph_text": "Bad [E01, paper-x]."})},
        {
            "content": json.dumps({
                "section_text": (
                    "Repair text is longer and fully grounded [REF:E01]."
                )
            }),
        },
    ])
    calls: list[str] = []

    def fake(agent_name: str, messages: list[dict], **_kwargs):
        calls.append(agent_name)
        return next(responses)

    monkeypatch.setattr(module, "call_qwen_chat", fake)
    draft = SectionWriter(real_llm=True).write(packet)

    assert calls == [
        "SectionWriterAgent",
        "SectionWriterParagraphRecoveryAgent",
        "SectionWriterParagraphRecoveryRetryAgent",
        "SectionWriterContractRepairAgent",
    ]
    assert "[REF:5b2f9e1c]" in draft.english_text
    assert "[E01, paper-x]" not in draft.english_text
    diagnostic = next(
        entry for entry in draft.revision_history
        if entry.get("stage") == "section_initial_contract_diagnostic"
    )
    assert any(
        "unresolved_handle_brackets=['[E01, paper-x]']" in failure
        for failure in diagnostic["hard_failures"]
    )


def test_section_writer_prompt_has_no_fixed_1200_contract() -> None:
    import optomind_research.review_writer as module

    prompt = module._read_prompt(module.SECTION_WRITER_PROMPT)
    assert "1200" not in prompt
    assert "whitespace-delimited" in prompt
    assert "requested_final_word_count" in prompt
    assert "recommended_words_by_paragraph" in prompt


def test_length_plan_truthful_and_non_mutating() -> None:
    import optomind_research.review_writer as module

    packet = _make_packet(word_budget=1400, paragraph_count=4)
    contract_before = copy.deepcopy(packet.section_contract)
    plan = module._build_length_plan(
        packet,
        word_budget=1400,
        expected_paragraphs=4,
        reference_target_words=1120,
        hard_max_words=1610,
    )

    assert plan["unit"] == "whitespace-delimited English prose words"
    assert plan["requested_final_word_count"] == 1400
    assert plan["soft_reference_word_count"] == 1120
    assert plan["hard_maximum_word_count"] == 1610
    assert plan["expected_paragraph_count"] == 4
    assert len(plan["recommended_words_by_paragraph"]) == 4
    assert sum(
        row["recommended_words"]
        for row in plan["recommended_words_by_paragraph"]
    ) == 1400
    assert packet.section_contract == contract_before


@pytest.mark.parametrize("compact", [False, True])
def test_length_plan_in_model_payload_legacy_and_compact(
    monkeypatch: pytest.MonkeyPatch,
    compact: bool,
) -> None:
    import optomind_research.review_writer as module

    packet = _make_packet(word_budget=1400, paragraph_count=1)
    if compact:
        packet.section_contract["writing_mode"] = (
            module.COMPACT_EVIDENCE_HANDLES_MODE
        )
    contract_before = copy.deepcopy(packet.section_contract)
    captured: dict[str, dict] = {}

    def fake(agent_name: str, messages: list[dict], **_kwargs):
        captured["payload"] = json.loads(messages[-1]["content"])
        marker = "[E01]" if compact else "[REF:paper1]"
        return {"content": json.dumps({
            "section_text": ("word " * 600) + marker + "."
        })}

    monkeypatch.setattr(module, "call_qwen_chat", fake)
    SectionWriter(real_llm=True).write(packet)

    plan = captured["payload"]["length_plan"]
    assert plan["unit"] == "whitespace-delimited English prose words"
    assert plan["requested_final_word_count"] == 1400
    assert plan["hard_maximum_word_count"] == 1609
    assert sum(
        row["recommended_words"]
        for row in plan["recommended_words_by_paragraph"]
    ) == 1400
    assert packet.section_contract == contract_before


def test_compact_mode_full_hard_failure_triggers_routed_paragraph_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import optomind_research.review_writer as module

    packet = _make_packet(word_budget=1400, paragraph_count=4)
    packet.section_contract["writing_mode"] = module.COMPACT_EVIDENCE_HANDLES_MODE
    packet.claims = [
        {
            "claim_id": f"C{index:02d}",
            "statement": f"Claim {index} statement about mechanism {index}.",
            "statement_for_writing": (
                f"Claim {index} is supported by evidence {index}."
            ),
            "role": "core",
            "caveats": [f"Caveat {index}."],
            "supported_components": [f"component {index}"],
            "missing_evidence_components": [],
            "writing_permission": "factual_assertion",
            "claim_state": "planned",
            "evidence_binding_status": "direct",
        }
        for index in range(1, 42)
    ]
    packet.section_contract["paragraph_functions"] = [
        {
            "paragraph_index": paragraph + 1,
            "title": f"Paragraph {paragraph + 1}",
            "purpose": f"Purpose {paragraph + 1}",
            "claim_ids": [
                f"C{index:02d}"
                for index in range(paragraph * 4 + 1, paragraph * 4 + 5)
            ],
            "transition_logic": f"Transition {paragraph + 1}",
        }
        for paragraph in range(4)
    ]
    packet.section_contract["argument_sequence"] = [
        {
            "step": f"Step {paragraph + 1}",
            "claim_ids": [f"C{paragraph + 1:02d}"],
        }
        for paragraph in range(4)
    ]
    evidence = []
    for index in range(1, 42):
        paper_id = f"{index:02x}" * 20
        evidence.append(EvidencePacket(
            claim_id=f"C{index:02d}",
            paper_id=paper_id,
            chunk_id=f"s2-body:{paper_id}:0010",
            exact_spans=[f"Evidence text for claim {index}."],
            source_title=f"Source {index}",
        ))
        if index % 3 == 0:
            evidence.append(EvidencePacket(
                claim_id=f"C{index:02d}",
                paper_id=paper_id,
                chunk_id=f"s2-body:{paper_id}:0015",
                exact_spans=[f"Second evidence for claim {index}."],
                source_title=f"Source {index}",
            ))
    packet.evidence_packets = evidence

    full_payload, _full_registry = module._compact_packet_payload(packet)
    full_payload_chars = len(json.dumps(full_payload))

    calls: list[str] = []
    paragraph_payloads: list[dict] = []
    full_call_payload: dict = {}

    def fake(agent_name: str, messages: list[dict], **_kwargs):
        calls.append(agent_name)
        payload = json.loads(messages[-1]["content"])
        if agent_name == "SectionWriterAgent":
            full_call_payload["payload"] = payload
            return {"content": json.dumps({"section_text": "Bad [REF:E99]."})}
        paragraph_payloads.append(payload)
        return {"content": json.dumps({
            "paragraph_text": (
                f"Paragraph {len(paragraph_payloads)} develops the mechanism "
                "[REF:E01]."
            )
        })}

    monkeypatch.setattr(module, "call_qwen_chat", fake)
    draft = SectionWriter(real_llm=True).write(packet)

    assert calls == [
        "SectionWriterAgent",
        "SectionWriterParagraphRecoveryAgent",
        "SectionWriterParagraphRecoveryAgent",
        "SectionWriterParagraphRecoveryAgent",
        "SectionWriterParagraphRecoveryAgent",
    ]
    assert draft.status == "draft"
    recovery = next(
        entry for entry in draft.revision_history
        if entry.get("stage") == "compact_evidence_handle_writing_retry"
    )
    assert recovery["accepted"] is True
    assert recovery["total_paragraph_calls"] == 4
    assert len(recovery["per_paragraph_input_char_counts"]) == 4
    assert recovery["unassigned_claim_ids"] == []
    routing = recovery["routing_diagnostics"]
    routed_ids = {
        claim_id
        for paragraph in routing["primary_claim_ids_by_paragraph"]
        for claim_id in paragraph
    }
    assert routed_ids == {f"C{index:02d}" for index in range(1, 42)}
    assert len(routed_ids) == 41

    serialized_full = json.dumps(full_call_payload["payload"])
    assert "chunk_id" not in serialized_full
    assert "paper_id" not in serialized_full
    assert "evidence_provenance" not in serialized_full
    assert len(paragraph_payloads) == 4
    for payload in paragraph_payloads:
        serialized = json.dumps(payload)
        assert serialized.count("\"chunk_id\"") == 0
        assert serialized.count("\"paper_id\"") == 0
        assert "evidence_provenance" not in serialized
        assert payload["allowed_reference_markers"]
        assert all(
            str(marker).startswith("E")
            for marker in payload["allowed_reference_markers"]
        )
        assert len(serialized) < full_payload_chars
        assert len(serialized) < full_payload_chars * 0.6
    assert max(recovery["per_paragraph_input_char_counts"]) < full_payload_chars
    assert (
        max(recovery["per_paragraph_input_char_counts"])
        < full_payload_chars * 0.6
    )
