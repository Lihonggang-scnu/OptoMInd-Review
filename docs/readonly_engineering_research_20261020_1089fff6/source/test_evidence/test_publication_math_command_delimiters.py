import pytest

from optomind_research.runtime.publication_integrity import (
    _formula_to_tex,
    prepare_publication_markdown,
)
from optomind_research.runtime.latex_publication_renderer import (
    _prepare_scientific_markdown_with_integrity,
)


@pytest.mark.parametrize("source,expected", [
    ("a·n", r"a\cdot n"),
    ("a×x", r"a\times x"),
    ("a≤n", r"a\leq n"),
    ("ΔR", r"\Delta R"),
])
def test_named_commands_do_not_merge_with_following_variables(source, expected):
    assert _formula_to_tex(source) == expected


def test_actual_onl_formula_keeps_operators_and_variables():
    actual = _formula_to_tex("d ≥ (N_x·λ_o)/(4·α·n_max)")
    assert actual == r"d \geq (N_x\cdot \lambda_o)/(4\cdot \alpha\cdot n_max)"
    assert r"\cdotn" not in actual


def test_non_whitespace_control_math_is_literalized_and_audited():
    source = "Before $a" + chr(8) + "+\nb$ after."

    rendered, audit = prepare_publication_markdown(source)

    assert "`$a\\b+\\nb$`" in rendered
    assert chr(8) not in rendered
    assert "literal_math_fallbacks" in audit
    assert len(audit["literal_math_fallbacks"]) == 1
    fallback = audit["literal_math_fallbacks"][0]
    assert fallback["offset"] == source.index("$")
    assert fallback["raw_span"] == "$a" + chr(8) + "+\nb$"
    assert fallback["control_codepoints"] == ["U+0008", "U+000A"]
    assert fallback["action"] == "literal_fallback"
    assert fallback["format"] == "markdown_inline_code"
    assert fallback["literal_projection"] == "`$a\\b+\\nb$`"


def test_whitespace_only_math_controls_are_diagnostic_only():
    source = "Before $a\nb$ and $c\td$ after."

    rendered, audit = prepare_publication_markdown(source)

    assert "$a\nb$" in rendered
    assert "$c\td$" in rendered
    assert audit["literal_math_fallbacks"] == []
    assert len(audit["math_control_diagnostics"]) == 2
    assert [
        row["control_codepoints"]
        for row in audit["math_control_diagnostics"]
    ] == [["U+000A"], ["U+0009"]]
    assert all(
        row["action"] == "diagnostic_only"
        and row["literal_projection"] is None
        for row in audit["math_control_diagnostics"]
    )


def test_clean_math_code_and_prose_controls_remain_unchanged():
    code_fence = "```text\n$a" + chr(8) + "+b$\n```"
    inline_code = "`$c" + chr(8) + "+d$`"
    source = "$a+b$\n" + code_fence + "\n" + inline_code + "\nplain " + chr(8)

    rendered, audit = prepare_publication_markdown(source)

    assert "$a+b$" in rendered
    assert code_fence in rendered
    assert inline_code in rendered
    assert rendered.endswith("plain " + chr(8) + "\n")
    assert audit["literal_math_fallbacks"] == []
    assert audit["math_control_diagnostics"] == []


def test_renderer_caller_preserves_raw_control_audit_before_unicode_cleaning():
    source = (
        "## Section\n\n"
        "broken $a" + chr(8) + "+\nb$; clean $x+y$.\n\n"
        "$c\td$.\n\n"
        "```text\n$x+y$\n```"
    )

    rendered, audit = _prepare_scientific_markdown_with_integrity(source)

    fallbacks = audit["literal_math_fallbacks"]
    diagnostics = audit["math_control_diagnostics"]
    assert len(fallbacks) == 1
    assert fallbacks[0]["audit_stage"] == "raw_pre_clean"
    assert fallbacks[0]["offset"] == source.index("$a")
    assert fallbacks[0]["raw_span"] == "$a" + chr(8) + "+\nb$"
    assert len(diagnostics) == 1
    assert diagnostics[0]["audit_stage"] == "raw_pre_clean"
    assert diagnostics[0]["raw_span"] == "$c\td$"
    assert rendered.count("`$a\\b+\\nb$`") == 1
    assert chr(8) not in rendered
    assert "$x+y$" in rendered
    assert "```text\n$x+y$\n```" in rendered
