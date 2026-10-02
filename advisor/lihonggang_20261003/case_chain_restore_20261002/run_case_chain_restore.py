"""Guarded continuation of the repaired BODY planning chain.

This file is intentionally kept in the isolated run root.  It reuses the
production ProgressiveReviewPlanner and Qwen client; the only extra behavior
is a stage gate that prevents an early cache miss from becoming a paid call.
"""
from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
import sys
from pathlib import Path
from typing import Any, Mapping


RUN_ROOT = Path(__file__).resolve().parent
PROD_ROOT = RUN_ROOT.parent
WORKTREE = PROD_ROOT / "worktree"
EARLY_ROOT = PROD_ROOT / "planning_level1"
POOL = Path(r"F:\OptoMind-Review-2\outputs\progressive_review_plan\20260926_astra_repair\INPUT_POOL.jsonl")
PLAN = Path(r"F:\OptoMind-Review-2\outputs\upgrade3\CROSSDOMAIN_REWORK\X1_microbiome_ICI\PLAN.json")
KEY_FILE = Path(r"F:\OptoMind-Review-2\api_keys\qwen-api-key.txt")
LEDGER = PROD_ROOT / "budget.sqlite"
TOKENIZER = Path(r"F:\OptoMind-Review-2\data\tokenizers\qwen3_5_9b\tokenizer.json")
MATERIAL_INDEX = EARLY_ROOT / "planning_material_index.sqlite"
TOPIC_ID = "microbiome_ici_review"
ALLOWED_PAID_STAGES = {
    "whole_plan_improvement",
    "affected_chapter_revision",
    "case_groups",
}

# These are the completed early artifacts required by the production flow.
# Final plan, case, owner-revision, writer packets, and old raw responses are
# deliberately not copied into this run root.
EARLY_FILES = (
    "stages/provisional_scope.json",
    "stages/level1_tools.json",
    "stages/level1_outline.json",
    "stages/source_routing_summary.json",
    "stages/chapter_proposals.json",
    "stages/harmonized_scope.json",
    "stages/level2_tools.json",
    "stages/finalize_chapter_scope.json",
    "stages/chapter_need_analysis.json",
    "stages/chapters_tools.json",
    "DIRECTED_MATERIAL_CACHE.json",
    "directed_reading.sqlite",
    "directed_reading.sqlite-shm",
    "directed_reading.sqlite-wal",
)
EARLY_DIRS = (
    "stages/source_routing",
    "stages/chapter_proposals",
    "stages/chapters",
    "level1",
    "level2",
    "chapters",
)
EARLY_COMPLETED = [
    "provisional_scope",
    "level1_tools",
    "level1_outline",
    "source_routing",
    "chapter_proposals",
    "harmonized_scope",
    "level2_tools",
    "finalize_chapter_scope",
    "chapter_need_analysis",
    "chapters_tools",
]
EARLY_INPUT_STAGE_NAMES = {
    "level1_tools",
    "level2_tools",
    "chapter_need_analysis",
    "chapters_tools",
}


def read_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8-sig") as handle:
        value = json.load(handle)
    if not isinstance(value, dict):
        raise RuntimeError(f"expected JSON object: {path}")
    return value


def write_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def budget_snapshot(path: Path = LEDGER) -> dict[str, Any]:
    with sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True) as db:
        row = db.execute("SELECT value FROM budget_meta WHERE key='limit_cny'").fetchone()
        limit = float(row[0]) if row else None
        settled = float(db.execute(
            "SELECT COALESCE(SUM(actual_cny),0) FROM reservations "
            "WHERE status='settled'"
        ).fetchone()[0])
        reserved = float(db.execute(
            "SELECT COALESCE(SUM(amount_cny),0) FROM reservations "
            "WHERE status='reserved'"
        ).fetchone()[0])
        uncertain = float(db.execute(
            "SELECT COALESCE(SUM(amount_cny),0) FROM reservations "
            "WHERE status='uncertain'"
        ).fetchone()[0])
    return {
        "limit_cny": limit,
        "settled_cny": settled,
        "reserved_cny": reserved,
        "uncertain_cny": uncertain,
        "available_after_holds_cny": (limit - settled - reserved - uncertain)
        if limit is not None else None,
    }


def copy_one(relative: str) -> None:
    source = EARLY_ROOT / relative
    target = RUN_ROOT / relative
    if not source.is_file():
        raise RuntimeError(f"missing early cache file: {source}")
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, target)


def copy_tree(relative: str) -> None:
    source = EARLY_ROOT / relative
    target = RUN_ROOT / relative
    if not source.is_dir():
        raise RuntimeError(f"missing early cache directory: {source}")
    shutil.copytree(source, target, dirs_exist_ok=False)


def prepare() -> dict[str, Any]:
    existing = [path for path in RUN_ROOT.iterdir() if path.name != Path(__file__).name]
    if existing:
        raise RuntimeError(
            "restore root is not empty; refusing to overwrite an isolated run: "
            + ", ".join(path.name for path in existing[:8])
        )
    for relative in EARLY_FILES:
        source = EARLY_ROOT / relative
        if source.is_file():
            copy_one(relative)
    for relative in EARLY_DIRS:
        copy_tree(relative)

    old_state = read_json(EARLY_ROOT / "RUN_STATE.json")
    old_inputs = old_state.get("stage_inputs") or {}
    early_inputs = {
        name: old_inputs[name]
        for name in EARLY_INPUT_STAGE_NAMES
        if name in old_inputs
    }
    missing_inputs = sorted(EARLY_INPUT_STAGE_NAMES - set(early_inputs))
    if missing_inputs:
        raise RuntimeError("missing cache input signatures: " + ", ".join(missing_inputs))
    state = {
        "schema_version": old_state.get("schema_version"),
        "topic_id": TOPIC_ID,
        "topic": old_state.get("topic"),
        "pool_path": str(POOL.resolve()),
        "plan_path": str(PLAN.resolve()),
        "pool_rows": old_state.get("pool_rows", 585),
        "shared_deep_read_budget": old_state.get("shared_deep_read_budget", 40),
        "planning_revision_enabled": True,
        "completed_stages": list(EARLY_COMPLETED),
        "completed_chapters": ["CH01", "CH02", "CH03", "CH04", "CH05"],
        "stage_inputs": early_inputs,
        "status": "prepared",
        "current_stage": "",
        "output": str(RUN_ROOT / "DETAILED_REVIEW_PLAN.json"),
        "paid_stage_allowlist": sorted(ALLOWED_PAID_STAGES),
        "early_cache_source": str(EARLY_ROOT.resolve()),
        "old_post_case_artifacts_excluded": True,
        "ledger": str(LEDGER.resolve()),
        "ledger_snapshot_before_run": budget_snapshot(),
    }
    write_json(RUN_ROOT / "RUN_STATE.json", state)

    chapter_files = sorted((RUN_ROOT / "stages" / "chapters").glob("CH[0-9][0-9].json"))
    route_files = sorted((RUN_ROOT / "stages" / "source_routing").glob("*.json"))
    manifest = {
        "status": "prepared",
        "run_root": str(RUN_ROOT.resolve()),
        "early_source": str(EARLY_ROOT.resolve()),
        "copied_early_files": list(EARLY_FILES),
        "copied_early_dirs": list(EARLY_DIRS),
        "chapter_packets": [path.name for path in chapter_files],
        "source_routing_batches": [path.name for path in route_files],
        "chapter_details_batch_cache": sorted(
            path.name for path in (RUN_ROOT / "stages" / "chapters" / "CH02_details_batches").glob("*.json")
        ),
        "excluded": [
            "stages/case_groups",
            "stages/whole_plan_improvement.json",
            "stages/affected_chapter_revision",
            "DETAILED_REVIEW_PLAN.json",
            "writer_packets",
        ],
        "local_material_index": str(MATERIAL_INDEX.resolve()),
        "pool": str(POOL.resolve()),
        "plan": str(PLAN.resolve()),
        "budget_before": budget_snapshot(),
        "paid_stage_allowlist": sorted(ALLOWED_PAID_STAGES),
        "models": {
            "main_planner": "qwen3.5-plus",
            "chapter_details_and_cases": "qwen3.7-flash",
        },
        "parameters": {
            "thinking_budget": 8192,
            "output_tokens": 32000,
            "timeout_seconds": 900,
            "chapter_workers": 3,
            "reader_workers": 3,
            "deep_read_limit": 40,
        },
    }
    write_json(RUN_ROOT / "PREPARED_MANIFEST.json", manifest)
    return manifest


class PaidStageGate(RuntimeError):
    pass


class CacheMissGate(RuntimeError):
    pass


class ProbePlanner:
    """Records the first planner stage reached without contacting a provider."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def __call__(self, stage: str, payload: Mapping[str, Any]) -> Any:
        self.calls.append(str(stage))
        if stage in ALLOWED_PAID_STAGES:
            raise PaidStageGate(f"paid_stage_gate_reached:{stage}")
        raise CacheMissGate(f"early_stage_cache_miss:{stage}")


def reject_tool(*args: Any, **kwargs: Any) -> Any:
    phase = kwargs.get("phase") or "unknown"
    raise CacheMissGate(f"early_tool_cache_miss:{phase}")


def make_config(planning: Any) -> Any:
    return planning.ProgressivePlannerConfig(
        topic_id=TOPIC_ID,
        pool_path=POOL.resolve(),
        plan_path=PLAN.resolve(),
        output_dir=RUN_ROOT.resolve(),
        shared_deep_read_budget=40,
        chapter_workers=3,
        reader_workers=3,
        chapter_model="qwen3.7-flash",
        timeout_seconds=900,
        thinking_budget=8192,
        planner_output_tokens=32000,
        tokenizer_path=TOKENIZER.resolve(),
        planning_revision_enabled=True,
        local_material_index_path=MATERIAL_INDEX.resolve(),
    )


def probe_early_reuse() -> dict[str, Any]:
    sys.path.insert(0, str(WORKTREE.resolve()))
    from optomind_research.runtime.upgrade3 import progressive_review_plan as planning

    cfg = make_config(planning)
    probe = ProbePlanner()
    flow = planning.ProgressiveReviewPlanner(
        cfg,
        planner=probe,
        supplement_runner=reject_tool,
        directed_reader=reject_tool,
        prior_readings=[],
        retrieval_loop_runner=reject_tool,
    )
    status = "failed"
    gate = ""
    try:
        flow.run(resume=True)
        status = "unexpectedly_completed_without_paid_gate"
    except PaidStageGate as exc:
        status = "early_cache_reuse_confirmed"
        gate = str(exc)
    except CacheMissGate as exc:
        gate = str(exc)
    state = read_json(RUN_ROOT / "RUN_STATE.json")
    # Restore a clean resumable state after the no-network probe's stage marker.
    state.update({"status": status, "current_stage": "", "probe_gate": gate,
                  "probe_calls": probe.calls})
    write_json(RUN_ROOT / "RUN_STATE.json", state)
    result = {
        "status": status,
        "gate": gate,
        "planner_calls_before_gate": probe.calls,
        "network_calls": 0,
        "budget_after_probe": budget_snapshot(),
        "expected_first_gate": "whole_plan_improvement",
        "cache_policy": "early miss raises before any provider/tool call",
    }
    write_json(RUN_ROOT / "EARLY_REUSE_PREFLIGHT.json", result)
    return result


def diagnose_chapter_cache() -> dict[str, Any]:
    """Capture rebuilt chapter payloads without invoking the planner."""
    sys.path.insert(0, str(WORKTREE.resolve()))
    from optomind_research.runtime.upgrade3 import progressive_review_plan as planning

    cfg = make_config(planning)
    captured: dict[str, dict[str, Any]] = {}

    class Capture(RuntimeError):
        pass

    original = planning._chapter_details_adaptive_record
    original_details = None
    rebuilt_pool: list[str] = []

    def capture(planner: Any, payload: Mapping[str, Any], *, chapter_id: str,
                cache_root: Path, resume: bool) -> Any:
        captured[str(chapter_id)] = dict(payload)
        raise Capture(str(chapter_id))

    planning._chapter_details_adaptive_record = capture
    probe = ProbePlanner()
    flow = planning.ProgressiveReviewPlanner(
        cfg,
        planner=probe,
        supplement_runner=reject_tool,
        directed_reader=reject_tool,
        prior_readings=[],
        retrieval_loop_runner=reject_tool,
    )
    original_details = flow._chapter_details

    def capture_details(**kwargs: Any) -> Any:
        rebuilt_pool.extend(
            str(row.get("_source_handle"))
            for row in (kwargs.get("candidate_pool") or [])
            if isinstance(row, Mapping) and row.get("_source_handle")
        )
        return original_details(**kwargs)

    flow._chapter_details = capture_details
    try:
        flow.run(resume=True)
    except Capture:
        pass
    finally:
        planning._chapter_details_adaptive_record = original

    rows: list[dict[str, Any]] = []
    for chapter_id, payload in sorted(captured.items()):
        cached_path = RUN_ROOT / "stages" / "chapters" / f"{chapter_id}.json"
        cached = read_json(cached_path) if cached_path.is_file() else {}
        old_sources = cached.get("source_materials") or []
        new_sources = payload.get("source_materials") or []
        old_handles = [str(item.get("source_handle")) for item in old_sources if isinstance(item, Mapping)]
        new_handles = [str(item.get("source_handle")) for item in new_sources if isinstance(item, Mapping)]
        old_candidates = cached.get("candidate_materials") or []
        new_candidates = payload.get("candidate_materials") or []
        rows.append({
            "chapter_id": chapter_id,
            "cached_source_count": len(old_sources),
            "rebuilt_source_count": len(new_sources),
            "cached_source_handles": old_handles,
            "rebuilt_source_handles": new_handles,
            "source_handles_equal": old_handles == new_handles,
            "source_handle_sets_equal": set(old_handles) == set(new_handles),
            "cached_candidate_count": len(old_candidates),
            "rebuilt_candidate_count": len(new_candidates),
            "candidate_handles_equal": [str(item.get("source_handle")) for item in old_candidates if isinstance(item, Mapping)] == [str(item.get("source_handle")) for item in new_candidates if isinstance(item, Mapping)],
            "candidate_navigation_equal": cached.get("candidate_navigation") == payload.get("candidate_navigation"),
            "adaptive_materials_equal": cached.get("_adaptive_input_materials") == payload.get("new_tool_materials", []),
        })
    result = {
        "status": "captured_without_model_call",
        "network_calls": 0,
        "planner_calls": probe.calls,
        "rebuilt_pool_count": len(rebuilt_pool),
        "rebuilt_pool_handles": rebuilt_pool,
        "chapters": rows,
        "budget_after": budget_snapshot(),
    }
    write_json(RUN_ROOT / "CHAPTER_CACHE_DIAGNOSIS.json", result)
    return result


class AllowlistedPlanner:
    def __init__(self, delegate: Any) -> None:
        self.delegate = delegate
        self.calls: list[str] = []

    def __call__(self, stage: str, payload: Mapping[str, Any]) -> Any:
        stage = str(stage)
        self.calls.append(stage)
        if stage not in ALLOWED_PAID_STAGES:
            raise CacheMissGate(f"early_stage_cache_miss_during_run:{stage}")
        return self.delegate(stage, payload)


def run_paid() -> dict[str, Any]:
    sys.path.insert(0, str(WORKTREE.resolve()))
    from optomind_research.runtime.upgrade3 import progressive_review_plan as planning

    cfg = make_config(planning)
    before = budget_snapshot()
    planner = planning.QwenProgressivePlanner(
        model="qwen3.5-plus",
        key_file=KEY_FILE,
        budget_ledger_path=LEDGER,
        budget_limit_cny=50,
        output_dir=RUN_ROOT,
        tokenizer_path=cfg.tokenizer_path,
        timeout_seconds=cfg.timeout_seconds,
        thinking_budget=cfg.thinking_budget,
        output_tokens=cfg.planner_output_tokens,
        chapter_model=cfg.chapter_model,
    )
    guarded = AllowlistedPlanner(planner)
    flow = planning.ProgressiveReviewPlanner(
        cfg,
        planner=guarded,
        supplement_runner=reject_tool,
        directed_reader=reject_tool,
        prior_readings=[],
        retrieval_loop_runner=reject_tool,
    )
    result = flow.run(resume=True)
    after = budget_snapshot()
    output = {
        "status": result.get("status"),
        "output_dir": str(RUN_ROOT.resolve()),
        "allowed_stage_calls": guarded.calls,
        "budget_before": before,
        "budget_after": after,
        "increment_cny": after["settled_cny"] - before["settled_cny"],
        "detailed_plan": str(RUN_ROOT / "DETAILED_REVIEW_PLAN.json"),
        "writer_packets": str(RUN_ROOT / "writer_packets"),
    }
    write_json(RUN_ROOT / "RUN_RESULT.json", output)
    return output


def main() -> int:
    parser = argparse.ArgumentParser()
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--prepare", action="store_true")
    mode.add_argument("--preflight", action="store_true")
    mode.add_argument("--diagnose-chapter-cache", action="store_true")
    mode.add_argument("--run", action="store_true")
    args = parser.parse_args()
    try:
        if args.prepare:
            result = prepare()
        elif args.preflight:
            result = probe_early_reuse()
        elif args.diagnose_chapter_cache:
            result = diagnose_chapter_cache()
        else:
            result = run_paid()
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except Exception as exc:
        error = {"status": "failed", "error_type": type(exc).__name__, "error": str(exc)}
        write_json(RUN_ROOT / ("PREPARATION_FAILURE.json" if args.prepare else "RUN_FAILURE.json"), error)
        print(json.dumps(error, ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
