"""Tests for chapter-parallel reviewer/author style governance."""

from __future__ import annotations

import json
import threading

from optomind_research.runtime.chapter_style_governance import (
    run_chapter_style_governance,
)


CHAPTER_ONE = (
    "While passive layers shape the wavefront, the network maps images. "
    "[REF:s2:a1] The system may operate without electronic intervention."
)
CHAPTER_TWO = (
    "Optical diffractive neural networks (ODNNs) process light in parallel. "
    "[REF:s2:b1] Their passive layers may reduce latency.\n\n"
    "Optical diffractive neural networks (ODNNs) also expose fabrication limits. "
    "[REF:s2:b2] The limits may affect deployment."
)


class _ChapterLLM:
    def __init__(self, *, bad_author: bool = False) -> None:
        self.events: list[tuple[str, str]] = []
        self.bad_author = bad_author
        self._lock = threading.Lock()

    def __call__(self, agent_name, system, payload, *, json_mode, model_tier=None):
        section_id = str(payload["section_id"])
        with self._lock:
            self.events.append((agent_name, section_id))
        if agent_name == "ChapterStyleReviewer":
            if section_id == "S01":
                edits = [
                    {
                        "paragraph_index": 1,
                        "action": "replace_first_sentence",
                        "original_text": CHAPTER_ONE.split(" [REF:")[0],
                        "replacement_text": (
                            "Passive layers shape the wavefront as the network maps images."
                        ),
                        "reason": "avoid a stock concessive opener",
                    }
                ]
            else:
                edits = [
                    {
                        "paragraph_index": 2,
                        "action": "replace_abbreviation",
                        "original_text": "Optical diffractive neural networks (ODNNs)",
                        "replacement_text": "ODNNs",
                        "reason": "the acronym was already defined in paragraph 1",
                    }
                ]
            return {
                "content": json.dumps({"edits": edits}),
                "_llm_usage": {
                    "model_name": "qwen3.5-plus",
                    "input_tokens": 100,
                    "output_tokens": 40,
                },
            }
        if agent_name == "ChapterStyleReviser":
            original = str(payload["original_chapter_text"])
            if self.bad_author:
                return {
                    "content": original + " Extra unsupported claim.",
                    "_llm_usage": {
                        "model_name": "qwen3.7-flash",
                        "input_tokens": 100,
                        "output_tokens": 40,
                    },
                }
            for edit in payload["reviewer_memo"]["edits"]:
                target = str(edit["original_text"])
                replacement = str(edit.get("replacement_text") or "")
                if edit.get("action") == "replace_abbreviation":
                    prefix, separator, suffix = original.rpartition(target)
                    original = (
                        prefix + separator + suffix
                        if not separator
                        else prefix + replacement + suffix
                    )
                else:
                    original = original.replace(target, replacement, 1)
            return {
                "content": original,
                "_llm_usage": {
                    "model_name": "qwen3.7-flash",
                    "input_tokens": 100,
                    "output_tokens": 40,
                },
            }
        raise AssertionError(agent_name)


def _manuscript() -> str:
    return (
        "## Abstract\n\nA short abstract that should not be sent to a chapter worker.\n\n"
        "## Foundations\n\n"
        + CHAPTER_ONE
        + "\n\n## Applications\n\n"
        + CHAPTER_TWO
        + "\n\n## Conclusion\n\nThe conclusion remains outside the body chapter selection."
    )


def test_chapters_run_in_parallel_but_reviewer_precedes_author() -> None:
    llm = _ChapterLLM()
    report = run_chapter_style_governance(
        _manuscript(),
        chapter_titles=["Foundations", "Applications"],
        chapter_ids={"Foundations": "S01", "Applications": "S02"},
        workers=2,
        protected_terms=[],
        llm_call=llm,
    )

    assert report["chapter_count"] == 2
    assert report["reviewer_calls"] == 2
    assert report["reviser_calls"] == 2
    assert report["chapters_changed"] == 2
    assert report["global_guard"]["ok"] is True
    assert report["improved"] is True
    for section_id in ("S01", "S02"):
        events = [agent for agent, observed_id in llm.events if observed_id == section_id]
        assert events == ["ChapterStyleReviewer", "ChapterStyleReviser"]
    assert "While passive" not in report["review_text"]
    assert "Optical diffractive neural networks (ODNNs) also" not in report["review_text"]
    assert "ODNNs also expose" in report["review_text"]


def test_author_output_outside_review_envelope_fails_open() -> None:
    llm = _ChapterLLM(bad_author=True)
    source = _manuscript()
    report = run_chapter_style_governance(
        source,
        chapter_titles=["Foundations"],
        chapter_ids={"Foundations": "S01"},
        workers=1,
        protected_terms=[],
        llm_call=llm,
    )

    assert report["review_text"] == source
    assert report["chapters_changed"] == 0
    assert report["chapters"][0]["status"] == "author_rejected_fail_open"
    assert report["chapters"][0]["author_rejected"] is True


def test_author_extra_unrequested_paragraph_is_discarded_safely() -> None:
    class _ExtraParagraphLLM(_ChapterLLM):
        def __call__(self, agent_name, system, payload, *, json_mode, model_tier=None):
            if agent_name == "ChapterStyleReviewer":
                return super().__call__(
                    agent_name,
                    system,
                    payload,
                    json_mode=json_mode,
                    model_tier=model_tier,
                )
            if agent_name == "ChapterStyleReviser":
                original = str(payload["original_chapter_text"])
                revised = original.replace(
                    "While passive layers shape the wavefront, the network maps images.",
                    "Passive layers shape the wavefront as the network maps images.",
                    1,
                )
                first_paragraph = revised
                return {
                    "content": json.dumps(
                        {
                            "revised_paragraphs": [
                                {"paragraph_index": 1, "text": first_paragraph},
                                {"paragraph_index": 99, "text": "Unsupported extra text."},
                            ]
                        }
                    ),
                    "_llm_usage": {
                        "model_name": "qwen3.7-flash",
                        "input_tokens": 100,
                        "output_tokens": 40,
                    },
                }
            raise AssertionError(agent_name)

    report = run_chapter_style_governance(
        _manuscript(),
        chapter_titles=["Foundations"],
        chapter_ids={"Foundations": "S01"},
        workers=1,
        protected_terms=[],
        llm_call=_ExtraParagraphLLM(),
    )

    assert report["chapters"][0]["status"] == "accepted"
    assert report["chapters"][0]["author_extra_paragraphs_discarded"] == [99]
    assert "Unsupported extra text." not in report["review_text"]


def test_fact_bearing_or_polarity_change_is_rejected() -> None:
    source = (
        "## Foundations\n\n"
        "While passive layers shape the wavefront, the network maps images "
        "[REF:s2:a1] at 450 nm. The system may operate without electronic intervention."
    )

    class _BadFactLLM(_ChapterLLM):
        def __call__(self, agent_name, system, payload, *, json_mode, model_tier=None):
            if agent_name == "ChapterStyleReviewer":
                return {
                    "content": json.dumps(
                        {
                            "edits": [
                                {
                                    "paragraph_index": 1,
                                    "action": "replace_first_sentence",
                                    "original_text": (
                                        "While passive layers shape the wavefront, the network maps images "
                                        "[REF:s2:a1] at 450 nm."
                                    ),
                                    "replacement_text": (
                                        "Passive layers shape the wavefront, and the network maps images."
                                    ),
                                }
                            ]
                        }
                    ),
                    "_llm_usage": {"model_name": "qwen3.5-plus", "input_tokens": 10, "output_tokens": 10},
                }
            if agent_name == "ChapterStyleReviser":
                return {
                    "content": payload["original_chapter_text"].replace(
                        "While passive layers shape the wavefront, the network maps images [REF:s2:a1] at 450 nm.",
                        "Passive layers shape the wavefront, and the network maps images.",
                    ),
                    "_llm_usage": {"model_name": "qwen3.7-flash", "input_tokens": 10, "output_tokens": 10},
                }
            raise AssertionError(agent_name)

    report = run_chapter_style_governance(
        source,
        chapter_titles=["Foundations"],
        chapter_ids={"Foundations": "S01"},
        workers=1,
        protected_terms=[],
        llm_call=_BadFactLLM(),
    )
    assert report["review_text"] == source
    assert report["chapters"][0]["status"] in {
        "author_rejected_fail_open",
        "unchanged",
    }
    assert report["changed"] is False


def test_disabled_governance_is_byte_identical() -> None:
    source = _manuscript()
    report = run_chapter_style_governance(source, enabled=False)
    assert report["review_text"] is source
    assert report["changed"] is False
    assert report["chapter_count"] == 2
