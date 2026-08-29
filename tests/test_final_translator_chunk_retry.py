from __future__ import annotations

import pytest

from optomind_research.review_writer import FinalTranslator, SectionDraft


def test_numeric_mismatch_is_corrected_with_exactly_one_bounded_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import optomind_research.review_writer as module

    source = "The measured value is 123.45 and the limit is 67.8 [1]."
    invalid = "测量值为 999.99，限值为 67.8 [1]。"
    corrected = "测量值为 123.45，限值为 67.8 [1]。"
    responses = iter([invalid, corrected])
    calls: list[tuple[str, dict, str]] = []

    def fake(agent_name: str, messages: list[dict], **kwargs):
        calls.append((agent_name, kwargs, messages[-1]["content"]))
        return {"content": next(responses)}

    monkeypatch.setattr(module, "call_qwen_chat", fake)
    draft = FinalTranslator(
        model_tier="c_model", real_llm=True
    ).translate(SectionDraft("S01", english_text=source))

    assert [name for name, _, _ in calls] == [
        "FinalTranslatorAgent",
        "FinalTranslatorCorrectionAgent",
    ]
    assert draft.chinese_text == corrected
    assert draft.status == "final"

    _, correction_kwargs, correction_prompt = calls[1]
    assert correction_kwargs["temperature"] == 0.0
    assert correction_kwargs["enable_thinking"] is False
    assert correction_kwargs["model_tier"] == "c_model"
    assert correction_kwargs["allow_model_fallback"] is False
    assert correction_kwargs["timeout_seconds"] == 180
    assert correction_kwargs["max_retries"] == 0
    assert source in correction_prompt
    assert invalid in correction_prompt
    assert "numeric literals" in correction_prompt
    assert "REF/citation markers" in correction_prompt


def test_persistent_numeric_mismatch_raises_after_exactly_two_calls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import optomind_research.review_writer as module

    source = "The measured value is 123.45 [1]."
    invalid = "测量值为 999.99 [1]。"
    calls: list[str] = []

    def fake(agent_name: str, *_args, **_kwargs):
        calls.append(agent_name)
        return {"content": invalid}

    monkeypatch.setattr(module, "call_qwen_chat", fake)
    with pytest.raises(RuntimeError, match="numeric literals"):
        FinalTranslator(real_llm=True).translate(
            SectionDraft("S01", english_text=source)
        )

    assert calls == [
        "FinalTranslatorAgent",
        "FinalTranslatorCorrectionAgent",
    ]


def test_already_validated_chunk_is_not_retranslated_when_later_chunk_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import optomind_research.review_writer as module

    first_source = "First paragraph measures 123.45. " * 100
    second_source = "Second paragraph measures 67.8. " * 100
    source = first_source + "\n\n" + second_source
    first_good = "第一段测量值为 123.45。" * 100
    second_invalid = "第二段测量值为 999.99。" * 100
    second_good = "第二段测量值为 67.8。" * 100
    responses = iter([first_good, second_invalid, second_good])
    calls: list[str] = []

    def fake(agent_name: str, messages: list[dict], **_kwargs):
        calls.append(messages[-1]["content"])
        return {"content": next(responses)}

    monkeypatch.setattr(module, "call_qwen_chat", fake)
    draft = FinalTranslator(real_llm=True).translate(
        SectionDraft("S01", english_text=source)
    )

    assert len(calls) == 3
    assert sum(1 for content in calls if first_source.strip() in content) == 1
    assert sum(1 for content in calls if second_source.strip() in content) == 2
    assert draft.status == "final"
    assert "123.45" in draft.chinese_text
    assert "67.8" in draft.chinese_text
    assert "999.99" not in draft.chinese_text


def test_numeric_validation_error_identifies_source_and_target_sets() -> None:
    with pytest.raises(
        RuntimeError,
        match=r"source=\['123.45'\], target=\['999.99'\]",
    ):
        FinalTranslator._validate_translation_chunk(
            "The value is 123.45.", "值为 999.99。"
        )


def test_citation_id_digits_do_not_enter_or_mask_numeric_sets() -> None:
    source = "The measured value is 123.45 [REF:paper1234:c5678]."
    valid = "测得值为 123.45 [REF:paper1234:c5678]。"
    FinalTranslator._validate_translation_chunk(source, valid)

    missing_prose_number = "测得值保持不变 [REF:paper1234:c5678]。"
    with pytest.raises(RuntimeError, match="numeric literals"):
        FinalTranslator._validate_translation_chunk(
            source, missing_prose_number
        )

    citation_only_source = "A claim [REF:paper1234:c5678]."
    citation_only_target = "某论断 [REF:paper1234:c5678]。"
    FinalTranslator._validate_translation_chunk(
        citation_only_source, citation_only_target
    )


def test_chunking_4500_keeps_paragraph_alignment_without_text_loss() -> None:
    first = "First paragraph sentence with evidence and mechanism 123.45. " * 60
    second = "Second paragraph sentence with boundary conditions 67.8. " * 60
    third = "Third paragraph sentence comparing results and limits 9.9. " * 70
    source = first + "\n\n" + second + "\n\n" + third

    chunks = FinalTranslator._split_translation_chunks(source)

    assert len(source) > 10000
    assert len(chunks) == 3
    assert all(len(chunk) <= FinalTranslator._MAX_CHUNK_CHARS for chunk in chunks)
    assert chunks == [first.strip(), second.strip(), third.strip()]
    expected = "\n\n".join(part.strip() for part in source.split("\n\n"))
    assert "\n\n".join(chunks) == expected


def test_exponent_notation_semantic_chinese_is_accepted() -> None:
    source = "The embedding space has 10^4 dimensions [1]."
    FinalTranslator._validate_translation_chunk(
        source, "该嵌入空间具有 10 的四次方个维度，且数量与源文本一致 [1]。"
    )
    FinalTranslator._validate_translation_chunk(
        source, "该嵌入空间具有 10⁴ 个维度，数量与源文本一致 [1]。"
    )
    FinalTranslator._validate_translation_chunk(
        source, "该嵌入空间具有 10 的 4 次方个维度，数量与源文本一致 [1]。"
    )
    FinalTranslator._validate_translation_chunk(
        source, "该嵌入空间具有 10^4 个维度，数量与源文本一致 [1]。"
    )


def test_exponent_quantity_omitted_or_changed_is_rejected() -> None:
    source = "The system used 10^4 cells [2]."
    with pytest.raises(RuntimeError, match="numeric literals"):
        FinalTranslator._validate_translation_chunk(
            source, "系统使用了细胞 [2]。"
        )
    with pytest.raises(RuntimeError, match="numeric literals"):
        FinalTranslator._validate_translation_chunk(
            source, "系统使用了 10 的五次方细胞 [2]。"
        )
    with pytest.raises(RuntimeError, match="numeric literals"):
        FinalTranslator._validate_translation_chunk(
            source, "系统使用了 10⁵ 细胞 [2]。"
        )


def test_real_measurements_and_percentages_remain_strict() -> None:
    source = "Accuracy rose to 58% and the loss was 0.052 [3]."
    valid = "准确率提升至 58%，损失为 0.052 [3]。"
    FinalTranslator._validate_translation_chunk(source, valid)
    with pytest.raises(RuntimeError, match="numeric literals"):
        FinalTranslator._validate_translation_chunk(
            source, "准确率有所提升，损失较低 [3]。"
        )
    with pytest.raises(RuntimeError, match="numeric literals"):
        FinalTranslator._validate_translation_chunk(
            source, "准确率提升至 58% [3]。"
        )


def test_citation_markers_remain_exact_in_exponent_translation() -> None:
    source = "The space has 10^4 dimensions [7]."
    with pytest.raises(RuntimeError, match="citation markers"):
        FinalTranslator._validate_translation_chunk(
            source, "空间具有 10 的四次方维度 [8]。"
        )
    FinalTranslator._validate_translation_chunk(
        source, "该空间具有 10 的四次方个维度，数量与源文本一致 [7]。"
    )


def test_chinese_magnitude_wan_equivalent_quantity_is_accepted() -> None:
    source = "The space has 10^4 dimensions and 58% failures [1]."
    FinalTranslator._validate_translation_chunk(
        source, "该空间具有一万个维度，失败率为 58% [1]。"
    )
    FinalTranslator._validate_translation_chunk(
        source, "该空间具有万个维度，失败率为 58% [1]。"
    )
    FinalTranslator._validate_translation_chunk(
        source, "该空间具有 1 万个维度，失败率为 58% [1]。"
    )

    FinalTranslator._validate_translation_chunk(
        "The system used 10^5 cells [2].",
        "该系统使用了十万个细胞，数量与源文本完全一致 [2]。",
    )
    FinalTranslator._validate_translation_chunk(
        "The system used 10^6 cells [2].",
        "该系统使用了 100 万个细胞，数量与源文本完全一致 [2]。",
    )
    FinalTranslator._validate_translation_chunk(
        "The system used 10^8 cells [2].",
        "该系统使用了一亿个细胞，数量与源文本完全一致 [2]。",
    )


def test_chinese_magnitude_omitted_or_changed_still_fails() -> None:
    source = "The space has 10^4 dimensions and 58% failures [1]."
    with pytest.raises(RuntimeError, match="numeric literals"):
        FinalTranslator._validate_translation_chunk(
            source, "该空间具有维度，失败率为 58% [1]。"
        )
    with pytest.raises(RuntimeError, match="numeric literals"):
        FinalTranslator._validate_translation_chunk(
            source, "该空间具有十万个维度，失败率为 58% [1]。"
        )
    with pytest.raises(RuntimeError, match="numeric literals"):
        FinalTranslator._validate_translation_chunk(
            "The system used 10^5 cells [2].",
            "该系统使用了 10^4 个细胞 [2]。",
        )


def test_translation_chunk_language_strictness_is_optional() -> None:
    source = "The value is 123.45 [1]."
    non_chinese = "The value is 123.45 [1]."
    with pytest.raises(RuntimeError, match="no Chinese text"):
        FinalTranslator._validate_translation_chunk(source, non_chinese)
    FinalTranslator._validate_translation_chunk(
        source, non_chinese, require_chinese=False
    )
    with pytest.raises(RuntimeError, match="numeric literals"):
        FinalTranslator._validate_translation_chunk(
            source,
            "The value is 999.99 [1].",
            require_chinese=False,
        )
    with pytest.raises(RuntimeError, match="citation markers"):
        FinalTranslator._validate_translation_chunk(
            source,
            "The value is 123.45 [2].",
            require_chinese=False,
        )


def test_dimension_and_model_tokens_are_not_asserted() -> None:
    source = "The solver runs large-scale 3D simulations [1]."
    FinalTranslator._validate_translation_chunk(
        source, "该求解器运行大规模三维仿真，规模与源文本一致 [1]。"
    )
    FinalTranslator._validate_translation_chunk(
        source, "该求解器运行大规模仿真，规模与源文本一致 [1]。"
    )

    model_source = "Using FDTD2024 v2 with 58% accuracy [1]."
    FinalTranslator._validate_translation_chunk(
        model_source, "使用 FDTD2024 v2，准确率达到 58%，与源文本一致 [1]。"
    )
    with pytest.raises(RuntimeError, match="numeric literals"):
        FinalTranslator._validate_translation_chunk(
            model_source, "使用 FDTD2024 v2，准确率较高，未保留具体数值 [1]。"
        )


def test_unit_and_measurement_numbers_remain_strict() -> None:
    spaced = "The spacing is 3.5 nm and loss is 0.052 [1]."
    FinalTranslator._validate_translation_chunk(
        spaced, "间距为 3.5 nm，损失为 0.052，与源文本一致 [1]。"
    )
    attached = "The spacing is 3.5nm and loss is 0.052 [1]."
    FinalTranslator._validate_translation_chunk(
        attached, "间距为 3.5nm，损失为 0.052，与源文本一致 [1]。"
    )
    with pytest.raises(RuntimeError, match="numeric literals"):
        FinalTranslator._validate_translation_chunk(
            spaced, "间距较密，损失为 0.052，未保留间距数值 [1]。"
        )

    percent_source = "Coverage reached 58% [1]."
    FinalTranslator._validate_translation_chunk(
        percent_source, "覆盖率达到了 58%，与源文本一致 [1]。"
    )
    with pytest.raises(RuntimeError, match="numeric literals"):
        FinalTranslator._validate_translation_chunk(
            percent_source, "覆盖率较高，未保留具体百分比 [1]。"
        )
