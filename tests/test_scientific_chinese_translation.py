from __future__ import annotations

import hashlib
import json
from pathlib import Path

from optomind_research.runtime.scientific_chinese_translator import (
    ScientificChineseTranslator,
    _normalize_scientific_terminology,
    _is_reference_only_block,
    _known_audit_false_positive,
    _protect_fragile_tokens,
    _restore_fragile_tokens,
    _validate_translation,
)


def test_deterministic_term_normalization_repairs_common_publication_terms() -> None:
    normalized, notes = _normalize_scientific_terminology(
        "伴随源方法 improves the footprint of the design."
    )

    assert normalized == "伴随法 improves the 器件占地尺寸 of the design."
    assert len(notes) == 2


def test_deterministic_term_normalization_repairs_translation_audit_terms() -> None:
    normalized, notes = _normalize_scientific_terminology(
        '“疫苗式”鲁棒性策略 and shallower designs'
    )

    assert normalized == '基于“免疫训练”的鲁棒性策略 and shallower 设计方案'
    assert notes == [
        '“疫苗式”鲁棒性策略->基于“免疫训练”的鲁棒性策略',
        "designs->设计方案",
    ]


def test_deterministic_term_normalization_repairs_vaccination_parenthetical() -> None:
    normalized, notes = _normalize_scientific_terminology(
        '诸如“疫苗”（vaccination）之类的鲁棒性策略'
    )

    assert normalized == '基于“免疫训练”的鲁棒性策略'
    assert notes == [
        '诸如“疫苗”（vaccination）之类的鲁棒性策略->基于“免疫训练”的鲁棒性策略'
    ]


def test_deterministic_term_normalization_repairs_vaccination_noun_variant() -> None:
    normalized, notes = _normalize_scientific_terminology(
        '例如，诸如“疫苗接种”（vaccination）之类的鲁棒性策略'
    )

    assert normalized == '例如，基于“免疫训练”（vaccination）的鲁棒性策略'
    assert notes == [
        '诸如“疫苗接种”（vaccination）之类的鲁棒性策略->基于“免疫训练”（vaccination）的鲁棒性策略'
    ]


def test_deterministic_term_normalization_repairs_short_vaccination_label() -> None:
    normalized, notes = _normalize_scientific_terminology(
        '称为“免疫”（vaccination）的训练方案'
    )

    assert normalized == '称为“免疫训练”（vaccination）的训练方案'
    assert notes == ['“免疫”（vaccination）->“免疫训练”（vaccination）']


def test_deterministic_term_normalization_repairs_quoted_vaccination_variants() -> None:
    normalized, notes = _normalize_scientific_terminology(
        '例如” 免疫” 等策略；称为“免疫训练式”（vaccination）的训练方案'
    )

    assert normalized == (
        '例如” 免疫训练” 等策略；称为“免疫训练”（vaccination）的训练方案'
    )
    assert notes == [
        '免疫->免疫训练',
        '免疫训练式->免疫训练',
    ]


def test_deterministic_term_normalization_does_not_rewrite_unrelated_immunity() -> None:
    normalized, notes = _normalize_scientific_terminology('“免疫”系统的响应')

    assert normalized == '“免疫”系统的响应'
    assert notes == []


def test_semantic_audit_does_not_reject_acronym_present_in_source() -> None:
    assert _known_audit_false_positive(
        "This establishes ODNNs as physically constrained systems.",
        "这确立了 ODNNs 是受物理约束的系统。",
        "major:the term 'ODNNs' does not appear in the source text",
    )


def test_semantic_audit_preserves_source_faithful_monochromatic_claim() -> None:
    assert _known_audit_false_positive(
        "Specialized kernel designs can achieve monochromatic imaging without losing color data by integrating hyperspectral techniques.",
        "特殊的核设计能够在不丢失颜色数据的前提下实现单色成像。",
        "major:logical contradiction with color data and hyperspectral techniques",
    )


def test_cached_translation_receives_deterministic_term_normalization(
    tmp_path: Path,
    monkeypatch,
) -> None:
    source = tmp_path / "review.md"
    source.write_text(
        "Robustness strategies such as vaccination improve deployment.",
        encoding="utf-8",
    )
    output_dir = tmp_path / "out"
    translator = ScientificChineseTranslator(
        source_markdown_path=source,
        output_dir=output_dir,
        semantic_audit=False,
    )
    units, _ = translator._units(source.read_text(encoding="utf-8"))
    unit = units[0]
    stale_target = "例如，采用“疫苗接种”（vaccination）的鲁棒性策略。"
    protected_target = _protect_fragile_tokens(stale_target)[0]
    prompt_hash = hashlib.sha256(
        translator._prompt().encode("utf-8")
    ).hexdigest()
    output_dir.mkdir(parents=True)
    (output_dir / "TRANSLATION_CACHE.json").write_text(
        json.dumps(
            {
                "prompt_hash": prompt_hash,
                "records": {
                    unit.cache_key: {
                        "translation": stale_target,
                        "protected_translation": protected_target,
                        "model_name": "cached-test",
                        "model_tier": "c_model",
                        "semantic_audit_passed": True,
                    }
                },
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    def fail_if_model_called(*args, **kwargs):
        raise AssertionError("cached unit should not call the model")

    monkeypatch.setattr(ScientificChineseTranslator, "_call_batch", fail_if_model_called)
    report = translator.translate()

    assert report["status"] == "completed"
    assert "疫苗接种" not in report["units"][0]["translation_preview"]
    assert "免疫训练" in report["units"][0]["translation_preview"]


def _validate(source: str, target: str) -> list[str]:
    protected_source, mapping = _protect_fragile_tokens(source)
    protected_target = target
    for token, value in mapping.items():
        protected_target = protected_target.replace(value, token)
    restored = _restore_fragile_tokens(protected_target, mapping)
    return _validate_translation(
        source,
        protected_source,
        protected_target,
        restored,
        mapping,
    )


def test_fragile_scientific_tokens_round_trip_exactly() -> None:
    source = (
        "The BIC mode reaches Q = $10^6$ at 1550 nm "
        "[REF:doi:10.1000/example]."
    )
    protected, mapping = _protect_fragile_tokens(source)

    assert "10^6" not in protected
    assert "1550" not in protected
    assert "[REF:" not in protected
    assert _restore_fragile_tokens(protected, mapping) == source


def test_dimensional_label_is_protected_as_one_scientific_token() -> None:
    source = (
        "The 3D-normalized correlation exceeds 0.95 "
        "[REF:CorpusId:263226765]."
    )
    protected, mapping = _protect_fragile_tokens(source)

    assert "3D-normalized" not in protected
    assert "3D" in mapping.values()
    assert _validate(
        source,
        "3D 归一化相关系数超过 0.95 [REF:CorpusId:263226765]。",
    ) == []
    assert "protected_token_mismatch" in _validate(
        source,
        "归一化相关系数超过 0.95 [REF:CorpusId:263226765]。",
    )
    assert "protected_token_mismatch" in _validate(
        source,
        (
            "3D 归一化相关系数超过 0.95，重复值为 3D 和 0.95 "
            "[REF:CorpusId:263226765]。"
        ),
    )


def test_verification_deferred_is_preserved_for_chinese_plan_readers() -> None:
    source = "All future simulations remain verification_deferred."
    protected, mapping = _protect_fragile_tokens(source)
    assert "verification_deferred" not in protected
    assert "verification_deferred" in mapping.values()
    assert _restore_fragile_tokens(protected, mapping) == source


def test_technical_english_terms_are_allowed_in_chinese() -> None:
    source = (
        "A quasi-BIC resonance improves the Q-factor and FOM for optical "
        "sensing [REF:doi:10.1000/example]."
    )
    target = (
        "quasi-BIC 共振提高了光学传感的 Q-factor 与 FOM "
        "[REF:doi:10.1000/example]。"
    )

    assert _validate(source, target) == []


def test_citation_or_number_changes_fail_closed() -> None:
    source = (
        "The resonance occurs at 1550 nm "
        "[REF:doi:10.1000/example]."
    )
    target = (
        "该共振出现在 1500 nm "
        "[REF:doi:10.1000/different]。"
    )

    errors = _validate(source, target)
    assert "citation_marker_mismatch" in errors
    assert "numeric_token_mismatch" in errors


def test_horizontal_rule_uses_deterministic_path(tmp_path: Path) -> None:
    source = tmp_path / "review.md"
    source.write_text("---\n\n## Heading\n", encoding="utf-8")
    translator = ScientificChineseTranslator(
        source_markdown_path=source,
        output_dir=tmp_path / "out",
    )

    units, _ = translator._units(source.read_text(encoding="utf-8"))

    assert units[0].status == "validated"
    assert units[0].model_tier == "deterministic"
    assert units[0].translated == "---"


def test_formula_indices_and_spelled_one_do_not_trigger_numeric_mismatch() -> None:
    source = (
        "The structure is $J_N = E_0 I + S$, where the shift operator has "
        "ones on the superdiagonal."
    )
    target = (
        "该结构为 $J_N = E_0 I + S$，其中移位算符的超对角线元素为 1。"
    )

    assert _validate(source, target) == []


def test_short_status_line_can_preserve_required_english_token() -> None:
    source = "**Verification status.** verification_deferred"
    target = "**验证状态。** verification_deferred"

    assert _validate(source, target) == []


def test_controlled_readiness_status_is_protected_in_short_line() -> None:
    source = "**Readiness.** needs_more_literature"
    target = "**成熟度。** needs_more_literature"

    assert _validate(source, target) == []


def test_multiplier_and_structural_ids_do_not_create_numeric_false_positive() -> None:
    protected_ids, id_mapping = _protect_fragile_tokens("P01 to WP03")
    source = "Training stops if runtime exceeds 10x across 3 random seeds."
    target = "当运行时间超过 10x 且覆盖 3 个随机种子时停止训练。"
    _, mapping = _protect_fragile_tokens(source)

    assert protected_ids == "P01 to WP03"
    assert id_mapping == {}
    assert "10x" in mapping.values()
    assert _validate(source, target) == []


def test_reference_only_blocks_are_copied_deterministically(
    tmp_path: Path,
) -> None:
    source_text = (
        "- [REF:doi:10.1000/example]\n"
        "- [REF:CorpusId:12345]\n"
    )
    source = tmp_path / "review.md"
    source.write_text(source_text, encoding="utf-8")
    translator = ScientificChineseTranslator(
        source_markdown_path=source,
        output_dir=tmp_path / "out",
    )

    assert _is_reference_only_block(source_text)
    units, _ = translator._units(source_text)
    assert units[0].status == "validated"
    assert units[0].model_tier == "deterministic"
    assert units[0].translated == source_text.strip()


def test_short_validation_failure_gets_bounded_repair_turn(
    tmp_path: Path,
    monkeypatch,
) -> None:
    source = tmp_path / "review.md"
    source.write_text(
        "The 3D-normalized correlation exceeds 0.95 "
        "[REF:CorpusId:263226765].",
        encoding="utf-8",
    )
    tiers: list[str] = []

    def fake_call_batch(self, batch, *, tier):
        tiers.append(tier)
        unit = batch[0]
        values = unit.protected_mapping
        token_for = {value: token for token, value in values.items()}
        if tier != "b_minus_model":
            # Deliberately omit the dimensional label while keeping all other
            # protected content; deterministic validation must reject it.
            text = (
                "归一化相关系数超过 "
                f"{token_for['0.95']} {token_for['[REF:CorpusId:263226765]']}。"
            )
        else:
            text = (
                f"{token_for['3D']} 归一化相关系数超过 "
                f"{token_for['0.95']} {token_for['[REF:CorpusId:263226765]']}。"
            )
        return {unit.unit_id: text}, {}

    monkeypatch.setattr(
        ScientificChineseTranslator,
        "_call_batch",
        fake_call_batch,
    )
    report = ScientificChineseTranslator(
        source_markdown_path=source,
        output_dir=tmp_path / "out",
        semantic_audit=False,
        cost_budget_cny=10.0,
    ).translate()

    assert report["status"] == "completed"
    assert tiers == ["c2_model", "c_model", "b_minus_model"]
    unit = report["units"][0]
    assert any(
        row.get("kind") == "deterministic_validation_repair"
        for row in unit["retry_history"]
    )
    assert "3D" in unit["translation_preview"]


def test_translation_fail_open_writes_partial_test_draft(
    tmp_path: Path,
    monkeypatch,
) -> None:
    source = tmp_path / "review.md"
    source.write_text(
        "The first mechanism is important for optical design.\n\n"
        "The second mechanism remains under active study.",
        encoding="utf-8",
    )

    def fake_call_batch(self, batch, *, tier):
        unit = batch[0]
        if unit.unit_id == "B0001":
            return {unit.unit_id: "第一种机制对光学设计很重要。"}, {}
        return {}, {}

    monkeypatch.setattr(ScientificChineseTranslator, "_call_batch", fake_call_batch)
    report = ScientificChineseTranslator(
        source_markdown_path=source,
        output_dir=tmp_path / "out",
        semantic_audit=False,
        cost_budget_cny=10.0,
    ).translate(allow_partial_output=True)

    assert report["status"] == "completed_with_warnings"
    assert report["partial_output"] is True
    translated = (tmp_path / "out" / "FINAL_REVIEW_ZH.md").read_text(
        encoding="utf-8"
    )
    assert "第一种机制对光学设计很重要" in translated
    assert "The second mechanism remains under active study" in translated


def test_semantic_repair_cannot_leave_deterministically_invalid_unit(
    tmp_path: Path,
    monkeypatch,
) -> None:
    source = tmp_path / "review.md"
    source.write_text(
        "The 3D-normalized correlation exceeds 0.95 "
        "[REF:CorpusId:263226765].",
        encoding="utf-8",
    )
    audit_calls = 0

    def fake_call_batch(self, batch, *, tier):
        unit = batch[0]
        token_for = {
            value: token for token, value in unit.protected_mapping.items()
        }
        if tier == "c_model":
            text = (
                "归一化相关系数超过 "
                f"{token_for['0.95']} {token_for['[REF:CorpusId:263226765]']}。"
            )
        else:
            text = (
                f"{token_for['3D']} 归一化相关系数超过 "
                f"{token_for['0.95']} {token_for['[REF:CorpusId:263226765]']}。"
            )
        return {unit.unit_id: text}, {}

    def fake_audit_batch(self, batch):
        nonlocal audit_calls
        audit_calls += 1
        if audit_calls == 1:
            return {batch[0].unit_id: "major:clarify the scientific wording"}, {}
        return {}, {}

    monkeypatch.setattr(
        ScientificChineseTranslator,
        "_call_batch",
        fake_call_batch,
    )
    monkeypatch.setattr(
        ScientificChineseTranslator,
        "_audit_batch",
        fake_audit_batch,
    )
    report = ScientificChineseTranslator(
        source_markdown_path=source,
        output_dir=tmp_path / "out",
        cost_budget_cny=10.0,
    ).translate()

    assert report["status"] == "completed"
    assert report["failed_unit_ids"] == []
    assert report["units"][0]["semantic_audit_passed"] is True
