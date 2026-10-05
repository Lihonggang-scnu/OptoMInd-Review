"""Offline tests of the standalone, ownership-only post-BODY applier."""

import importlib.util
from pathlib import Path
import sys

import pytest


# Loading the complete production module avoids runtime/__init__.py's optional
# live AgentScope dependency; no mocked application code or provider is used.
SOURCE = (Path(__file__).resolve().parents[2]
          / "optomind_research/runtime/upgrade3/serial_parts_application.py")
SPEC = importlib.util.spec_from_file_location("_serial_parts_application_tests", SOURCE)
app = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = app
SPEC.loader.exec_module(app)

PARTS = {"title": "A bounded review", "abstract": "Actual synthesis.",
         "keywords": ["evidence", "limits"], "introduction": "Actual scope.",
         "conclusion": "Actual conclusion."}


def marker(part, edge):
    return f"<!-- manuscript-part:{part}:{edge} -->"


def owned(part, text="old"):
    return f"{marker(part, 'start')}\n{text}\n{marker(part, 'end')}\n"


@pytest.mark.parametrize("source", [
    "# Original title\n\n## Methods\n\nBODY [1].\n\n## References\n[1] Paper.\n",
    "## Methods\nBODY without a document title",
    "\n\n# Original title\r\n\r\n## Methods\r\nBODY  \r\n\r\n## References\r\n[1] Paper.",
    "# Original title\n\n## Methods\nBODY without final newline",
    "BODY with no Markdown headings",
    "## Methods",
    "",
])
def test_lossless_body_projection_and_idempotence(source):
    before = app.body_preservation_projection(source)
    result, log = app.apply_serial_parts(source, PARTS)
    assert app.body_preservation_projection(result) == before
    assert [item["part"] for item in log] == ["title", "abstract", "introduction", "conclusion"]
    for part in ("title", "abstract", "introduction", "conclusion"):
        assert result.count(marker(part, "start")) == 1
        assert result.count(marker(part, "end")) == 1
    repeated, repeated_log = app.apply_serial_parts(result, PARTS)
    assert repeated == result
    assert all(item["position"] == "owned_part_replaced" for item in repeated_log)
    updated, _ = app.apply_serial_parts(result, {**PARTS, "introduction": "Revised scope."})
    assert "Actual scope." not in updated and updated.count("Revised scope.") == 1
    assert app.body_preservation_projection(updated) == before
    if "## References" in source:
        assert updated.index("Actual conclusion.") < updated.index("## References")
        assert updated.count("## References") == 1
        assert updated.endswith(source[source.index("## References"):])


@pytest.mark.parametrize("heading", [
    "Abstract", "ABSTRACT", "摘要", "Introduction", "引言", "绪论",
    "Conclusion", "Conclusions", "结论", "结语", "总结",
    "Chapter 1 Introduction", "第1章 引言", "CH01 Introduction",
    "1. Introduction", "1 Introduction", "第十二章 总结", "Introduction ###",
])
def test_exact_unowned_article_headings_block_without_replacing_body(heading):
    source = f"# Article\n\n## {heading}\n\nOWNED BY BODY.\n"
    conflicts, _warnings = app.preflight_placement(source)
    assert conflicts and all(row["code"] == "placement_conflict" for row in conflicts)
    assert all(row["severity"] == "error" for row in conflicts)
    with pytest.raises(app.SerialPartsApplicationError, match="placement_conflict"):
        app.apply_serial_parts(source, PARTS)
    assert "OWNED BY BODY." in app.extract_body(source)


@pytest.mark.parametrize("heading", [
    "Introduction to Bayesian methods", "Introduction: calibration tools",
    "Conclusion of an optimization proof", "引言：符号系统",
])
def test_ambiguous_technical_titles_warn_but_do_not_block_or_remove_body(heading):
    source = f"# Article\n\n## {heading}\n\nSUBSTANTIVE BODY.\n"
    conflicts, warnings = app.preflight_placement(source)
    assert conflicts == []
    assert any(row["code"] == "ambiguous_part_heading" for row in warnings)
    result, _ = app.apply_serial_parts(source, PARTS)
    assert f"## {heading}\n\nSUBSTANTIVE BODY.\n" in result
    assert app.extract_body(result) == app.extract_body(source)


@pytest.mark.parametrize("fence", ["```", "````", "~~~"])
def test_fences_and_subsections_do_not_establish_article_part_locations(fence):
    example = (f"{fence}markdown\n# Not an article title\n## Introduction\n"
               + marker("abstract", "start") + "\nnot ownership\n"
               + marker("abstract", "end") + f"\n{fence}\n")
    source = ("# Article\n\n## Methods\n\n" + example
              + "\n### Abstract\nA subsection.\n#### Conclusion\nA proof.\n"
              + "    ## Introduction\n")
    assert app.preflight_placement(source) == ([], [])
    result, _ = app.apply_serial_parts(source, PARTS)
    assert app.extract_body(result) == app.extract_body(source)
    assert example in result


def test_short_fence_and_nonempty_closer_do_not_end_long_code_fence():
    source = "# Article\n\n## Methods\n````md\n```\n## Abstract\n```` example\n## Conclusion\n````\n"
    assert app.preflight_placement(source) == ([], [])


@pytest.mark.parametrize("role,heading", [
    ({"chapter_id": "CH01", "role": "introduction"}, "CH01 Evidence frame"),
    ({"title": "Evidence frame", "role": "opening"}, "Evidence frame"),
    ({"title": "Evidence frame", "role": "引言"}, "第1章 Evidence frame"),
    ({"chapter_id": "CH12", "role": "conclusion"}, "第十二章 Evidence frame"),
    ({"chapter_number": 2, "role": "closing"}, "Chapter 2 Evidence frame"),
    ({"chapter_id": "CH02", "role": "abstract"}, "2. Evidence frame"),
])
def test_explicit_structured_part_roles_resolve_and_block(role, heading):
    source = f"# Article\n\n## {heading}\nOriginal BODY.\n"
    conflicts, warnings = app.preflight_placement(source, [role])
    assert len(conflicts) == 1 and warnings == []
    assert conflicts[0]["existing_location"] == f"## {heading}"
    with pytest.raises(app.SerialPartsApplicationError, match="placement_conflict"):
        app.apply_serial_parts(source, PARTS, [role])


@pytest.mark.parametrize("role,headings", [
    ({"role": "introduction"}, ["Chapter 1 Theory"]),
    ({"chapter_id": "CH01", "role": "introduction"}, ["CH010 Theory"]),
    ({"chapter_id": "CH03", "role": "conclusion"}, ["Chapter 1 Theory", "Chapter 2 Results"]),
    ({"title": "Methods", "role": "introduction"}, ["Methods", "Methods"]),
    ({"chapter_id": "CH01", "title": "Results", "role": "conclusion"}, ["CH01 Theory", "Results"]),
    ({"chapter_number": 1, "role": "introduction"}, ["1.1 Introduction to Bayes"]),
    ({"chapter_id": "CH01", "role": "introduction"}, ["CH01.1 Theory"]),
])
def test_unresolved_or_contradictory_role_identities_only_warn(role, headings):
    source = "# Article\n\n" + "\n".join(f"## {heading}\nBODY.\n" for heading in headings)
    conflicts, warnings = app.preflight_placement(source, [role])
    assert conflicts == []
    assert any(row["code"] == "unresolved_part_role" for row in warnings)
    result, _ = app.apply_serial_parts(source, PARTS, [role])
    assert app.extract_body(result, [role]) == app.extract_body(source, [role])


def test_narrative_role_substrings_do_not_establish_article_responsibility():
    source = "# Article\n\n## Theory\nBODY.\n"
    roles = [{"title": "Theory", "role": "An introduction to Bayesian models"}]
    assert app.preflight_placement(source, roles) == ([], [])


@pytest.mark.parametrize("source,reason", [
    (marker("introduction", "start"), "unclosed"),
    (marker("introduction", "end"), "unmatched"),
    (owned("introduction") + owned("introduction"), "duplicate"),
    (marker("introduction", "start") + "\n" + owned("abstract") + marker("introduction", "end"), "nested"),
    (marker("introduction", "start") + "\n" + marker("abstract", "end"), "unmatched"),
    ("<!-- manuscript-part:introduction:start-->", "malformed"),
    ("<!-- manuscript-part:keywords:start -->", "malformed"),
    ("<!-- Manuscript-part:introduction:start -->", "malformed"),
    ("<!-- manuscript-part:introduction:start", "malformed"),
])
def test_all_corrupt_markers_fail_closed_before_any_application(source, reason):
    source = "# Article\n\n## Methods\nBODY.\n" + source
    conflicts, _warnings = app.preflight_placement(source)
    assert conflicts[0]["code"] == "invalid_owned_part_markers"
    assert reason in conflicts[0]["reason"]
    for operation in (app.extract_body, app.extract_owned_parts,
                      lambda text: app.apply_serial_parts(text, PARTS)):
        with pytest.raises(app.SerialPartsApplicationError, match="invalid_owned_part_markers"):
            operation(source)


@pytest.mark.parametrize("source,roles", [
    ("# Article\n\n# BODY chapter\nEvidence.\n", []),
    ("Preface prose\n\n# BODY chapter\nEvidence.\n", []),
    ("# Introduction\nEvidence.\n", []),
    ("# Chapter 1 Methods\nEvidence.\n", []),
    ("# Methods\nEvidence.\n", [{"title": "Methods", "role": "body"}]),
    (owned("title", "# Generated") + "# Existing title\nBODY", []),
])
def test_ambiguous_h1_fails_closed_instead_of_overwriting_body(source, roles):
    conflicts, _warnings = app.preflight_placement(source, roles)
    assert conflicts[0]["code"] == "ambiguous_document_title"
    with pytest.raises(app.SerialPartsApplicationError, match="ambiguous_document_title"):
        app.extract_body(source, roles)
    with pytest.raises(app.SerialPartsApplicationError, match="ambiguous_document_title"):
        app.apply_serial_parts(source, PARTS, roles)


def test_valid_owned_spans_are_excluded_from_conflicts_and_extraction():
    source = (owned("title", "# Existing title") + owned("abstract", "## Abstract\nOld abstract")
              + owned("introduction", "## Introduction\nOld introduction")
              + "\n## Methods\nExact BODY.\n\n" + owned("conclusion", "## Conclusion\nOld conclusion")
              + "## References\n[1] Exact reference.")
    assert app.preflight_placement(source) == ([], [])
    assert app.extract_body(source) == "\n## Methods\nExact BODY.\n\n## References\n[1] Exact reference."
    extracted = app.extract_owned_parts(source)
    assert extracted["title"] == "# Existing title"
    assert extracted["abstract"] == "## Abstract\nOld abstract"
    assert "Exact BODY" not in " ".join(extracted.values())
    result, _ = app.apply_serial_parts(source, PARTS)
    assert app.extract_body(result) == app.extract_body(source)
    assert result.count("Exact reference.") == 1


@pytest.mark.parametrize("language", ["en", "en-US", "en_GB"])
def test_english_headings_and_keywords(language):
    result, _ = app.apply_serial_parts("## Methods\nBODY", PARTS, language=language)
    parts = app.extract_owned_parts(result)
    assert parts["abstract"].startswith("## Abstract\n")
    assert "**Keywords:** evidence; limits" in parts["abstract"]
    assert parts["introduction"].startswith("## Introduction\n")
    assert parts["conclusion"].startswith("## Conclusion\n")


@pytest.mark.parametrize("part,heading", [
    ("abstract", "摘要"), ("abstract", "ABSTRACT"),
    ("introduction", "引言"), ("introduction", "绪论"), ("introduction", "Introduction"),
    ("conclusion", "结语"), ("conclusion", "结论"), ("conclusion", "Conclusions"),
])
@pytest.mark.parametrize("prefix", ["#", "##"])
def test_one_exact_leading_matching_outer_heading_is_removed(part, heading, prefix):
    source = "## Methods\r\nExact BODY without a final newline"
    content = "Actual content.\r\n\r\n### Conditions\r\nKeep these conditions."
    parts = {part: f"{prefix} {heading}\r\n\r\n{content}"}
    result, _ = app.apply_serial_parts(source, parts, language="en")
    assert app.extract_owned_parts(result)[part] == f"## {part.title()}\r\n\r\n{content}"
    assert app.extract_body(result) == source
    repeated, _ = app.apply_serial_parts(result, parts, language="en")
    assert repeated == result


@pytest.mark.parametrize("indent", ["    ", "\t"])
def test_removing_outer_heading_preserves_indented_part_content(indent):
    source = "## Methods\nExact BODY."
    content = f"{indent}## Internal literal\n{indent}Code content."
    parts = {"conclusion": "\n\n## Conclusion\n \n" + content}
    result, _ = app.apply_serial_parts(source, parts, language="en")
    assert app.extract_owned_parts(result)["conclusion"] == "## Conclusion\n\n" + content
    assert app.extract_body(result) == source
    repeated, _ = app.apply_serial_parts(result, parts, language="en")
    assert repeated == result


@pytest.mark.parametrize("value", [
    "Body content without a heading.",
    "## Abstract\nA nonmatching part heading.",
    "## Introduction to Bayesian methods\nA compound heading.",
    "## 引言：符号系统\nA compound Chinese heading.",
    "## 1. Introduction\nA numbered heading is not an exact match.",
    "### Introduction\nA legitimate internal heading.",
    "### 引言\nA legitimate internal Chinese heading.",
    "Prose first.\n\n## Introduction\nA later heading must remain.",
    "```markdown\n## Introduction\n```\nA literal example.",
    "    ## Introduction\n    An indented code heading.",
    "\n\n\t## Introduction\n\tA tab-indented code heading.",
])
def test_nonmatching_compound_and_internal_part_headings_are_preserved(value):
    source = "## Methods\nExact BODY."
    result, _ = app.apply_serial_parts(source, {"introduction": value}, language="en")
    assert app.extract_owned_parts(result)["introduction"] == "## Introduction\n\n" + value.strip()
    assert app.extract_body(result) == source


@pytest.mark.parametrize("parts,reason", [
    ({"title": "First\nSecond"}, "single_line"),
    ({"abstract": 123}, "must_be_text"),
    ({"abstract": marker("conclusion", "start")}, "ownership_marker"),
    ({"keywords": "a string", "abstract": "Text"}, "list_of_text"),
    ({"keywords": ["word"]}, "require_supplied_abstract"),
    ({"keywords": ["one\ntwo"], "abstract": "Text"}, "invalid_keyword_text"),
    ({"abstract": "```\nunclosed generated fence"}, "unclosed_markdown_fence"),
])
def test_invalid_generated_values_cannot_cross_ownership_boundary(parts, reason):
    with pytest.raises(app.SerialPartsApplicationError, match=reason):
        app.apply_serial_parts("# Article\n\n## Methods\nBODY.", parts)


def test_unclosed_source_fence_blocks_and_unknown_language_is_explicit():
    with pytest.raises(app.SerialPartsApplicationError, match="unclosed_markdown_fence"):
        app.apply_serial_parts("# Article\n```\nBODY", PARTS)
    with pytest.raises(app.SerialPartsApplicationError, match="unsupported_application_language"):
        app.apply_serial_parts("## Methods\nBODY", PARTS, language="fr")


def test_empty_update_keeps_manuscript_identical_and_partial_update_keeps_other_parts():
    source = "# Original title\n\n## Methods\nBODY."
    assert app.apply_serial_parts(source, {}) == (source, [])
    first, _ = app.apply_serial_parts(source, PARTS)
    before = app.extract_owned_parts(first)
    after, _ = app.apply_serial_parts(first, {"conclusion": "Different conclusion."})
    after_parts = app.extract_owned_parts(after)
    for part in ("title", "abstract", "introduction"):
        assert after_parts[part] == before[part]
    assert app.extract_body(after) == app.extract_body(source)


@pytest.mark.parametrize("ticks", ["`", "``", "```"])
def test_inline_literal_marker_pairs_are_never_owned_or_removed(ticks):
    literal = (f"The literal pair {ticks}<!-- manuscript-part:abstract:start --> "
               f"BODY EXAMPLE <!-- manuscript-part:abstract:end -->{ticks} should remain.")
    source = "# Article\n\n## Methods\n" + literal
    assert literal in app.extract_body(source)
    assert app.extract_owned_parts(source) == {}
    result, _ = app.apply_serial_parts(source, PARTS)
    assert literal in result
    assert literal in app.extract_body(result)


def test_multiline_inline_code_marker_examples_are_preserved_verbatim():
    literal = ("A code example `with a newline\n"
               "<!-- manuscript-part:abstract:start -->\n"
               "BODY EXAMPLE\n"
               "<!-- manuscript-part:abstract:end -->\n"
               "and closing backtick` in its paragraph.")
    source = "# Article\n\n## Methods\n" + literal
    assert literal in app.extract_body(source)
    result, _ = app.apply_serial_parts(source, PARTS)
    assert literal in result and literal in app.extract_body(result)


def test_marker_pair_nested_in_an_ordinary_html_comment_is_not_owned():
    example = ("<!-- Literal comment example:\n" + owned("abstract", "BODY EXAMPLE") + "-->\n")
    source = "# Article\n\n## Methods\n" + example
    conflicts, _ = app.preflight_placement(source)
    assert conflicts[0]["reason"] == "invalid_owned_part_markers:inside_html_comment"
    with pytest.raises(app.SerialPartsApplicationError, match="inside_html_comment"):
        app.apply_serial_parts(source, PARTS)


def test_escaped_backtick_does_not_hide_an_owned_span():
    source = "# Article\n\n## Methods\nLiteral \\` character. " + owned("conclusion", "Old conclusion.")
    assert app.extract_owned_parts(source)["conclusion"] == "Old conclusion."
    result, _ = app.apply_serial_parts(source, PARTS)
    assert "Literal \\` character. " in result
    assert "Old conclusion." not in result


def test_unclosed_inline_code_with_ownership_tokens_is_ambiguous():
    source = "# Article\n\n## Methods\nAn unmatched ` with " + owned("abstract", "Example")
    with pytest.raises(app.SerialPartsApplicationError, match="ambiguous_inline_code"):
        app.extract_body(source)
