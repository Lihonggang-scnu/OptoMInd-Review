"""Test-only quality barriers around unchanged production planner calls."""
import argparse
import importlib.util
import json
import os
import tempfile
import threading
import time
from pathlib import Path

import staged_driver as staged

ROOT = staged.ROOT
production = staged.production


class ReviewStopped(BaseException):
    pass


class QualityBarriers:
    STAGES = {
        "provisional_scope", "level1_tools", "level1_outline",
        "chapter_proposals", "harmonized_scope", "level2_tools",
        "chapter_need_analysis", "chapters_tools",
        "whole_plan_improvement", "case_groups",
    }
    METHODS = {
        "_route_sources": "source_routing",
        "_chapter_details": "chapter_details",
        "_post_case_review": "owner_revision",
    }

    def __init__(self, root):
        self.root = Path(root)
        self.sequence = 0
        self.originals = {}
        self.method_seen = set()

    def review(self, stage, result_path):
        self.sequence += 1
        gates = self.root / "quality_gates"
        gates.mkdir(parents=True, exist_ok=True)
        key = f"{self.sequence:02d}_{stage}"
        decision = gates / (key + ".decision.json")
        # A different process must not consume an earlier process's decision.
        decision = decision.with_name(f"{key}_pid{os.getpid()}.decision.json")
        request = {
            "status": "waiting_for_root_quality_review", "stage": stage,
            "pid": os.getpid(), "stage_result": str(result_path),
            "decision_path": str(decision),
            "no_model_call_while_waiting": True,
        }
        (gates / (key + ".request.json")).write_text(
            json.dumps(request, ensure_ascii=False, indent=2), encoding="utf-8")
        (self.root / "EXECUTION_STATE.json").write_text(
            json.dumps(request, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps(request, ensure_ascii=False), flush=True)
        while True:
            if decision.is_file():
                item = json.loads(decision.read_text(encoding="utf-8"))
                if item.get("stage") != stage:
                    raise ReviewStopped("quality_decision_stage_mismatch")
                if item.get("action") == "stop":
                    raise ReviewStopped(str(item.get("reason") or "root_quality_stop"))
                if item.get("action") != "continue":
                    raise ReviewStopped("invalid_quality_decision")
                request.update(status="running_after_root_review", review=item)
                (self.root / "EXECUTION_STATE.json").write_text(
                    json.dumps(request, ensure_ascii=False, indent=2), encoding="utf-8")
                return
            time.sleep(1)

    def install(self):
        cls = production.ProgressiveReviewPlanner
        original = cls._stage
        self.originals["_stage"] = original
        coordinator = self

        def stage_call(flow, name, fn, **kwargs):
            result = original(flow, name, fn, **kwargs)
            if name in coordinator.STAGES:
                coordinator.review(name, flow.config.output_dir / "stages" / (name + ".json"))
            return result

        cls._stage = stage_call
        for method, label in self.METHODS.items():
            implementation = getattr(cls, method)
            self.originals[method] = implementation

            def factory(implementation, label):
                def method_call(flow, *args, **kwargs):
                    result = implementation(flow, *args, **kwargs)
                    # Initial source routing is reviewed once; late-source
                    # routing continues under the existing owner/case gates.
                    if label not in coordinator.method_seen:
                        coordinator.method_seen.add(label)
                        out = flow.config.output_dir / "quality_checkpoints"
                        out.mkdir(parents=True, exist_ok=True)
                        path = out / (label + ".json")
                        path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
                        coordinator.review(label, path)
                    return result
                return method_call

            setattr(cls, method, factory(implementation, label))

    def restore(self):
        for name, implementation in self.originals.items():
            setattr(production.ProgressiveReviewPlanner, name, implementation)


def offline_check():
    with tempfile.TemporaryDirectory(prefix="continuous_gate_", dir=ROOT) as temp:
        root = Path(temp)
        cfg = production.ProgressivePlannerConfig(topic_id="gate-control",
            pool_path=root / "unused", plan_path=root / "unused", output_dir=root / "planning")
        flow = production.ProgressiveReviewPlanner(cfg, planner=lambda *args: None)
        barriers = QualityBarriers(root)
        state = {"completed_stages": []}
        calls = []
        def reviewer():
            while not (root / "EXECUTION_STATE.json").is_file():
                time.sleep(.05)
            req = json.loads((root / "EXECUTION_STATE.json").read_text(encoding="utf-8"))
            assert Path(req["stage_result"]).is_file()
            assert calls == [1]
            time.sleep(.2)
            assert calls == [1]
            Path(req["decision_path"]).write_text(json.dumps(
                {"stage": req["stage"], "action": "continue", "reason": "offline control"}), encoding="utf-8")
        thread = threading.Thread(target=reviewer, daemon=True)
        barriers.install()
        try:
            thread.start()
            result = flow._stage("provisional_scope", lambda: calls.append(1) or {"response": {"saved": True}},
                resume=False, state=state, cache_inputs={"control": 1})
            thread.join(timeout=2)
            assert result["response"]["saved"] is True and calls == [1]
        finally:
            barriers.restore()
    print(json.dumps({"status": "passed", "model_calls": 0,
        "production_result_saved_before_review": True,
        "same_process_continues_only_after_decision": True,
        "no_repeat_stage_call_while_waiting": True}), flush=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--offline-check", action="store_true")
    parser.add_argument("production_args", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    if args.offline_check:
        offline_check()
        return 0
    argv = args.production_args
    if argv and argv[0] == "--":
        argv = argv[1:]
    spec = importlib.util.spec_from_file_location("production_planner_cli", staged.WORKTREE / "scripts/upgrade3/progressive_review_plan.py")
    cli = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cli)
    cli_args = cli.parser().parse_args(argv)
    barriers = QualityBarriers(ROOT)
    barriers.install()
    try:
        code = cli.main(argv)
        (ROOT / "EXECUTION_STATE.json").write_text(json.dumps({
            "status": "production_cli_finished", "exit_code": code,
            "budget_after": cli.budget_snapshot(cli_args.budget_ledger)}, indent=2), encoding="utf-8")
        return code
    except ReviewStopped as exc:
        (ROOT / "EXECUTION_STATE.json").write_text(json.dumps({
            "status": "stopped_by_root_quality_review", "reason": str(exc),
            "budget_after": cli.budget_snapshot(cli_args.budget_ledger)}, indent=2), encoding="utf-8")
        return 3
    finally:
        barriers.restore()


if __name__ == "__main__":
    raise SystemExit(main())
