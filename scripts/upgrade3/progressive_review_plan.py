"""Run progressive review planning from the complete, already-read B pool."""
from __future__ import annotations

import argparse
import json
import math
import sqlite3
import sys
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from optomind_research.runtime.upgrade3 import progressive_review_plan as planning


def parser():
    p = argparse.ArgumentParser(description="全量 B → 两级备料与规划 → 可写细纲与章节素材包")
    mode = p.add_mutually_exclusive_group()
    mode.add_argument("--preflight", action="store_true", help="只读预检，不调用模型或检索")
    mode.add_argument("--run", action="store_true", help="运行真实规划与按需工具调用")
    p.add_argument("--pool", type=Path, required=True)
    p.add_argument("--plan", type=Path, required=True)
    p.add_argument("--output-dir", type=Path, required=True)
    p.add_argument("--topic-id", default="progressive-review")
    p.add_argument("--key-file", type=Path)
    p.add_argument("--budget-ledger", type=Path)
    p.add_argument("--budget-limit-cny", type=float)
    p.add_argument("--deep-read-limit", type=int, default=40)
    p.add_argument("--chapter-workers", type=int, default=3)
    p.add_argument("--reader-workers", type=int, default=3)
    p.add_argument("--chapter-model", default=planning.DEFAULT_READER_MODEL)
    p.add_argument("--timeout-seconds", type=float, default=900)
    p.add_argument("--thinking-budget", type=int, default=8192)
    p.add_argument("--output-tokens", type=int, default=18000)
    p.add_argument("--tokenizer", type=Path, default=planning.DEFAULT_TOKENIZER_PATH)
    p.add_argument("--prior-reading", type=Path, action="append", default=[])
    p.add_argument("--local-material-index", type=Path,
                   help="SQLite index built by planning_material_search; enables the local-first triage")
    p.add_argument("--no-external-supplement", action="store_true",
                   help="Triage locally and only emit external requests; never contact a provider")
    p.add_argument("--stop-after", choices=["level1", "level2"], default="")
    p.add_argument("--resume", action="store_true")
    p.add_argument("--editorial-feedback", type=Path,
                   help="可选的内容编辑意见 JSON，交给已有审阅与章节返修环节")
    p.add_argument(
        "--planning-revision",
        action="store_true",
        help="启用默认关闭的 M1 材料导航与本地按需回填路径",
    )
    p.add_argument(
        "--recover-chapters-from",
        type=Path,
        help="从明确指定的历史输出根恢复兼容的成功章节 owner plan；不扫描历史",
    )
    p.add_argument(
        "--recover-chapter",
        action="append",
        default=[],
        help="限制恢复的章节 ID，可重复传入；省略则按兼容性逐章判断",
    )
    return p


def budget_snapshot(path):
    if not path or not path.is_file():
        return None
    with sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True) as db:
        limit = db.execute("SELECT value FROM budget_meta WHERE key='limit_cny'").fetchone()
        actual = float(db.execute("SELECT COALESCE(SUM(actual_cny),0) FROM reservations").fetchone()[0])
        held = float(db.execute("SELECT COALESCE(SUM(amount_cny),0) FROM reservations WHERE status IN ('reserved','uncertain')").fetchone()[0])
    value = float(limit[0]) if limit else None
    return {"limit_cny": value, "actual_cny": actual, "held_cny": held,
            "available_cny": value - actual - held if value is not None else None}


def preflight(cfg, args):
    from optomind_research.runtime.upgrade3.module4.runtime import estimated_cost_cny
    rows = planning.load_planning_pool(cfg.pool_path)
    plan = planning.load_original_plan(cfg.plan_path)
    payload = {"topic_id": cfg.topic_id, "research_question": plan.get("question_en") or plan.get("question"),
               "original_plan": plan, "pool_row_count": len(rows),
               "candidate_pool": [r["_b_summary"] for r in rows],
               "required_behavior": {"read_all_candidates": True, "candidate_pool_is_complete": True, "do_not_force_use": True}}
    messages = planning._messages_for("provisional_scope", payload)
    count = planning.qwen_local_token_counter(cfg.tokenizer_path)(b"", messages)
    reserved_input = math.ceil(count * planning.TOKEN_MARGIN_MULTIPLIER) + planning.TOKEN_FRAMING_MARGIN
    total = reserved_input + cfg.planner_output_tokens + cfg.thinking_budget
    if total >= 1_000_000:
        raise planning.ProgressivePlanError(f"full_pool_context_too_large:{total}")
    missing = [str(r.get("card_path")) for r in rows if not Path(str(r.get("card_path") or "")).is_file()]
    return {"status": "preflight_only", "network_calls": 0, "paper_count": len(rows),
            "local_input_token_estimate": count, "input_with_estimation_margin": reserved_input,
            "total_context_with_output_and_thinking": total, "missing_card_paths": missing,
            "first_call_reservation_estimate_cny": estimated_cost_cny({"prompt_tokens": reserved_input,
                "completion_tokens": cfg.planner_output_tokens + cfg.thinking_budget}, model=cfg.planner_model, conservative=True),
            "budget": budget_snapshot(args.budget_ledger), "deep_read_limit": cfg.shared_deep_read_budget,
            "planner_model": cfg.planner_model, "reader_model": cfg.reader_model,
            "notes": ["本地分词估算，真实用量以服务返回为准。", "首次调用成本不是完整流程费用。", "各级与章内按具体问题补充，先查本地、再小批外部检索；所有章节共享精读额度。"]}


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    args = parser().parse_args(argv)
    cfg = planning.ProgressivePlannerConfig(
        topic_id=args.topic_id, pool_path=args.pool.resolve(), plan_path=args.plan.resolve(),
        output_dir=args.output_dir.resolve(), shared_deep_read_budget=args.deep_read_limit,
        chapter_workers=args.chapter_workers, reader_workers=args.reader_workers,
        chapter_model=args.chapter_model,
        timeout_seconds=args.timeout_seconds, thinking_budget=args.thinking_budget,
        planner_output_tokens=args.output_tokens, tokenizer_path=args.tokenizer.resolve(),
        planning_revision_enabled=bool(args.planning_revision),
        local_material_index_path=(args.local_material_index.resolve() if args.local_material_index else None),
        recovery_from=(args.recover_chapters_from.resolve() if args.recover_chapters_from else None),
        recovery_chapters=tuple(args.recover_chapter or ()),
    )
    try:
        if not args.run:
            result = preflight(cfg, args)
            out = cfg.output_dir.parent / (cfg.output_dir.name + ".PREFLIGHT.json")
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        else:
            if not args.key_file or not args.budget_ledger or args.budget_limit_cny is None:
                raise planning.ProgressivePlanError("run_requires_key_file_and_shared_budget")
            before = budget_snapshot(args.budget_ledger)
            rows = planning.load_planning_pool(cfg.pool_path)
            prior = planning.load_prior_readings(args.prior_reading, rows)
            planner = planning.QwenProgressivePlanner(
                model=cfg.planner_model, key_file=args.key_file, budget_ledger_path=args.budget_ledger,
                budget_limit_cny=args.budget_limit_cny, output_dir=cfg.output_dir,
                tokenizer_path=cfg.tokenizer_path, timeout_seconds=cfg.timeout_seconds,
                thinking_budget=cfg.thinking_budget, output_tokens=cfg.planner_output_tokens,
                chapter_model=cfg.chapter_model,
            )
            supplement = planning.make_planning_supplement_runner(
                cfg,
                key_file=args.key_file,
                budget_ledger_path=args.budget_ledger,
                budget_limit_cny=args.budget_limit_cny,
                local_index_path=(args.local_material_index.resolve() if args.local_material_index else None),
                allow_external=not args.no_external_supplement,
            )
            reader = planning.make_directed_reading_runner(cfg, key_file=args.key_file, budget_ledger_path=args.budget_ledger, budget_limit_cny=args.budget_limit_cny, prior_readings=prior)
            adaptive = planning.make_retrieval_loop_runner(
                cfg,
                key_file=args.key_file,
                budget_ledger_path=args.budget_ledger,
                budget_limit_cny=args.budget_limit_cny,
                local_index_path=(args.local_material_index.resolve() if args.local_material_index else None),
                allow_external=not args.no_external_supplement,
                supplement_runner=supplement,
                directed_reader=reader,
                prior_readings=prior,
            )
            flow = planning.ProgressiveReviewPlanner(
                cfg, planner=planner, supplement_runner=supplement, directed_reader=reader,
                prior_readings=prior, retrieval_loop_runner=adaptive,
            )
            editorial_feedback = (json.loads(args.editorial_feedback.read_text(encoding="utf-8-sig"))
                                  if args.editorial_feedback else None)
            result = flow.run(resume=args.resume, stop_after=args.stop_after,
                              editorial_feedback=editorial_feedback)
            after = budget_snapshot(args.budget_ledger)
            usage = {"budget_before": before, "budget_after": after, "increment_cny": after["actual_cny"] - before["actual_cny"] if before and after else None}
            usage_path = cfg.output_dir / ("CALL_COST" + ("_" + args.stop_after if args.stop_after else "") + ".json")
            usage_path.write_text(json.dumps(usage, ensure_ascii=False, indent=2), encoding="utf-8")
            result = {"status": result.get("status"), "completed_through": result.get("completed_through", "chapter_details" if result.get("status") == "initial_draft" else "complete"),
                      "output_dir": str(cfg.output_dir), "chapter_count": len(result.get("chapters") or []),
                      "candidate_screening": {k: v for k, v in (result.get("candidate_screening") or {}).items() if k != "candidate_rows"},
                      "deep_read_budget": result.get("deep_read_budget"), **usage}
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except Exception as exc:
        error = {"status": "failed", "error_type": type(exc).__name__, "error": str(exc)}
        if cfg.output_dir.is_dir():
            import traceback
            (cfg.output_dir / "RUN_FAILURE.json").write_text(
                json.dumps({**error, "traceback": traceback.format_exc()}, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        print(json.dumps(error, ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
