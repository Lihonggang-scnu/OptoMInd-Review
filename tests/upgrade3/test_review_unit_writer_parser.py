"""Offline parser checks for writer response envelopes."""

import json

from optomind_research.runtime.upgrade3.review_unit_writer import (
    _decode_json_content,
    _response_issues,
    parse_unit_body,
)
from scripts.upgrade3.review_unit_writer import _parser as cli_parser


def test_unescaped_newline_envelope_keeps_body_table_and_issues():
    table = "| 试验 | 结果 |\n| :--- | :--- |\n| A | 保留 |"
    # These are literal newlines in quoted JSON strings, matching the two
    # Plus responses that were stored in raw_responses.
    content = '{\n  "body_markdown": "第一段\n第二段",\n  "issues": [{"unit_id": "U01", "action": "omit"}],\n  "table_markdown": "' + table + '"\n}'

    decoded = _decode_json_content(content)
    assert decoded is not None
    assert decoded["body_markdown"] == "第一段\n第二段"
    assert decoded["table_markdown"] == table

    body = parse_unit_body({"content": content})
    assert body == "第一段\n第二段\n\n" + table
    assert '"body_markdown"' not in body
    assert _response_issues({"content": content}) == [{"unit_id": "U01", "action": "omit"}]


def test_separate_table_is_not_duplicated_when_already_in_body():
    table = "| A | B |\n|---|---|\n| 1 | 2 |"
    envelope = {"body_markdown": "正文\n\n" + table, "table_markdown": table}
    assert parse_unit_body(envelope) == envelope["body_markdown"]


def test_plain_markdown_and_valid_json_envelopes_remain_unchanged():
    assert parse_unit_body("普通 Markdown\n\n第二段") == "普通 Markdown\n\n第二段"
    envelope = {"body_markdown": "正文", "table_markdown": "| A |"}
    assert parse_unit_body(json.dumps(envelope, ensure_ascii=False)) == "正文\n\n| A |"


def test_existing_invalid_latex_backslash_repair_is_retained():
    assert parse_unit_body(r'{"body_markdown":"x \sim y"}') == r"x \sim y"


def test_writer_preview_defaults_use_restored_plus_profile_without_calling_api():
    args = cli_parser().parse_args(["--arrangement", "arrangement.json"])
    assert args.model == "qwen3.5-plus"
    assert args.output_tokens == 12000
    assert args.thinking_budget == 4000
    assert not args.run
