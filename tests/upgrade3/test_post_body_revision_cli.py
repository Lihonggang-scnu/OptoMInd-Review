"""No production network: subprocess recording tests and mocked live boundary."""
from __future__ import annotations

import argparse
import copy
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
CLI = ROOT / "scripts/upgrade3/post_body_revision.py"
FIXTURE = ROOT / "tests/fixtures/post_body_revision"
spec = importlib.util.spec_from_file_location("revision_cli", CLI)
cli = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cli)


def invoke(*args):
    return subprocess.run([sys.executable, str(CLI), *map(str, args)], cwd=ROOT,
                          capture_output=True, text=True, timeout=30)


def args(**updates):
    result = argparse.Namespace(run=False, allow_paid=False, recordings=None,
                                budget_cny=None, budget_ledger=None, key_file=None)
    result.__dict__.update(updates)
    return result


def config(variant="A"):
    return cli.validate_config(cli.read_json(ROOT / "config/post_body_revision" / (variant + ".json")), variant)


@pytest.mark.parametrize("variant", list("ABC"))
def test_real_subprocess_recording_and_resume(tmp_path, variant):
    command = ["run", "--case", FIXTURE / "case.json", "--variant", variant,
               "--recordings", FIXTURE / "recordings.json", "--output-root", tmp_path]
    result = invoke(*command)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["mode"] == "recording"
    resumed = invoke(*command, "--resume")
    assert resumed.returncode == 0, resumed.stderr
    output = tmp_path / variant / "recording"
    assert output.exists()
    report = json.loads((output / "report.json").read_text())
    assert report["counts"]["applied"] == 1
    assert report["counts"]["pending"] == 0
    assert report["execution_mode"] == "recording"
    assert any(p.name.startswith("candidate") for p in output.rglob("*"))


def test_preview_never_constructs_provider(tmp_path, monkeypatch):
    import optomind_research.runtime.upgrade3.module4.runtime as runtime
    def forbidden(**kwargs):
        pytest.fail("Preview constructed a provider or ledger")
    monkeypatch.setattr(runtime, "QwenDirectClient", forbidden)
    monkeypatch.setattr(runtime, "GlobalBudgetLedger", forbidden)
    assert cli.main(["run", "--case", str(FIXTURE / "case.json"), "--variant", "A",
                     "--key-file", "/never/read/this-secret", "--output-root", str(tmp_path)]) == 0
    assert (tmp_path / "A/preview/preview.json").exists()


@pytest.mark.parametrize("updates", [
    {}, {"run": True}, {"allow_paid": True},
    {"run": True, "allow_paid": True},
    {"run": True, "allow_paid": True, "budget_cny": float("nan"), "budget_ledger": "unused"},
    {"run": True, "allow_paid": True, "budget_cny": float("inf"), "budget_ledger": "unused"},
    {"run": True, "allow_paid": True, "budget_cny": -1, "budget_ledger": "unused"},
    {"run": True, "allow_paid": True, "budget_cny": 1},
    {"run": True, "allow_paid": True, "budget_cny": 1, "budget_ledger": "unused", "recordings": "any"},
])
def test_live_gate_before_provider(updates, monkeypatch):
    import optomind_research.runtime.upgrade3.module4.runtime as runtime
    def forbidden(**kwargs):
        pytest.fail("Invalid gate reached provider boundary")
    monkeypatch.setattr(runtime, "QwenDirectClient", forbidden)
    monkeypatch.setattr(runtime, "GlobalBudgetLedger", forbidden)
    with pytest.raises(ValueError):
        cli.make_live_client(args(**updates), config())


def test_live_uses_explicit_models_one_shared_ledger_no_retries(tmp_path, monkeypatch):
    import optomind_research.runtime.upgrade3.module4.runtime as runtime
    providers = []
    class Provider:
        def __init__(self, **kwargs):
            self.kwargs = kwargs
            providers.append(kwargs)
        def __call__(self, messages, **kwargs):
            assert kwargs["model"] == self.kwargs["model"]
            reservation = self.kwargs["budget_ledger"].reserve(0.1, kwargs["call_id"])
            self.kwargs["budget_ledger"].settle(reservation["reservation_id"], 0.08)
            return {"content": "{}", "usage": {"prompt_tokens": 1, "completion_tokens": 1}}
    monkeypatch.setattr(runtime, "QwenDirectClient", Provider)
    ledger_path = tmp_path / "budget.sqlite"
    for variant, model in [("A", "qwen3.7-flash"), ("C", "qwen3.5-plus")]:
        client = cli.make_live_client(args(run=True, allow_paid=True, budget_cny=1,
                                           budget_ledger=str(ledger_path), key_file="not-read"), config(variant))
        client("review", [], model=model)
    assert len(providers) == 2
    assert all(p["max_retries"] == 0 and p["max_keys"] == 1 for p in providers)
    assert runtime.GlobalBudgetLedger(limit_cny=1, path=ledger_path).as_dict()["actual_cny"] == pytest.approx(0.16)
    with pytest.raises(runtime.QwenTransportError):
        cli.make_live_client(args(run=True, allow_paid=True, budget_cny=2,
                                  budget_ledger=str(ledger_path)), config())


def test_config_rejects_incompatible_thinking():
    c = config("C")
    c["model_settings"]["qwen3.5-plus"]["thinking"] = True
    with pytest.raises(ValueError, match="thinking_json"):
        cli.validate_config(c, "C")


def test_prepare_preserves_crlf_and_bounded_fields(tmp_path):
    import gzip
    body = tmp_path / "body.md"
    body.write_bytes(b"SENTINEL first.\r\n\r\nSENTINEL second.\r\n")
    plan = tmp_path / "plan.json.gz"
    plan.write_bytes(gzip.compress(json.dumps({"research_question": "SENTINEL?", "shared_outline": ["Toy"],
       "chapters": [{"source_materials": [{"source_handle": "P0001", "study_summary_A": {"finding": "SENTINEL"},
                                            "raw_database": "EXCLUDED", "path": "/never/read"}]}]}).encode()))
    output = tmp_path / "case.json"
    result = invoke("prepare", "--body", body, "--plan", plan, "--output", output)
    assert result.returncode == 0, result.stderr
    case = json.loads(output.read_text())
    assert case["draft_text"].encode() == body.read_bytes()
    assert "EXCLUDED" not in output.read_text()
    assert case["materials"]["P0001"]["text"]


def test_prepare_rejects_conflicting_handles_and_unknown_shape(tmp_path):
    body = tmp_path / "body.md"
    body.write_text("SENTINEL")
    plan = tmp_path / "plan.json"
    for content in [{"unrecognized": []}, {"source_materials": [
        {"source_handle": "P0001", "paper_id": "first", "study_summary_A": "first"},
        {"source_handle": "P0001", "paper_id": "other", "study_summary_A": "different"}]}]:
        plan.write_text(json.dumps(content))
        result = invoke("prepare", "--body", body, "--plan", plan, "--output", tmp_path / "case.json")
        assert result.returncode == 2


def test_resume_rejects_changed_provider_options(tmp_path):
    c = config()
    path = tmp_path / "config.json"
    path.write_text(json.dumps(c))
    command = ["run", "--case", FIXTURE / "case.json", "--variant", "A", "--config", path,
               "--recordings", FIXTURE / "recordings.json", "--output-root", tmp_path / "out"]
    assert invoke(*command).returncode == 0
    c["model_settings"]["qwen3.7-flash"]["timeout_seconds"] = 301
    path.write_text(json.dumps(c))
    assert invoke(*command, "--resume").returncode == 2


def test_prepare_finalplan_shape_complements_and_content_variants(tmp_path):
    body = tmp_path / "body.md"
    body.write_text("SENTINEL [P0001]")
    plan = tmp_path / "plan.json"
    plan.write_text(json.dumps({
        "source_identity_map": {"P0001": {"paper_id": "toy", "doi": "10.toy/one", "card_path": "NOT_READ"}},
        "chapters": [
            {"source_identity_map": {"P0001": {"paper_id": "toy", "doi": "https://doi.org/10.toy/one"}},
             "source_materials": [{"source_handle": "P0001", "paper_id": "toy", "study_summary_A": "SENTINEL A"}]},
            {"source_materials": [{"source_handle": "P0001", "paper_id": "toy", "study_summary_A": "SENTINEL A", "deep_read_material": "SENTINEL extra"}]},
            {"source_materials": [{"source_handle": "P0001", "paper_id": "toy", "study_summary_A": "SENTINEL A"}]},
        ]}))
    output = tmp_path / "case.json"
    assert invoke("prepare", "--body", body, "--plan", plan, "--output", output).returncode == 0
    case = json.loads(output.read_text())
    assert len(case["materials"]) == 2
    assert len(case["source_identity_map"]["P0001"]["material_ids"]) == 2
    assert "SENTINEL extra" in output.read_text()


def test_prepare_keeps_explicit_material_identity_links(tmp_path):
    body = tmp_path / "body.md"
    body.write_text("SENTINEL")
    materials = tmp_path / "materials.json"
    materials.write_text(json.dumps({"materials": {"M1": {"text": "SENTINEL evidence"}},
                                     "source_identity_map": {"P0001": {"material_id": "M1"}}}))
    output = tmp_path / "case.json"
    result = invoke("prepare", "--body", body, "--materials", materials, "--output", output)
    assert result.returncode == 0, result.stderr
    assert json.loads(output.read_text())["source_identity_map"]["P0001"]["material_ids"] == ["M1"]


def test_failed_generation_has_nonzero_exit(tmp_path):
    recordings = tmp_path / "empty.json"
    recordings.write_text("{}")
    result = invoke("run", "--case", FIXTURE / "case.json", "--variant", "B", "--recordings", recordings,
                    "--output-root", tmp_path / "out")
    assert result.returncode == 2
    assert json.loads(result.stdout)["status"] == "failed"


def test_recording_never_constructs_provider_or_ledger(tmp_path, monkeypatch):
    import optomind_research.runtime.upgrade3.module4.runtime as runtime
    def forbidden(**kwargs):
        pytest.fail("Recording reached a paid boundary")
    monkeypatch.setattr(runtime, "QwenDirectClient", forbidden)
    monkeypatch.setattr(runtime, "GlobalBudgetLedger", forbidden)
    assert cli.main(["run", "--case", str(FIXTURE / "case.json"), "--variant", "B",
                     "--recordings", str(FIXTURE / "recordings.json"), "--key-file", "/never/read",
                     "--output-root", str(tmp_path)]) == 0


def test_recording_preserves_partial_response_metadata(tmp_path):
    recordings = tmp_path / "partial.json"
    recordings.write_text(json.dumps({"review": {"content": {"issues": []}, "complete": False}}))
    result = invoke("run", "--case", FIXTURE / "case.json", "--variant", "B", "--recordings", recordings,
                    "--output-root", tmp_path / "out")
    assert result.returncode == 2
    assert json.loads(result.stdout)["status"] == "failed"


def test_C_recording_exercises_one_distinct_model_escalation(tmp_path):
    result = invoke("run", "--case", FIXTURE / "case.json", "--variant", "C", "--recordings",
                    FIXTURE / "recordings_escalation.json", "--output-root", tmp_path)
    assert result.returncode == 0, result.stderr
    report = json.loads((tmp_path / "C/recording/report.json").read_text())
    assert report["counts"]["applied"] == 1
    calls = report["call_records"]
    assert len(calls) == 5
    assert [c["model"] for c in calls[-2:]] == ["qwen3.5-plus"] * 2


def test_live_preflight_rejects_overflow_before_provider(tmp_path, monkeypatch):
    import optomind_research.runtime.upgrade3.module4.runtime as runtime
    def forbidden(**kwargs):
        pytest.fail("Overflow reached a paid boundary")
    monkeypatch.setattr(runtime, "QwenDirectClient", forbidden)
    monkeypatch.setattr(runtime, "GlobalBudgetLedger", forbidden)
    c = config()
    c["max_input_chars"] = 1
    path = tmp_path / "tiny.json"
    path.write_text(json.dumps(c))
    assert cli.main(["run", "--case", str(FIXTURE / "case.json"), "--variant", "A", "--config", str(path),
                     "--run", "--allow-paid", "--budget-cny", "1", "--budget-ledger", str(tmp_path / "ledger.sqlite"),
                     "--output-root", str(tmp_path)]) == 2
    assert not (tmp_path / "ledger.sqlite").exists()


def test_prepare_preserves_fine_outline_and_review_argument(tmp_path):
    body = tmp_path / "body.md"
    body.write_text("SENTINEL body")
    chapter_plan = {"thesis": "SENTINEL thesis", "reader_objective": "SENTINEL objective", "units": [
        {"unit_id": "U1", "substantive_point": "SENTINEL point", "ordered_development": ["SENTINEL order"],
         "evidence_conditions": "SENTINEL boundary", "synthesis": "SENTINEL synthesis", "transition": "SENTINEL transition",
         "supporting_studies": [{"ignored_expansion": "EXCLUDED_CASE_EXPANSION"}],
         "paragraph_briefs": [{"point": "SENTINEL brief", "development": "SENTINEL development",
                               "finding_conditions": "SENTINEL condition", "source_handles": ["P0001"]}]}]}
    record = {"chapter": {"chapter_id": "C1"}, "chapter_plan": chapter_plan}
    plan = tmp_path / "plan.json"
    plan.write_text(json.dumps({"shared_scope": "SENTINEL scope", "review_argument": "SENTINEL argument",
                               "shared_outline": [{"chapter_id": "C1", "title": "SENTINEL coarse"}],
                               "chapters": [record], "writer_packets": [record],
                               "source_materials": [{"source_handle": "P0001", "study_summary_A": "SENTINEL evidence"}]}))
    output = tmp_path / "case.json"
    result = invoke("prepare", "--body", body, "--plan", plan, "--output", output)
    assert result.returncode == 0, result.stderr
    case = json.loads(output.read_text())
    assert case["scope"] == {"shared_scope": "SENTINEL scope", "review_argument": "SENTINEL argument"}
    contracts = case["outline"]["chapter_contracts"]
    assert len(contracts) == 1
    assert contracts[0]["thesis"] == "SENTINEL thesis"
    assert contracts[0]["units"][0]["paragraph_briefs"][0]["development"] == "SENTINEL development"
    assert contracts[0]["units"][0]["paragraph_briefs"][0]["finding_conditions"] == "SENTINEL condition"
    assert "EXCLUDED_CASE_EXPANSION" not in output.read_text()
    assert len(case["preparation_provenance"]["intent_projection"]["chapter_sources"]["C1"]) == 2
    changed = json.loads(plan.read_text())
    changed["writer_packets"][0]["chapter_plan"]["thesis"] = "SENTINEL different"
    plan.write_text(json.dumps(changed))
    result = invoke("prepare", "--body", body, "--plan", plan, "--output", tmp_path / "other.json")
    assert result.returncode == 2
    assert "conflicting_projected_chapter_plan" in result.stderr
