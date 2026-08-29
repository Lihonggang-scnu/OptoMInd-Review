"""Hard fact-preservation tests for the LLM style verifier (P1-1 section 7.2).

Eleven cases, including the mandatory positive case: a suite with only
rejections cannot prove that the verifier is not a blanket refusal.
"""

from __future__ import annotations

from optomind_research.runtime.style_hard_verifier import verify_rewrite

SOURCE = (
    "Metasurfaces enable compact wavefront control [REF:s2:abc123]. The "
    "measured efficiency is approximately 85 percent at 450 nm, and the "
    "device may improve bandwidth toward 40 percent in future arrays."
)


def _verify(rewritten):
    return verify_rewrite(original=SOURCE, rewritten=rewritten)


def test_dropped_citation_is_rejected():
    candidate = SOURCE.replace("[REF:s2:abc123]", "")
    verdict = _verify(candidate)
    assert verdict["ok"] is False
    assert any(v["check"] == "citations" for v in verdict["violations"])


def test_invented_citation_is_rejected():
    candidate = SOURCE.replace(
        "[REF:s2:abc123]", "[REF:s2:abc123] [REF:s9:zzz999]"
    )
    verdict = _verify(candidate)
    assert verdict["ok"] is False
    assert any(v["check"] == "citations" for v in verdict["violations"])


def test_changed_number_is_rejected():
    candidate = SOURCE.replace("450 nm", "550 nm")
    verdict = _verify(candidate)
    assert verdict["ok"] is False
    assert any(v["check"] == "numbers" for v in verdict["violations"])


def test_deleted_unit_is_rejected():
    candidate = SOURCE.replace("450 nm", "450")
    verdict = _verify(candidate)
    assert verdict["ok"] is False
    assert any(v["check"] == "units" for v in verdict["violations"])


def test_deleted_formula_is_rejected():
    source = "The phase profile follows $\\lambda(x, y)$ across the aperture "
    "and stays fixed under the paraxial approximation used throughout."
    candidate = source.replace("$\\lambda(x, y)$", "the local phase law")
    verdict = verify_rewrite(original=source, rewritten=candidate)
    assert verdict["ok"] is False
    assert any(v["check"] == "math" for v in verdict["violations"])


def test_deleted_hedge_is_rejected():
    candidate = SOURCE.replace("may improve", "improves")
    verdict = _verify(candidate)
    assert verdict["ok"] is False
    assert any(v["check"] == "hedges" for v in verdict["violations"])


def test_added_hedge_is_allowed():
    candidate = SOURCE.replace(
        "toward 40 percent", "likely toward 40 percent", 1,
    )
    verdict = _verify(candidate)
    assert verdict["ok"] is True


def test_dropped_protected_term_is_rejected():
    candidate = SOURCE.replace("Metasurfaces", "Thin films")
    verdict = verify_rewrite(
        original=SOURCE,
        rewritten=candidate,
        protected_terms=["metasurfaces"],
    )
    assert verdict["ok"] is False
    assert any(v["check"] == "protected_term" for v in verdict["violations"])


def test_acronym_is_not_counted_inside_unrelated_word():
    source = (
        "While the optical path remains passive, the HIL controller "
        "calibrates the detector under stable conditions."
    )
    candidate = source.replace("While", "Although")
    verdict = verify_rewrite(
        original=source,
        rewritten=candidate,
        protected_terms=["HIL"],
    )
    assert verdict["ok"] is True


def test_nested_protected_term_is_covered_by_validated_abbreviation_alias():
    source = (
        "Diffractive optical neural networks (DONNs) process light in parallel. "
        "Their passive layers may reduce latency."
    )
    candidate = source.replace(
        "Diffractive optical neural networks (DONNs)",
        "DONNs",
    )
    verdict = verify_rewrite(
        original=source,
        rewritten=candidate,
        protected_terms=["Optical Neural Networks"],
        protected_term_aliases={
            "Diffractive optical neural networks": "DONNs",
        },
    )
    assert verdict["ok"] is True, verdict["violations"]


def test_overlong_rewrite_is_rejected():
    filler = (
        " Additional supporting context sentences extend the discussion "
        "without changing any technical claim made anywhere above."
    ) * 3
    candidate = SOURCE + filler
    verdict = _verify(candidate)
    assert verdict["ok"] is False
    assert any(v["check"] == "length_ratio" for v in verdict["violations"])


def test_empty_rewrite_is_rejected():
    verdict = _verify("   ")
    assert verdict["ok"] is False
    assert any(v["check"] == "empty_rewrite" for v in verdict["violations"])


def test_first_sentence_rephrase_with_facts_intact_is_accepted():
    candidate = (
        "Flat optical components built from metasurfaces provide compact "
        "wavefront control [REF:s2:abc123]. The measured efficiency is "
        "approximately 85 percent at 450 nm, and the device may improve "
        "bandwidth toward 40 percent in future arrays."
    )
    verdict = _verify(candidate)
    assert verdict["ok"] is True, verdict["violations"]


# --- 大小写即事实（架构方补充：原 11 条全部只做增删变异，漏了纯大小写变异） ---
# _multiset 曾对所有键 .lower()，把 exact=True 悄悄降级成「忽略大小写的
# exact」。下面四条各钉一个量纲级事故，最后一条钉住对冲：限定词必须继续折叠。


def test_case_only_math_mutation_is_rejected():
    """$\\Lambda$（栅格周期）与 $\\lambda$（波长）是两个不同的物理量。"""

    source = (
        "The grating period $\\Lambda$ sets the diffraction order across the "
        "aperture and stays fixed under the paraxial approximation used here."
    )
    candidate = source.replace("$\\Lambda$", "$\\lambda$")
    verdict = verify_rewrite(original=source, rewritten=candidate)
    assert verdict["ok"] is False
    assert any(v["check"] == "math" for v in verdict["violations"])


def test_case_only_unit_mutation_is_rejected():
    """100 mW 与 100 MW 相差 10**6 倍，只差一个字母的大小写。"""

    source = (
        "The measured output reaches 100 mW under pulsed drive conditions "
        "reported for this device family [REF:s2:abc123]."
    )
    candidate = source.replace("100 mW", "100 MW")
    verdict = verify_rewrite(original=source, rewritten=candidate)
    assert verdict["ok"] is False
    assert any(v["check"] == "units" for v in verdict["violations"])


def test_case_only_time_unit_mutation_is_rejected():
    """5 ms 与 5 Ms 相差 10**9 倍。"""

    source = (
        "The thermal response takes 5 ms in the packaged prototype measured "
        "under ambient conditions [REF:s2:abc123]."
    )
    candidate = source.replace("5 ms", "5 Ms")
    verdict = verify_rewrite(original=source, rewritten=candidate)
    assert verdict["ok"] is False
    assert any(v["check"] == "units" for v in verdict["violations"])


def test_case_only_citation_mutation_is_rejected():
    """引用 id 大小写不同就是不同的证据条目，不得静默通过。"""

    source = (
        "This behaviour holds broadly across the reported device set and "
        "remains stable under fabrication tolerance [REF:s2:AbC123]."
    )
    candidate = source.replace("[REF:s2:AbC123]", "[REF:s2:abc123]")
    verdict = verify_rewrite(original=source, rewritten=candidate)
    assert verdict["ok"] is False
    assert any(v["check"] == "citations" for v in verdict["violations"])


def test_hedge_case_still_folds():
    """对冲测试：限定词必须继续忽略大小写，否则修复就改过头了。

    句首的 "May" 与句中的 "may" 表达同一份科学审慎；若这条变红，
    说明大小写敏感被错误地扩大到了 hedges 上。
    """

    source = (
        "May improve the yield of large arrays once the process window is "
        "characterised across wafers [REF:s2:abc123]."
    )
    candidate = (
        "This may improve the yield of large arrays once the process window "
        "is characterised across wafers [REF:s2:abc123]."
    )
    verdict = verify_rewrite(original=source, rewritten=candidate)
    assert verdict["ok"] is True, verdict["violations"]
