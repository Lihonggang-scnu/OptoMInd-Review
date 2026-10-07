"""Offline guarantees for the additive packed evidence routes."""
from copy import deepcopy
import json
from pathlib import Path
import socket

import pytest
from optomind_research.runtime.upgrade3 import evidence_body_writer as engine
from optomind_research.runtime.upgrade3.fullbody_contracts import build_fullbody_input, fullbody_task_catalog
from optomind_research.runtime.upgrade3.writer_candidates_contracts import INPUT_SCHEMA

@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def denied(*a, **k):
        raise AssertionError("Network is forbidden in offline tests")
    monkeypatch.setattr(socket.socket, "connect", denied)

@pytest.fixture
def book():
    chapters = []
    for index in (1, 2):
        handle = f"P{index:04d}"
        chapters.append({"schema_version": INPUT_SCHEMA, "chapter_id": f"C{index}", "language": "en",
            "chapter_frame": {"research_question": "Explain relationships", "chapter_title": f"Chapter {index}"},
            "other_chapters": [], "units": [{"unit_id": "U", "focus": "Explain conditions",
                "paragraph_tasks": [{"paragraph_id": f"T{part}", "point": f"Explain relation {part}", "source_handles": [handle]} for part in (1,2)],
                "table_tasks": [], "owner_unit_context": {}, "source_handles": [handle]}],
            "sources": [{"source_handle": handle, "paper_id": f"paper-{index}",
                "study_summary_A": {"finding": {"result": f"Result {index}", "conditions": "joint condition preserved"}},
                "review_planning_B": {"scope_interpretation_cautions": "Specific limits", "planning_summary": "Execution verbs only"}}],
            "chapter_tool_materials": [], "provenance": {}, "warnings": []})
    return build_fullbody_input(chapters)

@pytest.fixture
def config():
    profile = {"thinking_budget": 1024, "max_output_tokens": 4096, "timeout_seconds": 15,
        "stream_overall_timeout_seconds": 30, "prompt_token_multiplier": 1.0, "prompt_token_framing_margin": 0}
    return {"writer": profile, "curator": profile}

def meter(raw, messages):
    payload = json.loads(messages[-1]["content"])
    return 100 + 100 * len(payload["editable_task_ids"])

class Factory:
    execution_mode = "recording"
    fixture_sha256 = "evidence-body-fixture-1"
    def __init__(self, mode="normal"):
        self.mode, self.calls = mode, []
    def __call__(self, role, directory, profile):
        def call(messages, **kwargs):
            payload = json.loads(messages[-1]["content"])
            self.calls.append((role,payload))
            ids = payload["editable_task_ids"]
            assert (directory / "ACTUAL_REQUEST.json").exists()
            if role == "curator":
                obj = {"selected_atom_ids": [row["atom_id"] for row in payload["evidence_atoms"]], "complete": True}
                if self.mode == "bad_curator": obj["selected_atom_ids"] = ["unknown"]
            elif "missing_task_ids" in payload:
                obj = {"insertions": [{"after_anchor": "", "text": "\n\nAdditional supported explanation."}], "completed_task_ids": ids, "complete": True}
                if self.mode in {"bad_completion", "unhelpful_table"}: obj["insertions"][0]["after_anchor"] = "nonexistent"
                if self.mode == "failed_completion": raise RuntimeError("controlled completion transport failure")
                if self.mode == "table": obj["insertions"][0]["text"] = "\n\n| Condition | Result |\n| --- | --- |\n| Joint | Supported |"
            elif self.mode == "unknown_read" and payload["accepted_body_markdown"]:
                obj = {"read_atom_ids": ["unknown"]}
            elif self.mode == "repeat_read":
                obj = {"read_atom_ids": [payload["evidence_atoms"][0]["atom_id"]]}
            else:
                obj = {"body_markdown": f"## {ids[0]}\nSupported explanation under joint conditions.", "completed_task_ids": ids, "complete": True}
                if self.mode in {"missing", "bad_completion"}: obj["completed_task_ids"] = ids[:-1]
            return {"content": json.dumps(obj), "complete": True, "finish_reason": "stop", "usage": {"prompt_tokens": 100, "completion_tokens": 200}}
        return call

def run(book, config, tmp_path, route="packed_continuous", factory=None, **kw):
    return engine.run_evidence_body(book, route=route, output_dir=tmp_path, config=config,
        client_factory=factory, run=factory is not None, counter=meter, **kw)

def test_profiles_do_not_change_old_defaults():
    from optomind_research.runtime.upgrade3.writer_candidates import DEFAULT_PROFILE
    got = engine.validate_config({})
    assert got["writer"]["thinking_budget"] == 16384
    assert got["writer"]["max_output_tokens"] == 49152
    assert DEFAULT_PROFILE["max_output_tokens"] == 32768

@pytest.mark.parametrize("route",engine.ROUTES)
def test_complete_routes_archive_and_cache(book,config,tmp_path,route):
    factory = Factory()
    original = deepcopy(book)
    result = run(book,config,tmp_path,route,factory)
    assert result["complete"] and book == original
    assert len(result["completed_task_ids"]) == 4
    assert Path(result["evidence_archive_path"]).exists()
    calls = len(factory.calls)
    again = run(book,config,tmp_path,route,factory)
    assert again["complete"] and again["model_calls"] == 0 and len(factory.calls) == calls
    if route == "packed_continuous":
        assert factory.calls[1][1]["accepted_body_markdown"] == result["segments"][0]["body_markdown"]

def test_preview_only_actual_first_window(book,config,tmp_path):
    result = run(book,config,tmp_path)
    assert result["status"] == "preview" and result["model_calls"] == 0
    assert len(result["stages"]) == 1
    assert result["stages"][0]["task_ids"] == list(fullbody_task_catalog(book))[:2]

def test_capacity_only_splits_intact_tasks(book,config,tmp_path):
    config["max_input_tokens"] = 250
    factory = Factory()
    result = run(book,config,tmp_path,factory=factory)
    assert result["complete"] and len(factory.calls) == 4
    assert all(len(payload["editable_task_ids"]) == 1 for _,payload in factory.calls)

def test_whole_never_silently_switches_route(book,config,tmp_path):
    config["max_input_tokens"] = 250
    factory = Factory()
    result = run(book,config,tmp_path,"packed_whole",factory)
    assert not result["complete"] and not factory.calls
    assert result["stages"][0]["status"] == "capacity_blocked"

def test_dossier_declares_capacity_composition(book,config,tmp_path):
    config["max_input_tokens"] = 350
    result = run(book,config,tmp_path,"dossier_author",Factory())
    assert result["complete"]
    assert result["assembly_method"] == "natural_chapter_composition_with_actual_prefix"

def test_completion_preserves_original_characters(book,config,tmp_path):
    result = run(book,config,tmp_path,factory=Factory("missing"))
    assert result["complete"] and result["model_calls"] == 4
    assert all(segment["body_markdown"].endswith("Additional supported explanation.") for segment in result["segments"])

def test_invalid_completion_preserves_useful_draft(book,config,tmp_path):
    factory = Factory("bad_completion")
    result = run(book,config,tmp_path,factory=factory)
    assert not result["complete"] and result["partial_unaccepted_prose"]
    assert "Supported explanation" in result["body_markdown"]
    assert len(factory.calls) == 4
    assert len(result["segments"]) == 2
    assert all(not row["accepted"] for row in result["segments"])
    run(book,config,tmp_path,factory=factory)
    assert len(factory.calls) == 4

def test_missing_table_one_completion(book,config,tmp_path):
    book["chapters"][0]["units"][0]["table_tasks"] = [{"table_id": "TABLE", "point": "Compare conditions", "source_handles": ["P0001"]}]
    result = run(book,config,tmp_path,factory=Factory("table"))
    assert result["complete"] and "| Joint | Supported |" in result["body_markdown"]
    assert result["model_calls"] == 3

def test_unknown_reread_preserves_previous_chapter(book,config,tmp_path):
    result = run(book,config,tmp_path,factory=Factory("unknown_read"))
    assert not result["complete"] and len(result["segments"]) == 1
    assert result["body_markdown"] == result["segments"][0]["body_markdown"]
    assert result["stages"][-1]["status"] == "unknown_reread_atom"

def test_repeat_reread_stops_without_paid_loop(book,config,tmp_path):
    result = run(book,config,tmp_path,factory=Factory("repeat_read"))
    assert not result["complete"] and result["model_calls"] == 1
    assert result["stages"][-1]["status"] == "reread_bound_or_no_progress"

def test_unknown_curator_ids_never_dispatch_writer(book,config,tmp_path):
    factory = Factory("bad_curator")
    result = run(book,config,tmp_path,"dossier_author",factory)
    assert not result["complete"] and len(factory.calls) == 1
    assert factory.calls[0][0] == "curator"

def test_dossier_capacity_batches_all_intact_atoms(book,config,tmp_path):
    # A single task's source has several intact objects. Every batch must be
    # considered, even if only the last carries a useful finding.
    config["max_input_tokens"] = 250
    factory = Factory()
    def atoms_meter(raw,messages):
        payload = json.loads(messages[-1]["content"])
        return 100 + 100 * len(payload.get("evidence_atoms", []))
    result = engine.run_evidence_body(book,route="dossier_author",output_dir=tmp_path,
        config=config,client_factory=factory,run=True,counter=atoms_meter)
    curator_calls = [payload for role,payload in factory.calls if role == "curator"]
    pack = engine.writing_evidence.compile_evidence(book)
    considered = {atom["atom_id"] for payload in curator_calls for atom in payload["evidence_atoms"]}
    expected = {aid for ids in pack["task_atom_ids"].values() for aid in ids}
    assert considered == expected
    assert all(len(payload["evidence_atoms"]) == 1 for payload in curator_calls)
    # Writer can honestly remain capacity-blocked: no selected evidence is cut.
    assert result["model_calls"] == len(curator_calls)
    assert not result["complete"]

def test_completion_rejects_changed_anchor_and_keeps_exact_draft(book):
    ids = list(fullbody_task_catalog(book))[:2]
    parser = engine._completion_parser(book,ids,"EXACT prior prose",{"completed_task_ids":ids[:1]},ids[1:])
    with pytest.raises(Exception,match="match_once"):
        parser({"insertions":[{"after_anchor":"approximate prior prose","text":"new"}],"completed_task_ids":ids[1:],"complete":True})

def test_curator_empty_batch_allowed_and_unseen_known_atom_rejected(book):
    pack = engine.writing_evidence.compile_evidence(book)
    ids = list(pack["tasks"])[:1]
    atoms = list(pack["atoms"])
    parser = engine._curator_parser(pack,ids,atoms[:1])
    assert parser({"selected_atom_ids":[],"complete":True})["selected_atom_ids"] == []
    with pytest.raises(Exception,match="outside_considered_batch"):
        parser({"selected_atom_ids":atoms[1:2],"complete":True})

def test_author_accepts_natural_markdown_with_metadata(book):
    ids = list(fullbody_task_catalog(book))[:2]
    prose = "## Scientific relationship\n\nA supported explanation."
    parsed = engine._author_parser(book,ids)(prose+'\n```fullbody_metadata\n'+json.dumps({"completed_task_ids":ids,"complete":True})+'\n```')
    assert parsed["complete"] and parsed["body_markdown"] == prose


@pytest.mark.parametrize("route", ["packed_continuous", "dossier_author"])
def test_unhelpful_table_completion_retains_both_chapters_pending_table(book,config,tmp_path,route):
    book["chapters"][0]["units"][0]["table_tasks"] = [{"table_id":"TABLE", "point":"Compare conditions", "source_handles":["P0001"]}]
    config["max_input_tokens"] = 400  # Dossier full BODY exceeds capacity; each chapter fits.
    factory = Factory("unhelpful_table")
    result = run(book,config,tmp_path,route,factory)
    assert not result["complete"] and len(result["segments"]) == 2
    first, second = result["segments"]
    assert not first["accepted"] and second["accepted"]
    table_ids = [key for key,row in fullbody_task_catalog(book).items() if row["kind"] == "table"]
    assert result["pending_task_ids"] == table_ids
    assert first["pending_task_ids"] == table_ids
    assert all(row["status"] == "pending" for row in first["task_dispositions"] if row["task_id"] in table_ids)
    last_payload = factory.calls[-1][1]
    assert last_payload["accepted_body_markdown"] == first["body_markdown"]
    assert last_payload["writing_position"]["unresolved_prior_task_ids"] == table_ids
    assert not set(table_ids).intersection(last_payload["writing_position"]["completed_task_ids"])
    assert result["body_markdown"] == first["body_markdown"] + "\n\n" + second["body_markdown"]


def test_completion_transport_failure_stops_charged_chain(book,config,tmp_path):
    book["chapters"][0]["units"][0]["table_tasks"] = [{"table_id":"TABLE", "point":"Compare conditions", "source_handles":["P0001"]}]
    factory = Factory("failed_completion")
    result = run(book,config,tmp_path,factory=factory)
    assert not result["complete"] and len(factory.calls) == 2
    assert len(result["segments"]) == 0
    assert "Supported explanation" in result["body_markdown"]
    assert "C2" not in result["body_markdown"]
