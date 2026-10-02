import json

import pytest

from optomind_research.runtime.upgrade3.review_unit_writer import (
    UnitWritingError,
    UnitWritingView,
    build_completion_payload,
    completion_messages,
    run_unit_completion,
)


def _view():
    return UnitWritingView(
        chapter_id="CH03",
        unit_id="CH03_U01",
        focus="focused",
        unit_index=1,
        unit_count=2,
        sibling_units=[],
        chapter_frame={"chapter_title": "Chapter", "research_question": "Q"},
        other_chapters=[],
        paragraph_tasks=[
            {
                "paragraph_id": "CH03_U01_P01",
                "point": "point",
                "source_brief_details": [
                    {"point": "detail", "source_handles": ["P001"]},
                ],
                "source_uses": [{"source_handle": "P001", "role": "main", "use": "result"}],
            },
        ],
        table_tasks=[
            {
                "table_id": "CH03_U01_T01",
                "purpose": "compare",
                "columns": ["study", "result"],
                "row_tasks": [
                    {
                        "content": "P002 row",
                        "source_uses": [{"source_handle": "P002", "role": "row", "use": "result"}],
                    },
                ],
            },
        ],
        materials=[
            {"source_handle": "P001", "study_summary_A": {"key_findings": "A1"}, "review_planning_B": {"limits": "B1"}},
            {"source_handle": "P002", "study_summary_A": {"key_findings": "A2"}, "review_planning_B": {"limits": "B2"}},
            {"source_handle": "P003", "study_summary_A": {"key_findings": "A3"}, "review_planning_B": {"limits": "B3"}},
        ],
        chapter_tool_materials=[
            {"unit_key": "CH03:CH03_U01", "material": "unit"},
            {"source_handles": ["P002"], "material": "source"},
            {"unit_key": "CH03:CH03_U02", "material": "other-unit"},
            {"source_handles": ["P003"], "material": "unselected-source"},
            {"material": "chapter-wide-without-link"},
        ],
    )


class FakeClient:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def __call__(self, messages, **kwargs):
        self.calls.append((messages, kwargs))
        return self.response


def test_completion_payload_is_task_scoped_and_keeps_full_material():
    payload = build_completion_payload(_view(), "original", ["CH03_U01_T01"])
    assert payload["requested_source_handles"] == ["P002"]
    assert payload["sources"] == [_view().materials[1]]
    assert payload["table_tasks"][0]["row_tasks"][0]["source_uses"][0]["source_handle"] == "P002"
    assert [item.get("material") for item in payload["chapter_tool_materials"]] == ["unit", "source"]
    assert "chapter-wide-without-link" not in json.dumps(payload, ensure_ascii=False)


def test_completion_payload_uses_nested_brief_handles_and_rejects_missing():
    payload = build_completion_payload(_view(), "original", ["CH03_U01_P01"])
    assert payload["requested_source_handles"] == ["P001"]
    broken = _view()
    broken.materials = [item for item in broken.materials if item["source_handle"] != "P001"]
    with pytest.raises(UnitWritingError, match="completion_source_missing:P001"):
        build_completion_payload(broken, "original", ["CH03_U01_P01"])


def test_table_completion_accepts_real_table_and_preserves_original_prefix():
    client = FakeClient({
        "content": json.dumps({
            "body_markdown": "| study | result |\n|---|---|\n| [P002] | supported |",
            "status": "appended",
            "covered_task_ids": ["CH03_U01_T01"],
            "issues": [],
        }, ensure_ascii=False),
        "complete": True,
        "finish_reason": "stop",
    })
    result = run_unit_completion(
        _view(), existing_body="prefix\r\nexact", task_ids=["CH03_U01_T01"],
        client=client, simulated=True,
    )
    assert not result["pending"]
    assert result["table_check"]["valid"] is True
    assert result["body_markdown"].startswith("prefix\r\nexact")
    assert "模拟补写" in result["body_markdown"]
    assert result["body_markdown"].endswith("| [P002] | supported |")
    assert client.calls and client.calls[0][1]["call_id"].startswith("unit_completion_CH03_CH03_U01")


def test_table_task_metadata_is_not_accepted_as_a_table():
    client = FakeClient({
        "content": json.dumps({
            "body_markdown": "table_tasks: CH03_U01_T01\\nrow_tasks: copied task",
            "table_tasks": [{"table_id": "CH03_U01_T01"}],
            "status": "appended",
            "issues": [],
        }, ensure_ascii=False),
        "complete": True,
    })
    result = run_unit_completion(
        _view(), existing_body="original", task_ids=["CH03_U01_T01"], client=client,
    )
    assert result["pending"]
    assert result["body_markdown"] == "original"
    assert result["table_check"]["valid"] is False


def test_incomplete_valid_table_is_retained_as_candidate_but_not_applied():
    client = FakeClient({
        "body_markdown": "| study | result |\n|---|---|\n| [P002] | supported |",
        "status": "appended",
        "complete": False,
        "finish_reason": "length",
    })
    result = run_unit_completion(
        _view(), existing_body="original", task_ids=["CH03_U01_T01"], client=client,
    )
    assert result["pending"]
    assert result["body_markdown"] == "original"
    assert result["completion_fragment"].startswith("| study | result |")
    assert any(item["code"] == "completion_response_incomplete" for item in result["issues"])


def test_pending_status_fragment_is_candidate_only():
    client = FakeClient({
        "body_markdown": "a useful candidate",
        "status": "pending",
        "complete": True,
    })
    result = run_unit_completion(
        _view(), existing_body="original", task_ids=["CH03_U01_P01"], client=client,
    )
    assert result["pending"]
    assert result["body_markdown"] == "original"
    assert result["completion_fragment"] == "a useful candidate"
    assert any(item["code"] == "completion_status_not_appended" for item in result["issues"])


def test_table_data_column_count_must_match_header():
    client = FakeClient({
        "body_markdown": "| study | result |\n|---|---|\n| [P002] | supported | extra |",
        "status": "appended", "complete": True,
    })
    result = run_unit_completion(
        _view(), existing_body="original", task_ids=["CH03_U01_T01"], client=client,
    )
    assert result["pending"]
    assert result["table_check"]["valid"] is False


def test_paragraph_already_covered_and_provider_failure_keep_original():
    already = FakeClient({
        "body_markdown": "", "status": "already_covered",
        "covered_task_ids": ["CH03_U01_P01"], "issues": [], "complete": True,
    })
    result = run_unit_completion(
        _view(), existing_body="original", task_ids=["CH03_U01_P01"], client=already,
    )
    assert result["pending"]
    assert result["body_markdown"] == "original"
    assert any(item["code"] == "model_already_covered" for item in result["issues"])

    failing = FakeClient({"content": "", "complete": False})
    result = run_unit_completion(
        _view(), existing_body="original", task_ids=["CH03_U01_P01"], client=failing,
    )
    assert result["pending"]
    assert result["body_markdown"] == "original"

