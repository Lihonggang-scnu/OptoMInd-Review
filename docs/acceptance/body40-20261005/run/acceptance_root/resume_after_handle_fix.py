"""Test-only recovery wrapper for the reviewed BODY checkpoint."""
from __future__ import annotations

import json
import os
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

import continuous_driver as gated

ROOT = gated.ROOT
production = gated.production
REPLAY_STAGES = {
    "provisional_scope", "level1_tools", "level1_outline",
    "chapter_proposals", "harmonized_scope",
}


def _read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8-sig"))


def _write_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


class ResumeQualityBarriers(gated.QualityBarriers):
    """Use the existing gate lifecycle with reviewed stage replay hooks."""

    STAGES = set(gated.QualityBarriers.STAGES) - REPLAY_STAGES

    def __init__(self, root):
        super().__init__(root)
        # The previous run stopped at gate 07. The repaired recovery gate is 08.
        self.sequence = 7
        # The routing snapshot was already reviewed in the same run.
        self.method_seen.add("source_routing")
        self._recovery_originals = {}
        self.replayed = []

    def _replay_stage(self, flow, name):
        path = flow.config.output_dir / "stages" / f"{name}.json"
        if not path.is_file():
            raise RuntimeError(f"missing_reviewed_stage:{name}")
        self.replayed.append({"stage": name, "path": str(path), "provider_calls": 0})
        return _read_json(path)

    def _replay_routing(self, flow, state):
        candidates = [
            flow.config.output_dir / "quality_checkpoints" / "source_routing.json",
            flow.config.output_dir / "stages" / "source_routing_summary.json",
        ]
        path = next((item for item in candidates if item.is_file()), None)
        if path is None:
            raise RuntimeError("missing_reviewed_source_routing_snapshot")
        result = _read_json(path)
        if not isinstance(result, dict) or not isinstance(result.get("source_routes"), list):
            raise RuntimeError("source_routing_snapshot_invalid")
        state.update({
            "status": "in_progress", "current_stage": "",
            "source_routing_batches": int(result.get("batch_count") or 0),
            "completed_stages": list(dict.fromkeys([*(state.get("completed_stages") or []), "source_routing"])),
        })
        _write_json(flow.config.output_dir / "RUN_STATE.json", state)
        self.replayed.append({"stage": "source_routing", "path": str(path),
                             "provider_calls": 0, "routed_sources": len(result["source_routes"])})
        return result

    def install(self):
        cls = production.ProgressiveReviewPlanner
        self._recovery_originals = {"_stage": cls._stage, "_route_sources": cls._route_sources}
        original_stage = cls._stage
        original_route_sources = cls._route_sources
        owner = self

        def stage(flow, name, fn, **kwargs):
            if name in REPLAY_STAGES:
                return owner._replay_stage(flow, name)
            return original_stage(flow, name, fn, **kwargs)

        def route_sources(flow, *args, **kwargs):
            if kwargs.get("cache_namespace", "source_routing") == "source_routing":
                return owner._replay_routing(flow, kwargs["state"])
            return original_route_sources(flow, *args, **kwargs)

        # The inherited install wraps these methods with the existing quality
        # gates. It therefore gates level2 and all later stages unchanged.
        cls._stage = stage
        cls._route_sources = route_sources
        super().install()

    def restore(self):
        super().restore()
        for name, implementation in self._recovery_originals.items():
            setattr(production.ProgressiveReviewPlanner, name, implementation)


def offline_check() -> int:
    """Prove reviewed replay is offline and repaired level2 reaches the gate."""
    with tempfile.TemporaryDirectory(prefix="resume_handle_fix_", dir=ROOT) as temp:
        root = Path(temp)
        output_dir = root / "planning"
        (output_dir / "stages").mkdir(parents=True)
        (output_dir / "quality_checkpoints").mkdir(parents=True)
        for stage in REPLAY_STAGES:
            _write_json(output_dir / "stages" / f"{stage}.json", {"stage": stage, "response": {}})
        _write_json(output_dir / "quality_checkpoints" / "source_routing.json", {
            "batch_count": 1, "source_routes": [{"source_handle": "P0583", "chapter_ids": ["Ch1"]}],
        })
        cfg = production.ProgressivePlannerConfig(
            topic_id="resume-offline", pool_path=root / "POOL.jsonl",
            plan_path=root / "PLAN.json", output_dir=output_dir,
        )
        planner = production.ProgressiveReviewPlanner(cfg, planner=lambda *_args: {})
        barrier = ResumeQualityBarriers(root)
        gate_events = []
        barrier.review = lambda stage, path: gate_events.append((stage, path))
        barrier.install()
        try:
            calls = []
            state = {"completed_stages": []}
            for stage in REPLAY_STAGES:
                planner._stage(stage, lambda: calls.append(stage), resume=False, state=state, cache_inputs={"x": stage})
            assert calls == []
            route = planner._route_sources([], shared_outline={}, resume=True, state=state)
            assert route["source_routes"] and barrier.replayed[-1]["stage"] == "source_routing"

            handle_to_id = {f"P{value:04d}": f"paper-{value}" for value in (583, 578, 582, 585, 478, 327)}
            resolved = production._resolve_planner_handles({"directed_reads": [
                {"handle": handle, "chapter_ids": ["Ch1"], "reason": f"reason-{handle}"}
                for handle in handle_to_id
            ]}, handle_to_id)
            captured = {}
            adaptive_calls = []

            def fake_adaptive(**kwargs):
                adaptive_calls.append(1)
                captured.update(kwargs)
                return {"phase": "level2", "status": "complete", "directed_results": [],
                        "supplement_results": [], "tool_materials_by_chapter": {},
                        "consumed_paper_ids": []}

            planner.retrieval_loop_runner = fake_adaptive
            planner._tool_cycle(
                phase="level2", supplement_requests=[], directed_requests=resolved["directed_reads"],
                pool_rows=[{"_paper_id": paper_id, "_source_handle": handle}
                           for handle, paper_id in handle_to_id.items()],
                plan={"research_question": "offline"}, prior_directed=None,
                prior_tool_results={}, source_handle_map=handle_to_id, resume=True, state=state,
            )
            queued = captured["directed_requests"]
            assert len(adaptive_calls) == 1
            assert [row["paper_id"] for row in queued] == list(handle_to_id.values())
            assert all(row["chapter_ids"] == ["Ch1"] and row["reasons"] for row in queued)
            assert gate_events and gate_events[-1][0] == "level2_tools"
            assert (output_dir / "stages" / "level2_tools.json").is_file()
        finally:
            barrier.restore()

    report = {
        "status": "passed", "replayed_stage_fn_calls": 0,
        "replayed_routing_provider_calls": 0, "level2_fake_adaptive_calls": 1,
        "level2_queued_directed_reads": 6, "level2_stage_persisted_before_gate": True,
        "gate_stage": "level2_tools", "ledger_changed": False,
    }
    _write_json(ROOT / "RESUME_AFTER_HANDLE_FIX_OFFLINE.json", report)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    if "--offline-check" in sys.argv[1:]:
        raise SystemExit(offline_check())
    gated.QualityBarriers = ResumeQualityBarriers
    raise SystemExit(gated.main())
