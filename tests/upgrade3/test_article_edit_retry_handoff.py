"""Offline retry handoff for the formal whole-article BODY editor."""

import json
from pathlib import Path

from optomind_research.runtime.upgrade3 import article_text_editor as editor
from optomind_research.runtime.upgrade3 import legacy_unit_route as route
from tests.test_legacy_unit_route import Factory as AuthorFactory
from tests.test_legacy_unit_route import book, counter


def _editor_response(*, replacement=True):
    changes = ([{"operation": "replace", "original_text": "旧句。",
                 "replacement_text": "新句。", "reason": "offline retry seam"}]
               if replacement else [])
    return {"content": json.dumps({"changes": changes, "unresolved_questions": []}, ensure_ascii=False),
            "complete": True, "finish_reason": "stop", "returned_model": "qwen3.5-plus",
            "usage": {}}


class EditorFactory:
    """A provider seam that fails once, then returns a complete edit response."""

    execution_mode = "live"

    def __init__(self, *, fail_first=False, replacement=True):
        self.fail_first = fail_first
        self.replacement = replacement
        self.provider_calls = 0
        self.invocations = 0

    def __call__(self, role, path, profile):
        assert role == "full"
        assert path.name == "full" or path.name.startswith("attempt_")
        assert profile["model"] == "qwen3.5-plus"
        assert profile["max_output_tokens"] == 24576
        assert profile["thinking_budget"] == 16384
        self.provider_calls += 1
        should_fail = self.fail_first and self.provider_calls == 1

        def call(messages, **kwargs):
            self.invocations += 1
            if should_fail:
                raise RuntimeError("controlled_article_edit_failure")
            return _editor_response(replacement=self.replacement)

        return call


def _run_editor(tmp_path, factory, *, retry_failed=False, out_name="edit"):
    draft = tmp_path / "draft.md"
    if not draft.exists():
        draft.write_text("章节正文。旧句。\n", encoding="utf-8")
    return editor.run_text_edit_stage(
        draft_path=draft,
        chapter_roles=[],
        out_dir=tmp_path / out_name,
        client_factory=factory,
        run=True,
        token_counter=counter,
        retry_failed=retry_failed,
    )


def test_article_editor_failed_attempt_requires_explicit_retry_and_reuses_saved_raw(tmp_path):
    factory = EditorFactory(fail_first=True)

    failed = _run_editor(tmp_path, factory)
    assert failed["status"] == "pending"
    assert failed["call_state"] == "failed_saved_attempt"
    assert failed["model_calls"] == 1
    assert factory.provider_calls == factory.invocations == 1
    stage = Path(failed["output_dir"])
    assert (stage / "full" / "REQUEST.json").is_file()
    assert (stage / "full" / "STAGE_ERROR.json").is_file()
    assert not (stage / "full" / "RAW_RESPONSE.json").exists()

    # A normal resume sees the durable failure and does not silently buy it.
    waiting = _run_editor(tmp_path, factory)
    assert waiting["status"] == "pending"
    assert waiting["call_state"] == "pending_existing_attempt"
    assert waiting["model_calls"] == 0
    assert factory.provider_calls == factory.invocations == 1

    recovered = _run_editor(tmp_path, factory, retry_failed=True)
    assert recovered["status"] == "edited"
    assert recovered["call_state"] == "returned"
    assert recovered["model_calls"] == 1
    assert factory.provider_calls == factory.invocations == 2
    assert (stage / "full" / "retries" / "attempt_002" / "RAW_RESPONSE.json").is_file()
    assert (stage / "CANDIDATE_HANDLES.md").read_text(encoding="utf-8") == "章节正文。新句。\n"

    # A completed editor result is a cache hit even when retry_failed is set.
    again = _run_editor(tmp_path, factory, retry_failed=True)
    assert again["status"] == "edited"
    assert again["cache_hit"] is True
    assert again["model_calls"] == 0
    assert factory.provider_calls == factory.invocations == 2

    # Removing only the report/seal exercises free parsing of an already saved
    # RAW response; no provider may be constructed for this recovery.
    (stage / "TEXT_EDIT_REPORT.json").unlink()
    (stage / "EDIT_RESULT_SEAL.json").unlink()
    raw_bytes = (stage / "full" / "retries" / "attempt_002" / "RAW_RESPONSE.json").read_bytes()
    free = _run_editor(tmp_path, factory, retry_failed=True)
    assert free["status"] == "edited"
    assert free["call_state"] == "cache_hit"
    assert free["model_calls"] == 0
    assert factory.provider_calls == factory.invocations == 2
    assert (stage / "full" / "retries" / "attempt_002" / "RAW_RESPONSE.json").read_bytes() == raw_bytes


def test_legacy_route_passes_retry_failed_to_formal_article_editor(tmp_path):
    author = AuthorFactory()
    editor_factory = EditorFactory(fail_first=True, replacement=False)

    class CompositeFactory:
        execution_mode = "live"

        def __call__(self, role, path, profile):
            if role == "full":
                return editor_factory(role, path, profile)
            return author(role, path, profile)

    factory = CompositeFactory()
    first = route.run_legacy_units(
        book(1), output_dir=tmp_path, run=True, client_factory=factory,
        token_counter=counter, article_edit=True)
    assert first["article_edit"]["status"] == "pending"
    assert editor_factory.provider_calls == editor_factory.invocations == 1

    waiting = route.run_legacy_units(
        book(1), output_dir=tmp_path, run=True, client_factory=factory,
        token_counter=counter, article_edit=True)
    assert waiting["article_edit"]["status"] == "pending"
    assert waiting["article_edit"]["call_state"] == "pending_existing_attempt"
    assert editor_factory.provider_calls == editor_factory.invocations == 1

    recovered = route.run_legacy_units(
        book(1), output_dir=tmp_path, run=True, retry_failed=True,
        client_factory=factory, token_counter=counter, article_edit=True)
    assert recovered["article_edit"]["status"] == "no_change"
    assert recovered["article_edit"]["model_calls"] == 1
    assert editor_factory.provider_calls == editor_factory.invocations == 2

    cached = route.run_legacy_units(
        book(1), output_dir=tmp_path, run=True, retry_failed=True,
        client_factory=factory, token_counter=counter, article_edit=True)
    assert cached["article_edit"]["cache_hit"] is True
    assert cached["article_edit"]["model_calls"] == 0
    assert editor_factory.provider_calls == editor_factory.invocations == 2
