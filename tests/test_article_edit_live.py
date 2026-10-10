"""Offline whole-article edit, strict recovery and final citation tests."""
import json
from pathlib import Path

import pytest

from optomind_research.runtime.upgrade3 import article_text_editor as editor
from optomind_research.runtime.upgrade3 import legacy_unit_route as route
from tests.test_legacy_unit_route import book, counter, Factory


CATALOG = [{"references": [
    {"handles": ["P0001"], "paper_id": "one", "title": "One"},
    {"handles": ["P0002"], "paper_id": "two", "title": "Two"}]}]


class EditFactory:
    execution_mode = "live"
    def __init__(self, changes=None, complete=True):
        self.calls = 0
        self.changes = changes or []
        self.complete = complete
    def __call__(self, role, step, profile):
        assert profile["thinking_budget"] == 16384
        assert profile["max_output_tokens"] == 24576
        assert profile["stream"] and not profile["json_mode"]
        def call(messages, **kwargs):
            self.calls += 1
            return {"content": json.dumps({"changes": self.changes, "unresolved_questions": []}),
                "complete": self.complete, "finish_reason": "stop" if self.complete else "length",
                "returned_model": "qwen3.5-plus", "usage": {}}
        return call


def stage(tmp_path, factory, text="Body [P0002].\n\n## 参考文献（原始 handle）\n[P0001] One\n[P0002] Two\n"):
    draft = tmp_path / "draft.md"
    draft.write_text(text, encoding="utf-8")
    return editor.run_text_edit_stage(draft_path=draft, chapter_roles=[], out_dir=tmp_path / "edit",
        client_factory=factory, run=True, token_counter=counter, identity_catalogs=CATALOG)


def test_edits_use_original_snapshot_and_overlap_is_reported():
    changes = [{"operation": "replace", "original_text": "A.", "replacement_text": "X."},
               {"operation": "replace", "original_text": "X.", "replacement_text": "Y."}]
    text, applied, skipped = editor.apply_text_edits("A. B.", changes)
    assert text == "X. B." and len(applied) == 1
    assert skipped[0]["reason"] == "target_occurs_0_times"
    text, applied, skipped = editor.apply_text_edits("A. B.", [changes[0],
        {"operation": "remove", "original_text": "A. B."}])
    assert text == "X. B." and skipped[0]["reason"] == "overlapping_target"


def test_elsewhere_replacement_does_not_prove_missing_target_was_applied():
    change = {"operation": "replace", "original_text": "missing target", "replacement_text": "elsewhere"}
    text, applied, skipped = editor.apply_text_edits("elsewhere survives", [change])
    assert not applied and skipped[0]["reason"] == "target_occurs_0_times"
    append = {"operation": "replace", "original_text": "Target.", "replacement_text": "Target.Addition."}
    first, applied, _ = editor.apply_text_edits("Addition. Target.", [append])
    second, applied_again, skipped = editor.apply_text_edits(first, [append])
    assert first == second and not applied_again and skipped[0]["reason"] == "already_applied"


@pytest.mark.parametrize("proposals", [{"changes": ["bad"]}, {"changes": "bad"},
    {"changes": [{"operation": "replace", "original_text": 12, "replacement_text": "ok"}]}])
def test_malformed_proposal_is_explicit_error(proposals):
    with pytest.raises(editor.TextEditError):
        editor.parse_edit_proposals(proposals)


def test_live_no_change_numbering_excludes_old_bibliography_and_reuses_cache(tmp_path):
    factory = EditFactory()
    first = stage(tmp_path, factory)
    assert first["status"] == "no_change" and first["model_calls"] == factory.calls == 1
    refs = route._read(Path(first["numbering"]["references_path"]))["references"]
    assert [row["handles"] for row in refs] == [["P0002"]]
    assert "Body [1]." in Path(first["numbering"]["reader_draft"]).read_text(encoding="utf-8")
    assert Path(first["source_draft"]).read_bytes() == Path(first["original_snapshot"]).read_bytes()
    assert Path(first["edited_draft"]).read_bytes() == Path(first["original_snapshot"]).read_bytes()
    second = stage(tmp_path, factory)
    assert second["cache_hit"] and second["model_calls"] == 0 and factory.calls == 1


def test_raw_sse_only_recovers_without_constructing_factory(tmp_path):
    first = stage(tmp_path, EditFactory())
    target = Path(first["output_dir"])
    (target / "EDIT_RESULT_SEAL.json").unlink()
    (target / "TEXT_EDIT_REPORT.json").unlink()
    raw = route._read(target / "full" / "RAW_RESPONSE.json")
    (target / "full" / "RAW_RESPONSE.json").unlink()
    stream = target / "full" / "transport" / "saved.sse.partial"
    stream.parent.mkdir()
    event = {"model": "qwen3.5-plus", "choices": [{"delta": {"content": raw["content"]}, "finish_reason": "stop"}]}
    stream.write_bytes(("data: " + json.dumps(event) + "\n\ndata: [DONE]\n\n").encode())
    class NoFactory:
        def __call__(self, *args, **kwargs):
            pytest.fail("saved SSE recovery constructed provider")
    restored = stage(tmp_path, NoFactory())
    assert restored["status"] == "no_change" and restored["model_calls"] == 0


def test_partial_is_saved_and_never_rebought(tmp_path):
    factory = EditFactory(complete=False)
    first = stage(tmp_path, factory)
    assert first["status"] == "pending" and first["model_calls"] == 1
    assert Path(first["raw_response"]).is_file()
    assert "edited_draft" not in first
    second = stage(tmp_path, factory)
    assert second["status"] == "pending" and second["model_calls"] == 0 and factory.calls == 1


def test_input_or_request_change_is_rejected_without_new_call(tmp_path):
    factory = EditFactory()
    first = stage(tmp_path, factory)
    with pytest.raises(editor.TextEditError, match="identity_changed"):
        stage(tmp_path, factory, text="Changed manuscript [P0002].")
    assert factory.calls == 1


def test_source_identity_loss_reported_and_not_forced_back(tmp_path):
    factory = EditFactory([{"operation": "remove", "original_text": "First [P0001].\n"}])
    result = stage(tmp_path, factory, text="First [P0001].\nSecond [P0002].\n")
    assert result["status"] == "edited"
    assert result["source_identity_changes"]["removed"] == ["pid:one"]
    assert result["source_identity_changes"]["after_count"] == 1


@pytest.mark.parametrize("heading", ["## References", "## Bibliography", "# REFERENCES"])
def test_numbering_projection_handles_english_bibliography(heading):
    projection = editor._numbering_projection("Body [P0002].\n\n" + heading + "\n[P0001] old\n")
    assert "P0002" in projection and "P0001" not in projection


def test_route_subset_skips_article_and_full_book_runs_one_edit(tmp_path):
    author, edit = Factory(), EditFactory()
    def factory(role, path, profile):
        return edit(role, path, profile) if path.name == "full" else author(role, path, profile)
    factory.execution_mode = "live"
    subset = route.run_legacy_units(book(), output_dir=tmp_path / "subset", run=True,
        client_factory=factory, token_counter=counter, only_units=["CH1:U0"], article_edit=True)
    assert subset["article_edit"]["status"] == "skipped_selected_subset" and edit.calls == 0
    full = route.run_legacy_units(book(), output_dir=tmp_path / "full", run=True,
        client_factory=factory, token_counter=counter, article_edit=True)
    assert full["article_edit"]["status"] == "no_change" and edit.calls == 1
    resumed = route.run_legacy_units(book(), output_dir=tmp_path / "full", run=True,
        client_factory=factory, token_counter=counter, article_edit=True)
    assert resumed["model_calls"] == 0 and edit.calls == 1


@pytest.mark.parametrize("quality_status,expected_edit", [("pending", False), ("assessed_with_pending", True)])
def test_article_gate_distinguishes_unfinished_quality_from_retained_pending(tmp_path, monkeypatch, quality_status, expected_edit):
    from optomind_research.runtime.upgrade3 import unit_realization
    def quality(view, **kwargs):
        return {"status": quality_status, "model_calls": 0, "body_markdown": "Evidence [P0001].",
            "output_dir": str(kwargs["output_dir"]),
            "pending_problems": [{"code": "retained_quality_question"}]}
    monkeypatch.setattr(unit_realization, "run_unit_quality", quality)
    author, edit = Factory(), EditFactory()
    def factory(role, path, profile):
        return edit(role, path, profile) if path.name == "full" else author(role, path, profile)
    factory.execution_mode = "live"
    result = route.run_legacy_units(book(1), output_dir=tmp_path, run=True,
        client_factory=factory, token_counter=counter, quality_control=True, article_edit=True)
    assert edit.calls == int(expected_edit)
    assert result["article_edit"]["status"] == ("no_change" if expected_edit else "skipped_quality_pending")
    assert result["status"] == "restricted_draft"
    assert result["units"][0]["pending_problems"]


def test_changed_quality_body_creates_new_editor_cache_without_rebuying_author(tmp_path, monkeypatch):
    from optomind_research.runtime.upgrade3 import unit_realization
    body = ["First quality body [P0001]."]
    def quality(view, **kwargs):
        return {"status": "assessed_pending_human_review", "model_calls": 0, "body_markdown": body[0],
            "output_dir": str(Path(kwargs["output_dir"]) / route._hash(body[0])), "pending_problems": []}
    monkeypatch.setattr(unit_realization, "run_unit_quality", quality)
    author, edit = Factory(), EditFactory()
    def factory(role, path, profile):
        return edit(role, path, profile) if path.name == "full" else author(role, path, profile)
    factory.execution_mode = "live"
    def run():
        return route.run_legacy_units(book(1), output_dir=tmp_path, run=True,
            client_factory=factory, token_counter=counter, quality_control=True, article_edit=True)
    first = run()
    body[0] = "Updated quality body [P0001]."
    second = run()
    assert first["article_edit"]["output_dir"] != second["article_edit"]["output_dir"]
    assert author.calls == 1 and edit.calls == 2
    assert Path(first["article_edit"]["edited_draft"]).is_file()
    third = run()
    assert third["model_calls"] == 0 and author.calls == 1 and edit.calls == 2
