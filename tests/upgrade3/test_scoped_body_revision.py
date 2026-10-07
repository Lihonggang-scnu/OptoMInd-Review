"""Offline block IDs, atomic groups, complete prompts, failures and cache."""
from copy import deepcopy
import json
import socket
import pytest

from optomind_research.runtime.upgrade3 import scoped_body_revision as sr
from optomind_research.runtime.upgrade3.fullbody_contracts import build_fullbody_input, fullbody_task_catalog
from optomind_research.runtime.upgrade3.writer_candidates_contracts import INPUT_SCHEMA


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    monkeypatch.setattr(socket.socket, "connect", lambda *a, **k: pytest.fail("network forbidden"))


@pytest.fixture
def book():
    chapters = []
    for i in (1, 2):
        handle = f"P{i:04d}"
        chapters.append({"schema_version": INPUT_SCHEMA, "chapter_id": f"C{i}", "language": "en",
            "chapter_frame": {"research_question": "Compare mechanisms and conditions", "title": f"Chapter {i}"},
            "other_chapters": [], "units": [{"unit_id": "U", "focus": "Explain discrimination",
                "paragraph_tasks": [{"paragraph_id": "T", "point": "Explain evidence with conditions", "source_handles": [handle]}],
                "table_tasks": [], "owner_unit_context": {}, "source_handles": [handle]}],
            "sources": [{"source_handle": handle, "paper_id": f"paper{i}", "study_summary_A": {"finding": f"Entire finding {i}", "tail": f"CONDITION_TAIL_{i}"}}],
            "chapter_tool_materials": [], "provenance": {}, "warnings": []})
    return build_fullbody_input(chapters, research_question="Compare mechanisms and conditions", user_request="Explain evidence", target_reader="researcher")


@pytest.fixture
def config():
    profile = {"model": "qwen3.5-plus", "thinking": True, "thinking_budget": 1024, "max_output_tokens": 4096,
        "stream": True, "json_mode": False, "timeout_seconds": 15, "stream_overall_timeout_seconds": 30,
        "prompt_token_multiplier": 1.0, "prompt_token_framing_margin": 0}
    return {"writer": profile, "reader": deepcopy(profile), "reviser": deepcopy(profile)}


@pytest.fixture
def base(book):
    body = "# Chapter 1\r\n\r\nOriginal argument. [P0001]\r\n\r\nUntouched text.\r\n\r\n# Chapter 2\r\n\r\nSecond argument. [P0002]\r\n"
    return {"complete": True, "body_complete": True, "body_markdown": body, "body_sha256": sr.fw._text_hash(body),
        "input_hash": sr._hash(book), "completed_task_ids": list(fullbody_task_catalog(book)), "pending_task_ids": [],
        "cost_summary": {"candidate_stage_known_cost_cny": 3.0, "total_cost_complete": True}}


def issue(block, task, ident="I1"):
    return {"issue_id": ident, "target_block_ids": [block["block_id"]], "related_task_ids": [task],
            "source_handles": [], "reason": "Missing comparison", "goal": "Explain comparison"}


def group(body, block, ident="I1", text="Rewritten passage. [P0001]"):
    return {"issue_id": ident, "action": "replace_block", "original_body_sha256": sr.fw._text_hash(body),
            "replacements": [{"block_id": block["block_id"], "original_sha256": block["sha256"], "content": text}]}


class Factory:
    execution_mode = "recording"
    fixture_sha256 = "scoped-offline-v1"
    def __init__(self, mode="normal"):
        self.calls, self.mode = [], mode
    def __call__(self, role, directory, profile):
        def call(messages, **kwargs):
            p = json.loads(messages[-1]["content"])
            self.calls.append((role, p))
            assert (directory / "MESSAGES.json").exists()
            if role == "reader":
                blocks = [b for b in p["blocks"] if "argument." in b["content"]]
                tasks = list(p["tasks"])
                issues = [issue(blocks[0], tasks[0])]
                if self.mode in ("partial", "overlap"):
                    issues.append(issue(blocks[1 if self.mode == "partial" else 0], tasks[-1], "I2"))
                obj = {"complete": True, "issues": [] if self.mode == "empty" else issues, "assessment": "Argument needs work"}
            elif self.mode == "failure":
                raise RuntimeError("controlled failure")
            elif self.mode == "reread" and not p["reread_source_handles"]:
                obj = {"read_source_handles": ["P0002"]}
            elif self.mode == "repeat":
                obj = {"read_source_handles": ["P0002"]}
            elif self.mode == "unknown":
                obj = {"read_atom_ids": ["invented"]}
            else:
                ident = p["reader_issues"][0]["issue_id"]
                g = group(p["body_markdown"], p["editable_blocks"][0], ident)
                if self.mode == "partial" and ident == "I2":
                    g["replacements"][0]["original_sha256"] = "wrong"
                obj = {"complete": True, "groups": [g], "rejected_issues": []}
            return {"complete": True, "finish_reason": "stop", "content": json.dumps(obj),
                    "usage": {"prompt_tokens": 111, "completion_tokens": 222}}
        return call


def run(tmp_path, book, base, config, factory=None, **kw):
    return sr.run_scoped_revision(book, base, tmp_path, config, client_factory=factory, counter=lambda *a: 100, **kw)


def test_index_exact_offsets_chapters_and_fences(book, base):
    body = base["body_markdown"] + "\r\n```text\r\nA\r\n\r\nB\r\n```\r\n"
    blocks = sr.index_blocks(body, book)
    assert all(body[b["start"]:b["end"]] == b["content"] for b in blocks)
    assert [b["chapter_id"] for b in blocks] == ["C1"] * 3 + ["C2"] * 3
    assert "A\r\n\r\nB" in blocks[-1]["content"]
    assert blocks == sr.index_blocks(body, book)


def test_atomic_groups_preserve_unrelated(book, base):
    body = base["body_markdown"]
    blocks = sr.index_blocks(body, book)
    ids = list(fullbody_task_catalog(book))
    issues = [issue(blocks[1], ids[0]), issue(blocks[-1], ids[-1], "I2")]
    good, bad = group(body, blocks[1]), group(body, blocks[-1], "I2")
    bad["replacements"].append({"block_id": "invented", "original_sha256": "bad", "content": "bad"})
    result = sr.group_parser(body, blocks, issues)({"complete": True, "groups": [good, bad]})
    assert result["groups"] == [good]
    assert result["pending_issues"][0]["issue_id"] == "I2"
    revised = sr.apply_groups(body, blocks, result["groups"])
    assert revised == body[:blocks[1]["start"]] + good["replacements"][0]["content"] + body[blocks[1]["end"]:]


def test_multiple_blocks_and_hash_rejection(book, base):
    body = base["body_markdown"]
    blocks = sr.index_blocks(body, book)
    i = issue(blocks[1], next(iter(fullbody_task_catalog(book))))
    i["target_block_ids"].append(blocks[-1]["block_id"])
    g = group(body, blocks[1])
    g["replacements"] += group(body, blocks[-1])["replacements"]
    parse = sr.group_parser(body, blocks, [i])
    assert len(parse({"complete": True, "groups": [g]})["groups"]) == 1
    g["replacements"][-1]["original_sha256"] = "wrong"
    assert not parse({"complete": True, "groups": [g]})["groups"]


def test_preview_no_client_and_full_reader(tmp_path, book, base, config):
    f = Factory()
    result = run(tmp_path, book, base, config, f)
    assert not f.calls and result["body_complete"] and not result["revision_complete"]
    messages = json.loads(open(result["stages"][0]["messages_path"]).read())
    p = json.loads(messages[-1]["content"])
    assert "".join(b["separator_before"] + b["content"] for b in p["blocks"]) + p["trailing_separator"] == base["body_markdown"]
    assert "body_markdown" not in p
    assert p["tasks"] and p["research_question"] == book["research_question"]
    assert not p["evidence_atoms"] and "reread_source_ids" not in p


def test_run_cache_and_cost(tmp_path, book, base, config):
    f = Factory()
    result = run(tmp_path, book, base, config, f, run=True)
    assert result["complete"] and result["body_complete"] and len(f.calls) == 2
    assert "Untouched text.\r\n\r\n# Chapter 2" in result["body_markdown"]
    p = f.calls[-1][1]
    assert p["body_markdown"] == base["body_markdown"] and p["neighbor_blocks"]
    assert "CONDITION_TAIL_1" in json.dumps(p["evidence_atoms"])
    assert result["cost_summary"]["base_reuse_charged_again"] is False
    assert result["cost_summary"]["total_known_cost_including_base_cny"] >= 3
    cached = run(tmp_path, book, base, config, f, run=True)
    assert len(f.calls) == 2 and cached["model_calls"] == 0
    assert cached["body_markdown"] == result["body_markdown"]


@pytest.mark.parametrize("mode", ["failure", "unknown", "repeat"])
def test_failure_preserves_base(tmp_path, book, base, config, mode):
    f = Factory(mode)
    result = run(tmp_path, book, base, config, f, run=True)
    assert result["body_markdown"] == base["body_markdown"] and result["body_complete"]
    assert not result["revision_complete"] and result["pending_issues"]
    assert len(f.calls) <= 3


def test_partial_preserves_valid_other_issue(tmp_path, book, base, config):
    result = run(tmp_path, book, base, config, Factory("partial"), run=True)
    assert not result["complete"] and result["body_complete"]
    assert len(result["accepted_groups"]) == 1 and result["pending_issues"]
    assert "Second argument." in result["body_markdown"]


def test_reread_complete_source(tmp_path, book, base, config):
    f = Factory("reread")
    result = run(tmp_path, book, base, config, f, run=True)
    assert result["complete"] and len(f.calls) == 3
    assert "CONDITION_TAIL_2" in json.dumps(f.calls[-1][1]["evidence_atoms"])


def test_no_issues_valid_and_no_revision_call(tmp_path, book, base, config):
    f = Factory("empty")
    result = run(tmp_path, book, base, config, f, run=True)
    assert result["complete"] and len(f.calls) == 1 and result["semantic_quality_unreviewed"]


def test_overlap_between_issues_rejected(tmp_path, book, base, config):
    result = run(tmp_path, book, base, config, Factory("overlap"), run=True)
    assert len(result["accepted_groups"]) == 1 and not result["revision_complete"]


def test_base_input_mismatch_rejected(tmp_path, book, base, config):
    base["input_hash"] = "other"
    with pytest.raises(sr.CandidateError, match="input_hash_mismatch"):
        run(tmp_path, book, base, config)


def test_reuse_partial_revision_complete_body(tmp_path, book, base, config):
    base["complete"] = False
    result = run(tmp_path, book, base, config, Factory("empty"), run=True)
    assert result["complete"]


def test_group_failure_is_retryable_and_keeps_valid_group(book, base):
    blocks = sr.index_blocks(base["body_markdown"], book)
    ids = list(fullbody_task_catalog(book))
    issues = [issue(blocks[1], ids[0]), issue(blocks[-1], ids[-1], "I2")]
    good, bad = group(base["body_markdown"], blocks[1]), group(base["body_markdown"], blocks[-1], "I2")
    bad["original_body_sha256"] = "wrong"
    parsed = sr.group_parser(base["body_markdown"], blocks, issues)({"complete": True, "groups": [good, bad]})
    assert parsed["protocol_complete"] and not parsed["complete"] and parsed["groups"] == [good]


def test_duplicate_group_cannot_partially_apply(book, base):
    blocks = sr.index_blocks(base["body_markdown"], book)
    i = issue(blocks[1], next(iter(fullbody_task_catalog(book))))
    g = group(base["body_markdown"], blocks[1])
    parsed = sr.group_parser(base["body_markdown"], blocks, [i])({"complete": True, "groups": [g, g]})
    assert not parsed["groups"] and not parsed["complete"]


def test_malformed_reader_preserves_other_issue(book, base):
    blocks = sr.index_blocks(base["body_markdown"], book)
    catalog = fullbody_task_catalog(book)
    i = issue(blocks[1], next(iter(catalog)))
    parsed = sr.reader_parser(blocks, catalog, book)({"complete": True, "issues": [i, {"target_block_ids": [[]]}]})
    assert parsed["issues"] == [i] and parsed["pending_issues"]


def test_call_bound_preserves_pending_issue(tmp_path, book, base, config):
    config["max_author_calls"] = 1
    f = Factory()
    result = run(tmp_path, book, base, config, f, run=True)
    assert len(f.calls) == 1 and result["pending_issues"][0]["status"] == "call_limit_reached"


def test_malformed_base_hash_rejected(tmp_path, book, base, config):
    base["body_sha256"] = "wrong"
    with pytest.raises(sr.CandidateError, match="body_hash_mismatch"):
        run(tmp_path, book, base, config)


def test_missing_task_ids_never_expands_whole_book_evidence(book, base):
    pack = sr.we.compile_evidence(book)
    blocks = sr.index_blocks(base["body_markdown"], book)
    i = issue(blocks[1], next(iter(pack["tasks"])))
    i["related_task_ids"] = []
    scoped, atoms = sr._issue_evidence(pack, book, blocks, i)
    payload = sr._editor_payload(pack, base["body_markdown"], blocks, scoped, atoms, [])
    assert all(atom["source_handle"] == "P0001" for atom in payload["evidence_atoms"])
    i["target_block_ids"] = [blocks[2]["block_id"]]
    scoped, atoms = sr._issue_evidence(pack, book, blocks, i)
    assert scoped["fallback_intent_only"] and atoms == []


def test_compound_citations_and_numbered_exact_title(book):
    assert sr._cited_handles(book, "[P0001, P0002; unknown]") == ["P0001", "P0002"]
    book["chapters"][0]["chapter_frame"]["title"] = "An exact title"
    blocks = sr.index_blocks("# 第一章 An exact title\n\nPassage.", book)
    assert all(b["chapter_id"] == "C1" for b in blocks)


def test_disk_body_bytes_preserved(tmp_path, book, base, config):
    result = run(tmp_path, book, base, config, Factory("empty"), run=True)
    assert (tmp_path / "FULL_BODY.md").read_bytes() == base["body_markdown"].encode()


def test_archived_three_reader_issue_scopes_and_payload_sizes():
    """Historical evidence of addressing, not a new model/science-quality test."""
    import hashlib
    from pathlib import Path
    archive = Path(__file__).resolve().parents[2] / "docs/acceptance/fullbody-seven-routes-20261008"
    def archived(path):
        if path.exists():
            return json.loads(path.read_bytes())
        manifest = json.loads(path.with_name(path.name + ".parts.json").read_bytes())
        chunks = []
        for part in manifest["parts"]:
            raw = (archive / part["path"]).read_bytes()
            if len(raw) == part["bytes"] + 2 and raw.endswith(b"\r\n"):
                raw = raw[:-2]
            assert len(raw) == part["bytes"] and hashlib.sha256(raw).hexdigest() == part["sha256"]
            chunks.append(raw)
        raw = b"".join(chunks)
        assert hashlib.sha256(raw).hexdigest() == manifest["source_sha256"]
        return json.loads(raw)
    book = archived(archive / "plain_whole/FULL_BODY_INPUT.json")
    reader_dir = next((archive / "live_reader_revision/stages/reader_full_body").glob("*/attempt_001"))
    body = json.loads(archived(reader_dir / "MESSAGES.json")[-1]["content"])["body_markdown"]
    historical = sr.fw._object(archived(reader_dir / "RAW_RESPONSE.json"))["issues"]
    pack = sr.we.compile_evidence(book)
    blocks = sr.index_blocks(body, book)
    assert all(b["chapter_id"] is not None for b in blocks)
    reader = sr._reader_payload(pack, body, blocks)
    assert "".join(b["separator_before"] + b["content"] for b in reader["blocks"]) + reader["trailing_separator"] == body
    assert len(json.dumps(reader, ensure_ascii=False, separators=(",", ":"))) < 300000
    for old, chapter in zip(historical, ("Ch1", "Ch2", "Ch5")):
        assert body.count(old["anchor"]) == 1
        position = body.index(old["anchor"])
        target = next(b for b in blocks if b["start"] <= position < b["end"])
        assert target["chapter_id"] == chapter
        i = {"issue_id": old["issue_id"], "target_block_ids": [target["block_id"]], "related_task_ids": [],
             "source_handles": [], "reason": old["problem"], "goal": old["suggested_action"]}
        i, atoms = sr._issue_evidence(pack, book, blocks, i)
        payload = sr._editor_payload(pack, body, blocks, i, atoms, [])
        assert len(json.dumps(payload, ensure_ascii=False, separators=(",", ":"))) < 500000
        assert "reread_full_source_records" not in payload
