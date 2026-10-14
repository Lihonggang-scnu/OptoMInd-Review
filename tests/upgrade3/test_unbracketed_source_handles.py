"""Formatting of already explicit paper identities, without numeric guessing."""
from optomind_research.runtime.upgrade3 import review_unit_writer as writer
from optomind_research.runtime.upgrade3 import legacy_unit_route as legacy
import json


def test_known_prose_handles_keep_all_words_and_bind_citations():
    body = "P0001报告对照；P0002 compares conditions. [P0003] already cited."
    fixed, repairs = writer._repair_unbracketed_source_handles(body, ["P0001", "P0002", "P0003"])
    assert fixed == "[P0001]报告对照；[P0002] compares conditions. [P0003] already cited."
    assert writer.citations_in(fixed) == ["P0001", "P0002", "P0003"]
    assert [r["source_handle"] for r in repairs] == ["P0001", "P0002"]
    assert writer._repair_unbracketed_source_handles(fixed, ["P0001", "P0002", "P0003"]) == (fixed, [])


def test_unknown_numeric_and_foreign_namespace_are_not_guessed():
    body = "P0999 is unresolved. [11] stays. foreign::P0001 stays. XP0001 is a label."
    assert writer._repair_unbracketed_source_handles(body, ["P0001"]) == (body, [])
    diagnostics = writer._output_diagnostics(body, ["P0001"])
    assert diagnostics["unbracketed_source_handles"] == ["P0999"]
    assert {p["code"] for p in diagnostics["citation_problems"]} == {
        "unbracketed_source_handle", "numeric_citation_unresolved"}


def test_code_links_math_and_existing_brackets_remain_exact():
    body = ("`P0001` [P0001](https://example.test/P0001) ![P0001](image.png) "
            "$P0001$ $$P0001$$ \\(P0001\\) \\[P0001\\] [label P0001] "
            "<!-- P0001 -->\n```text\nP0001\n```\n    P0001\n")
    assert writer._repair_unbracketed_source_handles(body, ["P0001"]) == (body, [])


def test_markdown_table_source_column_is_bound_without_changing_results():
    body = "| Source | Result |\n|---|---|\n| P0001 | 2.5 ± 0.1 |"
    fixed, repairs = writer._repair_unbracketed_source_handles(body, ["P0001"])
    assert fixed == body.replace("P0001", "[P0001]")
    assert len(repairs) == 1
    assert writer._markdown_table_check(fixed)["valid"]


def test_write_unit_output_persists_normalization_and_original_raw_path(tmp_path):
    view = writer.UnitWritingView(chapter_id="CH", unit_id="U", focus="Comparison",
        unit_index=1, unit_count=1, sibling_units=[], chapter_frame={}, other_chapters=[],
        paragraph_tasks=[], table_tasks=[], materials=[{"source_handle":"P0001"}],
        sources={"P0001":{"source_handle":"P0001"}}, unit_notes="")
    result = writer.write_unit_output(view, "P0001 reports a result.", tmp_path,
        model="qwen3.5-plus", language="en", mode="run", used_messages=[], estimate={},
        response_path="original-response.json", finish_reason="stop")
    assert result["body_markdown"] == "[P0001] reports a result."
    assert result["used_source_handles"] == ["P0001"]
    assert result["raw_response"] == "original-response.json"
    assert result["unbracketed_source_handle_repairs"][0]["mapping_origin"] == "explicit_provided_handle"


def test_saved_response_format_recovery_preserves_original_and_is_reusable(tmp_path):
    view = writer.UnitWritingView(chapter_id="CH", unit_id="U", focus="Comparison",
        unit_index=1, unit_count=1, sibling_units=[], chapter_frame={}, other_chapters=[],
        paragraph_tasks=[], table_tasks=[], materials=[{"source_handle":"P0001"}],
        sources={"P0001":{"source_handle":"P0001"}}, unit_notes="")
    result = {"body_markdown":"P0001 reports 2.5.", "complete":True, "finish_reason":"stop"}
    original = json.dumps(result)
    attempt = tmp_path / "attempt_001"
    attempt.mkdir()
    (attempt / "UNIT_RESULT.json").write_text(original, encoding="utf-8")
    (attempt / "RAW_RESPONSE.json").write_text('{"original":true}', encoding="utf-8")
    new_dir, fixed, receipt = legacy._recover_cached_handle_format(view, result, attempt,
        messages=[], estimate={}, language="en", model="qwen3.5-plus", mode="live")
    assert new_dir != attempt
    assert fixed["body_markdown"] == "[P0001] reports 2.5."
    assert fixed["used_source_handles"] == ["P0001"]
    assert receipt["model_calls"] == 0
    assert (attempt / "UNIT_RESULT.json").read_text(encoding="utf-8") == original
    assert (attempt / "RAW_RESPONSE.json").read_text(encoding="utf-8") == '{"original":true}'
    assert legacy._recover_cached_handle_format(view, result, attempt,
        messages=[], estimate={}, language="en", model="qwen3.5-plus", mode="live")[1] == fixed


def test_accepted_cached_body_with_no_lost_identity_does_not_restart_quality(tmp_path):
    view = writer.UnitWritingView(chapter_id="CH", unit_id="U", focus="Comparison",
        unit_index=1, unit_count=1, sibling_units=[], chapter_frame={}, other_chapters=[],
        paragraph_tasks=[], table_tasks=[], materials=[{"source_handle":"P0001"}],
        sources={"P0001":{"source_handle":"P0001"}}, unit_notes="")
    result = {"body_markdown":"P0001 reports a result [P0001].", "complete":True}
    assert legacy._recover_cached_handle_format(view, result, tmp_path,
        messages=[], estimate={}, language="en", model="qwen3.5-plus", mode="live") == (tmp_path, result, None)
