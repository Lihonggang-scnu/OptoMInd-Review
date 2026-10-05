"""Run the unchanged production CLI, pausing after a persisted stage."""
from __future__ import annotations

import argparse
import importlib.util
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
WORKTREE = ROOT / "worktree"
sys.path.insert(0, str(WORKTREE))
from optomind_research.runtime.upgrade3 import progressive_review_plan as production


class QualityCheckpoint(BaseException):
    def __init__(self, stage: str, output_dir: Path):
        self.stage, self.output_dir = stage, output_dir


def install_checkpoint(target: str):
    original = production.ProgressiveReviewPlanner._stage
    originals = {"_stage": original}

    def paused(self, name, fn, **kwargs):
        result = original(self, name, fn, **kwargs)
        if name == target:
            # Original _stage has already persisted both output and resume state.
            raise QualityCheckpoint(name, self.config.output_dir)
        return result

    production.ProgressiveReviewPlanner._stage = paused
    # These production boundaries save results without going through _stage.
    # Pause only after their unchanged implementation has completed.
    manual_boundaries = {
        "source_routing": "_route_sources",
        "chapter_details": "_chapter_details",
        "owner_revision": "_post_case_review",
    }
    if target in manual_boundaries:
        method_name = manual_boundaries[target]
        implementation = getattr(production.ProgressiveReviewPlanner, method_name)
        originals[method_name] = implementation

        def pause_method(self, *args, **kwargs):
            result = implementation(self, *args, **kwargs)
            gate_dir = self.config.output_dir / "quality_checkpoints"
            gate_dir.mkdir(parents=True, exist_ok=True)
            (gate_dir / (target + ".json")).write_text(
                json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
            raise QualityCheckpoint(target, self.config.output_dir)

        setattr(production.ProgressiveReviewPlanner, method_name, pause_method)
    return originals


def restore_checkpoint(originals):
    for name, implementation in originals.items():
        setattr(production.ProgressiveReviewPlanner, name, implementation)


def offline_check():
    calls = []
    with tempfile.TemporaryDirectory(prefix="gate_check_", dir=ROOT) as temp:
        out = Path(temp)
        cfg = production.ProgressivePlannerConfig(
            topic_id="gate-control", pool_path=out / "unused_pool",
            plan_path=out / "unused_plan", output_dir=out,
        )
        flow = production.ProgressiveReviewPlanner(cfg, planner=lambda *args: None)
        state = {"completed_stages": []}
        original = install_checkpoint("provisional_scope")
        try:
            for resume in (False, True):
                try:
                    flow._stage("provisional_scope", lambda: calls.append(1) or {"response": {"control": "saved"}},
                                resume=resume, state=state, cache_inputs={"control": "same"})
                except QualityCheckpoint:
                    assert (out / "stages/provisional_scope.json").is_file()
                    assert "provisional_scope" in json.loads((out / "RUN_STATE.json").read_text(encoding="utf-8"))["completed_stages"]
                else:
                    raise AssertionError("checkpoint_not_reached")
            assert len(calls) == 1, calls
        finally:
            restore_checkpoint(original)
    report = {"status": "passed", "stage_persisted_before_pause": True,
              "same_input_resume_reuses_saved_result": True, "model_calls": 0}
    (ROOT / "STAGE_GATE_OFFLINE.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report))
    return 0


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--checkpoint")
    parser.add_argument("--offline-check", action="store_true")
    parser.add_argument("production_args", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    if args.offline_check:
        return offline_check()
    if not args.checkpoint:
        parser.error("--checkpoint is required")
    argv = args.production_args
    if argv and argv[0] == "--":
        argv = argv[1:]
    spec = importlib.util.spec_from_file_location("production_planner_cli", WORKTREE / "scripts/upgrade3/progressive_review_plan.py")
    cli = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cli)
    cli_args = cli.parser().parse_args(argv)
    original = install_checkpoint(args.checkpoint)
    before = cli.budget_snapshot(cli_args.budget_ledger)
    try:
        result = cli.main(argv)
        status = {"status": "production_cli_finished", "exit_code": result,
                  "requested_checkpoint": args.checkpoint,
                  "budget_before": before, "budget_after": cli.budget_snapshot(cli_args.budget_ledger)}
        (ROOT / "EXECUTION_STATE.json").write_text(json.dumps(status, ensure_ascii=False, indent=2), encoding="utf-8")
        return result
    except QualityCheckpoint as checkpoint:
        after = cli.budget_snapshot(cli_args.budget_ledger)
        stage_path = checkpoint.output_dir / "quality_checkpoints" / (checkpoint.stage + ".json")
        if not stage_path.is_file():
            stage_path = checkpoint.output_dir / "stages" / (checkpoint.stage + ".json")
        status = {"status": "waiting_for_root_quality_review", "stage": checkpoint.stage,
                  "stage_result": str(stage_path),
                  "planner_state": str(checkpoint.output_dir / "RUN_STATE.json"),
                  "budget_before": before, "budget_after": after}
        (ROOT / "EXECUTION_STATE.json").write_text(json.dumps(status, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps(status, ensure_ascii=False, indent=2))
        return 0
    finally:
        restore_checkpoint(original)


if __name__ == "__main__":
    raise SystemExit(main())
