from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from optomind_research.full_review_evidence import (
    build_evidence_portfolios,
    plan_visual_evidence,
    resolve_evidence_gaps,
    resolve_kb_sqlite,
)
from optomind_research.full_review_production import (
    apply_feedback_revision,
    audit_citations,
    finalize_review,
    run_global_review,
    run_peer_review_panel,
    run_supervisor_review,
)
from optomind_research.review_writer import (
    EvidencePacket,
    FinalTranslator,
    OverclaimAuditor,
    SectionDraft,
    SectionMaterialMapper,
    SectionMaterialPacket,
    SectionWriter,
)


def test_final_translator_chunks_long_sections_and_preserves_citations(monkeypatch):
    calls: list[str] = []

    def fake_call(_agent_name, messages, **_kwargs):
        source = messages[-1]["content"].split("\n\n", 1)[1]
        calls.append(source)
        return {"content": "\u8bd1\u6587\u5185\u5bb9\u3002" + source}

    monkeypatch.setattr("optomind_research.review_writer.call_qwen_chat", fake_call)
    paragraph = (
        "A multilayer filter must preserve angular selectivity while controlling dispersion [1]. "
        * 40
    )
    source = "\n\n".join([paragraph, paragraph.replace("[1]", "[2, 3]")])
    draft = SectionDraft("S01", english_text=source)
    translated = FinalTranslator(real_llm=True).translate(draft)
    assert len(calls) > 1
    assert translated.chinese_text.count("[1]") == source.count("[1]")
    assert translated.chinese_text.count("[2, 3]") == source.count("[2, 3]")


def test_final_translator_rejects_a_chunk_that_drops_a_citation(monkeypatch):
    monkeypatch.setattr(
        "optomind_research.review_writer.call_qwen_chat",
        lambda *_args, **_kwargs: {"content": "\u8fd9\u662f\u4e22\u5931\u5f15\u6587\u7684\u8bd1\u6587\u3002"},
    )
    draft = SectionDraft("S01", english_text="The measured response is reproducible [7].")
    import pytest
    with pytest.raises(RuntimeError, match="citation"):
        FinalTranslator(real_llm=True).translate(draft)


def _blueprint() -> dict:
    return {
        "sections": [{
            "section_id": "S01",
            "section_title": "Mechanisms and limits",
            "argument_role": "Explain the governing mechanism and its boundary conditions.",
            "planned_thesis": {
                "text": "The optical mechanism must be interpreted together with its boundary conditions."
            },
            "key_questions": ["Which mechanism controls the observed response?"],
        }]
    }


def _contracts() -> list[dict]:
    return [{
        "section_id": "S01",
        "section_title": "Mechanisms and limits",
        "central_thesis": "The optical mechanism must be interpreted together with its boundary conditions.",
        "required_evidence_roles": ["A physical mechanism analysis"],
        "expected_visual_roles": ["A mechanism schematic"],
        "forbidden_overclaims": ["Do not state unsupported numerical limits."],
    }]


def _mock_draft_bundle() -> dict:
    return {
        "blueprint": _blueprint(),
        "section_drafts": [{
            "section_id": "S01",
            "english_text": "The mechanism remains an open question.",
            "citation_map": {},
            "overclaim_flags": [],
            "contradiction_notes": [],
            "figure_placements": [],
            "status": "draft",
            "uncited_load_bearing": [],
            "revision_history": [],
        }],
        "material_packets": [{
            "section_id": "S01",
            "section_contract": {},
            "claims": [{
                "claim_id": "S01-C01",
                "statement": "The mechanism remains unresolved.",
                "load_bearing": True,
                "evidence_requirement": "factual",
            }],
            "evidence_packets": [],
            "contradictions": [],
            "open_questions": [],
            "transition_contract": {},
            "uncited_load_bearing_claim_ids": ["S01-C01"],
            "visual_evidence": [],
        }],
        "full_review_english": "The mechanism remains an open question.",
    }


def test_section_writer_retries_near_valid_contract_repair_with_unknown_marker(monkeypatch):
    import optomind_research.review_writer as module

    def prose(word: str, count: int, marker: str) -> str:
        first = " ".join([word] * (count // 2)) + f" [REF:{marker}]."
        second = " ".join([word] * (count - count // 2)) + "."
        return first + "\n\n" + second

    responses = iter([
        {"section_text": prose("short", 30, "paper1:C1")},
        {"paragraph_text": prose("first", 45, "paper1:C1")},
        {"paragraph_text": prose("unknown", 45, "unknown-paper:C1")},
        {"paragraph_text": prose("unknown", 45, "unknown-paper:C1")},
        {"section_text": prose("expanded", 90, "unknown-paper:C1")},
        {"section_text": prose("corrected", 90, "paper1:C1")},
    ])

    retry_payloads: list[dict] = []

    def fake_chat(agent_name, *args, **kwargs):
        if agent_name == "SectionWriterContractSafetyRetryAgent":
            retry_payloads.append(json.loads(args[0][-1]["content"]))
        return {"content": json.dumps(next(responses))}

    monkeypatch.setattr(module, "call_qwen_chat", fake_chat)
    packet = SectionMaterialPacket(
        section_id="S01",
        section_contract={
            "title": "Mechanism",
            "word_budget": 100,
            "paragraph_functions": ["Explain.", "Synthesize."],
        },
        claims=[{
            "claim_id": "C1",
            "statement": "A supported mechanism.",
            "statement_for_writing": "A supported mechanism.",
            "writing_permission": "factual_assertion",
        }],
        evidence_packets=[EvidencePacket(
            claim_id="C1",
            paper_id="paper1",
            chunk_id="chunk1",
            exact_spans=["A supported mechanism is reported."],
        )],
    )
    draft = SectionWriter(real_llm=True).write(packet)

    assert "[REF:paper1:C1]" in draft.english_text
    assert "unknown-paper" not in draft.english_text
    recovery = next(
        row for row in draft.revision_history
        if row["stage"] == "section_paragraph_recovery"
    )
    assert recovery["accepted"] is False
    assert recovery["failure_reason"] == "paragraph_1_hard_failure_after_retry"
    assert draft.revision_history[-1]["stage"] == "section_contract_repair_safety_retry"
    assert draft.revision_history[-1]["accepted"] is True
    assert retry_payloads[0]["contract_failures"]["reference_target_word_count"] == 80
    assert retry_payloads[0]["contract_failures"]["maximum_acceptable_word_count"] == 114
    assert draft.revision_history[-1]["reference_target_word_count"] == 80
    assert draft.revision_history[-1]["hard_max_word_count"] == 114


def test_material_mapper_preserves_verified_support_ids_when_relations_are_shorter(monkeypatch):
    mapper = SectionMaterialMapper()
    records = {
        f"chunk-{index}": {
            "paper_id": f"paper-{index}",
            "title": f"Paper {index}",
            "text": f"Verified evidence passage {index}.",
            "evidence_level": "fulltext",
            "source_kind": "fulltext",
            "scope_fit": "in_domain",
            "retrieval_role": "evidence_candidate",
            "factual_support_allowed": "true",
        }
        for index in range(1, 5)
    }
    monkeypatch.setattr(mapper, "_load_chunk_records", lambda chunk_ids: records)
    section = {
        "section_id": "S01",
        "title": "Mechanism",
        "claims": [{
            "claim_id": "C1",
            "statement": "A mechanism has four independently verified components.",
            "claim_state": "grounded",
            "evidence_requirement": "factual",
            "evidence_binding_status": "direct",
            "supporting_text_chunk_ids": [
                "chunk-1", "chunk-2", "chunk-3", "chunk-4"
            ],
            "evidence_relations": [{
                "chunk_id": "chunk-1",
                "paper_id": "paper-1",
                "relation_type": "direct_support",
                "exact_span": "Verified evidence passage 1.",
            }],
        }],
    }

    packet = mapper.map(section)
    assert {ep.chunk_id for ep in packet.evidence_packets} == {
        "chunk-1", "chunk-2", "chunk-3", "chunk-4"
    }


def test_overclaim_auditor_receives_citation_rejections_and_has_full_revision_budget(monkeypatch):
    import optomind_research.review_writer as module

    source_sentence = "The unsupported parameter is universally fixed at 42 units."
    filler = " ".join(["context"] * 90)
    original = f"{source_sentence}\n\n{filler}."
    revised = (
        "Whether the parameter has a universal value remains unresolved.\n\n"
        + filler
        + "."
    )
    captured: dict = {}

    def fake_chat(*args, **kwargs):
        captured["kwargs"] = kwargs
        captured["payload"] = json.loads(args[1][1]["content"])
        return {
            "content": json.dumps({
                "overclaim_flags": [{
                    "sentence_fragment": source_sentence,
                    "overclaim_type": "unsupported_number",
                    "issue": "No verified packet supports the numerical value.",
                    "revised_sentence": "Whether the parameter has a universal value remains unresolved.",
                }],
                "revised_text": revised,
            })
        }

    monkeypatch.setattr(module, "call_qwen_chat", fake_chat)
    draft = SectionDraft(
        section_id="S01",
        english_text=original,
        overclaim_flags=[{
            "overclaim_type": "uncited_after_entailment_rejection",
            "sentence_fragment": source_sentence,
            "issue": "Citation entailment failed.",
            "revised_sentence": "",
        }],
    )
    result = OverclaimAuditor(real_llm=True).audit(
        draft, SectionMaterialPacket(section_id="S01")
    )

    assert "remains unresolved" in result.english_text
    assert captured["kwargs"]["stream"] is True
    assert captured["kwargs"]["max_tokens"] >= 3200
    assert captured["payload"]["prior_overclaim_flags"][0]["overclaim_type"] == (
        "uncited_after_entailment_rejection"
    )
    prior = next(
        row for row in result.overclaim_flags
        if row.get("overclaim_type") == "uncited_after_entailment_rejection"
    )
    assert prior["resolved"] is True
    assert prior["resolution_status"] == "reworded_or_removed_after_audit"


def test_prevalence_backstop_is_domain_generic(monkeypatch):
    import optomind_research.review_writer as module

    monkeypatch.setattr(
        module,
        "call_qwen_chat",
        lambda *args, **kwargs: {
            "content": json.dumps({"overclaim_flags": [], "revised_text": ""})
        },
    )
    draft = SectionDraft(
        section_id="S01",
        english_text="Most nonlinear microscopy studies establish this relationship.",
    )
    result = OverclaimAuditor(real_llm=True).audit(
        draft, SectionMaterialPacket(section_id="S01")
    )

    assert "many nonlinear microscopy studies represented in the available evidence" in result.english_text
    assert any(
        row.get("overclaim_type") == "unsupported_prevalence"
        for row in result.overclaim_flags
    )


def test_prevalence_backstop_converts_invented_framework_attribution(monkeypatch):
    import optomind_research.review_writer as module

    monkeypatch.setattr(
        module,
        "call_qwen_chat",
        lambda *args, **kwargs: {
            "content": json.dumps({"overclaim_flags": [], "revised_text": ""})
        },
    )
    draft = SectionDraft(
        section_id="S01",
        english_text=(
            "Recent conceptual frameworks often characterize the challenge as "
            "a coupled optimization problem."
        ),
    )
    result = OverclaimAuditor(real_llm=True).audit(
        draft, SectionMaterialPacket(section_id="S01")
    )

    assert result.english_text.startswith("This review frames the challenge")
    assert any(
        row.get("overclaim_type") == "unsupported_prevalence"
        for row in result.overclaim_flags
    )


def test_prevalence_backstop_does_not_replace_absolute_with_uncited_few(monkeypatch):
    import optomind_research.review_writer as module

    monkeypatch.setattr(
        module,
        "call_qwen_chat",
        lambda *args, **kwargs: {
            "content": json.dumps({"overclaim_flags": [], "revised_text": ""})
        },
    )
    draft = SectionDraft(
        section_id="S01",
        english_text=(
            "The evidence suggests that few materials or structural approaches can "
            "satisfy every constraint."
        ),
    )
    result = OverclaimAuditor(real_llm=True).audit(
        draft, SectionMaterialPacket(section_id="S01")
    )
    assert "it remains unclear how many materials or structural approaches can" in result.english_text
    assert any(
        row.get("resolution_status") == "rewritten_as_bounded_uncertainty"
        for row in result.overclaim_flags
    )


def test_supervisor_does_not_transport_truncate_normal_full_section(monkeypatch):
    from optomind_research.supervisor import Supervisor

    supervisor = Supervisor(real_llm=True)
    captured: dict = {}

    def capture(payload):
        captured.update(payload)
        return []

    monkeypatch.setattr(supervisor, "_call_llm_for_suggestions", capture)
    text = "A" * 9000
    supervisor.review_section_draft(
        "S01",
        text,
        [],
        evaluation_context={"mode": "bounded_section_acceptance"},
    )

    assert captured["draft_text"] == text
    assert captured["draft_is_complete"] is True
    assert captured["evaluation_context"]["mode"] == "bounded_section_acceptance"


def test_supervisor_invalid_empty_response_is_visible_process_error(monkeypatch):
    import optomind_research.supervisor as module

    monkeypatch.setattr(
        module,
        "call_qwen_chat",
        lambda *_args, **_kwargs: {"content": ""},
    )
    supervisor = module.Supervisor(real_llm=True)
    suggestions = supervisor.review_section_draft("S01", "Draft text.", [])
    assert len(suggestions) == 1
    assert suggestions[0].issue_type == "supervisor_error"
    summary = supervisor.status_summary()
    assert summary["process_error_count"] == 1
    assert summary["review_complete"] is False


def test_resolve_kb_sqlite_accepts_direct_file(tmp_path: Path):
    db = tmp_path / "review_knowledge_base.sqlite"
    db.touch()
    assert resolve_kb_sqlite(db) == db


def test_mock_s10_never_invents_chunk_ids():
    bundle = build_evidence_portfolios(
        _blueprint(), _contracts(), kb_path=None, real_llm=False
    )
    claims = bundle["blueprint"]["sections"][0]["claims"]
    assert claims
    assert all(not claim["supporting_text_chunk_ids"] for claim in claims)
    assert claims[0]["evidence_requirement"] == "factual"
    assert claims[0]["claim_state"] == "open_question"


def test_mock_s11_reports_unresolved_factual_backbone():
    evidence = build_evidence_portfolios(
        _blueprint(), _contracts(), kb_path=None, real_llm=False
    )
    result = resolve_evidence_gaps(evidence, kb_path=None, real_llm=False)
    assert result["stop_reason"] == "mock_evidence_not_evaluated"
    assert result["unresolved_load_bearing_claim_ids"] == ["S01-C01"]


def test_text_only_visual_candidates_are_not_promoted():
    evidence = build_evidence_portfolios(
        _blueprint(), _contracts(), kb_path=None, real_llm=False
    )
    gaps = resolve_evidence_gaps(evidence, kb_path=None, real_llm=False)
    result = plan_visual_evidence(gaps, kb_path=None, real_llm=False)
    section = result["blueprint"]["sections"][0]
    assert section["verified_visual_chunk_ids"] == []
    assert result["quality_summary"]["sections_without_verified_visual_support"] == ["S01"]
    assert result["quality_summary"]["missing_required_visual_plan_count"] == 1
    assert result["visual_gap_plan"][0]["asset_status"] == "missing_required_visual"
    assert result["visual_gap_plan"][0]["evidence_status"].startswith("not_evidence")


def test_mock_citation_audit_never_claims_formal_readiness():
    audit = audit_citations(_mock_draft_bundle(), real_llm=False)
    assert audit["formal_ready_section_count"] == 0
    assert audit["uncited_load_bearing_claim_count"] == 1


def test_supervisor_does_not_silently_accept_suggestions():
    bundle = run_supervisor_review(_mock_draft_bundle(), real_llm=False)
    assert bundle["suggestions"]
    assert all(row["status"] == "pending" for row in bundle["suggestions"])


def test_feedback_revision_is_noop_without_approved_suggestion():
    draft = _mock_draft_bundle()
    supervisor = run_supervisor_review(draft, real_llm=False)
    revised = apply_feedback_revision(draft, supervisor, real_llm=False)
    assert revised["accepted_suggestion_count"] == 0
    assert revised["stop_reason"] == "no_approved_suggestions"
    assert (
        revised["section_drafts"][0]["english_text"]
        == draft["section_drafts"][0]["english_text"]
    )


def test_safe_claim_state_update_can_sync_blueprint_and_material_packet():
    from optomind_research.full_review_production import _apply_safe_claim_updates

    canonical_claim = {
        "claim_id": "S01-C01", "statement": "An unresolved proposition.",
        "claim_state": "grounded", "evidence_requirement": "factual", "load_bearing": True,
    }
    packet_claim = dict(canonical_claim)
    update = [{
        "claim_id": "S01-C01",
        "evidence_requirement": "open_question",
        "claim_state": "open_question",
    }]
    accepted = _apply_safe_claim_updates(
        {"sections": [{"claims": [canonical_claim]}]}, update
    )
    _apply_safe_claim_updates({"sections": [{"claims": [packet_claim]}]}, accepted)
    assert canonical_claim["claim_state"] == "open_question"
    assert packet_claim["claim_state"] == "open_question"
    assert packet_claim["load_bearing"] is False


def test_mock_global_and_peer_review_do_not_imply_acceptance():
    draft = _mock_draft_bundle()
    citation = audit_citations(draft, real_llm=False)
    global_review = run_global_review(
        draft,
        charter={},
        contracts=_contracts(),
        citation_bundle=citation,
        real_llm=False,
    )
    peers = run_peer_review_panel(draft, global_review, charter={}, real_llm=False)
    assert global_review["judgment"]["formal_readiness"] == "needs_revision"
    assert peers["recommendation_distribution"]["major_revision"] == 4


def test_finalizer_builds_traceable_reference_registry_and_stays_mock(tmp_path: Path):
    db = tmp_path / "review_knowledge_base.sqlite"
    connection = sqlite3.connect(str(db))
    connection.executescript(
        """
        CREATE TABLE papers(
          paper_id TEXT PRIMARY KEY, doi TEXT, title TEXT, year INTEGER,
          venue TEXT, raw_json TEXT NOT NULL
        );
        CREATE TABLE text_chunks(chunk_id TEXT PRIMARY KEY, paper_id TEXT NOT NULL);
        """
    )
    connection.execute(
        "INSERT INTO papers VALUES(?,?,?,?,?,?)",
        ("p1", "10.1000/example", "Example optical paper", 2025, "Optics Journal", json.dumps({"authors": ["A. Author"]})),
    )
    connection.execute("INSERT INTO text_chunks VALUES(?,?)", ("c1", "p1"))
    connection.commit()
    connection.close()
    bundle = _mock_draft_bundle()
    bundle["section_drafts"][0]["english_text"] = "A supported statement [REF:p1:S01-C01]."
    bundle["section_drafts"][0]["citation_map"] = {"0": ["c1"]}
    bundle["material_packets"][0]["evidence_packets"] = [{
        "claim_id": "S01-C01",
        "paper_id": "p1",
        "chunk_id": "c1",
        "exact_spans": ["A supported statement."],
    }]
    final = finalize_review(
        bundle,
        {"judgment": {"formal_readiness": "ready"}, "critical_issue_count": 0, "high_or_critical_issue_count": 0},
        {"peer_reviews": [], "critical_issue_count": 0, "high_or_critical_issue_count": 0},
        charter={"title": "Test Review"},
        kb_path=db,
        real_llm=False,
    )
    assert final["formal_status"] == "mock_not_formal"
    assert final["quality_summary"]["reference_count"] == 1
    assert "[1]" in final["english_review"]
    assert "https://doi.org/10.1000/example" in final["english_review"]


def test_evidence_verifier_batches_multi_claim_output_without_truncation(monkeypatch):
    import optomind_research.claim_evidence_verifier as module
    from optomind_research.claim_schema import Claim

    calls: list[list[str]] = []

    def fake_call(*_args, **kwargs):
        payload = json.loads(kwargs.get("messages", _args[1])[-1]["content"])
        ids = [row["claim_id"] for row in payload["claims"]]
        calls.append(ids)
        bindings = [
            {
                "claim_id": claim_id,
                "verdict": "direct",
                "confidence": "high",
                "section_fit": "central",
                "section_fit_reason": "Directly serves the section.",
                "supporting_text_refs": ["T01"],
                "supporting_visual_refs": [],
                "supported_rewrite": "",
                "synthesis_rationale": "",
                "supported_components": [
                    {"component": "mechanism", "text_refs": ["T01"]}
                ],
                "missing_components": [],
                "reason": "The anchor states the mechanism.",
                "evidence_spans": [
                    {"text_ref": "T01", "quote": "Optical coupling controls the response.", "quote_translation": "Optical coupling controls the response."}
                ],
            }
            for claim_id in ids
        ]
        return {"content": json.dumps({"bindings": bindings}), "_llm_usage": {}}

    monkeypatch.setattr(module, "call_qwen_chat", fake_call)
    claims = [
        Claim(
            claim_id=f"S01-C{i:02d}",
            statement=f"Optical coupling controls response mode {chr(64 + i)}.",
            evidence_type="mechanism",
        )
        for i in range(1, 5)
    ]
    section = {
        "section_id": "S01",
        "title": "Mechanism",
        "argument_role": "Explain optical coupling.",
        "candidate_text_chunks": [{
            "chunk_id": "c1",
            "paper_id": "p1",
            "text_preview": "Optical coupling controls the response.",
        }],
    }
    verifier = module.ClaimEvidenceVerifier()
    result = verifier.verify_and_bind(claims, section)
    assert len(calls) == 2
    assert all(len(batch) == 2 for batch in calls)
    assert verifier.last_audit["complete"] is True
    assert verifier.last_audit["batch_count"] == 2
    assert all(claim.evidence_binding_status == "direct" for claim in result)


def test_evidence_verifier_unifies_support_refs_components_and_spans(monkeypatch):
    import optomind_research.claim_evidence_verifier as module
    from optomind_research.claim_schema import Claim

    def fake_call(*_args, **_kwargs):
        return {"content": json.dumps({"bindings": [{
            "claim_id": "S01-C01",
            "verdict": "synthesized",
            "confidence": "high",
            "section_fit": "central",
            "section_fit_reason": "All anchors serve the section.",
            "supporting_text_refs": ["T01", "T04"],
            "supporting_visual_refs": [],
            "supported_rewrite": "",
            "synthesis_rationale": "The anchors establish complementary components.",
            "supported_components": [
                {"component": "component two", "text_refs": ["T02"]}
            ],
            "missing_components": [],
            "reason": "Three supplied anchors jointly support the claim.",
            "evidence_spans": [
                {
                    "text_ref": "T03",
                    "quote": "The third component is observed.",
                    "quote_translation": "The third component is observed.",
                    "scope_fit": "in_domain",
                    "retrieval_role": "evidence_candidate",
                },
                {
                    "text_ref": "T04",
                    "quote": "An adjacent-domain analogy is reported.",
                    "quote_translation": "An adjacent-domain analogy is reported.",
                    "scope_fit": "cross_domain_analogy",
                    "retrieval_role": "method_transfer",
                },
            ],
        }]})}

    monkeypatch.setattr(module, "call_qwen_chat", fake_call)
    chunks = [
        {
            "chunk_id": f"c{index}",
            "paper_id": f"p{index}",
            "text_preview": text,
        }
        for index, text in enumerate((
            "The first component is observed.",
            "The second component is observed.",
            "The third component is observed.",
            "An adjacent-domain analogy is reported.",
        ), start=1)
    ]
    claim = Claim(
        claim_id="S01-C01",
        statement="Three components jointly define the response.",
        evidence_type="mechanism",
    )
    verified = module.ClaimEvidenceVerifier().verify_and_bind(
        [claim], {"section_id": "S01", "candidate_text_chunks": chunks}
    )[0]
    # Only references with a locally verified verbatim span may enter the
    # factual writing support list. T01/T02 remain model-selected leads.
    assert verified.supporting_text_chunk_ids == ["c3"]
    assert verified.evidence_spans[-1]["chunk_id"] == "c4"
    assert verified.evidence_spans[-1]["retrieval_role"] == "method_transfer"


def test_evidence_verifier_downgrades_unanchored_numeric_precision(monkeypatch):
    import optomind_research.claim_evidence_verifier as module
    from optomind_research.claim_schema import Claim

    def fake_call(*_args, **_kwargs):
        return {
            "content": json.dumps({
                "bindings": [{
                    "claim_id": "S01-C01",
                    "verdict": "direct",
                    "confidence": "high",
                    "section_fit": "central",
                    "section_fit_reason": "Central mechanism.",
                    "supporting_text_refs": ["T01"],
                    "supporting_visual_refs": [],
                    "supported_rewrite": "High solar reflectance is required.",
                    "synthesis_rationale": "",
                    "supported_components": [{
                        "component": "high solar reflectance",
                        "text_refs": ["T01"],
                    }],
                    "missing_components": [],
                    "reason": "The anchor supports the qualitative requirement.",
                    "evidence_spans": [{
                        "text_ref": "T01",
                        "quote": "High solar reflectance is required.",
                        "quote_translation": "High solar reflectance is required.",
                    }],
                }]
            }),
            "_llm_usage": {},
        }

    monkeypatch.setattr(module, "call_qwen_chat", fake_call)
    claim = Claim(
        claim_id="S01-C01",
        statement="The design must suppress 1000 W/m2 of solar irradiance.",
        evidence_type="measurement",
    )
    section = {
        "section_id": "S01",
        "candidate_text_chunks": [{
            "chunk_id": "c1",
            "paper_id": "p1",
            "text_preview": "High solar reflectance is required.",
        }],
    }
    verified = module.ClaimEvidenceVerifier().verify_and_bind([claim], section)[0]
    assert verified.evidence_binding_status == "partial"
    assert any("unverified_quantitative_literals:1000" == flag for flag in verified.critic_flags)
    assert any("1000" in item for item in verified.missing_evidence_components)


def test_evidence_reverification_clears_stale_binding_flags(monkeypatch):
    import optomind_research.claim_evidence_verifier as module
    from optomind_research.claim_schema import Claim

    monkeypatch.setattr(module, "call_qwen_chat", lambda *_args, **_kwargs: {
        "content": json.dumps({"bindings": [{
            "claim_id": "S01-C01",
            "verdict": "direct",
            "confidence": "high",
            "section_fit": "central",
            "section_fit_reason": "The narrowed claim is explicit in the anchor.",
            "supporting_text_refs": ["T01"],
            "supporting_visual_refs": [],
            "supported_rewrite": "",
            "synthesis_rationale": "",
            "supported_components": [{
                "component": "qualitative requirement",
                "text_refs": ["T01"],
            }],
            "missing_components": [],
            "reason": "The supplied anchor states the narrowed claim.",
            "evidence_spans": [{
                "text_ref": "T01",
                "quote": "High solar reflectance is required.",
                "quote_translation": "High solar reflectance is required.",
            }],
        }]})
    })
    claim = Claim(
        claim_id="S01-C01",
        statement="High solar reflectance is required.",
        evidence_type="mechanism",
        critic_flags=[
            "unverified_quantitative_literals:100,1000",
            "evidence_binding_partial",
            "evidence_type_reclassified:measurement->mechanism",
        ],
    )
    verified = module.ClaimEvidenceVerifier().verify_and_bind(
        [claim],
        {
            "section_id": "S01",
            "candidate_text_chunks": [{
                "chunk_id": "c1",
                "paper_id": "p1",
                "text_preview": "High solar reflectance is required.",
            }],
        },
    )[0]

    assert verified.evidence_binding_status == "direct"
    assert not any(
        flag.startswith("unverified_quantitative_literals:")
        or flag == "evidence_binding_partial"
        for flag in verified.critic_flags
    )
    assert "evidence_type_reclassified:measurement->mechanism" in verified.critic_flags


def test_material_mapper_prefers_supported_rewrite_and_labels_review_synthesis(tmp_path: Path):
    from optomind_research.review_writer import SectionMaterialMapper

    db = tmp_path / "kb.sqlite"
    connection = sqlite3.connect(str(db))
    connection.execute("CREATE TABLE text_chunks(chunk_id TEXT, paper_id TEXT, text TEXT)")
    connection.execute("INSERT INTO text_chunks VALUES('c1','p1','Component evidence.')")
    connection.commit()
    connection.close()
    packet = SectionMaterialMapper(db).map({
        "section_id": "S01",
        "title": "Mechanism framing",
        "claims": [{
            "claim_id": "S01-C01",
            "statement": "This is the field's universally accepted organizing principle.",
            "supported_rewrite": "The evidence can be organized around this principle.",
            "evidence_binding_status": "partial",
            "claim_state": "partially_grounded",
            "section_fit": "central",
            "load_bearing": True,
            "supporting_text_chunk_ids": ["c1"],
            "missing_evidence_components": [
                "The framing is a synthesis not explicitly stated in a source."
            ],
        }],
    })
    claim = packet.claims[0]
    assert claim["statement_for_writing"] == "The evidence can be organized around this principle."
    assert claim["writing_permission"] == "interpretive_synthesis"


def test_material_mapper_preserves_legacy_verified_span_and_component_ids(tmp_path: Path):
    db = tmp_path / "kb.sqlite"
    connection = sqlite3.connect(str(db))
    connection.execute("CREATE TABLE text_chunks(chunk_id TEXT, paper_id TEXT, text TEXT)")
    connection.executemany(
        "INSERT INTO text_chunks VALUES(?,?,?)",
        [
            ("c1", "p1", "Primary support."),
            ("c2", "p2", "A broader database passage."),
            ("c3", "p3", "Component support."),
        ],
    )
    connection.commit()
    connection.close()
    packet = SectionMaterialMapper(db).map({
        "section_id": "S01",
        "claims": [{
            "claim_id": "S01-C01",
            "statement": "A bounded factual claim.",
            "evidence_binding_status": "synthesized",
            "evidence_requirement": "factual",
            "claim_state": "grounded",
            "supporting_text_chunk_ids": ["c1"],
            "evidence_spans": [{
                "chunk_id": "c2",
                "quote": "Exact verified support.",
                "quote_translation": "Exact verified support.",
            }],
            "evidence_component_map": [{
                "component": "component three",
                "chunk_ids": ["c3"],
            }],
        }],
    })
    by_id = {item.chunk_id: item for item in packet.evidence_packets}
    assert set(by_id) == {"c1", "c2", "c3"}
    assert by_id["c2"].exact_spans == ["Exact verified support."]
    assert by_id["c2"].retrieval_role == "evidence_candidate"
    assert by_id["c2"].scope_fit == "in_domain"


def test_material_mapper_recovers_transport_truncated_verified_quote(tmp_path: Path):
    db = tmp_path / "kb.sqlite"
    connection = sqlite3.connect(str(db))
    connection.execute("CREATE TABLE text_chunks(chunk_id TEXT, paper_id TEXT, text TEXT)")
    connection.execute(
        "INSERT INTO text_chunks VALUES(?,?,?)",
        ("c1", "p1", "The complete source sentence explains the latent heat balance."),
    )
    connection.commit()
    connection.close()
    packet = SectionMaterialMapper(db).map({
        "section_id": "S01",
        "claims": [{
            "claim_id": "S01-C01",
            "statement": "A bounded factual claim.",
            "evidence_binding_status": "direct",
            "evidence_requirement": "factual",
            "claim_state": "grounded",
            "supporting_text_chunk_ids": ["c1"],
            "evidence_spans": [{
                "chunk_id": "c1",
                "quote": "The complete source sentence explains the latent he",
            }],
        }],
    })
    assert packet.evidence_packets[0].exact_spans == [
        "The complete source sentence explains the latent heat balance."
    ]


def test_citation_binder_replaces_stale_entailment_flags():
    import optomind_research.review_writer as module

    draft = module.SectionDraft(
        section_id="S01",
        english_text="A bounded sentence.",
        overclaim_flags=[
            {"overclaim_type": "citation_entailment_failure", "issue": "old packet"},
            {"overclaim_type": "uncited_after_entailment_rejection", "issue": "old packet"},
            {"overclaim_type": "overgeneralization", "issue": "keep this"},
        ],
    )
    packet = module.SectionMaterialPacket(section_id="S01")
    bound = module.CitationBinder(real_llm=False).bind(draft, packet)
    assert [row["overclaim_type"] for row in bound.overclaim_flags] == [
        "overgeneralization"
    ]


def test_citation_binder_never_launders_an_uncited_sentence(monkeypatch):
    import optomind_research.review_writer as module

    def forbidden_call(*_args, **_kwargs):
        raise AssertionError("An uncited sentence must not trigger citation replacement.")

    monkeypatch.setattr(module, "call_qwen_chat", forbidden_call)
    packet = module.SectionMaterialPacket(
        section_id="S01",
        claims=[{"claim_id": "S01-C01", "statement": "A different claim."}],
        evidence_packets=[module.EvidencePacket(
            claim_id="S01-C01",
            paper_id="p1",
            chunk_id="c1",
            exact_spans=["Broadly similar optical terminology appears here."],
        )],
    )
    draft = module.CitationBinder(real_llm=True).bind(
        module.SectionDraft(
            section_id="S01",
            english_text="An uncited synthesis should remain visibly uncited.",
        ),
        packet,
    )
    assert draft.citation_map == {}
    assert "[REF:" not in draft.english_text


def test_citation_marker_is_bound_to_exact_claim_within_same_paper(monkeypatch):
    import optomind_research.review_writer as module

    judged_chunk_ids: list[str] = []

    def fake_call(*args, **kwargs):
        messages = kwargs.get("messages", args[1] if len(args) > 1 else [])
        payload = json.loads(messages[-1]["content"])
        judged_chunk_ids.append(payload["evidence"]["chunk_id"])
        return {"content": json.dumps({
            "supported": True,
            "support_type": "partial",
            "confidence": "high",
            "reason": "The selected passage supports the bounded sentence.",
        })}

    monkeypatch.setattr(module, "call_qwen_chat", fake_call)
    packet = module.SectionMaterialPacket(
        section_id="S01",
        claims=[
            {"claim_id": "S01-C01", "statement": "A bounded optical claim."},
            {"claim_id": "S01-C02", "statement": "A different claim from the same paper."},
        ],
        evidence_packets=[
            module.EvidencePacket(
                claim_id="S01-C01", paper_id="p1", chunk_id="c1",
                exact_spans=["The source gives partial support for the bounded optical claim."],
            ),
            module.EvidencePacket(
                claim_id="S01-C02", paper_id="p1", chunk_id="c2",
                exact_spans=["A bounded optical claim is fully repeated with many matching words."],
            ),
        ],
    )
    draft = module.CitationBinder(real_llm=True).bind(
        module.SectionDraft(
            section_id="S01",
            english_text="A bounded optical claim is reported [REF:p1:S01-C01].",
        ),
        packet,
    )
    assert judged_chunk_ids == ["c1"]
    assert draft.citation_map == {"0": ["c1"]}


def test_real_citation_audit_separates_citation_ready_from_missing_visual(monkeypatch):
    import optomind_research.full_review_production as module

    monkeypatch.setattr(module, "_judge_section_quality", lambda *_a, **_k: {
        "verdict": "usable",
        "unsupported_fact_detected": False,
        "scores": {"evidence_calibration": 4},
    })
    bundle = _mock_draft_bundle()
    bundle["material_packets"][0]["section_contract"] = {
        "expected_visual_arguments": ["A mechanism schematic"]
    }
    bundle["material_packets"][0]["evidence_packets"] = [{
        "claim_id": "S01-C01", "paper_id": "p1", "chunk_id": "c1",
        "exact_spans": ["The mechanism remains unresolved."],
    }]
    bundle["section_drafts"][0]["citation_map"] = {"0": ["c1"]}
    audit = module.audit_citations(bundle, real_llm=True)
    row = audit["citation_audits"][0]
    assert row["citation_ready"] is True
    assert row["required_visual_missing"] is True
    assert row["formal_ready"] is False


def test_section_quality_judge_retries_invalid_json_with_bounded_model_ladder(monkeypatch):
    import optomind_research.full_review_production as module

    calls: list[str] = []

    def fake_call(*_args, **kwargs):
        calls.append(str(kwargs.get("model_tier") or ""))
        if len(calls) == 1:
            return {"content": "not-json", "_llm_usage": {"success": True}}
        return {"content": json.dumps({
            "scores": {"argument_coherence": 4},
            "overall_score": 4,
            "verdict": "usable_with_minor_revision",
            "unsupported_fact_detected": False,
        }), "_llm_usage": {"success": True}}

    monkeypatch.setattr(module, "call_qwen_chat", fake_call)
    result = module._judge_section_quality(
        module.SectionDraft(section_id="S01", english_text="Bounded prose."),
        module.SectionMaterialPacket(section_id="S01"),
        real_llm=True,
    )
    assert calls == ["premium_model", "b_plus_model"]
    assert result["verdict"] == "usable_with_minor_revision"
    assert result["judge_process"]["attempt_count"] == 2


def test_section_quality_judge_failure_is_infrastructure_not_unsupported_fact(monkeypatch):
    import optomind_research.full_review_production as module

    monkeypatch.setattr(module, "call_qwen_chat", lambda *_a, **_k: {
        "content": "not-json",
        "_llm_usage": {"success": False, "error_type": "TimeoutError"},
    })
    result = module._judge_section_quality(
        module.SectionDraft(section_id="S01", english_text="Bounded prose."),
        module.SectionMaterialPacket(section_id="S01"),
        real_llm=True,
    )
    assert result["verdict"] == "judge_failed"
    assert result["infrastructure_failure"] is True
    assert result["unsupported_fact_detected"] is False
    assert result["judge_process"]["attempt_count"] == 3


def test_citation_audit_ignores_resolved_entailment_rejection(monkeypatch):
    import optomind_research.full_review_production as module

    monkeypatch.setattr(module, "_judge_section_quality", lambda *_a, **_k: {
        "verdict": "usable",
        "unsupported_fact_detected": False,
        "scores": {"evidence_calibration": 4},
    })
    bundle = _mock_draft_bundle()
    bundle["material_packets"][0]["evidence_packets"] = [{
        "claim_id": "S01-C01",
        "paper_id": "p1",
        "chunk_id": "c1",
        "exact_spans": ["The mechanism remains unresolved."],
    }]
    bundle["section_drafts"][0]["citation_map"] = {"0": ["c1"]}
    bundle["section_drafts"][0]["overclaim_flags"] = [{
        "overclaim_type": "uncited_after_entailment_rejection",
        "sentence_fragment": "An older unsupported sentence.",
        "resolved": True,
        "resolution_status": "reworded_or_removed_after_audit",
    }]
    audit = module.audit_citations(bundle, real_llm=True)
    row = audit["citation_audits"][0]
    assert row["uncited_after_entailment_rejection"] == []
    assert row["citation_ready"] is True


def test_compound_reference_markers_are_normalized_before_binding():
    import optomind_research.review_writer as module

    draft = module.CitationBinder(real_llm=False).bind(
        module.SectionDraft(
            section_id="S01",
            english_text="A result [REF:p1:C1; REF:p2:C2].",
        ),
        module.SectionMaterialPacket(section_id="S01"),
    )
    assert draft.english_text == "A result [REF:p1:C1] [REF:p2:C2]."


def test_section_writer_paragraph_recovery_repairs_underlength_contract(monkeypatch):
    import optomind_research.review_writer as module

    calls: list[dict] = []
    first_paragraph = (
        " ".join(["bounded"] * 44) + " [REF:p1:C1].\n\n"
    )
    second_paragraph = " ".join(["evidence"] * 44) + " [REF:p2:C2]."
    repaired = first_paragraph.strip() + "\n\n" + second_paragraph

    def fake_call(agent_name, *_args, **kwargs):
        calls.append(kwargs)
        if agent_name == "SectionWriterAgent":
            return {"content": json.dumps({"section_text": "Short [REF:p1:C1]."})}
        text = first_paragraph if len(calls) == 2 else second_paragraph
        return {"content": json.dumps({"paragraph_text": text})}

    monkeypatch.setattr(module, "call_qwen_chat", fake_call)
    packet = module.SectionMaterialPacket(
        section_id="S01",
        section_contract={
            "word_budget": 100,
            "paragraph_functions": ["Frame", "Synthesize"],
        },
        claims=[
            {"claim_id": "C1", "statement_for_writing": "A.", "writing_permission": "factual_assertion"},
            {"claim_id": "C2", "statement_for_writing": "B.", "writing_permission": "factual_assertion"},
        ],
        evidence_packets=[
            module.EvidencePacket(claim_id="C1", paper_id="p1", chunk_id="c1"),
            module.EvidencePacket(claim_id="C2", paper_id="p2", chunk_id="c2"),
        ],
    )
    draft = module.SectionWriter(real_llm=True).write(packet)
    assert len(calls) == 3
    assert all(call.get("stream") is True for call in calls)
    assert draft.revision_history[-1]["stage"] == "section_paragraph_recovery"
    assert draft.revision_history[-1]["accepted"] is True
    assert draft.revision_history[-1]["meets_80_percent_budget"] is True
    assert draft.english_text == repaired


def test_section_writer_locally_retries_hard_failed_paragraph(monkeypatch):
    import optomind_research.review_writer as module

    calls: list[dict] = []
    outputs = [
        " ".join(["short"] * 20) + " [REF:p1:C1].",
        " ".join(["partial"] * 60) + " [REF:unknown:C9].",
        " ".join(["complete"] * 89) + " [REF:p1:C1].",
    ]

    def fake_call(agent_name, *_args, **kwargs):
        calls.append(kwargs)
        if agent_name == "SectionWriterAgent":
            return {"content": json.dumps({"section_text": outputs[0]})}
        if agent_name == "SectionWriterParagraphRecoveryAgent":
            return {"content": json.dumps({"paragraph_text": outputs[1]})}
        return {"content": json.dumps({"paragraph_text": outputs[2]})}

    monkeypatch.setattr(module, "call_qwen_chat", fake_call)
    packet = module.SectionMaterialPacket(
        section_id="S01",
        section_contract={"word_budget": 100, "paragraph_functions": ["Explain"]},
        claims=[{
            "claim_id": "C1",
            "statement_for_writing": "A bounded statement.",
            "writing_permission": "factual_assertion",
        }],
        evidence_packets=[module.EvidencePacket(
            claim_id="C1", paper_id="p1", chunk_id="c1",
        )],
    )
    draft = module.SectionWriter(real_llm=True).write(packet)
    assert len(calls) == 3
    assert draft.english_text == outputs[-1]
    assert draft.revision_history[-1]["stage"] == "section_paragraph_recovery"
    assert draft.revision_history[-1]["accepted"] is True
    paragraph = draft.revision_history[-1]["per_paragraph"][0]
    assert paragraph["retry_attempted"] is True
    assert paragraph["call_attempts"] == 2
    assert paragraph["first_attempt_failures"] == [
        "unknown_reference_markers=['unknown:C9']"
    ]


def test_word_budget_contract_blocks_formal_readiness(monkeypatch):
    import optomind_research.full_review_production as module

    monkeypatch.setattr(module, "_judge_section_quality", lambda *_a, **_k: {
        "verdict": "usable",
        "unsupported_fact_detected": False,
        "scores": {"evidence_calibration": 4},
    })
    bundle = _mock_draft_bundle()
    bundle["material_packets"][0]["section_contract"] = {
        "word_budget": 1000,
        "paragraph_functions": ["Frame", "Explain", "Synthesize"],
    }
    bundle["material_packets"][0]["evidence_packets"] = [{
        "claim_id": "S01-C01", "paper_id": "p1", "chunk_id": "c1",
        "exact_spans": ["The mechanism remains unresolved."],
    }]
    bundle["section_drafts"][0]["citation_map"] = {"0": ["c1"]}
    audit = module.audit_citations(bundle, real_llm=True)
    row = audit["citation_audits"][0]
    assert row["citation_ready"] is True
    assert row["word_budget_compliant"] is False
    assert row["formal_ready"] is False


def test_post_audit_two_percent_word_drift_does_not_fail_formal_gate(monkeypatch):
    import optomind_research.full_review_production as module

    monkeypatch.setattr(module, "_judge_section_quality", lambda *_a, **_k: {
        "verdict": "usable",
        "unsupported_fact_detected": False,
        "scores": {"evidence_calibration": 4},
    })
    bundle = _mock_draft_bundle()
    bundle["material_packets"][0]["section_contract"] = {
        "word_budget": 100,
        "paragraph_functions": ["Frame", "Synthesize"],
    }
    bundle["material_packets"][0]["evidence_packets"] = [{
        "claim_id": "S01-C01", "paper_id": "p1", "chunk_id": "c1",
        "exact_spans": ["A bounded mechanism statement."],
    }]
    bundle["section_drafts"][0]["english_text"] = (
        " ".join(["bounded"] * 40) + ".\n\n" + " ".join(["analysis"] * 39) + "."
    )
    bundle["section_drafts"][0]["citation_map"] = {"0": ["c1"]}
    bundle["section_drafts"][0]["revision_history"] = [{
        "stage": "section_contract_repair",
        "accepted": True,
        "meets_80_percent_budget": True,
    }]
    audit = module.audit_citations(bundle, real_llm=True)
    row = audit["citation_audits"][0]
    assert row["word_budget_ratio"] == 0.79
    assert row["word_budget_compliant"] is True
    assert row["word_budget_compliance_mode"] == "post_audit_safety_drift_tolerance"


def test_finalizer_cannot_ignore_failed_post_revision_citation_gate(
    tmp_path: Path, monkeypatch
):
    import optomind_research.full_review_production as module

    db = tmp_path / "review_knowledge_base.sqlite"
    connection = sqlite3.connect(str(db))
    connection.executescript(
        """
        CREATE TABLE papers(
          paper_id TEXT PRIMARY KEY, doi TEXT, title TEXT, year INTEGER,
          venue TEXT, raw_json TEXT NOT NULL
        );
        CREATE TABLE text_chunks(chunk_id TEXT PRIMARY KEY, paper_id TEXT NOT NULL);
        """
    )
    connection.commit()
    connection.close()

    monkeypatch.setattr(module, "call_qwen_chat", lambda *_a, **_k: {
        "content": json.dumps({
            "title_zh": "\u6d4b\u8bd5\u7efc\u8ff0",
            "section_titles_zh": [{"section_id": "S01", "title_zh": "\u673a\u5236"}],
        })
    })

    def fake_translate(self, draft):
        draft.chinese_text = "\u8be5\u673a\u5236\u4ecd\u662f\u5f00\u653e\u95ee\u9898\u3002"
        return draft

    monkeypatch.setattr(module.FinalTranslator, "translate", fake_translate)
    bundle = _mock_draft_bundle()
    bundle["supervisor_status_summary"] = {}
    global_bundle = {
        "judgment": {"formal_readiness": "ready"},
        "critical_issue_count": 0,
        "high_or_critical_issue_count": 0,
        "post_revision_citation_audit": {
            "citation_audits": [{"section_id": "S01", "formal_ready": False}],
            "formal_ready_section_count": 0,
            "invalid_citation_count": 0,
            "uncited_load_bearing_claim_count": 1,
        },
    }
    final = module.finalize_review(
        bundle,
        global_bundle,
        {
            "peer_reviews": [{"recommendation": "accept"}],
            "critical_issue_count": 0,
            "high_or_critical_issue_count": 0,
        },
        charter={"title": "Test Review"},
        kb_path=db,
        real_llm=True,
    )
    assert final["formal_status"] == "research_draft_needs_revision"
    assert final["quality_summary"]["citation_gate_failed"] is True


def test_finalizer_distinguishes_valid_citations_from_formal_contract_failure(
    tmp_path: Path, monkeypatch
):
    import optomind_research.full_review_production as module

    db = tmp_path / "review_knowledge_base.sqlite"
    connection = sqlite3.connect(str(db))
    connection.executescript(
        """
        CREATE TABLE papers(
          paper_id TEXT PRIMARY KEY, doi TEXT, title TEXT, year INTEGER,
          venue TEXT, raw_json TEXT NOT NULL
        );
        CREATE TABLE text_chunks(chunk_id TEXT PRIMARY KEY, paper_id TEXT NOT NULL);
        """
    )
    connection.commit()
    connection.close()

    monkeypatch.setattr(module, "call_qwen_chat", lambda *_a, **_k: {
        "content": json.dumps({
            "title_zh": "\u6d4b\u8bd5\u7efc\u8ff0",
            "section_titles_zh": [{"section_id": "S01", "title_zh": "\u673a\u5236"}],
        })
    })

    def fake_translate(self, draft):
        draft.chinese_text = "\u8be5\u673a\u5236\u4ecd\u662f\u5f00\u653e\u95ee\u9898\u3002"
        return draft

    monkeypatch.setattr(module.FinalTranslator, "translate", fake_translate)
    bundle = _mock_draft_bundle()
    bundle["supervisor_status_summary"] = {}
    final = module.finalize_review(
        bundle,
        {
            "judgment": {"formal_readiness": "ready_after_minor_revision"},
            "critical_issue_count": 0,
            "high_or_critical_issue_count": 1,
            "post_revision_citation_audit": {
                "citation_audits": [{
                    "section_id": "S01",
                    "citation_ready": True,
                    "formal_ready": False,
                }],
                "citation_ready_section_count": 1,
                "formal_ready_section_count": 0,
                "invalid_citation_count": 0,
                "uncited_load_bearing_claim_count": 0,
            },
        },
        {
            "peer_reviews": [{"recommendation": "minor_revision"}],
            "critical_issue_count": 0,
            "high_or_critical_issue_count": 0,
        },
        charter={"title": "Test Review"},
        kb_path=db,
        real_llm=True,
    )

    summary = final["quality_summary"]
    assert final["formal_status"] == "research_draft_needs_revision"
    assert summary["citation_gate_failed"] is False
    assert summary["formal_contract_gate_failed"] is True
    assert summary["citation_ready_sections"] == 1
    assert summary["formal_ready_sections"] == 0


def test_partial_claim_without_rewrite_uses_only_verified_components():
    from optomind_research.review_writer import SectionMaterialMapper

    packet = SectionMaterialMapper().map({
        "section_id": "S01",
        "claims": [{
            "claim_id": "S01-C01",
            "statement": "Supported mechanism plus an unsupported universal conclusion.",
            "evidence_binding_status": "partial",
            "evidence_component_map": [
                {"component": "the measured angular response", "chunk_ids": ["c1"]},
                {"component": "the reported water-vapor sensitivity", "chunk_ids": ["c2"]},
            ],
            "missing_evidence_components": ["the universal conclusion"],
        }],
    })
    authorized = packet.claims[0]["statement_for_writing"]
    assert "measured angular response" in authorized
    assert "water-vapor sensitivity" in authorized
    assert "universal conclusion" not in authorized


def test_rejected_factual_marker_creates_explicit_uncited_fact_flag(monkeypatch):
    import optomind_research.review_writer as module

    monkeypatch.setattr(module, "call_qwen_chat", lambda *_a, **_k: {
        "content": json.dumps({
            "supported": False,
            "support_type": "unsupported",
            "confidence": "high",
            "reason": "The passage does not support the asserted predicate.",
        })
    })
    packet = module.SectionMaterialPacket(
        section_id="S01",
        claims=[{
            "claim_id": "S01-C01",
            "statement": "A factual optical assertion.",
            "writing_permission": "factual_assertion",
        }],
        evidence_packets=[module.EvidencePacket(
            claim_id="S01-C01", paper_id="p1", chunk_id="c1",
            exact_spans=["A different observation."],
        )],
    )
    draft = module.CitationBinder(real_llm=True).bind(
        module.SectionDraft(
            section_id="S01",
            english_text="A factual optical assertion [REF:p1:S01-C01].",
        ),
        packet,
    )
    assert draft.citation_map == {}
    assert any(
        row.get("overclaim_type") == "uncited_after_entailment_rejection"
        for row in draft.overclaim_flags
    )


def test_peer_reviewers_do_not_receive_upstream_llm_judgment(monkeypatch):
    import optomind_research.full_review_production as module

    payloads: list[dict] = []

    def fake_call(*args, **kwargs):
        messages = kwargs.get("messages", args[1] if len(args) > 1 else [])
        payloads.append(json.loads(messages[-1]["content"]))
        return {"content": json.dumps({
            "recommendation": "minor_revision",
            "confidence": "medium",
            "strengths": [],
            "issues": [],
            "questions_for_authors": [],
            "publication_blockers": [],
        })}

    monkeypatch.setattr(module, "call_qwen_chat", fake_call)
    module.run_peer_review_panel(
        _mock_draft_bundle(),
        {
            "judgment": {"issues": [{"description": "Do not cascade this opinion."}]},
            "deterministic_metrics": {"section_count": 1},
            "post_revision_citation_audit": {
                "citation_audits": [{
                    "section_id": "S01",
                    "uncited_after_entailment_rejection": [{"sentence_fragment": "x"}],
                    "section_quality_judgment": {
                        "verdict": "needs_major_revision",
                        "unsupported_fact_detected": True,
                    },
                }],
            },
        },
        charter={},
        real_llm=True,
    )
    assert len(payloads) == 4
    assert all("global_review_summary" not in payload for payload in payloads)
    assert all("Do not cascade this opinion." not in json.dumps(payload) for payload in payloads)
    assert all(
        payload["independent_deterministic_audit"][
            "uncited_after_entailment_rejection_count"
        ] == 1
        for payload in payloads
    )
    assert all(
        payload["independent_deterministic_audit"]["section_status"][0][
            "unsupported_fact_detected"
        ] is True
        for payload in payloads
    )


def test_peer_review_panel_retries_invalid_role_output(monkeypatch):
    import optomind_research.full_review_production as module

    attempts: dict[str, int] = {}

    def fake_call(agent_name, *_args, **_kwargs):
        role = agent_name.split(":", 1)[1]
        attempts[role] = attempts.get(role, 0) + 1
        if attempts[role] == 1:
            return {"content": "not-json", "_llm_usage": {"error_type": "TimeoutError"}}
        return {"content": json.dumps({
            "recommendation": "minor_revision",
            "confidence": "medium",
            "strengths": [],
            "issues": [],
            "questions_for_authors": [],
            "publication_blockers": [],
        })}

    monkeypatch.setattr(module, "call_qwen_chat", fake_call)
    result = module.run_peer_review_panel(
        _mock_draft_bundle(),
        {"deterministic_metrics": {}, "post_revision_citation_audit": {}},
        charter={},
        real_llm=True,
    )
    assert set(attempts.values()) == {2}
    assert all(
        review["review_process"]["attempt_count"] == 2
        and review["review_process"]["valid_output"] is True
        for review in result["peer_reviews"]
    )


def test_peer_panel_caps_missing_visual_alone_at_high(monkeypatch):
    import optomind_research.full_review_production as module

    def fake_call(*_args, **_kwargs):
        return {"content": json.dumps({
            "recommendation": "major_revision",
            "confidence": "high",
            "strengths": [],
            "issues": [{
                "issue_id": "PR-01",
                "severity": "critical",
                "section_ids": ["S01"],
                "issue_type": "visual",
                "description": "A required explanatory figure is absent.",
                "recommended_action": "Add the planned figure.",
            }],
            "questions_for_authors": [],
            "publication_blockers": ["Add the required figure."],
        })}

    monkeypatch.setattr(module, "call_qwen_chat", fake_call)
    result = module.run_peer_review_panel(
        _mock_draft_bundle(),
        {
            "deterministic_metrics": {"section_count": 1},
            "post_revision_citation_audit": {
                "citation_audits": [{
                    "section_id": "S01",
                    "required_visual_missing": True,
                    "section_quality_judgment": {
                        "unsupported_fact_detected": False,
                    },
                }],
            },
        },
        charter={},
        real_llm=True,
    )
    assert result["critical_issue_count"] == 0
    assert result["high_or_critical_issue_count"] == 4
    assert all(
        review["issues"][0]["severity"] == "high"
        for review in result["peer_reviews"]
    )


def test_global_score_protocol_normalizes_accidental_percentage():
    from optomind_research.full_review_production import _normalize_five_point_scores

    result = _normalize_five_point_scores({
        "scores": {"thesis_control": 5, "argument_arc": 80},
        "overall_score": 82,
    })
    assert result["scores"] == {"thesis_control": 5.0, "argument_arc": 4.0}
    assert result["overall_score"] == 4.1
    assert result["score_scale_audit"]["overall_score"]["raw"] == 82.0


def test_section_contract_survives_into_writer_packet():
    from optomind_research.review_writer import SectionMaterialMapper

    contract = {
        "section_purpose": "Establish the physical framework.",
        "central_thesis": "A bounded central thesis.",
        "argument_sequence": ["Derive the balance", "Compare ideal and real conditions"],
        "paragraph_functions": ["Orientation", "Framework", "Comparison"],
        "required_evidence_roles": ["A thermodynamic analysis", "An experimental comparison"],
        "forbidden_overclaims": ["Do not invent a theoretical limit."],
        "open_questions": ["Which boundary dominates?"],
        "word_budget": 1800,
    }
    packet = SectionMaterialMapper().map({
        "section_id": "S01",
        "title": "Physical framework",
        "section_contract": contract,
    })
    assert packet.section_contract["word_budget"] == 1800
    assert packet.section_contract["paragraph_functions"] == contract["paragraph_functions"]
    assert packet.section_contract["required_evidence_roles"] == contract["required_evidence_roles"]
    assert packet.section_contract["open_questions"] == contract["open_questions"]


def test_section_queries_do_not_truncate_required_evidence_roles():
    from optomind_research.full_review_evidence import _section_queries

    roles = [
        "thermodynamic energy balance equation",
        "ideal versus measured performance comparison",
        "angular dependence characterization",
        "nonradiative convective heat transfer",
        "spectral spatial photonic framework",
    ]
    queries = _section_queries({
        "title": "Radiative cooling constraints",
        "argument_role": "Establish physical limits.",
        "key_questions": [f"Question {index} about deployment" for index in range(5)],
        "section_contract": {
            "central_thesis": "Environmental constraints shape optical performance.",
            "required_evidence_roles": roles,
            "argument_sequence": [f"Step {index} additional reasoning" for index in range(5)],
        },
    })
    joined = " ".join(queries)
    for required_term in ("thermodynamic", "measured", "angular", "convective", "photonic"):
        assert required_term in joined
    assert len(queries) > 8


def test_f2_to_f3_contract_repairs_clipped_central_thesis():
    from optomind_research.full_review_evidence import _merge_contract

    full_thesis = (
        "The optical response must be evaluated under realistic angular and "
        "environmental boundary conditions before a design can be considered robust."
    )
    section = {
        "section_id": "S01",
        "section_title": "Boundary conditions",
        "planned_thesis": {"text": full_thesis},
    }
    contract = {
        "section_id": "S01",
        "central_thesis": "The optical response must be evaluated under realis",
    }
    merged = _merge_contract(section, contract)
    assert merged["section_contract"]["central_thesis"] == full_thesis
    assert merged["section_contract"]["central_thesis_source"] == "selected_blueprint_planned_thesis"


def test_manuscript_continuity_audit_catches_repeated_acronym_and_body_closure():
    from optomind_research.review_writer import (
        SectionDraft,
        SectionMaterialPacket,
        audit_manuscript_continuity,
    )

    drafts = [
        SectionDraft(
            section_id="S01",
            english_text="Passive daytime radiative cooling (PDRC) is introduced here.\n\nThe physical question remains open.",
        ),
        SectionDraft(
            section_id="S02",
            english_text=(
                "Passive daytime radiative cooling (PDRC) is a thermal strategy.\n\n"
                "In summary, the evidence establishes the mechanism."
            ),
        ),
        SectionDraft(section_id="S03", english_text="The qualified findings can now be integrated."),
    ]
    packets = [
        SectionMaterialPacket("S01", section_contract={"section_role": "introduction"}),
        SectionMaterialPacket("S02", section_contract={"section_role": "body"}),
        SectionMaterialPacket("S03", section_contract={"section_role": "synthesis"}),
    ]
    audit = audit_manuscript_continuity(drafts, packets)
    issue_types = {row["issue_type"] for row in audit["findings"]}
    assert "repeated_topic_definition" in issue_types
    assert "body_mini_conclusion" in issue_types
    assert not audit["passed"]


def test_approved_ai_conceptual_visual_can_be_placed_but_is_not_evidence(tmp_path):
    from optomind_research.review_writer import FigurePlanner, SectionDraft, SectionMaterialPacket

    image = tmp_path / "concept.png"
    image.write_bytes(b"not-a-real-image-but-path-is-auditable")
    packet = SectionMaterialPacket(
        section_id="S02",
        visual_gap_plan=[{
            "visual_plan_id": "S02-VG01",
            "argument_role": "Explain a causal workflow.",
            "asset_status": "approved_ai_conceptual_schematic",
            "human_approved": True,
            "local_image_path": str(image),
            "model_review": {"approved_caption_boundary": "Conceptual workflow only."},
        }],
    )
    draft = FigurePlanner(real_llm=False).plan(SectionDraft("S02", english_text="Body."), packet)
    assert draft.figure_placements[0]["evidence_status"] == "explanatory_not_empirical_evidence"
    assert draft.figure_placements[0]["source_paper_id"] == "AI-generated conceptual schematic"


def test_nonstandard_body_role_still_receives_continuity_checks():
    from optomind_research.review_writer import audit_manuscript_continuity

    drafts = [
        SectionDraft("S01", english_text="Passive daytime radiative cooling (PDRC) is introduced."),
        SectionDraft("S02", english_text="Mechanisms are bounded.\n\nThese questions frame subsequent analysis."),
        SectionDraft("S03", english_text="The findings are integrated."),
    ]
    packets = [
        SectionMaterialPacket("S01", section_contract={"section_role": "introduction"}),
        SectionMaterialPacket("S02", section_contract={"section_role": "physical_foundations"}),
        SectionMaterialPacket("S03", section_contract={"section_role": "synthesis"}),
    ]
    audit = audit_manuscript_continuity(drafts, packets)
    assert any(row["issue_type"] == "body_mini_conclusion" for row in audit["findings"])


def test_cross_section_editor_only_calls_llm_for_flagged_sections(monkeypatch):
    import optomind_research.review_writer as module

    calls: list[str] = []

    def fake_call(*_args, **kwargs):
        messages = kwargs.get("messages", _args[1] if len(_args) > 1 else [])
        payload = json.loads(messages[-1]["content"])
        section_id = payload["editable_section"]["section_id"]
        calls.append(section_id)
        return {"content": json.dumps({"operations": []})}

    monkeypatch.setattr(module, "call_qwen_chat", fake_call)
    drafts = [
        module.SectionDraft(section_id="S01", english_text="Opening text."),
        module.SectionDraft(section_id="S02", english_text="Body text."),
        module.SectionDraft(section_id="S03", english_text="Closing text."),
    ]
    packets = [module.SectionMaterialPacket(section_id=s.section_id) for s in drafts]
    module.CrossSectionEditor(real_llm=True).edit(
        drafts,
        packets,
        audit_findings=[{
            "section_id": "S02",
            "issue_type": "document_navigation_instead_of_scientific_transition",
            "description": "Replace document navigation with a scientific handoff.",
        }],
    )
    assert calls == ["S02"]


def test_missing_visual_removes_only_broken_promise():
    from optomind_research.review_writer import remove_broken_visual_promises

    draft = SectionDraft(
        "S01",
        english_text=(
            "The mechanism couples solar rejection and thermal emission. "
            "A conceptual diagram of these pillars is presented.\n\n"
            "The scientific argument remains valid without that image."
        ),
        figure_placements=[{"asset_status": "missing_required_visual", "local_image_path": ""}],
    )
    repaired = remove_broken_visual_promises(draft)
    assert "is presented" not in repaired.english_text
    assert "scientific argument remains valid" in repaired.english_text


def test_pending_visual_assets_are_deduplicated_by_plan_id():
    from optomind_research.full_review_production import _dedupe_pending_visual_assets

    rows = [
        {"visual_plan_id": "S03-VG01", "description": "A mechanism schematic."},
        {
            "visual_plan_id": "S03-VG01",
            "description": "A mechanism schematic.",
            "generation_status": "generated_human_review_pending",
            "local_image_path": "candidate.png",
        },
        {"visual_plan_id": "S04-VG01", "description": "A measured spectrum."},
    ]
    deduped = _dedupe_pending_visual_assets(rows)
    assert [row["visual_plan_id"] for row in deduped] == ["S03-VG01", "S04-VG01"]
    assert deduped[0]["local_image_path"] == "candidate.png"


def test_visual_creation_classifier_distinguishes_framework_from_data_plot():
    from optomind_research.full_review_evidence import classify_visual_creation

    assert classify_visual_creation(
        "A framework diagram that maps material classes to application domains."
    ) == "author_synthesized_conceptual_schematic"
    assert classify_visual_creation(
        "A scatter plot of measured cooling power under different humidity conditions."
    ) == "source_data_replot_or_verified_source_figure"


def test_removed_entailment_failure_no_longer_blocks_current_text():
    draft = SectionDraft(
        "S01",
        english_text="A narrower supported sentence remains.",
        overclaim_flags=[{
            "overclaim_type": "uncited_after_entailment_rejection",
            "sentence_fragment": "The removed unsupported sentence.",
            "revised_sentence": "",
        }],
    )
    bundle = {
        "section_drafts": [{
            "section_id": draft.section_id,
            "english_text": draft.english_text,
            "overclaim_flags": draft.overclaim_flags,
            "citation_map": {},
        }],
        "material_packets": [{"section_id": "S01", "claims": [], "evidence_packets": []}],
    }
    report = audit_citations(bundle, real_llm=False)
    assert report["citation_audits"][0]["uncited_after_entailment_rejection"] == []
