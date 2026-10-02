import json

from optomind_research.runtime.upgrade3 import planning_material_triage as triage
from optomind_research.runtime.upgrade3.planning_retrieval_loop import (
    InformationNeed,
    LoopConfig,
    needs_from_planner_gap_rows,
    run_retrieval_loop,
)
from optomind_research.runtime.upgrade3.progressive_review_plan import _normalize_gaps


def _passage() -> triage.LocalReadingPassage:
    return triage.LocalReadingPassage(
        source_handle="paper:one",
        paper_id="P1",
        title="A relevant study",
        year="2024",
        doi="10.1000/example",
        reading_role="direct_evidence",
        text="The study reports a relevant finding in the requested setting.",
        best_sentence="The study reports a relevant finding in the requested setting.",
        section_path=("results",),
        material_depth="body",
        reading_path="/tmp/body.txt",
        card_path="/tmp/card.json",
        from_existing_field=True,
    )


def _bundle(gap: triage.LocalGap) -> triage.LocalReadingBundle:
    return triage.LocalReadingBundle(gap=gap, passages=[_passage()], search_found=True)


def _judge(**values):
    answer = {
        "decision": "direct_use",
        "usable_content": "The local material supplies useful context.",
        "still_missing": "",
        "read_focus": "",
        "external_ask": "",
        "reason": "test",
    }
    answer.update(values)
    return lambda gap, bundle: answer


def test_clinical_evidence_alias_routes_to_application_outcome_plus(monkeypatch, tmp_path):
    gap = triage.LocalGap("g1", "What clinical outcome is reported?", intended_use="clinical_evidence")
    assert gap.intended_use == "application_outcome"

    from optomind_research.runtime.upgrade3.module4 import runtime

    captured = {}

    class FakeLedger:
        def __init__(self, **kwargs):
            captured["ledger"] = kwargs

    class FakeClient:
        def __init__(self, **kwargs):
            captured["client"] = kwargs

    def fake_invoke(client, messages, **kwargs):
        captured["invoke"] = kwargs
        return {"content": json.dumps({
            "decision": "direct_use",
            "answers_requested_question": True,
            "usable_content": "The outcome is reported.",
        })}

    monkeypatch.setattr(runtime, "GlobalBudgetLedger", FakeLedger)
    monkeypatch.setattr(runtime, "QwenDirectClient", FakeClient)
    monkeypatch.setattr(runtime, "invoke_client", fake_invoke)
    judge = triage.QwenLocalTriageJudge(
        key_file=tmp_path / "key",
        budget_ledger_path=tmp_path / "budget.sqlite",
        budget_limit_cny=50,
    )
    result = judge(gap, _bundle(gap))
    assert result["answers_requested_question"] is True
    assert captured["client"]["model"] == "qwen3.5-plus"


def test_partial_context_false_routes_external_and_keeps_content(monkeypatch):
    gap = triage.LocalGap("g1", "Does the intervention improve survival?", intended_use="clinical_evidence")
    bundle = _bundle(gap)
    monkeypatch.setattr(triage, "prepare_local_reading", lambda *args, **kwargs: (bundle, None))
    result = triage.triage_gap(None, gap, judge=_judge(answers_requested_question=False))
    assert result.decision == "external_research"
    assert result.usable_content == "The local material supplies useful context."
    assert result.still_missing == gap.question
    assert gap.question in result.external_ask
    assert result.to_dict()["answers_requested_question"] is False
    assert triage.build_writer_material(result)["answers_requested_question"] is False


def test_explicit_negative_and_source_limited_answers_stay_direct(monkeypatch):
    for question, reason in (
        ("Did this experiment find an effect?", "explicit negative"),
        ("Within the cited 2020 sources, was an effect reported?", "source limited"),
    ):
        gap = triage.LocalGap("g1", question, intended_use="application_outcome")
        bundle = _bundle(gap)
        monkeypatch.setattr(triage, "prepare_local_reading", lambda *args, **kwargs: (bundle, None))
        result = triage.triage_gap(
            None,
            gap,
            judge=_judge(
                answers_requested_question=True,
                usable_content=f"{reason} answer",
            ),
        )
        assert result.decision == "direct_use"
        assert result.answers_requested_question is True


def test_absent_or_non_boolean_answer_signal_keeps_legacy_direct_behavior(monkeypatch):
    gap = triage.LocalGap("g1", "What does the study report?", intended_use="mechanism")
    bundle = _bundle(gap)
    monkeypatch.setattr(triage, "prepare_local_reading", lambda *args, **kwargs: (bundle, None))
    for signal in (None, "false"):
        values = {} if signal is None else {"answers_requested_question": signal}
        result = triage.triage_gap(None, gap, judge=_judge(**values))
        assert result.decision == "direct_use"
        assert result.answers_requested_question is None
        assert "answers_requested_question" not in result.to_dict()


def test_normalizers_share_clinical_evidence_route_and_feedback_reaches_bounded_loop(tmp_path):
    normalized = _normalize_gaps([{
        "gap_id": "g1",
        "gap_question": "What application outcome is reported?",
        "intended_use": "clinical_evidence",
    }])
    assert normalized[0]["intended_use"] == "application_outcome"
    needs = needs_from_planner_gap_rows(normalized)
    assert needs[0].intended_use == "application_outcome"
    assert InformationNeed("n1", "question", intended_use="clinical_evidence").intended_use == "application_outcome"

    need = InformationNeed(
        "n1",
        "What application outcome is reported?",
        owners=("chapter_1",),
        intended_use="clinical_evidence",
        round_specs=({"round": 1, "targeted_queries": [{"query_text": "outcome", "query_type": "keyword"}]},),
    )
    result = run_retrieval_loop(
        [need],
        LoopConfig(index_path=tmp_path / "unused.sqlite", journal_path=tmp_path / "journal.jsonl", max_rounds=1),
        local_triage=lambda **kwargs: {
            "decision": "external_research",
            "usable_content": "Useful partial context.",
            "still_missing": kwargs["gap"].question,
            "answers_requested_question": False,
            "passages": [],
        },
        external_closure=None,
    )
    state = result["needs"][0]
    assert state["status"] == "stopped_rounds_exhausted"
    assert state["local_triage"]["answers_requested_question"] is False
    assert result["owner_content"]["n1"]["chapter_1"]["answers_requested_question"] is False
