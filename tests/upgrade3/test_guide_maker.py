"""Offline outline-to-guide runtime tests: whole packets, draft/cache safety."""
from copy import deepcopy
import json
from pathlib import Path
import socket

import pytest

from optomind_research.runtime.upgrade3 import guide_maker as engine
from optomind_research.runtime.upgrade3.guided_body_contracts import validate_guide
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
        chapter_frame={"title": f"Chapter {i}", "research_question": "Explain observations"}, other_chapters=[],
        units=[dict(unit_id="U", focus="Explain", paragraph_tasks=[dict(paragraph_id="T1", point="Original task",
            source_handles=[f"P{i:04d}"])], table_tasks=[], owner_unit_context={}, source_handles=[f"P{i:04d}"])],
        sources=[dict(source_handle=f"P{i:04d}", paper_id=f"paper-{i}", study_summary_A={"finding": f"INTACT_EVIDENCE_{i}",
            "conditions": {"temperature": 321, "pressure": "controlled", "limitation": "complete conditions"}})],
        chapter_tool_materials=[], provenance={}, warnings=[]) for i in (1, 2, 3)])


@pytest.fixture
def guide(book):
    return {"manuscript_guide": "Explain observations with their conditions.",
        "chapters": [{"chapter_id": row["chapter_id"], "title": f"Chapter {i}",
            "writing_arrangement": "Develop the relationship in natural prose.",
            "required_content": ["Explain the important boundary."]} for i, row in enumerate(book["chapters"], 1)]}


@pytest.fixture
def config():
    return {"maker": {"model": "qwen3.5-plus", "thinking": True, "thinking_budget": 1024,
        "max_output_tokens": 4096, "stream": True, "json_mode": False, "timeout_seconds": 15,
        "stream_overall_timeout_seconds": 30, "prompt_token_multiplier": 1.0, "prompt_token_framing_margin": 0}}


def response(guide, handles=(), complete=None, question="Which conditions change this writing arrangement?"):
    return dict(guide=deepcopy(guide), reading_needs=[dict(need_id="N1", question=question, source_handles=list(handles))] if handles else [],
                complete=not handles if complete is None else complete, changes=["Clarified the comparison."])


class Factory:
    execution_mode = "recording"
    fixture_sha256 = "maker-offline-v1"

    def __init__(self, *responses, direct=False):
        self.responses, self.calls, self.direct = list(responses), [], direct

    def __call__(self, role, directory, profile):
        assert role == "maker"
        def call(messages, **kwargs):
            self.calls.append(json.loads(messages[-1]["content"]))
            answer = self.responses[len(self.calls) - 1]
            if isinstance(answer, Exception):
                raise answer
            if self.direct:
                return deepcopy(answer)
            return {"content": json.dumps(answer), "complete": True, "finish_reason": "stop",
                    "usage": {"prompt_tokens": 10, "completion_tokens": 20}}
        return call


def execute(tmp_path, book, config, factory=None, **kwargs):
    return engine.run_guide_maker(book, tmp_path / "out", config, client_factory=factory,
        run=kwargs.pop("run", True), token_counter=kwargs.pop("token_counter", lambda raw, messages: 100), **kwargs)


def test_two_round_guide_reads_and_final_handoff(tmp_path, book, guide, config):
    final = deepcopy(guide)
    final["chapters"][0]["writing_arrangement"] = "Explain the controlled 321 K evidence and its boundary."
    factory = Factory(response(guide, ["P0001"]), response(final))
    result = execute(tmp_path, book, config, factory)
    assert result["complete"], result
    assert result["model_calls"] == 2 and result["paid_dispatch_count"] == 0
    assert result["cost_summary"]["total_known_cost_including_base_cny"] == 0
    first, second = factory.calls
    assert first["prior_guide"] is None and not first["materials"]
    assert "INTACT_EVIDENCE" not in json.dumps(first)
    assert second["prior_guide"]["manuscript_guide"] == guide["manuscript_guide"]
    assert "INTACT_EVIDENCE_1" in json.dumps(second["materials"])
    assert "complete conditions" in json.dumps(second["materials"])
    assert first["full_outline"] == second["full_outline"]
    assert result["read_trace"][1]["changed_chapter_ids"] == ["C1"]
    exported = json.loads((tmp_path / "out/GUIDE.json").read_text())
    assert validate_guide(exported, book) == result["guide"]
    assert final["chapters"][0]["writing_arrangement"] in (tmp_path / "out/GUIDE.md").read_text()
    cached = execute(tmp_path, book, config, factory)
    assert cached["complete"] and cached["model_calls"] == 0 and len(factory.calls) == 2


def test_zero_read_complete_and_feedback_only(tmp_path, book, guide, config):
    factory = Factory(response(guide))
    result = execute(tmp_path, book, config, factory, feedback="Organize around conditions.")
    assert result["complete"] and result["model_calls"] == 1
    assert factory.calls[0]["feedback"] == "Organize around conditions."
    assert not result["read_history"]


def test_cold_start_preview_without_client(tmp_path, book, config):
    result = execute(tmp_path, book, config, run=False)
    assert result["status"] == "preview" and result["model_calls"] == 0
    assert result["guide"] is None and not (tmp_path / "out/GUIDE.json").exists()
    payload = json.loads(Path(result["stages"][0]["messages_path"]).read_text())
    assert "INTACT_EVIDENCE" not in payload[-1]["content"]


def packet_meter(raw, messages):
    return 100 + len(json.loads(messages[-1]["content"])["materials"]) * 100


def test_packet_queue_carries_and_does_not_accumulate(tmp_path, book, guide, config):
    config["max_input_tokens"] = 250
    factory = Factory(response(guide, ["P0001", "P0002", "P0003"]), response(guide), response(guide), response(guide))
    result = execute(tmp_path, book, config, factory, token_counter=packet_meter)
    assert result["complete"] and result["model_calls"] == 4, result
    assert [len(call["materials"]) for call in factory.calls] == [0, 1, 1, 1]
    assert [len(call["read_history"]) for call in factory.calls] == [0, 0, 1, 2]
    assert len(result["read_history"]) == 3
    assert "INTACT_EVIDENCE_1" not in json.dumps(factory.calls[-1])


def test_oversized_packet_blocks_intact_with_draft(tmp_path, book, guide, config):
    config["max_input_tokens"] = 150
    factory = Factory(response(guide, ["P0001"]))
    result = execute(tmp_path, book, config, factory, token_counter=packet_meter)
    assert result["status"] == "capacity_blocked" and len(factory.calls) == 1, result
    assert result["guide"] and (tmp_path / "out/DRAFT_GUIDE.json").exists()
    assert not (tmp_path / "out/GUIDE.json").exists()
    payload = Path(result["stages"][-1]["messages_path"]).read_text()
    assert "INTACT_EVIDENCE_1" in payload and "complete conditions" in payload


def test_repeated_reads_stop_without_third_paid_call(tmp_path, book, guide, config):
    factory = Factory(response(guide, ["P0001"]), response(guide, ["P0001"]))
    result = execute(tmp_path, book, config, factory)
    assert not result["complete"] and result["model_calls"] == 2
    assert "repeated_read_request" in json.dumps(result["issues"])
    assert result["guide"]


def test_model_limit_resume_from_cache(tmp_path, book, guide, config):
    config["max_model_calls"] = 1
    factory = Factory(response(guide, ["P0001"]), response(guide))
    result = execute(tmp_path, book, config, factory)
    assert result["status"] == "model_call_limit" and result["guide"]
    config["max_model_calls"] = 2
    resumed = execute(tmp_path, book, config, factory)
    assert resumed["complete"] and resumed["model_calls"] == 1 and len(factory.calls) == 2


def test_failure_retains_latest_draft_and_no_implicit_retry(tmp_path, book, guide, config):
    factory = Factory(response(guide, ["P0001"]), RuntimeError("uncertain transport"))
    result = execute(tmp_path, book, config, factory)
    assert result["status"] == "transport_failed" and result["guide"]
    assert not (tmp_path / "out/GUIDE.json").exists()
    again = execute(tmp_path, book, config, factory)
    assert again["status"] == "transport_failed" and again["model_calls"] == 0


def test_direct_provisional_json_is_cacheable(tmp_path, book, guide, config):
    factory = Factory(response(guide, ["P0001"]), response(guide), direct=True)
    result = execute(tmp_path, book, config, factory)
    assert result["complete"], result
    assert execute(tmp_path, book, config, factory)["model_calls"] == 0


def test_changed_feedback_preserves_old_ready_named_version(tmp_path, book, guide, config):
    first = execute(tmp_path, book, config, Factory(response(guide)))
    failed = execute(tmp_path, book, config, Factory(RuntimeError("transport failed")), feedback="New question")
    assert first["complete"] and not failed["complete"]
    assert failed["selected_version"] == first["run_id"] and not failed["selected_input_matches_current"]
    assert json.loads((tmp_path / "out/GUIDE.json").read_text()) == first["guide"]


def test_invalid_read_retains_provisional_but_cannot_be_ready(tmp_path, book, guide, config):
    factory = Factory(response(guide, ["P9999"]))
    result = execute(tmp_path, book, config, factory)
    assert not result["complete"] and result["model_calls"] == 1
    assert result["guide"] and result["status"] == "response_invalid", result
    assert not (tmp_path / "out/GUIDE.json").exists()


def test_requested_source_change_reuses_cold_start_only(tmp_path, book, guide, config):
    initial = execute(tmp_path, book, config, Factory(response(guide, ["P0001"]), response(guide)))
    assert initial["complete"]
    changed = deepcopy(book)
    for row in changed["sources"] + [source for chapter in changed["chapters"] for source in chapter["sources"]]:
        if row["source_handle"] == "P0001":
            row["study_summary_A"]["finding"] = "NEW_INTACT_FINDING"
    factory = Factory(response(guide))
    new = execute(tmp_path, changed, config, factory)
    assert new["complete"] and new["model_calls"] == 1, new
    assert "NEW_INTACT_FINDING" in json.dumps(factory.calls[0])
    assert new["stages"][0]["cache_hit"]


def test_bad_config_stops_without_client(tmp_path, book, config):
    config["schema_version"] = "wrong"
    factory = Factory()
    result = execute(tmp_path, book, config, factory)
    assert not result["complete"] and not factory.calls
    assert "guide_maker_config_schema_invalid" in json.dumps(result["issues"])


def test_explicit_reread_allowed_but_repeated_purpose_stops(tmp_path, book, guide, config):
    reread = response(guide, ["P0001"])
    reread["reading_needs"][0]["reread_reason"] = "Revisit the pressure caveat after changing the chapter ordering."
    factory = Factory(response(guide, ["P0001"]), reread, reread)
    result = execute(tmp_path, book, config, factory)
    assert result["model_calls"] == 3 and not result["complete"], result
    assert "repeated_reread_purpose" in json.dumps(result["issues"])
    assert len(result["read_history"]) == 2
    assert len(list((tmp_path / "out/reads").glob("*/SOURCE_PACKET.json"))) == 1


def test_partial_transport_never_exports_new_guide(tmp_path, book, guide, config):
    class Partial(Factory):
        def __call__(self, role, directory, profile):
            base = super().__call__(role, directory, profile)
            def call(messages, **kwargs):
                value = base(messages, **kwargs)
                if len(self.calls) == 2:
                    value.update(complete=False, finish_reason="length")
                return value
            return call
    altered = deepcopy(guide)
    altered["manuscript_guide"] = "This partial guide must not replace the accepted draft."
    result = execute(tmp_path, book, config, Partial(response(guide, ["P0001"]), response(altered)))
    assert result["status"] == "transport_failed" and not result["complete"]
    assert result["guide"]["manuscript_guide"] == guide["manuscript_guide"]
    assert not (tmp_path / "out/GUIDE.json").exists()


def test_repeated_pending_need_does_not_duplicate_queued_packets(tmp_path, book, guide, config):
    config["max_input_tokens"] = 250
    request = response(guide, ["P0001", "P0002", "P0003"])
    factory = Factory(request, request, response(guide), response(guide))
    result = execute(tmp_path, book, config, factory, token_counter=packet_meter)
    assert result["complete"] and result["model_calls"] == 4, result
    assert [call["materials"][0]["source_handle"] for call in factory.calls[1:]] == ["P0001", "P0002", "P0003"]


@pytest.mark.parametrize("reread", [False, True])
def test_rejected_repeat_requires_explicit_retry_and_is_repairable(tmp_path, book, guide, config, reread):
    request = response(guide, ["P0001"])
    answers = [request]
    if reread:
        request = deepcopy(request)
        request["reading_needs"][0]["reread_reason"] = "Check the limitations again after restructuring."
        answers.append(request)
    answers.extend([request, response(guide)])
    factory = Factory(*answers)
    first = execute(tmp_path, book, config, factory)
    expected_calls = 3 if reread else 2
    assert not first["complete"] and first["model_calls"] == expected_calls
    rejected = json.loads((Path(first["stages"][-1]["attempt_dir"]) / "RESULT.json").read_text())
    assert rejected["complete"] is False and rejected["guide"]
    held = execute(tmp_path, book, config, factory)
    assert not held["complete"] and held["model_calls"] == 0
    assert len(factory.calls) == expected_calls
    repaired = execute(tmp_path, book, config, factory, retry_failed=True)
    assert repaired["complete"] and repaired["model_calls"] == 1, repaired
    assert len(factory.calls) == expected_calls + 1


def test_tool_supplement_follows_same_whole_packet_read_loop(tmp_path, book, guide, config):
    supplement = {"question": "Which supplementary boundary controls interpretation?",
        "usable_content": "COMPLETE SUPPLEMENTAL SCIENCE " * 250,
        "conditions": {"temperature": 321, "pressure": "controlled"},
        "limits": "Not transferable", "unknown_science": {"negative_result": "Null"}}
    book["chapters"][0]["chapter_tool_materials"] = [supplement]
    handle = engine.compile_guide_input(book)["tool_catalog"][0]["tool_handle"]
    initial = response(guide, complete=False)
    initial["reading_needs"] = [{"need_id": "N1", "question": supplement["question"], "tool_handles": [handle]}]
    factory = Factory(initial, response(guide))
    result = execute(tmp_path, book, config, factory)
    assert result["complete"] and result["model_calls"] == 2, result
    assert "COMPLETE SUPPLEMENTAL SCIENCE" not in json.dumps(factory.calls[0])
    packet = factory.calls[1]["materials"][0]
    assert packet["packet_kind"] == "tool" and packet["source_handle"] == handle
    assert packet["scientific_tool"] == supplement
    assert result["read_trace"][1]["reads"][0]["source_handle"] == handle
    assert validate_guide(result["guide"], book) == result["guide"]
