"""Offline wiring checks for the basic Docling route and local formula hook."""
from __future__ import annotations

import json
from pathlib import Path

from scripts.upgrade3 import build_local_materials as materials
from scripts.upgrade3 import progressive_review_plan as cli
from optomind_research.runtime.upgrade3 import planning_supplement as supplement
from optomind_research.runtime.upgrade3 import progressive_review_plan as planning
from test_docling_primary_material import _fixture


def test_basic_parser_flags_are_shared_by_material_and_plan_clis(tmp_path):
    data, _, _, config = _fixture(tmp_path)
    pdf = tmp_path / "paper.pdf"
    pdf.write_bytes(data)
    formula_config = tmp_path / "formula.json"
    formula_config.write_text(json.dumps({"python": "python", "driver_config": "driver.json"}), encoding="utf-8")
    material_args = materials._build_parser().parse_args([
        "--input", str(pdf), "--output-root", str(tmp_path / "materials"),
        "--material-parser-mode", "docling_basic",
        "--docling-document-json", str(config.document_path),
        "--docling-run-json", str(config.run_metadata_path),
        "--disable-rescue",
    ])
    assert materials.material_parser_mode(material_args) == "docling_basic"
    factory = materials.make_material_acquirer_factory(material_args)
    acquirer = factory(tmp_path / "snapshots")
    assert acquirer.config.docling_enabled and acquirer.config.docling_primary
    assert acquirer.providers["docling"].__class__.__name__ == "DoclingCliAdapter"

    plan_args = cli.parser().parse_args([
        "--pool", "pool.jsonl", "--plan", "plan.json", "--output-dir", "output",
        "--material-parser-mode", "docling_basic",
        "--docling-document-json", str(config.document_path),
        "--docling-run-json", str(config.run_metadata_path),
        "--local-formula-config", str(formula_config),
        "--local-formula-cache-root", str(tmp_path / "formula-cache"),
    ])
    assert materials.material_parser_mode(plan_args) == "docling_basic"
    assert plan_args.local_formula_config == formula_config
    assert plan_args.local_formula_cache_root == tmp_path / "formula-cache"


def test_planning_supplement_receives_shared_basic_factory_without_external_call(tmp_path, monkeypatch):
    captured = {}

    def fake_supplement(request_path, *, output_dir, acquirer_factory, **kwargs):
        captured["factory"] = acquirer_factory
        captured["request"] = Path(request_path)
        return {"output_dir": str(output_dir), "status": "completed", "source_units": []}

    monkeypatch.setattr(supplement, "run_planning_supplement", fake_supplement)
    monkeypatch.setattr(supplement, "run_qwen_fulfillment_judge", lambda **_: object())
    monkeypatch.setattr(supplement, "CompositeS2OpenAlexGateway", lambda *_: object())
    shared_factory = object()
    cfg = planning.ProgressivePlannerConfig(
        topic_id="docling-wiring", pool_path=tmp_path / "pool.jsonl",
        plan_path=tmp_path / "plan.json", output_dir=tmp_path / "planning",
    )
    runner = planning.make_planning_supplement_runner(
        cfg, key_file=tmp_path / "missing.key", budget_ledger_path=tmp_path / "budget.sqlite",
        budget_limit_cny=0, allow_external=True, acquirer_factory=shared_factory,
    )
    result = runner([{
        "gap_id": "G1", "gap_question": "Which local evidence is needed?",
        "known_papers": [], "targeted_queries": [],
    }], phase="level1", pool_rows=[], plan={}, skip_local_triage=True)
    assert result["results"]
    assert captured["factory"] is shared_factory
    assert captured["request"].is_file()


def test_local_formula_profile_is_bound_to_directed_reader_runtime(tmp_path):
    profile = tmp_path / "hook.json"
    driver = tmp_path / "driver.json"
    driver.write_text(json.dumps({"model_cache": str(tmp_path / "model-cache"), "device": "cpu"}), encoding="utf-8")
    profile.write_text(json.dumps({"python": "python", "driver_config": str(driver)}), encoding="utf-8")
    cfg = planning.ProgressivePlannerConfig(
        topic_id="formula-wiring", pool_path=tmp_path / "pool.jsonl",
        plan_path=tmp_path / "plan.json", output_dir=tmp_path / "planning",
        local_formula_config_path=profile,
    )
    settings = planning._directed_reader_settings(cfg)
    assert settings["local_formula_config_sha256"]
