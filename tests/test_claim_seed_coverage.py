from __future__ import annotations

import json
from pathlib import Path


def _claim(
    statement: str,
    *,
    fit: str = "central",
    status: str = "direct",
    refs: list[str] | None = None,
):
    from optomind_research.claim_schema import Claim

    claim = Claim(
        claim_id=f"C{abs(hash(statement)) % 100000}",
        statement=statement,
        evidence_type="mechanism",
        supporting_text_chunk_ids=list(refs if refs is not None else ["t1"]),
        saturation_score=1.0,
        load_bearing=True,
    )
    claim.section_fit = fit
    claim.evidence_binding_status = status
    return claim


def _section_with_seeds() -> dict:
    return {
        "section_id": "S04",
        "title": "Application domains",
        "argument_role": "Separate required application directions rather than selecting only the best-supported one.",
        "claim_graph_seed": {
            "central_claim_candidates": [
                {
                    "claim_seed_id": "seed-building",
                    "claim_seed": "Building-integrated radiative cooling panels reduce envelope heat gain",
                },
                {
                    "claim_seed_id": "seed-wearable",
                    "claim_seed": "Wearable passive cooling textiles improve personal thermal comfort",
                },
                {
                    "claim_seed_id": "seed-agriculture",
                    "claim_seed": "Agricultural cooling films protect crops under daytime heat",
                },
            ]
        },
    }


def test_seed_coverage_audit_identifies_one_missing_seed():
    from optomind_research.claim_decomposer import audit_claim_seed_coverage

    section = _section_with_seeds()
    claims = [
        _claim("Building integrated cooling panels reduce heat gain in envelopes."),
        _claim("Wearable passive cooling textiles improve comfort for users."),
        _claim("Radiative cooling materials improve daytime thermal management."),
    ]

    audit = audit_claim_seed_coverage(section, claims)

    assert audit["covered_seed_indices"] == [0, 1]
    assert audit["missing_seed_indices"] == [2]
    assert "Agricultural cooling films" in audit["missing_seed_texts"][0]


def test_seed_coverage_uses_basic_morphology_without_easy_false_misses():
    from optomind_research.claim_decomposer import audit_claim_seed_coverage

    section = {
        "claim_graph_seed": {
            "central_claim_candidates": [
                {
                    "claim_seed": "Wearable cooling devices integrate flexible textile membranes",
                }
            ]
        }
    }
    claims = [
        _claim("Wearables use cooled device integration in flexibly structured textiles and membranes."),
    ]

    audit = audit_claim_seed_coverage(section, claims)

    assert audit["missing_seed_indices"] == []
    assert audit["covered_seed_indices"] == [0]


def test_claim_set_quality_prioritizes_seed_coverage_and_section_fit_over_evidence_count():
    from optomind_research.claim_decomposer import rank_claim_set_quality

    section = _section_with_seeds()
    complete_with_gap = [
        _claim("Building integrated cooling panels reduce envelope heat gain.", refs=["b1"]),
        _claim("Wearable passive cooling textiles improve personal thermal comfort.", refs=["w1"]),
        _claim(
            "Agricultural cooling films protect crops during daytime heat.",
            status="insufficient",
            refs=[],
        ),
    ]
    evidence_rich_but_missing_seed = [
        _claim("Building integrated cooling panels reduce envelope heat gain.", refs=["b1", "b2", "b3"]),
        _claim("Wearable passive cooling textiles improve personal thermal comfort.", refs=["w1", "w2", "w3"]),
        _claim("Radiative cooling materials have extensive daytime measurement evidence.", refs=["e1", "e2", "e3"]),
    ]

    assert rank_claim_set_quality(complete_with_gap, section) > rank_claim_set_quality(
        evidence_rich_but_missing_seed,
        section,
    )


def test_decomposer_repairs_missing_seed_with_m3_gap_instruction(monkeypatch, tmp_path: Path):
    import optomind_research.claim_decomposer as module
    from optomind_research.claim_evidence_verifier import ClaimEvidenceVerifier
    from optomind_research.evidence_arbiter import EvidenceTypeArbiter

    prompt = tmp_path / "prompt.txt"
    prompt.write_text("Return JSON.", encoding="utf-8")
    calls: list[dict] = []
    verified_batches: list[list[str]] = []

    initial_statements = [
        "Building integrated cooling panels reduce envelope heat gain.",
        "Wearable passive cooling textiles improve personal thermal comfort.",
        "Radiative cooling materials have extensive daytime measurement evidence.",
    ]
    repaired_statements = [
        "Building integrated cooling panels reduce envelope heat gain.",
        "Wearable passive cooling textiles improve personal thermal comfort.",
        "Agricultural cooling films protect crops during daytime heat.",
    ]

    def fake_chat(agent_name, messages, **kwargs):
        payload = json.loads(messages[-1]["content"])
        calls.append(payload)
        statements = repaired_statements if len(calls) == 2 else initial_statements
        return {
            "content": json.dumps(
                {
                    "claims": [
                        {
                            "statement": statement,
                            "evidence_type": "application",
                            "supporting_text_refs": [] if "Agricultural" in statement else ["T01"],
                            "saturation_score": 1.0,
                            "load_bearing": idx == 0,
                        }
                        for idx, statement in enumerate(statements)
                    ]
                }
            )
        }

    def fake_verify(self, claims, section):
        verified_batches.append([claim.statement for claim in claims])
        for claim in claims:
            claim.section_fit = "central"
            if "Agricultural" in claim.statement:
                claim.supporting_text_chunk_ids = []
                claim.evidence_binding_status = "insufficient"
            else:
                claim.supporting_text_chunk_ids = ["chunk:1"]
                claim.evidence_binding_status = "direct"
        return claims

    def fake_arbitrate(self, claims, section):
        return claims

    monkeypatch.setattr(module, "call_qwen_chat", fake_chat)
    monkeypatch.setattr(ClaimEvidenceVerifier, "verify_and_bind", fake_verify)
    monkeypatch.setattr(EvidenceTypeArbiter, "arbitrate_section", fake_arbitrate)

    section = _section_with_seeds()
    section["candidate_text_chunks"] = [{"chunk_id": "chunk:1", "text_preview": "Supported building and wearable evidence."}]
    section["candidate_text_chunk_ids"] = ["chunk:1"]

    claims = module.ClaimDecomposer(prompt_path=prompt, real_llm=True).decompose_section(section)

    assert len(calls) == 2
    repair_instruction = calls[1]["repair_instruction"]
    assert "deterministic pre-verifier seed coverage audit" in repair_instruction
    assert "M3 gap" in repair_instruction
    assert "empty supporting_text_refs" in repair_instruction
    assert "Agricultural cooling films protect crops" in repair_instruction
    assert len(verified_batches) == 1
    assert verified_batches[0] == repaired_statements
    assert any("Agricultural cooling films" in claim.statement for claim in claims)
    assert [claim.supporting_text_chunk_ids for claim in claims if "Agricultural" in claim.statement] == [[]]


def test_decomposer_can_still_repair_after_verifier_fit_problem(monkeypatch, tmp_path: Path):
    import optomind_research.claim_decomposer as module
    from optomind_research.claim_evidence_verifier import ClaimEvidenceVerifier
    from optomind_research.evidence_arbiter import EvidenceTypeArbiter

    prompt = tmp_path / "prompt.txt"
    prompt.write_text("Return JSON.", encoding="utf-8")
    calls: list[dict] = []
    verify_batches: list[list[str]] = []

    initial_statements = [
        "Building integrated cooling panels reduce envelope heat gain.",
        "Wearable passive cooling textiles improve personal thermal comfort.",
        "Agricultural cooling films protect crops during daytime heat.",
    ]
    repaired_statements = [
        "Building integrated cooling panels reduce envelope heat gain.",
        "Wearable passive cooling textiles improve personal thermal comfort.",
        "Agricultural cooling films protect crops under daytime heat stress.",
    ]

    def fake_chat(agent_name, messages, **kwargs):
        payload = json.loads(messages[-1]["content"])
        calls.append(payload)
        statements = repaired_statements if len(calls) == 2 else initial_statements
        return {
            "content": json.dumps(
                {
                    "claims": [
                        {
                            "statement": statement,
                            "evidence_type": "application",
                            "supporting_text_refs": ["T01"],
                            "saturation_score": 1.0,
                            "load_bearing": idx == 0,
                        }
                        for idx, statement in enumerate(statements)
                    ]
                }
            )
        }

    def fake_verify(self, claims, section):
        verify_batches.append([claim.statement for claim in claims])
        for claim in claims:
            claim.supporting_text_chunk_ids = ["chunk:1"]
            claim.evidence_binding_status = "direct"
            claim.section_fit = "central"
        if len(verify_batches) == 1:
            claims[2].section_fit = "off_scope"
        return claims

    def fake_arbitrate(self, claims, section):
        return claims

    monkeypatch.setattr(module, "call_qwen_chat", fake_chat)
    monkeypatch.setattr(ClaimEvidenceVerifier, "verify_and_bind", fake_verify)
    monkeypatch.setattr(EvidenceTypeArbiter, "arbitrate_section", fake_arbitrate)

    section = _section_with_seeds()
    section["candidate_text_chunks"] = [{"chunk_id": "chunk:1", "text_preview": "Application-domain evidence."}]
    section["candidate_text_chunk_ids"] = ["chunk:1"]

    claims = module.ClaimDecomposer(prompt_path=prompt, real_llm=True).decompose_section(section)

    assert len(calls) == 2
    assert "repair_instruction" not in calls[0]
    assert "Avoid these rejected or misplaced propositions" in calls[1]["repair_instruction"]
    assert len(verify_batches) == 2
    assert verify_batches[0] == initial_statements
    assert verify_batches[1] == repaired_statements
    assert all(claim.section_fit == "central" for claim in claims)
