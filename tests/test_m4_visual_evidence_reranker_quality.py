from __future__ import annotations

import json


def _blueprint(statement: str = "Accurate optical claim statement about selective emissivity.") -> dict:
    return {
        "sections": [
            {
                "section_id": "S1",
                "title": "Selective thermal emission",
                "argument_role": "Evidence section for optical selectivity.",
                "claims": [
                    {
                        "claim_id": "C1",
                        "statement": statement,
                        "evidence_type": "measurement",
                        "evidence_binding_reason": "Binding verifies spectral selectivity against source text.",
                        "evidence_component_map": [
                            {"component": "selective emissivity exceeds the baseline"}
                        ],
                        "missing_evidence_components": ["long-term outdoor durability"],
                    }
                ],
            }
        ]
    }


def _claim_support(claim_id: str = "C1", chunk_id: str = "v1") -> list[dict]:
    return [
        {
            "claim_id": claim_id,
            "section_id": "S1",
            "evidence_type": "measurement",
            "candidate_visual_recommendations": [
                {
                    "chunk_id": chunk_id,
                    "visual_argument_type": "quantitative_comparison",
                    "source": "provided_by_blueprint",
                    "score": 1.0,
                }
            ],
        }
    ]


def _chunk_index(chunk_id: str = "v1") -> dict[str, dict]:
    return {
        chunk_id: {
            "chunk_id": chunk_id,
            "title": "Measured selective emissivity",
            "caption": "Figure shows selective emissivity exceeding the baseline.",
            "search_text": "Nearby text reports selective emissivity and the tested baseline.",
            "visual_argument_type": "quantitative_comparison",
            "visual_argument_status": "ok",
            "visual_argument_confidence": "high",
            "local_image_path": "unused-by-monkeypatch.png",
        }
    }


def _patch_vision(monkeypatch, payload: dict, captured: dict | None = None):
    import llm.qwen_vision_client as qwen_vision_client

    def fake_call_qwen_vision(**kwargs):
        if captured is not None:
            captured["prompt"] = kwargs["text_prompt"]
            captured["calls"] = captured.get("calls", 0) + 1
        return {
            "_evidence_mode": "vision_image_text",
            "_failure_reason": "",
            "content": json.dumps(payload),
        }

    monkeypatch.setattr(qwen_vision_client, "call_qwen_vision", fake_call_qwen_vision)


def test_rerank_claim_support_passes_accurate_statement_and_binding_fields(monkeypatch):
    from optomind_research.visual_evidence_reranker import VisualEvidenceReranker

    captured: dict = {}
    _patch_vision(
        monkeypatch,
        {
            "fit_score": 4.2,
            "support_strength": "strong",
            "best_use": "main_figure",
            "directness": "direct",
            "supported_claim_components": ["selective emissivity exceeds the baseline"],
            "unsupported_claim_components": [],
            "provable_claim_part": "selective emissivity exceeds the baseline",
            "why_this_visual": "The figure directly plots the measured component.",
            "risk_or_caveat": "",
        },
        captured,
    )

    reranker = VisualEvidenceReranker(real_llm=True, workers=1)
    out = reranker.rerank_claim_support(_claim_support(), _blueprint(), _chunk_index())

    prompt = captured["prompt"]
    assert "Claim: Accurate optical claim statement about selective emissivity." in prompt
    assert "Binding verifies spectral selectivity" in prompt
    assert "selective emissivity exceeds the baseline" in prompt
    assert "long-term outdoor durability" in prompt
    assert out[0]["claim_statement"] == "Accurate optical claim statement about selective emissivity."
    assert out[0]["claim_input_integrity"] == "ok"


def test_empty_blueprint_statement_fails_closed_without_model_call(monkeypatch):
    from optomind_research.visual_evidence_reranker import VisualEvidenceReranker

    captured = {"calls": 0}
    _patch_vision(monkeypatch, {}, captured)

    reranker = VisualEvidenceReranker(real_llm=True, workers=1)
    out = reranker.rerank_claim_support(_claim_support(), _blueprint(statement=""), _chunk_index())

    assert captured["calls"] == 0
    assert out[0]["claim_input_integrity"] == "missing_claim_statement"
    assert out[0]["reranked_visual_chunks"] == []
    assert out[0]["rejected_visual_chunks"][0]["support_strength"] == "reject"
    assert out[0]["rejected_visual_chunks"][0]["failure_reason"].startswith(
        "claim_input_integrity_failed:"
    )


def test_contextual_four_point_model_output_cannot_remain_medium(monkeypatch):
    from optomind_research.visual_evidence_reranker import VisualEvidenceReranker

    _patch_vision(
        monkeypatch,
        {
            "fit_score": 4.0,
            "support_strength": "medium",
            "best_use": "supporting_figure",
            "directness": "contextual",
            "supported_claim_components": ["same broad topic"],
            "unsupported_claim_components": ["selective emissivity exceeds the baseline"],
            "provable_claim_part": "same broad topic only",
            "why_this_visual": "It is from the same topic area.",
            "risk_or_caveat": "It does not explicitly address key aspects of the claim.",
        },
    )

    reranker = VisualEvidenceReranker(real_llm=True, workers=1)
    out = reranker.rerank_claim_support(_claim_support(), _blueprint(), _chunk_index())
    result = out[0]["reranked_visual_chunks"][0]

    assert result["directness"] == "contextual"
    assert result["fit_score"] <= 0.9
    assert result["support_strength"] == "decorative"
    assert result["best_use"] == "background"
    assert result["needs_human_review"] is True


def test_partial_support_is_capped_at_weak_and_requires_human_review(monkeypatch):
    from optomind_research.visual_evidence_reranker import VisualEvidenceReranker

    _patch_vision(
        monkeypatch,
        {
            "fit_score": 4.6,
            "support_strength": "strong",
            "best_use": "main_figure",
            "directness": "partial",
            "supported_claim_components": ["selective emissivity"],
            "unsupported_claim_components": ["baseline comparison"],
            "provable_claim_part": "selective emissivity only",
            "why_this_visual": "It covers only one component.",
            "risk_or_caveat": "",
        },
    )

    reranker = VisualEvidenceReranker(real_llm=True, workers=1)
    out = reranker.rerank_claim_support(_claim_support(), _blueprint(), _chunk_index())
    result = out[0]["reranked_visual_chunks"][0]

    assert result["directness"] == "partial"
    assert result["support_strength"] == "weak"
    assert result["best_use"] == "supporting_figure"
    assert result["needs_human_review"] is True


def test_direct_vision_output_can_remain_strong_main_figure(monkeypatch):
    from optomind_research.visual_evidence_reranker import VisualEvidenceReranker

    _patch_vision(
        monkeypatch,
        {
            "fit_score": 4.6,
            "support_strength": "strong",
            "best_use": "main_figure",
            "directness": "direct",
            "supported_claim_components": ["selective emissivity exceeds the baseline"],
            "unsupported_claim_components": [],
                "provable_claim_part": "selective emissivity exceeds the baseline",
                "entity_alignment": "exact",
                "entity_mismatch_reason": "The claim and visual concern the same emitter comparison.",
                "why_this_visual": "It directly plots the core comparison.",
            "risk_or_caveat": "",
        },
    )

    reranker = VisualEvidenceReranker(real_llm=True, workers=1)
    out = reranker.rerank_claim_support(_claim_support(), _blueprint(), _chunk_index())
    result = out[0]["reranked_visual_chunks"][0]

    assert result["directness"] == "direct"
    assert result["support_strength"] == "strong"
    assert result["best_use"] == "main_figure"
    assert result["needs_human_review"] is False


def test_cross_application_entity_mismatch_is_rejected(monkeypatch):
    from optomind_research.visual_evidence_reranker import VisualEvidenceReranker

    _patch_vision(
        monkeypatch,
        {
            "fit_score": 4.5,
            "support_strength": "strong",
            "best_use": "main_figure",
            "directness": "direct",
            "supported_claim_components": ["transparent radiative cooling"],
            "unsupported_claim_components": [],
            "provable_claim_part": "transparent radiative cooling",
            "entity_alignment": "mismatch",
            "entity_mismatch_reason": "The visual concerns a greenhouse crop film, while the claim concerns a photovoltaic cell.",
            "why_this_visual": "They share a broad optical-cooling topic.",
            "risk_or_caveat": "The application objects differ.",
        },
    )

    reranker = VisualEvidenceReranker(real_llm=True, workers=1)
    out = reranker.rerank_claim_support(_claim_support(), _blueprint(), _chunk_index())
    assert out[0]["reranked_visual_chunks"] == []
    rejected = out[0]["rejected_visual_chunks"][0]
    assert rejected["support_strength"] == "reject"
    assert rejected["entity_alignment"] == "mismatch"


def test_reranker_does_not_fabricate_supporting_visual_chunk_ids(monkeypatch):
    from optomind_research.visual_evidence_reranker import VisualEvidenceReranker

    _patch_vision(
        monkeypatch,
        {
            "fit_score": 4.0,
            "support_strength": "medium",
            "best_use": "supporting_figure",
            "directness": "direct",
            "supported_claim_components": ["selective emissivity exceeds the baseline"],
            "unsupported_claim_components": [],
            "provable_claim_part": "selective emissivity exceeds the baseline",
            "why_this_visual": "Directly supports the claim.",
            "risk_or_caveat": "",
        },
    )

    reranker = VisualEvidenceReranker(real_llm=True, workers=1)
    out = reranker.rerank_claim_support(_claim_support(), _blueprint(), _chunk_index())

    assert "supporting_visual_chunk_ids" not in out[0]


def test_global_budget_counts_only_scored_candidates_after_fail_closed(monkeypatch):
    from optomind_research.visual_evidence_reranker import VisualEvidenceReranker

    captured = {"calls": 0}
    _patch_vision(
        monkeypatch,
        {
            "fit_score": 4.0,
            "support_strength": "medium",
            "best_use": "supporting_figure",
            "directness": "direct",
            "supported_claim_components": ["second claim component"],
            "unsupported_claim_components": [],
            "provable_claim_part": "second claim component",
            "why_this_visual": "Direct support.",
            "risk_or_caveat": "",
        },
        captured,
    )
    blueprint = {
        "sections": [
            {
                "section_id": "S1",
                "claims": [
                    {"claim_id": "Cbad", "statement": "", "evidence_type": "measurement"},
                    {
                        "claim_id": "Cgood",
                        "statement": "Second claim component is directly measured.",
                        "evidence_type": "measurement",
                    },
                ],
            }
        ]
    }
    support = _claim_support("Cbad", "vbad") + _claim_support("Cgood", "vgood")
    chunks = _chunk_index("vbad")
    chunks.update(_chunk_index("vgood"))

    reranker = VisualEvidenceReranker(real_llm=True, workers=1)
    out = reranker.rerank_claim_support(support, blueprint, chunks, max_items=1)

    assert captured["calls"] == 1
    assert out[0]["claim_input_integrity"] == "missing_claim_statement"
    assert len(out[0]["rejected_visual_chunks"]) == 1
    assert out[1]["claim_input_integrity"] == "ok"
    assert len(out[1]["reranked_visual_chunks"]) == 1
from optomind_research.visual_argument_alignment import summarize_post_rerank_claim_support


def test_post_rerank_summary_does_not_treat_candidates_as_support():
    rows = [
        {
            "claim_id": "S01-C01",
            "section_id": "S01",
            "candidate_visual_recommendations": [{"chunk_id": "v1"}],
            "reranked_visual_chunks": [{
                "chunk_id": "v1",
                "support_strength": "decorative",
                "directness": "contextual",
            }],
            "rejected_visual_chunks": [],
        },
        {
            "claim_id": "S02-C01",
            "section_id": "S02",
            "candidate_visual_recommendations": [{"chunk_id": "v2"}],
            "reranked_visual_chunks": [],
            "rejected_visual_chunks": [],
        },
    ]

    summary = summarize_post_rerank_claim_support(rows)

    assert rows[0]["post_rerank_support_status"] == "background_only"
    assert rows[1]["post_rerank_support_status"] == "not_evaluated"
    assert summary["visual_gap_claim_ids"] == ["S01-C01"]
    assert summary["visual_gap_section_ids"] == ["S01"]
    assert summary["not_evaluated_claims"] == 1


def test_post_rerank_summary_counts_partial_as_provisional():
    rows = [{
        "claim_id": "S01-C01",
        "section_id": "S01",
        "candidate_visual_recommendations": [{"chunk_id": "v1"}],
        "reranked_visual_chunks": [{
            "chunk_id": "v1",
            "support_strength": "weak",
            "directness": "partial",
        }],
        "rejected_visual_chunks": [],
    }]

    summary = summarize_post_rerank_claim_support(rows)

    assert rows[0]["post_rerank_support_status"] == "provisional_support"
    assert summary["provisional_support_claims"] == 1
    assert summary["visual_gap_section_ids"] == []
