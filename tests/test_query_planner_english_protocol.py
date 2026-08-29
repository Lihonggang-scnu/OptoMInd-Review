from __future__ import annotations

from optomind_research.query_planner import QueryPlannerAgent, QueryPlannerResponse
from optomind_research.query_planner import FormatValidationReport


def _payload(user_query: str) -> dict:
    return {
        "input": {"user_query": user_query},
        "output": {
            "problem_understanding": "A scholarly investigation of an optical multilayer filter.",
            "scope_definition": {
                "main_scope": "Focus on inverse design and experimental validation.",
                "scope_items": ["Inverse design methods", "Fabrication constraints"],
            },
            "keyword_decomposition": {
                "keywords": ["optical multilayer inverse design", "thin film filter fabrication"],
            },
            "extra_notes": "",
        },
    }


def test_query_plan_rejects_non_english_intermediate_query() -> None:
    agent = QueryPlannerAgent(real_llm=False)
    report = agent.validate_payload(_payload("光学多层膜逆向设计"))
    assert not report.ok
    assert any("$.input.user_query" in error for error in report.errors)


def test_model_validation_does_not_restore_raw_non_english_query() -> None:
    payload = _payload("Optical multilayer inverse design")
    response = QueryPlannerResponse.model_validate(payload, user_query="光学多层膜逆向设计")
    assert response.input.user_query == "Optical multilayer inverse design"


def test_invalid_optional_extra_notes_can_be_dropped_without_losing_core_plan() -> None:
    agent = QueryPlannerAgent(real_llm=False)
    payload = _payload("Optical multilayer inverse design")
    payload["output"]["extra_notes"] = "中文说明"
    validation = agent.validate_payload(payload)
    assert not validation.ok
    salvaged = agent._salvage_optional_extra_notes(
        payload, validation=validation, user_query="原始中文问题"
    )
    assert salvaged is not None
    normalized, final_validation = salvaged
    assert final_validation.ok
    assert normalized["output"]["extra_notes"] == ""
    assert normalized["output"]["problem_understanding"] == payload["output"]["problem_understanding"]
