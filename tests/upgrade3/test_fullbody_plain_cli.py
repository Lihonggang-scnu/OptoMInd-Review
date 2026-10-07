"""Offline CLI coverage for plain complete-BODY drafts and global integration."""
from __future__ import annotations

import copy
import hashlib

import pytest

from optomind_research.runtime.upgrade3 import fullbody_writer as engine
from optomind_research.runtime.upgrade3.module4 import runtime
from scripts.upgrade3 import fullbody_writer as cli
from test_fullbody_cli import case, dump, no_network  # noqa: F401


PLAIN_ROUTES = ("plain_whole", "chapter_concat", "hierarchical_full")
CHAPTER_TEXTS = ("First independent chapter explains conditions [P0001].",
                 "Second independent chapter explains comparisons [P0001].")
WHOLE_TEXT = "The complete body explains conditions and comparisons [P0001]."
INTEGRATED_TEXT = "The integrated complete body connects both conditional comparisons [P0001]."


@pytest.fixture(autouse=True)
def no_live_transport(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Plain CLI regression tests must not construct live transport or a ledger")
    monkeypatch.setattr(runtime, "QwenDirectClient", forbidden)
    monkeypatch.setattr(runtime, "GlobalBudgetLedger", forbidden)
    monkeypatch.setattr(cli, "make_live_factory", forbidden)


@pytest.fixture
def recordings(tmp_path, case):
    book, _ = cli.load_body_manifest(case["manifest"])
    ids = list(book["task_catalog"])

    def response(task_ids, body):
        return {"body_markdown": body, "completed_task_ids": task_ids, "complete": True}

    return dump(tmp_path / "plain-recordings.json", {
        "plain_whole": response(ids, WHOLE_TEXT),
        "chapter_001": response([key for key in ids if key.startswith("CH01::")], CHAPTER_TEXTS[0]),
        "chapter_002": response([key for key in ids if key.startswith("CH02::")], CHAPTER_TEXTS[1]),
        "integrate_full_body": response(ids, INTEGRATED_TEXT),
    })


def _args(case, output, route, *extra):
    return ["--manifest", str(case["manifest"]), "--output", str(output), "--route", route, *extra]


def _draft(book, **changes):
    body = "\n\n".join(CHAPTER_TEXTS)
    result = {"complete": True, "body_markdown": body,
        "body_sha256": hashlib.sha256(body.encode()).hexdigest(),
        "input_hash": cli._hash(book), "execution_mode": "recording",
        "completed_task_ids": list(book["task_catalog"]), "pending_task_ids": [],
        "requested_route": "chapter_concat", "effective_route": "chapter_concat"}
    result.update(changes)
    return result


def test_parser_exposes_all_plain_routes_without_changing_default():
    parser = cli.parser()
    assert set(PLAIN_ROUTES) <= set(cli.ROUTES)
    assert parser.parse_args(["--manifest", "explicit.json", "--output", "out"]).route == "whole_author"
    for route in PLAIN_ROUTES:
        args = parser.parse_args(["--manifest", "explicit.json", "--output", "out", "--route", route])
        assert args.route == route and not args.run and not args.allow_max
        assert args.draft is None


@pytest.mark.parametrize("route", ("whole_author", "continuous_author", "workbench", "plain_whole", "chapter_concat"))
def test_draft_is_rejected_for_nonrevision_nonhierarchical_routes(tmp_path, monkeypatch, capsys, route):
    def forbidden(*args, **kwargs):
        pytest.fail("Unsupported --draft must fail before reading material or the draft")
    monkeypatch.setattr(cli, "load_body_manifest", forbidden)
    monkeypatch.setattr(cli, "load_draft", forbidden)
    assert cli.main(["--manifest", "unread.json", "--output", str(tmp_path), "--route", route,
                     "--draft", "unread/FULL_BODY_RESULT.json"]) == 2
    assert "draft_only_supported" in capsys.readouterr().err


@pytest.mark.parametrize("route", PLAIN_ROUTES)
def test_plain_routes_reject_continuation_before_material_or_live_factory(tmp_path, monkeypatch, capsys, route):
    def forbidden(*args, **kwargs):
        pytest.fail("Unsupported plain-route continuation must fail before material or transport setup")
    monkeypatch.setattr(cli, "load_body_manifest", forbidden)
    monkeypatch.setattr(cli, "load_draft", forbidden)
    monkeypatch.setattr(cli, "make_live_factory", forbidden)
    output = tmp_path / "output"
    ledger = tmp_path / "ledger.sqlite"
    assert cli.main(["--manifest", "unread.json", "--output", str(output), "--route", route,
        "--continue-incomplete", "--run", "--budget-ledger", str(ledger), "--budget-limit", "100"]) == 2
    assert "continue_incomplete_not_supported_for_plain_routes" in capsys.readouterr().err
    assert not output.exists()
    assert not ledger.exists()


@pytest.mark.parametrize("route", PLAIN_ROUTES)
def test_plain_preview_uses_real_stages_with_no_transport_or_credentials(tmp_path, case, route):
    output = tmp_path / route
    assert cli.main(_args(case, output, route, "--key-file", "/never/read/secret")) == 0
    report = cli.read_json(output / "CLI_RUN.json")
    manifest = cli.read_json(output / "RUN_MANIFEST.json")
    stages = manifest["stages"]
    expected = ["plain_whole"] if route == "plain_whole" else ["chapter_001", "chapter_002"]
    assert report["execution_mode"] == "preview" and report["complete"] is False
    assert report["requested_route"] == report["effective_route"] == route
    assert report["expected_chapter_ids"] == report["actual_chapter_ids"] == ["CH01", "CH02"]
    assert [stage["stage_id"] for stage in stages] == expected
    assert all(stage["role"] == "writer" and stage["status"] == "planned" for stage in stages)
    assert all(stage["model_calls"] == stage["paid_dispatch_count"] == 0 for stage in stages)
    assert (output / "SOURCE_MANIFEST.json").is_file()
    prompt_route = "plain_whole" if route == "plain_whole" else "chapter_concat"
    expected_prompt = "\n\n".join((engine.PROMPT_ROOT / (name + ".md")).read_text(encoding="utf-8")
                                for name in ("plain_writer", prompt_route))
    for stage in stages:
        messages = cli.read_json(stage["messages_path"])
        assert messages[0]["content"] == expected_prompt
    assert "/never/read/secret" not in "\n".join(path.read_text() for path in output.rglob("*.json"))


@pytest.mark.parametrize("route,expected_calls,expected_body", [
    ("plain_whole", ["plain_whole"], WHOLE_TEXT),
    ("chapter_concat", ["chapter_001", "chapter_002"], "\n\n".join(CHAPTER_TEXTS)),
    ("hierarchical_full", ["chapter_001", "chapter_002", "integrate_full_body"], INTEGRATED_TEXT),
])
def test_plain_recordings_complete_full_body_and_resume_without_calls(
        tmp_path, case, recordings, route, expected_calls, expected_body):
    output = tmp_path / route
    argv = _args(case, output, route, "--responses", str(recordings))
    assert cli.main(argv) == 0
    report = cli.read_json(output / "CLI_RUN.json")
    result = cli.read_json(output / "FULL_BODY_RESULT.json")
    book, _ = cli.load_body_manifest(case["manifest"])
    assert report["recorded_response_calls"] == expected_calls
    assert result["complete"] is True and result["body_markdown"] == expected_body
    assert result["completed_task_ids"] == list(book["task_catalog"])
    assert result["pending_task_ids"] == [] and result["input_hash"] == cli._hash(book)
    assert report["execution_mode"] == "recording" and report["current_run_cost_cny"] == 0
    assert result["paid_dispatch_count"] == 0
    expected_roles = ["writer"] * len(expected_calls)
    if route == "hierarchical_full":
        expected_roles[-1] = "reviser"
        independent = cli.read_json(output / "INDEPENDENT_FULL_BODY_RESULT.json")
        assert independent["complete"] and independent["effective_route"] == "chapter_concat"
        assert independent["body_markdown"] == "\n\n".join(CHAPTER_TEXTS)
        assert result["global_integration_performed"] is True
    assert [stage["role"] for stage in result["stage_lineage"]] == expected_roles
    assert cli.main(argv) == 0
    resumed = cli.read_json(output / "CLI_RUN.json")
    assert resumed["complete"] is True and resumed["recorded_response_calls"] == []
    assert resumed["body_markdown"] == expected_body
    assert cli.read_json(output / "FULL_BODY_RESULT.json")["body_sha256"] == result["body_sha256"]


@pytest.mark.parametrize("draft_name,fallback_route", [
    ("FULL_BODY_RESULT.json", False),
    ("INDEPENDENT_FULL_BODY_RESULT.json", False),
    ("FULL_BODY_RESULT.json", True),
])
def test_hierarchical_reuses_explicit_independent_draft_without_writer_calls(
        tmp_path, case, recordings, draft_name, fallback_route):
    draft_output = tmp_path / "draft"
    assert cli.main(_args(case, draft_output, "chapter_concat", "--responses", str(recordings))) == 0
    original = cli.read_json(draft_output / draft_name)
    if fallback_route:
        original.pop("effective_route")
    draft = dump(tmp_path / "selected" / draft_name, original)
    draft_bytes = draft.read_bytes()
    # A missing writer recording is deliberate: any draft call is a regression.
    edit_only = dump(tmp_path / "integration-only.json", {
        "integrate_full_body": cli.read_json(recordings)["integrate_full_body"]})
    output = tmp_path / "integration"
    argv = _args(case, output, "hierarchical_full", "--draft", str(draft), "--responses", str(edit_only))
    assert cli.main(argv) == 0
    report = cli.read_json(output / "CLI_RUN.json")
    manifest = cli.read_json(output / "RUN_MANIFEST.json")
    assert report["complete"] is True and report["body_markdown"] == INTEGRATED_TEXT
    assert report["recorded_response_calls"] == ["integrate_full_body"]
    assert [(stage["stage_id"], stage["role"]) for stage in manifest["stages"]] == [
        ("integrate_full_body", "reviser")]
    assert report["cost_summary"]["base_reuse_charged_again"] is False
    assert (output / "INDEPENDENT_FULL_BODY.md").read_text() == original["body_markdown"]
    assert draft.read_bytes() == draft_bytes
    assert cli.main(argv) == 0
    assert cli.read_json(output / "CLI_RUN.json")["recorded_response_calls"] == []


@pytest.mark.parametrize("filename", ("FULL_BODY_RESULT.json", "INDEPENDENT_FULL_BODY_RESULT.json"))
def test_hierarchical_draft_loader_accepts_only_complete_independent_bodies(tmp_path, case, filename):
    book, _ = cli.load_body_manifest(case["manifest"])
    path = dump(tmp_path / filename, _draft(book))
    loaded = cli.load_draft(path, route="hierarchical_full")
    assert loaded["_loaded_from"] == {"path": str(path.resolve()), "sha256": cli.sha256_file(path),
                                     "body_sha256": loaded["body_sha256"]}
    if filename == "FULL_BODY_RESULT.json":
        assert cli.load_draft(path)["body_markdown"] == loaded["body_markdown"]
    else:
        with pytest.raises(ValueError, match="FULL_BODY_RESULT"):
            cli.load_draft(path)


def test_hierarchical_rejects_chapter_result_filename_before_reading_it(tmp_path, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("A chapter-only result path must be rejected before reading its content")
    monkeypatch.setattr(cli, "read_json", forbidden)
    with pytest.raises(ValueError, match="FULL_BODY_RESULT"):
        cli.load_draft(tmp_path / "CHAPTER_RESULT.json", route="hierarchical_full")


@pytest.mark.parametrize("changes,error", [
    ({"complete": False}, "complete_fullbody_draft"),
    ({"pending_task_ids": ["CH02::U1::P1"]}, "complete_fullbody_draft"),
    ({"body_markdown": ""}, "no_body"),
    ({"body_sha256": "0" * 64}, "body_hash_mismatch"),
    ({"effective_route": "whole_author"}, "chapter_concat"),
    ({"effective_route": "hierarchical_full"}, "chapter_concat"),
    ({"effective_route": "whole_author", "requested_route": "chapter_concat"}, "chapter_concat"),
    ({"input_hash": "0" * 64}, "input_hash_mismatch"),
])
def test_invalid_hierarchical_draft_fails_before_recording_factory(
        tmp_path, case, monkeypatch, capsys, changes, error):
    book, _ = cli.load_body_manifest(case["manifest"])
    draft = dump(tmp_path / "FULL_BODY_RESULT.json", _draft(book, **changes))

    def forbidden(*args, **kwargs):
        pytest.fail("Invalid draft must fail before any recording or provider factory is constructed")
    monkeypatch.setattr(cli, "RecordingFactory", forbidden)
    assert cli.main(_args(case, tmp_path / "out", "hierarchical_full", "--draft", str(draft),
                          "--responses", "unread-recordings.json")) == 2
    assert error in capsys.readouterr().err
    assert not (tmp_path / "out" / "CLI_RUN.json").exists()


@pytest.mark.parametrize("mode", ("recording", "preview", None))
def test_live_hierarchy_rejects_nonlive_reused_draft_before_factory(tmp_path, case, capsys, mode):
    book, _ = cli.load_body_manifest(case["manifest"])
    draft = dump(tmp_path / "FULL_BODY_RESULT.json", _draft(book, execution_mode=mode))
    ledger = tmp_path / "never-created.sqlite"
    assert cli.main(_args(case, tmp_path / "out", "hierarchical_full", "--draft", str(draft), "--run",
                          "--budget-ledger", str(ledger), "--budget-limit", "100")) == 2
    assert "live_hierarchical_full_requires_live_fullbody_draft" in capsys.readouterr().err
    assert not ledger.exists()


@pytest.mark.parametrize("route,reuse,role,requires_permission", [
    ("plain_whole", False, "writer", True),
    ("plain_whole", False, "reader", False),
    ("plain_whole", False, "reviser", False),
    ("chapter_concat", False, "writer", True),
    ("chapter_concat", False, "reader", False),
    ("chapter_concat", False, "reviser", False),
    ("hierarchical_full", False, "writer", True),
    ("hierarchical_full", False, "reader", False),
    ("hierarchical_full", False, "reviser", True),
    ("hierarchical_full", True, "writer", False),
    ("hierarchical_full", True, "reader", False),
    ("hierarchical_full", True, "reviser", True),
    ("reader_revision", True, "writer", False),
    ("reader_revision", True, "reader", True),
    ("reader_revision", True, "reviser", True),
])
def test_live_max_permission_checks_only_roles_that_can_run_before_any_inputs(
        tmp_path, monkeypatch, capsys, route, reuse, role, requires_permission):
    config = copy.deepcopy(cli.read_json(cli.DEFAULT_CONFIG))
    config[role]["model"] = "qwen3.8-max"
    config_path = dump(tmp_path / "max-config.json", config)

    def material_marker(*args, **kwargs):
        if requires_permission:
            pytest.fail("Max permission must be checked before reading material")
        raise ValueError("material_read_marker_after_role_gate")

    def forbidden(*args, **kwargs):
        pytest.fail("Role-gate test must never read the draft or create a factory")
    monkeypatch.setattr(cli, "load_body_manifest", material_marker)
    monkeypatch.setattr(cli, "load_draft", forbidden)
    argv = ["--manifest", "unread.json", "--output", str(tmp_path / "out"), "--route", route,
            "--config", str(config_path), "--run", "--budget-ledger", str(tmp_path / "ledger.sqlite"),
            "--budget-limit", "100"]
    if reuse:
        argv.extend(["--draft", "unread/FULL_BODY_RESULT.json"])
    assert cli.main(argv) == 2
    error = capsys.readouterr().err
    expected = "max_execution_requires_allow_max:" + role if requires_permission else "material_read_marker_after_role_gate"
    assert expected in error
    assert not (tmp_path / "ledger.sqlite").exists()


def test_explicit_allow_max_passes_hierarchical_reviser_gate_without_creating_transport(tmp_path, monkeypatch, capsys):
    config = copy.deepcopy(cli.read_json(cli.DEFAULT_CONFIG))
    config["reviser"]["model"] = "qwen3.8-max"
    config_path = dump(tmp_path / "max-config.json", config)

    def material_marker(*args, **kwargs):
        raise ValueError("material_read_marker_after_approved_max")
    monkeypatch.setattr(cli, "load_body_manifest", material_marker)
    assert cli.main(["--manifest", "unread.json", "--output", str(tmp_path / "out"),
        "--route", "hierarchical_full", "--config", str(config_path), "--run", "--allow-max"]) == 2
    assert "material_read_marker_after_approved_max" in capsys.readouterr().err
