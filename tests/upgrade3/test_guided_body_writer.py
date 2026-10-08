"""Offline guide runtime: actual prefixes, bounded reads, atomic gaps, recovery."""
from copy import deepcopy
import json
from pathlib import Path
import socket

import pytest

from optomind_research.runtime.upgrade3 import guided_body_writer as engine
from optomind_research.runtime.upgrade3.fullbody_contracts import build_fullbody_input
from optomind_research.runtime.upgrade3.writer_candidates_contracts import INPUT_SCHEMA


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def denied(*args, **kwargs):
        raise AssertionError("network prohibited")
    monkeypatch.setattr(socket, "create_connection", denied)
    monkeypatch.setattr(socket.socket, "connect", denied)


@pytest.fixture
def book():
    return build_fullbody_input([dict(schema_version=INPUT_SCHEMA, chapter_id=f"C{i}", language="en",
        chapter_frame={"title": f"Chapter {i}", "research_question": "Explain the observations"}, other_chapters=[],
        units=[dict(unit_id="U", focus="Explain", paragraph_tasks=[dict(paragraph_id="T1", point="Original task",
            source_handles=[f"P{i:04d}"])], table_tasks=[], owner_unit_context={}, source_handles=[f"P{i:04d}"])],
        sources=[dict(source_handle=f"P{i:04d}", paper_id=f"paper-{i}", study_summary_A={"finding": f"INTACT_EVIDENCE_{i}"})],
        chapter_tool_materials=[], provenance={}, warnings=[]) for i in (1, 2, 3)])


@pytest.fixture
def guide(book):
    return {"manuscript_guide": "Explain observations as a connected argument, preserving conditions.",
        "chapters": [{"chapter_id": row["chapter_id"], "title": f"Chapter {i}",
            "writing_arrangement": "Develop the main relationship in natural prose.",
            "required_content": ["Explain the important boundary."]} for i, row in enumerate(book["chapters"], 1)]}


@pytest.fixture
def config():
    return {"writer": {"model": "qwen3.5-plus", "thinking": True, "thinking_budget": 1024,
        "max_output_tokens": 4096, "stream": True, "json_mode": False, "timeout_seconds": 15,
        "stream_overall_timeout_seconds": 30, "prompt_token_multiplier": 1.0, "prompt_token_framing_margin": 0}}


def meter(raw, messages):
    return 100


class Factory:
    execution_mode = "recording"
    fixture_sha256 = "guided-offline-v1"

    def __init__(self, mode="normal"):
        self.mode, self.calls = mode, []

    def __call__(self, role, directory, profile):
        def call(messages, **kwargs):
            payload = json.loads(messages[-1]["content"])
            self.calls.append(payload)
            assignment = payload["chapter_assignment"]
            chapter = assignment["chapter_id"]
            assert set(payload) == {"manuscript_guide", "chapter_assignment", "materials", "accepted_body_markdown"}
            if self.mode == "failure":
                raise RuntimeError("uncertain response failure")
            if "draft_body_markdown" in assignment:
                obj = {"insertions": [{"after_anchor": "absent" if self.mode == "bad_insert" else "", "text": " The boundary is explicit."}],
                    "complete": True, "remaining_content": []}
            elif self.mode == "read" and len(self.calls) == 1:
                obj = {"read_source_handles": ["P0003"]}
            elif self.mode == "repeat_read":
                obj = {"read_source_handles": ["P0003"]}
            else:
                gap = self.mode in ("gap", "bad_insert") and chapter == "C1"
                obj = {"body_markdown": f"Prose for {chapter}.", "complete": not gap,
                    "remaining_content": ["Explain boundary"] if gap else []}
            response = {"content": json.dumps(obj), "complete": True, "finish_reason": "stop",
                "usage": {"prompt_tokens": 10, "completion_tokens": 20}}
            if self.mode == "length":
                response.update(complete=False, finish_reason="length")
            return response
        return call


def execute(tmp_path, book, guide, config, factory=None, **kwargs):
    return engine.run_guided_body(book, guide, tmp_path / "out", config,
        client_factory=factory, run=kwargs.pop("run", True), token_counter=meter, **kwargs)


def test_complete_actual_prefix_cache_and_guide_invalidation(tmp_path, book, guide, config):
    factory = Factory()
    result = execute(tmp_path, book, guide, config, factory)
    assert result["complete"], result
    assert result["model_calls"] == 3 and result["paid_dispatch_count"] == 0
    assert result["cost_summary"]["total_known_cost_including_base_cny"] == 0
    assert factory.calls[0]["accepted_body_markdown"] == ""
    assert factory.calls[2]["accepted_body_markdown"] == "Prose for C1.\n\nProse for C2."
    assert "task_id" not in json.dumps(factory.calls)
    replay = execute(tmp_path, book, guide, config, factory)
    assert replay["complete"] and replay["model_calls"] == 0 and len(factory.calls) == 3
    changed = deepcopy(guide)
    changed["manuscript_guide"] += " Describe limits."
    rerun = execute(tmp_path, book, changed, config, factory)
    assert rerun["complete"] and rerun["model_calls"] == 3
    assert rerun["guide_sha256"] != result["guide_sha256"]


def test_preview_never_constructs_client(tmp_path, book, guide, config):
    def forbidden(*args):
        raise AssertionError("preview constructed client")
    result = execute(tmp_path, book, guide, config, forbidden, run=False)
    assert result["status"] == "preview" and result["model_calls"] == 0
    assert len(result["stages"]) == 1 and not result["body_markdown"]


def test_capacity_blocks_intact_chapter(tmp_path, book, guide, config):
    config["max_input_tokens"] = 1
    factory = Factory()
    result = execute(tmp_path, book, guide, config, factory)
    assert result["status"] == "capacity_blocked" and not factory.calls
    assert "No truncation" in result["required_action"]
    payload = json.loads(Path(result["stages"][0]["messages_path"]).read_text())[-1]["content"]
    assert "INTACT_EVIDENCE_1" in payload


@pytest.mark.parametrize("mode,complete", [("gap", True), ("bad_insert", False)])
def test_declared_gap_one_atomic_supplement(tmp_path, book, guide, config, mode, complete):
    factory = Factory(mode)
    result = execute(tmp_path, book, guide, config, factory)
    assert result["complete"] is complete, result
    assert len(factory.calls) == 4
    assert factory.calls[1]["chapter_assignment"]["draft_body_markdown"] == "Prose for C1."
    if complete:
        assert result["body_markdown"].startswith("Prose for C1. The boundary is explicit.")
    else:
        assert result["body_markdown"].startswith("Prose for C1.\n\n")
        assert result["pending_chapter_ids"] == ["C1"]
    assert factory.calls[2]["accepted_body_markdown"] == result["segments"][0]["body_markdown"]


def test_readback_preserves_selected_evidence(tmp_path, book, guide, config):
    factory = Factory("read")
    result = execute(tmp_path, book, guide, config, factory)
    assert result["complete"], result
    material = json.dumps(factory.calls[1]["materials"])
    assert "INTACT_EVIDENCE_1" in material and "INTACT_EVIDENCE_3" in material
    assert len(factory.calls) == 4


def test_repeated_reads_stop_bounded(tmp_path, book, guide, config):
    factory = Factory("repeat_read")
    result = execute(tmp_path, book, guide, config, factory)
    assert result["status"] == "repeated_read_request"
    assert len(factory.calls) == 2


def test_uncertain_failure_requires_explicit_retry(tmp_path, book, guide, config):
    factory = Factory("failure")
    result = execute(tmp_path, book, guide, config, factory)
    assert not result["complete"] and len(factory.calls) == 1
    cached = execute(tmp_path, book, guide, config, factory)
    assert not cached["complete"] and len(factory.calls) == 1
    factory.mode = "normal"
    retried = execute(tmp_path, book, guide, config, factory, retry_failed=True)
    assert retried["complete"] and retried["model_calls"] == 3
    assert len(list((tmp_path / "out" / "stages" / "author_001").glob("*/attempt_*"))) == 2


def test_transport_truncation_retains_prose_and_stops(tmp_path, book, guide, config):
    factory = Factory("length")
    result = execute(tmp_path, book, guide, config, factory)
    assert result["status"] == "transport_failed"
    assert result["body_markdown"] == "Prose for C1." and len(factory.calls) == 1


def test_existing_complete_selected_version_survives_block(tmp_path, book, guide, config):
    original = execute(tmp_path, book, guide, config, Factory())
    changed = deepcopy(guide)
    changed["manuscript_guide"] += " New guide."
    blocked = execute(tmp_path, book, changed, {**config, "max_input_tokens": 1}, Factory())
    assert blocked["selected_version"] == original["run_id"]
    assert not blocked["selected_input_matches_current"]
    assert json.loads((tmp_path / "out" / "FULL_BODY_RESULT.json").read_text())["complete"]


def test_reject_obsolete_settings_before_factory(tmp_path, book, guide, config):
    factory = Factory()
    result = execute(tmp_path, book, guide, {**config, "reader": {}}, factory)
    assert result["status"] == "blocked" and not factory.calls


def test_gap_retry_reuses_author_success_and_invalidates_prefix_dependents(tmp_path, book, guide, config):
    factory = Factory("bad_insert")
    first = execute(tmp_path, book, guide, config, factory)
    assert not first["complete"] and len(factory.calls) == 4
    factory.mode = "gap"
    retried = execute(tmp_path, book, guide, config, factory, retry_failed=True)
    assert retried["complete"], retried
    # The author draft is a successful reusable stage even when it declares gaps.
    assert "draft_body_markdown" in factory.calls[4]["chapter_assignment"]
    assert retried["model_calls"] == 3
    assert factory.calls[5]["accepted_body_markdown"].endswith("The boundary is explicit.")


def test_unknown_read_is_blocked_without_second_dispatch(tmp_path, book, guide, config):
    class Unknown(Factory):
        def __call__(self, role, directory, profile):
            def call(messages, **kwargs):
                self.calls.append(messages)
                return {"content": json.dumps({"read_source_handles": ["P9999"]}), "complete": True}
            return call
    factory = Unknown()
    result = execute(tmp_path, book, guide, config, factory)
    assert result["status"] == "blocked" and len(factory.calls) == 1


def test_completion_disabled_keeps_gap_without_critic(tmp_path, book, guide, config):
    factory = Factory("gap")
    result = execute(tmp_path, book, guide, {**config, "completion_on_missing": False}, factory)
    assert not result["complete"] and len(factory.calls) == 3
    assert result["pending_chapter_ids"] == ["C1"]


def test_valid_partial_supplement_is_retained_in_actual_prefix(tmp_path, book, guide, config):
    class Partial(Factory):
        def __call__(self, role, directory, profile):
            normal = super().__call__(role, directory, profile)
            def call(messages, **kwargs):
                response = normal(messages, **kwargs)
                obj = json.loads(response["content"])
                if "insertions" in obj:
                    obj.update(complete=False, remaining_content=["One condition remains unclear"])
                    response["content"] = json.dumps(obj)
                return response
            return call
    factory = Partial("gap")
    result = execute(tmp_path, book, guide, config, factory)
    assert not result["complete"] and result["pending_chapter_ids"] == ["C1"]
    assert result["body_markdown"].startswith("Prose for C1. The boundary is explicit.")
    assert factory.calls[2]["accepted_body_markdown"] == "Prose for C1. The boundary is explicit."
    assert result["segments"][0]["remaining_content"] == ["One condition remains unclear"]


def test_direct_semantic_gap_is_not_transport_failure(tmp_path, book, guide, config):
    class Direct(Factory):
        def __call__(self, role, directory, profile):
            wrapped = super().__call__(role, directory, profile)
            def call(messages, **kwargs):
                return json.loads(wrapped(messages, **kwargs)["content"])
            return call
    factory = Direct("gap")
    result = execute(tmp_path, book, guide, config, factory)
    assert result["complete"] and len(factory.calls) == 4, result
    assert result["body_markdown"].startswith("Prose for C1. The boundary is explicit.")
    replay = execute(tmp_path, book, guide, config, factory)
    assert replay["complete"] and replay["model_calls"] == 0


def test_read_of_initially_supplied_source_has_no_extra_dispatch(tmp_path, book, guide, config):
    class AlreadySupplied(Factory):
        def __call__(self, role, directory, profile):
            def call(messages, **kwargs):
                self.calls.append(messages)
                return {"content": json.dumps({"read_source_handles": ["P0001"]}), "complete": True}
            return call
    factory = AlreadySupplied()
    result = execute(tmp_path, book, guide, config, factory)
    assert result["status"] == "repeated_read_request" and len(factory.calls) == 1


def test_interrupted_attempt_without_response_is_not_implicitly_retried(tmp_path, book, guide, config):
    factory = Factory()
    first = execute(tmp_path, book, guide, config, factory)
    attempt = Path(first["stages"][0]["attempt_dir"])
    (attempt / "RESULT.json").unlink()
    (attempt / "RAW_RESPONSE.json").unlink()
    second = execute(tmp_path, book, guide, config, factory)
    assert second["status"] == "uncertain_attempt" and second["model_calls"] == 0
    assert len(factory.calls) == 3
    assert second["selected_version"] == first["run_id"]
