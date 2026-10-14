"""Synthetic fixtures for lossless completion representation recovery; no API."""
import json

import pytest

from optomind_research.runtime.upgrade3.review_unit_writer import (
    UnitWritingView,
    _completion_adjacent_body_strings,
    _completion_envelope_details,
    run_unit_completion,
    write_unit_completion,
)


def view():
    return UnitWritingView(
        chapter_id="CH", unit_id="U", focus="Synthetic comparison", unit_index=1,
        unit_count=1, sibling_units=[], chapter_frame={}, other_chapters=[],
        paragraph_tasks=[{"paragraph_id": "p1", "point": "Synthetic omitted comparison",
                          "source_uses": [{"source_handle": "P0001"}]}],
        table_tasks=[], materials=[{"source_handle": "P0001", "study_summary_A": {
            "text": "Synthetic chemical and sensor examples, with their limitations."}}],
    )


FIRST = 'SYNTHETIC chemical result: "yield" is conditional [P0001].\r\nExact line.'
SECOND = "SYNTHETIC sensor noise comparison remains qualitative [P0001]."
ISSUES = [{"problem": "Synthetic numeric uncertainty", "action": "omit"}]


def malformed():
    return ('{ "body_markdown": ' + json.dumps(FIRST) + ',\n  '
            + json.dumps(SECOND) + ', "issues": ' + json.dumps(ISSUES) + ' }')


def run(raw, **telemetry):
    supplied = {"content": raw, "complete": True, "finish_reason": "stop", **telemetry}
    result = run_unit_completion(view(), existing_body="original\r\nexact", task_ids=["p1"],
                                 client=lambda *args, **kwargs: supplied)
    assert supplied["content"] == raw
    return result


def test_adjacent_complete_strings_preserved_only_as_candidate_with_exact_offsets(tmp_path):
    raw = malformed()
    result = run(raw)
    assert result["parsed_via"] == "json_adjacent_body_strings"
    assert result["pending"] and result["candidate_eligible"]
    assert result["body_markdown"] == "original\r\nexact"
    assert result["completion_fragment"] == FIRST + "\n\n" + SECOND
    assert result["candidate_body_markdown"] == "original\r\nexact\n\n" + FIRST + "\n\n" + SECOND
    assert result["model_status"] == "" and not result["model_status_present"]
    assert result["covered_task_ids"] == []
    assert "status" not in result["response_envelope"]
    assert "covered_task_ids" not in result["response_envelope"]
    assert result["issues"][:1] == ISSUES
    recovery = next(item for item in result["issues"]
                    if item.get("code") == "completion_adjacent_body_strings_recovered")
    assert recovery["candidate_only"] is True
    assert recovery["offset_basis"] == "stripped_json_content_unicode_codepoints"
    assert [json.loads(raw[s["start"]:s["end"]]) for s in recovery["string_spans"]] == [FIRST, SECOND]
    report = write_unit_completion(view(), result, tmp_path, estimate={}, language="en")
    assert (tmp_path / "COMPLETED_BODY.md").read_bytes() == b"original\r\nexact"
    assert (tmp_path / "CANDIDATE_BODY.md").read_bytes() == result["candidate_body_markdown"].encode()
    assert recovery in report["issues"]


@pytest.mark.parametrize("raw", [
    malformed()[:-1],                         # missing closing object
    '{"body_markdown":"first", "cut off', # incomplete adjacent string
    '{"body_markdown":"first", 42, "issues":[]}',
    '{"body_markdown":"first", "second" "issues":[]}',
    '{"body_markdown":"first", "second", "body_markdown":"duplicate", "issues":[]}',
    '{"body_markdown":"first", "second", "issues":[{"x":1,"x":2}]}',
    '{"body_markdown":"first", "issues":[], "misplaced"}',
    '{"body_markdown":"first", "second", "unknown_member":[]}',
    '{"body_markdown":"first", "second", "issues":[],}',
])
def test_truncation_unknown_tokens_and_ambiguity_not_candidate(raw):
    assert _completion_adjacent_body_strings(raw) is None
    result = run(raw)
    assert result["pending"] and not result["candidate_eligible"]
    assert result["body_markdown"] == "original\r\nexact"
    assert not result["candidate_body_markdown"]


@pytest.mark.parametrize("metadata", [{"status": "appended"}, {"covered_task_ids": ["p1"]}])
def test_recovery_does_not_authorize_explicit_completion_metadata(metadata):
    raw = malformed()[:-2] + ', ' + json.dumps(metadata)[1:]
    assert _completion_adjacent_body_strings(raw) is None
    result = run(raw)
    assert result["pending"] and not result["candidate_eligible"]
    assert result["body_markdown"] == "original\r\nexact"


def test_complete_transport_required_and_normal_json_unchanged():
    result = run(malformed(), complete=False, finish_reason="length")
    assert result["pending"] and not result["candidate_eligible"]
    normal = {"body_markdown": FIRST, "status": "appended", "covered_task_ids": ["p1"], "issues": ISSUES}
    envelope, mode = _completion_envelope_details({"content": json.dumps(normal), "complete": True,
                                                  "finish_reason": "stop"})
    assert envelope == normal and mode == "json"
    result = run(json.dumps(normal))
    assert not result["pending"] and not result["candidate_eligible"]
    assert result["body_markdown"] == "original\r\nexact\n\n" + FIRST
