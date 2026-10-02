"""Offline regression tests for provider-alias material reuse."""

import json
from pathlib import Path

from optomind_research.runtime.upgrade3 import planning_supplement
from optomind_research.runtime.upgrade3.progressive_review_plan import (
    ProgressivePlannerConfig,
    make_planning_supplement_runner,
)


DOI = "10.1038/s41467-022-33116-z"


def _pool_row(tmp_path: Path, *, with_assets: bool = True) -> dict:
    row = {
        "paper_id": "CorpusId:252309032",
        "planning_view": {
            "paper_identity": {
                "canonical_paper_id": "CorpusId:252309032",
                "doi": DOI,
                "title": "Inhibition of UBA6 by inosine augments tumour immunogenicity and responses",
            }
        },
        "_b_summary": {"declared_content_depth": "fulltext"},
    }
    if not with_assets:
        row["card_path"] = str(tmp_path / "missing" / "PAPER_READING_CARD.json")
        return row

    source_unit = tmp_path / "existing" 
    card_path = source_unit / "card" / "PAPER_READING_CARD.json"
    snapshot = source_unit / "materials" / "CorpusId_252309032" / "snapshot-existing"
    card_path.parent.mkdir(parents=True)
    snapshot.mkdir(parents=True)
    (snapshot / "manifest.json").write_text(json.dumps({"snapshot_id": "snapshot-existing"}), encoding="utf-8")
    (source_unit / "SOURCE_UNIT.json").write_text(
        json.dumps({"snapshot_path": str(snapshot)}), encoding="utf-8"
    )
    card_path.write_text(
        json.dumps({"material": {"snapshot_id": "snapshot-existing"}}), encoding="utf-8"
    )
    row["card_path"] = str(card_path)
    return row


def _reuse_callback(tmp_path: Path, monkeypatch, pool_row: dict):
    captured = {}
    acquire_calls = []

    def fake_judge(**kwargs):
        return object()

    def fake_supplement(
        request_path, *, output_dir, acquirer_factory, reuse_material, **kwargs
    ):
        record = {
            "canonical_paper_id": "OpenAlex:W4296038319",
            "doi": "https://doi.org/" + DOI,
            "title": "Inhibition of UBA6 by inosine augments tumour immunogenicity and responses",
        }
        reused = reuse_material(record=record, pool_rows=[pool_row])
        captured["reused"] = reused
        if reused is None:
            acquire_calls.append(record)
        return {"status": "fulfilled", "output_dir": str(output_dir)}

    monkeypatch.setattr(planning_supplement, "run_qwen_fulfillment_judge", fake_judge)
    monkeypatch.setattr(planning_supplement, "run_planning_supplement", fake_supplement)
    config = ProgressivePlannerConfig(
        topic_id="reuse-test",
        pool_path=tmp_path / "pool.jsonl",
        plan_path=tmp_path / "plan.json",
        output_dir=tmp_path / "run",
    )
    runner = make_planning_supplement_runner(
        config,
        key_file=tmp_path / "missing-key.txt",
        budget_ledger_path=tmp_path / "budget.sqlite",
        budget_limit_cny=50,
    )
    runner(
        [{"gap_id": "G1", "gap_question": "test"}],
        phase="level1",
        output_dir=tmp_path / "phase",
        pool_rows=[pool_row],
        plan={"research_question": "test"},
        skip_local_triage=True,
    )
    return captured["reused"], acquire_calls


def test_unique_doi_alias_reuses_existing_material_without_acquisition(tmp_path, monkeypatch):
    reused, acquire_calls = _reuse_callback(tmp_path, monkeypatch, _pool_row(tmp_path))

    assert reused is not None
    assert reused["snapshot"].name == "snapshot-existing"
    assert reused["card_path"].endswith("PAPER_READING_CARD.json")
    assert acquire_calls == []


def test_unrelated_doi_does_not_false_match(tmp_path, monkeypatch):
    row = _pool_row(tmp_path)
    row["planning_view"]["paper_identity"]["doi"] = "10.9999/unrelated"
    reused, acquire_calls = _reuse_callback(tmp_path, monkeypatch, row)

    assert reused is None
    assert len(acquire_calls) == 1


def test_doi_match_without_assets_falls_back_to_acquisition(tmp_path, monkeypatch):
    reused, acquire_calls = _reuse_callback(
        tmp_path, monkeypatch, _pool_row(tmp_path, with_assets=False)
    )

    assert reused is None
    assert len(acquire_calls) == 1

