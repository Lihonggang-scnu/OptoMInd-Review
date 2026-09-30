import json
from pathlib import Path

from optomind_research.runtime.upgrade3 import planning_supplement as supplement_module
from optomind_research.runtime.upgrade3.progressive_review_plan import (
    ProgressivePlannerConfig,
    make_planning_supplement_runner,
)


def test_revision_supplement_retry_uses_new_attempt_and_preserves_first_output(tmp_path, monkeypatch):
    calls = []

    def stub_run(request_path, *, output_dir, **_kwargs):
        output = Path(output_dir)
        calls.append(output)
        output.mkdir(parents=True, exist_ok=True)
        if output.name == "G_LEGACY" and any(output.iterdir()):
            raise supplement_module.OutputDirectoryError("output_directory_must_be_new_or_empty")
        marker = output / "attempt-marker.txt"
        marker.write_text(f"run-{len(calls)}", encoding="utf-8")
        source = output / "source" / "SOURCE.json"
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_text("{}", encoding="utf-8")
        (output / "SUPPLEMENT_INDEX.json").write_text(json.dumps({
            "source_units": [{"output_dir": str(output), "source_path": str(source)}],
            "citation_anchors": [],
            "fulfillment_judgment": {},
            "substantive_gap_status": "resolved",
        }), encoding="utf-8")
        return {"status": "completed", "output_dir": str(output)}

    monkeypatch.setattr(supplement_module, "run_planning_supplement", stub_run)
    gap = {
        "gap_id": "G_RETRY",
        "gap_question": "Resolve the evidence gap.",
        "chapter_ids": ["CH01"],
    }
    phase_root = tmp_path / "revision" / "chapters"
    config = ProgressivePlannerConfig(
        topic_id="attempt-fixture",
        pool_path=tmp_path / "POOL.jsonl",
        plan_path=tmp_path / "PLAN.json",
        output_dir=tmp_path / "runner-out",
        planning_revision_enabled=True,
    )
    runner = make_planning_supplement_runner(
        config,
        key_file=tmp_path / "missing-key",
        budget_ledger_path=tmp_path / "budget.sqlite",
        budget_limit_cny=100,
        allow_external=True,
    )

    first = runner(
        [gap], phase="chapters", skip_local_triage=True,
        pool_rows=[], plan={}, output_dir=phase_root,
    )
    base = phase_root / "supplements" / "G_RETRY"
    first_marker = base / "attempt-marker.txt"
    assert calls[-1] == base
    assert first_marker.read_text(encoding="utf-8") == "run-1"
    assert first["results"][0]["output_dir"] == str(base)
    assert first["results"][0]["source_units"][0]["source_path"] == str(base / "source" / "SOURCE.json")

    second = runner(
        [gap], phase="chapters", skip_local_triage=True,
        pool_rows=[], plan={}, output_dir=phase_root,
    )
    retry = base / "attempt_02"
    assert calls[-1] == retry
    assert first_marker.read_text(encoding="utf-8") == "run-1"
    assert second["results"][0]["output_dir"] == str(retry)
    assert second["results"][0]["source_units"][0]["source_path"] == str(retry / "source" / "SOURCE.json")

    legacy_base = tmp_path / "legacy" / "supplements" / "G_LEGACY"
    legacy_base.mkdir(parents=True)
    legacy_marker = legacy_base / "old.txt"
    legacy_marker.write_text("keep", encoding="utf-8")
    legacy_root = legacy_base.parents[1]
    legacy_config = ProgressivePlannerConfig(
        topic_id="legacy-fixture",
        pool_path=tmp_path / "POOL-legacy.jsonl",
        plan_path=tmp_path / "PLAN-legacy.json",
        output_dir=tmp_path / "runner-legacy",
        planning_revision_enabled=False,
    )
    legacy_runner = make_planning_supplement_runner(
        legacy_config,
        key_file=tmp_path / "missing-key-legacy",
        budget_ledger_path=tmp_path / "budget-legacy.sqlite",
        budget_limit_cny=100,
        allow_external=True,
    )
    legacy_result = legacy_runner(
        [{**gap, "gap_id": "G_LEGACY"}], phase="chapters", skip_local_triage=True,
        pool_rows=[], plan={}, output_dir=legacy_root,
    )
    assert calls[-1] == legacy_base
    assert legacy_marker.read_text(encoding="utf-8") == "keep"
    assert legacy_result["results"][0]["status"] == "failed"

