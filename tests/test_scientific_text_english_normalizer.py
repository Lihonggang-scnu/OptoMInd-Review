from __future__ import annotations

import json
import re
from pathlib import Path

import optomind_research.scientific_text_english_normalizer as module
from optomind_research.scientific_text_english_normalizer import (
    ScientificTextEnglishNormalizer,
    ensure_english_strings,
)


_ENGLISH = (
    "This paragraph was faithfully translated into English for the "
    "retrieval, planning, and writing pipeline."
)


def _normalizer(
    tmp_path: Path,
    *,
    batch_size: int = 4,
    timeout_seconds: float = 60.0,
) -> ScientificTextEnglishNormalizer:
    prompt = tmp_path / "prompt.txt"
    prompt.write_text("Translate CJK paragraphs to English.", encoding="utf-8")
    return ScientificTextEnglishNormalizer(
        prompt_path=prompt,
        batch_size=batch_size,
        workers=2,
        timeout_seconds=timeout_seconds,
    )


def _payload_rows(messages: list) -> list[dict]:
    payload = json.loads(messages[1]["content"])
    return payload["paragraphs"]


def _valid_response(model_tier: str, messages: list) -> dict:
    rows = [
        {"ref": row["ref"], "text_en": _ENGLISH}
        for row in _payload_rows(messages)
    ]
    return {
        "content": json.dumps({"translations": rows}),
        "_llm_usage": {
            "model_name": model_tier,
            "success": True,
            "error_type": "",
        },
    }


def test_calls_are_bounded_to_two_batch_passes_not_per_paragraph(
    monkeypatch,
    tmp_path: Path,
) -> None:
    paragraphs = [
        f"中文段落编号{i}，描述超表面光学研究。"
        for i in range(12)
    ]
    calls: list[dict] = []

    def fake_chat(agent_name, messages, **kwargs):
        calls.append(dict(kwargs))
        rows = [
            # Deliberately non-English so local validation fails both passes.
            {"ref": row["ref"], "text_en": "仍然是中文内容。"}
            for row in _payload_rows(messages)
        ]
        return {
            "content": json.dumps({"translations": rows}, ensure_ascii=False),
            "_llm_usage": {
                "model_name": kwargs["model_tier"],
                "success": True,
                "error_type": "",
            },
        }

    monkeypatch.setattr(module, "call_qwen_chat", fake_chat)
    normalizer = _normalizer(tmp_path, batch_size=4, timeout_seconds=33.5)
    records = normalizer.normalize(paragraphs)

    assert len(records) == 12
    assert all(
        record.translation_status == "translation_failed_quarantined"
        for record in records
    )
    assert all(record.text_en == "" for record in records)
    assert all(
        record.source_text == source
        for record, source in zip(records, paragraphs)
    )

    audit = normalizer.last_audit
    assert audit["provider_call_count"] == 6
    assert audit["primary_batch_calls"] == 3
    assert audit["escalation_batch_calls"] == 3
    assert audit["max_translation_passes"] == 2
    assert audit["quarantined"] == 12
    assert len(calls) == 6
    # Every provider call carried a full batch of four paragraphs, proving the
    # escalation path is batched rather than one paragraph at a time.
    assert all(
        usage["paragraph_count"] == 4
        for usage in audit["usage"]
    )
    for kwargs in calls:
        assert kwargs["enable_thinking"] is False
        assert kwargs["allow_model_fallback"] is False
        assert kwargs["max_retries"] == 0
        assert kwargs["max_key_candidates"] == 1
        assert kwargs["max_transport_key_candidates"] == 1
        assert kwargs["timeout_seconds"] == 33.5


def test_successful_primary_items_are_retained_and_only_failed_are_escalated(
    monkeypatch,
    tmp_path: Path,
) -> None:
    paragraphs = [
        f"中文段落编号{i}，描述超表面光学研究。"
        for i in range(8)
    ]
    calls: list[dict] = []

    def fake_chat(agent_name, messages, **kwargs):
        tier = kwargs["model_tier"]
        calls.append({"tier": tier, "rows": _payload_rows(messages)})
        rows = []
        for row in _payload_rows(messages):
            marker = re.search(r"编号(\d)", row["source_text"])
            index = int(marker.group(1)) if marker else -1
            if tier == "standard_model":
                valid = index in {0, 1, 2, 3, 4}
            else:
                valid = True
            rows.append(
                {
                    "ref": row["ref"],
                    "text_en": _ENGLISH if valid else "保留中文内容。",
                }
            )
        return {
            "content": json.dumps({"translations": rows}, ensure_ascii=False),
            "_llm_usage": {
                "model_name": tier,
                "success": True,
                "error_type": "",
            },
        }

    monkeypatch.setattr(module, "call_qwen_chat", fake_chat)
    normalizer = _normalizer(tmp_path, batch_size=4)
    records = normalizer.normalize(paragraphs)

    assert [record.translation_status for record in records[:5]] == [
        "translated"
    ] * 5
    assert [
        record.translation_status for record in records[5:]
    ] == ["translated_after_escalation"] * 3
    assert all(record.text_en == _ENGLISH for record in records)
    assert normalizer.last_audit["provider_call_count"] == 3
    assert normalizer.last_audit["primary_batch_calls"] == 2
    assert normalizer.last_audit["escalation_batch_calls"] == 1
    assert normalizer.last_audit["quarantined"] == 0

    escalation_call = next(
        call for call in calls if call["tier"] == "premium_model"
    )
    escalated_markers = {
        re.search(r"编号(\d)", row["source_text"]).group(1)
        for row in escalation_call["rows"]
    }
    assert escalated_markers == {"5", "6", "7"}


def test_batch_exception_quarantines_without_blocking_the_paper(
    monkeypatch,
    tmp_path: Path,
) -> None:
    paragraphs = [
        f"中文段落编号{i}，描述超表面光学研究。"
        for i in range(8)
    ]
    primary_calls = 0

    def fake_chat(agent_name, messages, **kwargs):
        nonlocal primary_calls
        if kwargs["model_tier"] == "standard_model":
            primary_calls += 1
            if primary_calls == 2:
                raise RuntimeError("synthetic transport failure")
            return _valid_response(kwargs["model_tier"], messages)
        raise OSError("synthetic escalation provider failure")

    monkeypatch.setattr(module, "call_qwen_chat", fake_chat)
    normalizer = _normalizer(tmp_path, batch_size=4)
    records = normalizer.normalize(paragraphs)

    assert len(records) == 8
    assert [
        record.translation_status for record in records[:4]
    ] == ["translated"] * 4
    assert [
        record.translation_status for record in records[4:]
    ] == ["translation_failed_quarantined"] * 4
    assert all(record.text_en == "" for record in records[4:])
    assert all(
        "unresolved_after_bounded_passes" in record.validation_errors
        for record in records[4:]
    )
    assert normalizer.last_audit["provider_call_count"] == 3
    assert normalizer.last_audit["primary_batch_calls"] == 2
    assert normalizer.last_audit["escalation_batch_calls"] == 1


def test_provider_fallback_and_malformed_payload_are_quarantined(
    monkeypatch,
    tmp_path: Path,
) -> None:
    paragraphs = ["中文段落甲，描述超表面光学研究。"]
    tiers_seen: list[str] = []

    def fake_chat(agent_name, messages, **kwargs):
        tiers_seen.append(kwargs["model_tier"])
        content = (
            "[fallback] Qwen chat failed: simulated provider failure."
            if kwargs["model_tier"] == "standard_model"
            else "definitely not json"
        )
        return {
            "content": content,
            "_llm_usage": {
                "model_name": kwargs["model_tier"],
                "success": False,
                "error_type": "SimulatedProviderFailure",
            },
        }

    monkeypatch.setattr(module, "call_qwen_chat", fake_chat)
    normalizer = _normalizer(tmp_path, batch_size=4)
    records = normalizer.normalize(paragraphs)

    assert records[0].translation_status == "translation_failed_quarantined"
    assert records[0].text_en == ""
    assert records[0].source_text == paragraphs[0]
    assert tiers_seen == ["standard_model", "premium_model"]
    assert normalizer.last_audit["provider_call_count"] == 2
    assert normalizer.last_audit["usage"][0]["provider_fallback_returned"] is True
    assert normalizer.last_audit["usage"][1]["error_type"] == (
        "SimulatedProviderFailure"
    )


def test_english_only_input_makes_no_provider_calls(
    monkeypatch,
    tmp_path: Path,
) -> None:
    paragraphs = [
        "Pure English scientific sentence.",
        "Another English paragraph about optics.",
    ]

    def fail_if_called(agent_name, messages, **kwargs):
        raise AssertionError("English-only normalization must not call a model")

    monkeypatch.setattr(module, "call_qwen_chat", fail_if_called)
    normalizer = _normalizer(tmp_path, batch_size=4)
    records = normalizer.normalize(paragraphs)

    assert [
        record.translation_status for record in records
    ] == ["original_english"] * 2
    assert [record.text_en for record in records] == paragraphs
    assert [record.source_language for record in records] == ["english"] * 2
    assert normalizer.last_audit["translation_required"] == 0
    assert normalizer.last_audit["provider_call_count"] == 0
    assert normalizer.last_audit["primary_batch_calls"] == 0
    assert normalizer.last_audit["escalation_batch_calls"] == 0
    assert ensure_english_strings(paragraphs) == paragraphs
