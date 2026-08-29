from __future__ import annotations

import json
import os
import shutil
import uuid
from pathlib import Path
from typing import Any

import pytest

from optomind_research.runtime import material_proposition_extractor as module
from optomind_research.runtime.material_proposition_extractor import (
    MaterialCardExtractionError,
    build_material_extraction_messages,
    extract_one_material_card,
    run_material_proposition_extraction,
    sanitize_material_proposition_card,
)


def _packet() -> dict:
    return {
        "canonical_work_id": "work:one",
        "canonical_identity": {
            "paper_id": "p1",
            "doi": "10.1/test",
            "title": "A generic scientific paper",
            "year": 2024,
            "venue": "Journal",
        },
        "member_paper_ids": ["p1"],
        "material_classes": ["abstract_claim"],
        "question": "How do two scientific approaches compare and validate?",
        "seed_axis_catalog": [
            {
                "axis_id": "Q01",
                "description": "Comparison of the two approaches",
                "origin": "user_question",
                "status": "seed",
            }
        ],
        "selected_evidence": [
            {
                "chunk_id": "c1",
                "text": "The authors report a validation result under a stated condition.",
                "evidence_ceiling": "contextual_or_qualified_support",
            },
            {
                "chunk_id": "c2",
                "text": "The full text identifies an implementation boundary.",
                "evidence_ceiling": "factual_support",
            },
        ],
        "selection_audit": {"selected_chunk_count": 2},
    }


@pytest.fixture()
def extractor_tmp() -> Path:
    root = (
        Path(__file__).resolve().parent.parent
        / f"mat-prop-extractor-tmp-{uuid.uuid4().hex[:8]}"
    )
    os.makedirs(root, exist_ok=False)
    try:
        yield root
    finally:
        shutil.rmtree(root, ignore_errors=True)


def _usage(
    input_tokens: int = 100,
    output_tokens: int = 50,
    *,
    success: bool = True,
) -> dict[str, Any]:
    return {
        "module": "MaterialPropositionExtractor",
        "agent_name": "MaterialPropositionExtractor",
        "model_tier": "b_plus_model",
        "model_name": "qwen3.7-flash",
        "task_type": "research_chat",
        "mock_llm": False,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": input_tokens + output_tokens,
        "token_usage_source": "provider_response",
        "success": success,
        "failure": not success,
        "error_type": "",
        "provider_usage": {
            "prompt_tokens": input_tokens,
            "completion_tokens": output_tokens,
        },
    }


def _valid_raw() -> dict[str, Any]:
    return {
        "question_relevance": "central",
        "paper_functions": ["reported_result"],
        "seed_axis_assignments": [],
        "emergent_axis_candidates": [],
        "propositions": [{
            "statement": "The paper reports a validated scientific result.",
            "proposition_kind": "finding",
            "stance": "reports",
            "question_function": "direct_answer",
            "evidence_chunk_ids": ["c1"],
            "explicitly_stated": True,
        }],
        "background_contexts": [],
    }


def _fake_call(responses: list[tuple[str, dict[str, Any]]]):
    calls: list[dict[str, Any]] = []

    def fake(agent_name: str, messages: list[dict[str, str]], **kwargs):
        calls.append({
            "agent_name": agent_name,
            "messages": messages,
            "kwargs": kwargs,
        })
        content, usage = responses[len(calls) - 1]
        return {"content": content, "_llm_usage": usage}

    return fake, calls


def test_system_prompt_is_generic_and_axes_come_from_packet() -> None:
    messages = build_material_extraction_messages(_packet())
    system = messages[0]["content"].casefold()
    user = messages[1]["content"]

    assert "scientific axes are open-world" in system
    assert "pinn" not in system
    assert "metasurface" not in system
    assert '"axis_id": "Q01"' in user


def test_supplementary_gap_context_is_forwarded_into_prompt() -> None:
    packet = _packet()
    packet["supplementary_context"] = {
        "task_id": "task-c1",
        "gap_type": "claim_evidence_gap",
        "search_background_cue": (
            "optical electromagnetic near-field fidelity"
        ),
        "coverage_catalog": [
            {
                "coverage_id": "F1",
                "description": "near-field truncation error",
            }
        ],
        "task_context_fields": {
            "missing_fact_units": ["near-field truncation error"]
        },
    }
    messages = build_material_extraction_messages(packet)
    user = json.loads(messages[1]["content"])
    assert user["supplementary_gap_context"]["gap_type"] == (
        "claim_evidence_gap"
    )
    assert user["supplementary_gap_context"]["task_context_fields"][
        "missing_fact_units"
    ] == ["near-field truncation error"]
    system = messages[0]["content"]
    assert "supplementary_gap_context" in system
    assert (
        "mechanism, boundary, validation, counterexample, or background"
        in system
    )
    assert "out_of_scope" in system
    assert "BOTH the supplied user_question" in system
    lower = system.casefold()
    for banned in (
        "pinn",
        "near-field",
        "optical",
        "electromagnetic",
        "stock",
        "epidemiology",
        "underwater",
        "acoustic",
        "alignment",
        "positioning",
    ):
        assert banned not in lower


def test_supplementary_task_reference_is_persisted_in_query_annotation() -> None:
    packet = _packet()
    packet["supplementary_context"] = {
        "task_id": "task-c1",
        "gap_type": "claim_evidence_gap",
        "search_background_cue": "bounded background cue",
        "exclusion_boundaries": ["excluded domain"],
        "coverage_catalog": [
            {"coverage_id": "F1", "description": "near-field truncation error"},
            {"coverage_id": "F2", "description": "alignment tolerance"},
        ],
        "task_context_fields": {
            "missing_fact_units": ["near-field truncation error"]
        },
    }
    raw = {
        "question_relevance": "contextual",
        "paper_functions": ["background_context"],
        "seed_axis_assignments": [],
        "emergent_axis_candidates": [],
        "propositions": [],
        "background_contexts": [
            {
                "statement": "The excerpt provides contextual information.",
                "basis_chunk_ids": ["c1"],
            }
        ],
    }
    card, audit = sanitize_material_proposition_card(raw, packet)
    assert audit["status"] == "passed"
    reference = card["query_annotation"]["supplementary_task_reference"]
    assert reference["task_id"] == "task-c1"
    assert reference["gap_type"] == "claim_evidence_gap"
    assert reference["coverage_ids"] == ["F1", "F2"]
    assert reference["context_sha256"].startswith("sha256:")

    ordinary = _packet()
    ordinary_card, _ = sanitize_material_proposition_card(raw, ordinary)
    assert "supplementary_task_reference" not in ordinary_card[
        "query_annotation"
    ]


def test_extract_valid_response_single_call(monkeypatch: pytest.MonkeyPatch) -> None:
    fake, calls = _fake_call([
        (json.dumps(_valid_raw()), _usage(100, 50)),
    ])
    monkeypatch.setattr(module, "call_qwen_chat", fake)
    card, audit = extract_one_material_card(_packet())
    assert len(calls) == 1
    assert audit["format_retry_count"] == 0
    usage = audit["llm_usage"]
    assert usage["model_call_count"] == 1
    assert usage["input_tokens"] == 100
    assert usage["output_tokens"] == 50
    assert len(usage["per_attempt_usage"]) == 1
    assert card["query_annotation"]["model_version"] == "qwen3.7-flash"


def test_truncated_response_then_valid_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake, calls = _fake_call([
        ('{"truncated', _usage(100, 50)),
        (json.dumps(_valid_raw()), _usage(200, 80)),
    ])
    monkeypatch.setattr(module, "call_qwen_chat", fake)
    card, audit = extract_one_material_card(_packet())
    assert len(calls) == 2
    assert audit["format_retry_count"] == 1
    usage = audit["llm_usage"]
    assert usage["model_call_count"] == 2
    assert usage["input_tokens"] == 300
    assert usage["output_tokens"] == 130
    assert len(usage["per_attempt_usage"]) == 2
    assert calls[1]["kwargs"]["max_tokens"] >= 8000
    assert calls[1]["kwargs"]["max_retries"] == 0
    retry_text = " ".join(
        str(message.get("content") or "")
        for message in calls[1]["messages"]
    ).casefold()
    assert "valid json object" in retry_text
    assert "no narration" in retry_text


def test_two_failed_parses_preserve_usage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake, calls = _fake_call([
        ("{bad json", _usage(100, 50)),
        ("also not json", _usage(200, 80)),
    ])
    monkeypatch.setattr(module, "call_qwen_chat", fake)
    with pytest.raises(MaterialCardExtractionError) as exc:
        extract_one_material_card(_packet())
    assert len(calls) == 2
    error = exc.value
    assert error.usage["input_tokens"] == 300
    assert error.usage["output_tokens"] == 130
    assert error.usage["model_call_count"] == 2
    assert len(error.per_attempt_usage) == 2
    assert error.format_retry_count == 1


def test_run_one_failed_status_preserves_usage(
    monkeypatch: pytest.MonkeyPatch,
    extractor_tmp: Path,
) -> None:
    fake, _ = _fake_call([
        ("{bad json", _usage(100, 50)),
        ("also not json", _usage(200, 80)),
    ])
    monkeypatch.setattr(module, "call_qwen_chat", fake)
    packet_path = extractor_tmp / "packets.json"
    packet_path.write_text(
        json.dumps({"question": _packet()["question"], "packets": [_packet()]}),
        encoding="utf-8",
    )
    summary = run_material_proposition_extraction(
        packet_path=packet_path,
        output_dir=extractor_tmp / "out",
        skip_existing=True,
    )
    row = summary["rows"][0]
    assert row["status"] == "failed"
    assert row["llm_usage"]["input_tokens"] == 300
    assert row["llm_usage"]["model_call_count"] == 2
    assert len(row["per_attempt_usage"]) == 2
    assert row["format_retry_count"] == 1
    assert summary["failed_count"] == 1
    assert summary["format_retry_count"] == 1


def test_resume_reused_rows_carry_usage(
    monkeypatch: pytest.MonkeyPatch,
    extractor_tmp: Path,
) -> None:
    packet_path = extractor_tmp / "packets.json"
    packet_path.write_text(
        json.dumps({"question": _packet()["question"], "packets": [_packet()]}),
        encoding="utf-8",
    )
    output_dir = extractor_tmp / "out"
    first_fake, first_calls = _fake_call([
        (json.dumps(_valid_raw()), _usage(100, 50)),
    ])
    monkeypatch.setattr(module, "call_qwen_chat", first_fake)
    first = run_material_proposition_extraction(
        packet_path=packet_path,
        output_dir=output_dir,
        skip_existing=True,
    )
    assert first["rows"][0]["status"] == "passed"
    assert len(first_calls) == 1

    def should_not_call(*args, **kwargs):
        raise AssertionError("reused card must not invoke the model")

    monkeypatch.setattr(module, "call_qwen_chat", should_not_call)
    resumed = run_material_proposition_extraction(
        packet_path=packet_path,
        output_dir=output_dir,
        skip_existing=True,
    )
    row = resumed["rows"][0]
    assert row["status"] == "reused"
    assert row["llm_usage"]["input_tokens"] == 100
    assert row["llm_usage"]["model_call_count"] == 1
    assert len(row["per_attempt_usage"]) == 1
    assert resumed["reused_count"] == 1
    assert resumed["reused_usage_count"] == 1


def test_legacy_facet_catalog_is_normalized_to_seed_axes() -> None:
    packet = _packet()
    packet.pop("seed_axis_catalog")
    packet["facet_catalog"] = [
        {
            "facet_id": "F01",
            "description": "Legacy question facet",
        }
    ]

    messages = build_material_extraction_messages(packet)
    assert '"axis_id": "F01"' in messages[1]["content"]

    raw = {
        "question_relevance": "contextual",
        "paper_functions": ["background_context"],
        "seed_axis_assignments": [
            {
                "axis_id": "F01",
                "fit": "contextual",
                "question_function": "background_context",
                "reason": "The supplied excerpt provides context.",
                "basis_chunk_ids": ["c1"],
            }
        ],
        "emergent_axis_candidates": [],
        "propositions": [],
        "background_contexts": [
            {
                "statement": "The excerpt provides contextual information.",
                "basis_chunk_ids": ["c1"],
            }
        ],
    }
    card, audit = sanitize_material_proposition_card(raw, packet)
    assert audit["status"] == "passed"
    assert card["seed_axis_assignments"][0]["axis_id"] == "F01"


def test_sanitizer_accepts_evidence_bound_emergent_axis() -> None:
    raw = {
        "question_relevance": "central",
        "paper_functions": ["validation_or_translation"],
        "seed_axis_assignments": [
            {
                "axis_id": "Q01",
                "fit": "substantial",
                "question_function": "comparison_input",
                "reason": "The paper supplies one comparison dimension.",
                "basis_chunk_ids": ["c1"],
            }
        ],
        "emergent_axis_candidates": [
            {
                "label": "Implementation boundary conditions",
                "definition": "Conditions that constrain practical implementation.",
                "why_seed_axes_are_insufficient": "The seed compares methods but does not represent implementation constraints.",
                "relationship_to_question": "It changes how the comparison can be interpreted in practice.",
                "proposed_level": "cross_cutting_candidate",
                "parent_seed_axis_ids": ["Q01"],
                "basis_chunk_ids": ["c2"],
            }
        ],
        "propositions": [
            {
                "statement": "The authors report a validation result under a stated condition.",
                "proposition_kind": "validation",
                "stance": "reports",
                "question_function": "validation_boundary",
                "explicitly_stated": True,
                "evidence_chunk_ids": ["c1"],
            }
        ],
        "background_contexts": [],
        "extraction_warnings": [],
    }
    card, audit = sanitize_material_proposition_card(raw, _packet())

    assert audit["status"] == "passed"
    assert card["emergent_axis_candidates"][0]["origin"] == "material_emergent"
    assert card["emergent_axis_candidates"][0]["promotion_status"] == "candidate_only"
    assert card["propositions"][0]["strongest_evidence_ceiling"] == (
        "contextual_or_qualified_support"
    )


def test_sanitizer_rejects_closed_world_and_fabricated_evidence() -> None:
    raw = {
        "question_relevance": "substantial",
        "paper_functions": ["method_or_model"],
        "seed_axis_assignments": [
            {
                "axis_id": "NOT_A_SEED",
                "fit": "central",
                "question_function": "direct_answer",
                "reason": "fabricated",
                "basis_chunk_ids": ["c1"],
            }
        ],
        "emergent_axis_candidates": [
            {
                "label": "Comparison of the two approaches",
                "definition": "Just renames the seed.",
                "why_seed_axes_are_insufficient": "They are not.",
                "relationship_to_question": "Duplicate.",
                "proposed_level": "primary_axis_candidate",
                "parent_seed_axis_ids": [],
                "basis_chunk_ids": ["c1"],
            }
        ],
        "propositions": [
            {
                "statement": "This proposition cites a chunk that was never supplied.",
                "proposition_kind": "finding",
                "stance": "supports",
                "question_function": "direct_answer",
                "explicitly_stated": True,
                "evidence_chunk_ids": ["fake"],
            },
            {
                "statement": "This mechanism was inferred rather than explicitly stated.",
                "proposition_kind": "mechanism",
                "stance": "supports",
                "question_function": "mechanism_context",
                "explicitly_stated": False,
                "evidence_chunk_ids": ["c2"],
            },
        ],
        "background_contexts": [],
    }
    card, audit = sanitize_material_proposition_card(raw, _packet())

    assert card["seed_axis_assignments"] == []
    assert card["emergent_axis_candidates"] == []
    assert card["propositions"] == []
    assert audit["status"] == "empty"
    assert audit["removed"]["unknown_seed_axis_ids"] == ["NOT_A_SEED"]
    assert audit["removed"]["unknown_chunk_ids"] == ["fake"]
    assert audit["removed"]["redundant_emergent_axes"] == 1
    assert audit["removed"]["non_explicit_propositions"] == 1
