"""Optional local-focus boundary, using synthetic material and real SQLite."""
import copy
import socket
import sqlite3

import pytest

from optomind_research.runtime.upgrade3 import planning_material_search as search
from optomind_research.runtime.upgrade3 import planning_material_triage as triage
from test_body03_local_lookup_reading import index_with


UNSEARCHABLE_FOCUS = "无需再读当前材料，需检索新的可见光大孔径消色差超透镜文献。"


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def denied(*args, **kwargs):
        raise AssertionError("Optional focus tests forbid network access")
    monkeypatch.setattr(socket.socket, "connect", denied)
    monkeypatch.setattr(socket, "create_connection", denied)


def fixture(tmp_path, focus, *, concepts=(), initial_papers=1):
    path = index_with(tmp_path, [
        ("a", "P1", "Thermal conductivity thermal conductivity BASELINE: the initial coupon was measured at room temperature."),
        ("b", "P2", "Thermal conductivity ADDITIONAL_BOUNDARY: the comparison coupon was measured at 600 C."),
    ])
    gap = triage.LocalGap("G", "thermal conductivity", concepts=concepts)
    calls = []
    def judge(gap, bundle):
        calls.append(copy.deepcopy(bundle.to_dict()))
        return {"decision": "external_research", "answers_requested_question": False,
                "usable_content": "Initial partial measurement." if len(calls) == 1 else "Additional partial measurement.",
                "still_missing": "The cyclic boundary remains unmeasured.", "read_focus": focus,
                "external_ask": "Find a measured cyclic comparison."}
    return path, gap, judge, calls, initial_papers


@pytest.mark.parametrize("focus, reason", [
    (UNSEARCHABLE_FOCUS, "query_has_no_searchable_terms"),
    ("the and a", "query_has_no_searchable_terms"),
    ("!!!", "query_has_no_searchable_terms"),
    ("   ", "empty_query"),
])
def test_optional_unsearchable_focus_keeps_question_anchors_and_partial(tmp_path, focus, reason):
    path, gap, judge, calls, _ = fixture(tmp_path, focus)
    with search.PlanningMaterialIndex(path, readonly=True) as index:
        initial = triage.triage_gap(index, gap, judge=judge, top_papers=1, max_passages=1)
        if focus and not focus.strip():
            initial.read_focus = focus  # Also cover direct callers before judgment normalization.
        result = triage.read_local_capture(index, initial, judge=judge)
    assert len(calls) == 2  # The existing bounded read, with new question-anchored material.
    assert {row["paper_id"] for row in calls[1]["passages"]} == {"a", "b"}
    assert "Initial partial measurement." in result.usable_content
    assert result.decision == "external_research" and result.answers_requested_question is False
    assert result.external_ask == "Find a measured cyclic comparison."
    assert result.local_reading.search_found is True
    assert result.local_reading.not_matched_reason == ""
    assert "read_focus_search_unavailable:" + reason in result.local_reading.bounded_read_omissions


def test_no_new_material_retains_judgment_without_extra_judge_call(tmp_path):
    path, gap, judge, calls, _ = fixture(tmp_path, UNSEARCHABLE_FOCUS)
    with search.PlanningMaterialIndex(path, readonly=True) as index:
        initial = triage.triage_gap(index, gap, judge=judge, top_papers=12, max_passages=12)
        before = initial.to_dict()
        result = triage.read_local_capture(index, initial, judge=judge)
    assert len(calls) == 1
    for key in ("decision", "usable_content", "still_missing", "read_focus", "external_ask", "passages", "model_calls"):
        assert result.to_dict()[key] == before[key]
    assert "read_focus_search_unavailable:query_has_no_searchable_terms" in result.local_reading.bounded_read_omissions


@pytest.mark.parametrize("focus, concepts", [("thermal conductivity", ()), (UNSEARCHABLE_FOCUS, ("conductivity",))])
def test_searchable_focus_or_explicit_concept_uses_existing_path(tmp_path, focus, concepts):
    path, gap, judge, calls, _ = fixture(tmp_path, focus, concepts=concepts)
    with search.PlanningMaterialIndex(path, readonly=True) as index:
        initial = triage.triage_gap(index, gap, judge=judge, top_papers=1, max_passages=1)
        result = triage.read_local_capture(index, initial, judge=judge)
    assert len(calls) == 2 and {row["paper_id"] for row in calls[1]["passages"]} == {"a", "b"}
    assert not any(item.startswith("read_focus_search_unavailable:") for item in result.local_reading.bounded_read_omissions)


@pytest.mark.parametrize("error", [search.PlanningMaterialSearchError("different_search_error"), sqlite3.OperationalError("invalid index")])
def test_other_focus_search_errors_still_propagate(tmp_path, monkeypatch, error):
    path, gap, judge, calls, _ = fixture(tmp_path, "thermal focus")
    original = triage.search
    def fail_focus(index, question, **kwargs):
        if question == "thermal focus":
            raise error
        return original(index, question, **kwargs)
    monkeypatch.setattr(triage, "search", fail_focus)
    with search.PlanningMaterialIndex(path, readonly=True) as index:
        initial = triage.triage_gap(index, gap, judge=judge, top_papers=1, max_passages=1)
        with pytest.raises(type(error), match=str(error)):
            triage.read_local_capture(index, initial, judge=judge)


def test_main_query_and_low_level_fts_remain_strict(tmp_path):
    path, _, _, _, _ = fixture(tmp_path, "")
    gap = triage.LocalGap("G", UNSEARCHABLE_FOCUS)
    judgment = triage.TriageJudgment(gap, "external_research", read_focus="thermal conductivity")
    with search.PlanningMaterialIndex(path, readonly=True) as index:
        with pytest.raises(search.PlanningMaterialSearchError, match="^query_has_no_searchable_terms$"):
            triage.read_local_capture(index, judgment, judge=lambda *_: pytest.fail("Must not judge a failed main query"))
    with pytest.raises(search.PlanningMaterialSearchError, match="^query_has_no_searchable_terms$"):
        search.fts_query([])
