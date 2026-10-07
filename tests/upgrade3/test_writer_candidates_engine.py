"""Offline engine controls exercise real projections, partitioning and files."""
from copy import deepcopy
import json
from pathlib import Path
import shutil
import socket

import pytest

from optomind_research.runtime.upgrade3 import writer_candidates as engine
from optomind_research.runtime.upgrade3.writer_candidates_contracts import task_catalog

TABLE = "| Setting | Result |\n|---|---|\n| Controlled fixture | Recorded fixture [P0001] |"


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def fail(*args, **kwargs):
        raise AssertionError("engine tests must not use the network")
    monkeypatch.setattr(socket, "create_connection", fail)
    monkeypatch.setattr(socket.socket, "connect", fail)


@pytest.fixture
def chapter():
    return {"schema_version": "test", "chapter_id": "C1", "chapter_frame": {"question": "Test task relationships"},
            "other_chapters": [{"chapter_id": "C2", "scope": "Read-only context"}], "language": "en",
            "units": [{"unit_id": f"U{i}", "focus": f"Role {i}", "owner_unit_context": {}, "table_tasks": [],
                       "paragraph_tasks": [{"paragraph_id": f"P{i}", "point": f"Full task {i}",
                           "source_uses": [{"source_handle": f"P{i:04d}", "role": "test"}],
                           "source_brief_details": [{"conditions": "Do not lose this condition"}]}],
                       "source_handles": [f"P{i:04d}"]} for i in range(1, 5)],
            "sources": [{"source_handle": f"P{i:04d}", "paper_id": f"paper-{i}",
                         "study_summary_A": {"work_summary": f"Intact evidence {i}", "tail": "END_OF_FULL_SOURCE"}}
                        for i in range(1, 5)], "chapter_tool_materials": [],
            "provenance": {"arrangement_sha256": "test-baseline"}, "warnings": []}


@pytest.fixture
def config():
    profile = {"model": "qwen3.5-plus", "thinking": True, "thinking_budget": 2048,
               "max_output_tokens": 4096, "stream": True, "json_mode": False,
               "timeout_seconds": 15, "stream_overall_timeout_seconds": 30,
               "prompt_token_multiplier": 1.0, "prompt_token_framing_margin": 0}
    return {"writer": deepcopy(profile), "editor": deepcopy(profile), "editor_pass": True}


def read(path):
    return json.loads(Path(path).read_text())


class Factory:
    execution_mode = "recording"
    fixture_sha256 = "engine-fixture-v1"

    def __init__(self, chapter, mode="valid"):
        self.chapter = chapter
        self.calls = []
        self.mode = mode

    def __call__(self, role, directory, profile):
        def call(messages, **kwargs):
            payload = json.loads(messages[-1]["content"])
            self.calls.append({"role": role, "messages": messages, "payload": payload, "profile": profile,
                               "kwargs": kwargs, "directory": directory})
            assert (directory / "MESSAGES.json").exists()
            assert (directory / "ACTUAL_REQUEST.json").exists()
            assert kwargs["stream"] is True
            assert kwargs["max_output_tokens"] == profile["max_output_tokens"]
            ids = list(payload["editable_task_ids"])
            if self.mode == "fail_editor" and role == "editor":
                raise RuntimeError("controlled editor refusal")
            if self.mode == "missing_writer_task" and role == "writer" and ids == ["U2::P2"]:
                return {"content": json.dumps({"blocks": [], "issues": []}), "complete": True, "finish_reason": "stop"}
            blocks = []
            catalog = task_catalog(self.chapter)
            for key in ids:
                body = TABLE if catalog[key]["kind"] == "table" else f"{role} controlled body for {key}. [P0001]"
                if self.mode == "missing_writer_table" and role == "writer" and catalog[key]["kind"] == "table":
                    body = "Table to follow."
                if self.mode == "drop_editor_table" and role == "editor" and catalog[key]["kind"] == "table":
                    continue
                blocks.append({"block_id": key, "task_ids": [key], "body_markdown": body})
            if self.mode == "one_block" and role == "writer":
                blocks = [{"block_id": "coherent-whole", "task_ids": ids, "body_markdown": "One cross-unit chapter. [P0001]"}]
            return {"content": json.dumps({"blocks": blocks, "issues": []}),
                    "complete": self.mode != "incomplete_writer" or role != "writer",
                    "finish_reason": "length" if self.mode == "incomplete_writer" and role == "writer" else "stop",
                    "usage": {"prompt_tokens": 111, "completion_tokens": 222}}
        return call


def meter(raw, messages):
    payload = json.loads(messages[-1]["content"])
    return (100 + 100 * len(payload["editable_task_ids"]) + 50 * len(payload.get("draft_blocks", []))
            + 25 * len(payload.get("read_only_neighbors", [])))


def run(tmp_path, chapter, config, factory, route="units_edit", **kwargs):
    return engine.run_candidate(chapter, route=route, output_dir=tmp_path / "out", config=config,
                                client_factory=factory, run=True, token_counter=meter, **kwargs)


def test_preview_has_exact_initial_requests_and_no_clients(tmp_path, chapter, config):
    def forbidden(*args):
        pytest.fail("preview constructed a client")
    result = engine.run_candidate(chapter, route="units_edit", output_dir=tmp_path / "out", config=config,
                                  client_factory=forbidden, run=False, token_counter=meter)
    assert result["status"] == "preview"
    assert len(result["stages"]) == 4
    assert all(Path(s["messages_path"]).is_file() for s in result["stages"])
    manifest = read(tmp_path / "out/RUN_MANIFEST.json")
    assert manifest["not_executed"][0]["role"] == "editor"
    assert not (tmp_path / "out/stages").exists()


def test_chapter_is_one_real_scope_no_editor(tmp_path, chapter, config):
    factory = Factory(chapter, "one_block")
    result = run(tmp_path, chapter, config, factory, "chapter")
    assert result["complete"]
    assert len(factory.calls) == 1
    assert factory.calls[0]["payload"]["editable_task_ids"] == list(task_catalog(chapter))
    assert len(result["blocks"]) == 1
    assert result["semantic_quality_unreviewed"]


def test_units_edits_actual_draft_and_full_material(tmp_path, chapter, config):
    factory = Factory(chapter)
    result = run(tmp_path, chapter, config, factory)
    assert result["complete"]
    assert len(factory.calls) == 5
    editor = factory.calls[-1]
    assert editor["role"] == "editor"
    assert len(editor["payload"]["draft_blocks"]) == 4
    assert all("END_OF_FULL_SOURCE" in json.dumps(s) for s in editor["payload"]["sources"])
    assert "editor controlled body" in result["body_markdown"]
    assert "writer controlled body" in (tmp_path / "out/runs" / result["run_id"] / "DRAFT_BODY.md").read_text()


def test_hierarchical_runs_real_bounded_edit_windows(tmp_path, chapter, config):
    config["max_input_tokens"] = 300
    factory = Factory(chapter)
    result = run(tmp_path, chapter, config, factory, "hierarchical")
    assert result["complete"], result
    writers = [c for c in factory.calls if c["role"] == "writer"]
    editors = [c for c in factory.calls if c["role"] == "editor"]
    assert len(writers) == 2
    assert len(editors) == 4
    assert all(meter(b"", c["messages"]) <= 300 for c in factory.calls)
    assert all(c["payload"]["read_only_neighbors"] for c in editors)
    assert all(len(c["payload"]["editable_task_ids"]) == 1 for c in editors)
    assert {key for b in result["blocks"] for key in b["task_ids"]} == set(task_catalog(chapter))
    assert result["selected_kind"] == "edited"


def test_explicit_fallback_only(tmp_path, chapter, config):
    config["max_input_tokens"] = 300
    factory = Factory(chapter)
    blocked = run(tmp_path, chapter, config, factory, "chapter")
    assert not blocked["complete"] and factory.calls == []
    fallback = run(tmp_path, chapter, config, factory, "chapter", fallback_hierarchical=True)
    assert fallback["complete"]
    assert fallback["requested_route"] == "chapter" and fallback["effective_route"] == "hierarchical"


def test_single_atom_capacity_pause_keeps_material_manifest(tmp_path, chapter, config):
    config["max_input_tokens"] = 199
    factory = Factory(chapter)
    result = run(tmp_path, chapter, config, factory, "hierarchical")
    assert not result["complete"] and not factory.calls
    assert len(result["stages"]) == 4
    assert all(s["status"] == "capacity_blocked" and s["required_action"] for s in result["stages"])
    assert all(s["material_manifest"]["material_preserved"] for s in result["stages"])
    assert "END_OF_FULL_SOURCE" in Path(result["stages"][0]["messages_path"]).read_text()


def test_success_resume_never_constructs_second_client(tmp_path, chapter, config):
    factory = Factory(chapter)
    first = run(tmp_path, chapter, config, factory)
    calls = len(factory.calls)
    second = run(tmp_path, chapter, config, factory)
    assert second["complete"] and len(factory.calls) == calls
    assert all(s["cache_hit"] for s in second["stages"])
    assert first["body_markdown"] == second["body_markdown"]


def test_raw_saved_before_parse_can_resume_without_charge(tmp_path, chapter, config):
    factory = Factory(chapter)
    first = run(tmp_path, chapter, config, factory, "chapter")
    stage = Path(first["stages"][0]["attempt_dir"])
    (stage / "RESULT.json").unlink()  # controlled crash after raw response save
    calls = len(factory.calls)
    second = run(tmp_path, chapter, config, factory, "chapter")
    assert second["complete"] and len(factory.calls) == calls
    assert (stage / "RESULT.json").exists()


def test_failed_editor_needs_explicit_retry_and_preserves_drafts(tmp_path, chapter, config):
    factory = Factory(chapter, "fail_editor")
    first = run(tmp_path, chapter, config, factory)
    assert not first["complete"] and "writer controlled body" in first["body_markdown"]
    original_calls = len(factory.calls)
    factory.mode = "valid"
    held = run(tmp_path, chapter, config, factory)
    assert not held["complete"] and len(factory.calls) == original_calls
    retried = run(tmp_path, chapter, config, factory, retry_failed=True)
    assert retried["complete"] and len(factory.calls) == original_calls + 1
    assert len(list((tmp_path / "out/stages/editor_chapter").rglob("RAW_RESPONSE.json"))) == 2


def test_real_editor_repairs_missing_task_with_full_scope(tmp_path, chapter, config):
    factory = Factory(chapter, "missing_writer_task")
    result = run(tmp_path, chapter, config, factory)
    assert result["complete"], result
    editor = factory.calls[-1]["payload"]
    assert set(editor["editable_task_ids"]) == set(task_catalog(chapter))
    assert "U2::P2" in editor["pending_task_ids"]
    assert any(block.get("placeholder_for_unwritten_task") for block in editor["draft_blocks"])
    assert any(source["source_handle"] == "P0002" for source in editor["sources"])


def test_real_editor_repairs_table_but_failed_table_edit_keeps_original(tmp_path, chapter, config):
    chapter["units"][0]["table_tasks"] = [{"table_id": "T1", "source_uses": [{"source_handle": "P0001"}]}]
    factory = Factory(chapter, "missing_writer_table")
    repaired = run(tmp_path, chapter, config, factory)
    assert repaired["complete"] and TABLE in repaired["body_markdown"]
    config["editor"]["thinking_budget"] += 1
    factory.mode = "drop_editor_table"
    failed = run(tmp_path, chapter, config, factory)
    assert not failed["complete"]
    # Existing valid version is retained at the public selected-result path.
    selected = read(tmp_path / "out/CHAPTER_RESULT.json")
    assert selected["run_id"] == repaired["run_id"]
    assert TABLE in selected["body_markdown"]
    assert read(tmp_path / "out/RUN_MANIFEST.json")["previous_complete_version_preserved"]


def test_transport_incomplete_preserves_text_and_requires_retry(tmp_path, chapter, config):
    factory = Factory(chapter, "incomplete_writer")
    result = run(tmp_path, chapter, config, factory)
    assert not result["complete"] and result["body_markdown"]
    assert all(call["role"] == "writer" for call in factory.calls)
    assert all(s["status"] == "pending" for s in result["stages"])


def test_editor_config_and_prompt_invalidate_only_editor(tmp_path, chapter, config, monkeypatch):
    prompt_dir = tmp_path / "prompts"
    shutil.copytree(engine.PROMPT_ROOT, prompt_dir)
    monkeypatch.setattr(engine, "PROMPT_ROOT", prompt_dir)
    factory = Factory(chapter)
    run(tmp_path, chapter, config, factory)
    assert len(factory.calls) == 5
    config["editor"]["thinking_budget"] += 1
    run(tmp_path, chapter, config, factory)
    assert len(factory.calls) == 6
    with (prompt_dir / "editor.md").open("a") as handle:
        handle.write("\nInspect full scientific relationships carefully.\n")
    run(tmp_path, chapter, config, factory)
    assert len(factory.calls) == 7 and factory.calls[-1]["role"] == "editor"


def test_fixture_and_live_cache_identities_are_distinct(tmp_path, chapter, config):
    factory = Factory(chapter)
    run(tmp_path, chapter, config, factory, "chapter")
    factory.execution_mode = "live"  # controlled boundary still offline
    result = run(tmp_path, chapter, config, factory, "chapter")
    assert len(factory.calls) == 2 and not result["stages"][0]["cache_hit"]
    factory.fixture_sha256 = "changed"
    run(tmp_path, chapter, config, factory, "chapter")
    assert len(factory.calls) == 3


def test_unsupported_caps_and_disabled_editor_stop_before_call(tmp_path, chapter, config):
    factory = Factory(chapter)
    config["writer"]["max_output_tokens"] = 65536
    result = run(tmp_path, chapter, config, factory)
    assert result["status"] == "blocked" and not factory.calls
    config["writer"]["max_output_tokens"] = 4096
    config["editor_pass"] = False
    result = run(tmp_path, chapter, config, factory)
    assert result["status"] == "blocked" and not factory.calls


def test_mixed_window_failure_truthful_lineage(tmp_path, chapter, config):
    config["max_input_tokens"] = 300
    base = Factory(chapter)

    class FailOne(Factory):
        def __call__(self, role, directory, profile):
            client = super().__call__(role, directory, profile)
            def call(messages, **kwargs):
                if kwargs["stage_id"] == "editor_window_002":
                    raise RuntimeError("controlled window failure")
                return client(messages, **kwargs)
            return call
    factory = FailOne(chapter)
    result = run(tmp_path, chapter, config, factory, "hierarchical")
    assert not result["complete"]
    assert result["selected_kind"] == "mixed_partial_edits"
    assert "editor controlled body" in result["body_markdown"]
    assert "writer controlled body for U2::P2" in result["body_markdown"]
    selected = [stage for stage in result["stage_lineage"] if stage["contributes_selected_blocks"]]
    assert {stage["role"] for stage in selected} == {"writer", "editor"}
    assert any(stage.get("call_error", {}).get("error") == "controlled window failure"
               for stage in result["stages"] if stage.get("call_error"))


def test_interrupted_attempt_never_automatically_replays(tmp_path, chapter, config):
    factory = Factory(chapter)
    first = run(tmp_path, chapter, config, factory, "chapter")
    attempt = Path(first["stages"][0]["attempt_dir"])
    (attempt / "RAW_RESPONSE.json").unlink()
    (attempt / "RESULT.json").unlink()
    second = run(tmp_path, chapter, config, factory, "chapter")
    assert second["stages"][0]["status"] == "uncertain_attempt"
    assert len(factory.calls) == 1
    assert read(tmp_path / "out/CHAPTER_RESULT.json")["run_id"] == first["run_id"]
    third = run(tmp_path, chapter, config, factory, "chapter", retry_failed=True)
    assert third["complete"] and len(factory.calls) == 2


def test_changed_material_invalidates_current_input_identity(tmp_path, chapter, config):
    factory = Factory(chapter)
    first = run(tmp_path, chapter, config, factory, "chapter")
    chapter["sources"][0]["study_summary_A"]["tail"] = "NEW_COMPLETE_SOURCE_TAIL"
    second = run(tmp_path, chapter, config, factory, "chapter")
    assert second["complete"] and len(factory.calls) == 2
    assert first["input_hash"] != second["input_hash"]
    assert "NEW_COMPLETE_SOURCE_TAIL" in Path(second["stages"][0]["messages_path"]).read_text()


def test_priority_rules_reach_writer_and_editor_without_domain_answers(tmp_path, chapter, config):
    factory = Factory(chapter)
    result = run(tmp_path, chapter, config, factory)
    assert result["complete"]
    for call in factory.calls:
        system = call["messages"][0]["content"]
        assert "chapter_argument" in system and "chapter_scope" in system
        assert "研究范围" in system and "材料" in system
