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
from scripts.upgrade3 import outline_strengthening_pipeline as strengthening_pipeline
from scripts.upgrade3.build_local_materials import (
    add_material_acquisition_arguments,
    make_material_acquirer_factory,
    material_parser_mode,
)


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
    p.add_argument("--thinking-budget", type=int, default=planning.DEFAULT_PLANNER_THINKING_BUDGET,
                   help="Thinking allowance for every planner stage; 0 disables thinking")
    p.add_argument("--output-tokens", type=int, default=planning.DEFAULT_PLANNER_OUTPUT_TOKENS,
                   help="Answer allowance for every planner stage")
    p.add_argument("--reader-thinking-budget", type=int, default=planning.DEFAULT_READER_THINKING_BUDGET)
    p.add_argument("--reader-output-tokens", type=int, default=planning.DEFAULT_READER_OUTPUT_TOKENS)
    p.add_argument("--tokenizer", type=Path, default=planning.DEFAULT_TOKENIZER_PATH)
    p.add_argument("--prior-reading", type=Path, action="append", default=[])
    p.add_argument("--local-material-index", type=Path,
                   help="SQLite index built by planning_material_search; enables the local-first triage")
    p.add_argument("--no-external-supplement", action="store_true",
                   help="Triage locally and only emit external requests; never contact a provider")
    p.add_argument("--stop-after", choices=["level1", "level2"], default="")
    p.add_argument("--resume", action="store_true")
    p.add_argument("--outline-strengthening", action=argparse.BooleanOptionalAction, default=True,
                   help="完整规划默认后置任务簇加强；--no-outline-strengthening 显式关闭，阶段停点与预检不会运行")
    p.add_argument("--outline-strengthening-output", type=Path,
                   help="加强恢复目录；默认是规划输出同级的 <output-dir>.outline_strengthening")
    p.add_argument("--retry-unresolved", action="store_true",
                   help="明确重试加强阶段保存的无效回答一次；保留已成功阶段、原尝试与预算记录")
    p.add_argument("--editorial-feedback", type=Path,
                   help="可选的内容编辑意见 JSON，交给已有审阅与章节返修环节")
    p.add_argument(
        "--planning-revision",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="默认启用 M1 材料导航与本地按需回填；--no-planning-revision 使用旧编排，但仍提供完整 A/B 材料",
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
    add_material_acquisition_arguments(p, include_shared_budget_options=False)
    p.add_argument("--local-formula-config", type=Path, help="explicit offline CodeFormulaV2 batch profile; enhance a selected paper before directed reading")
    p.add_argument("--local-formula-cache-root", type=Path)
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
    settings = planning._planner_call_settings(None, "provisional_scope", config=cfg)
    total = reserved_input + settings["output_tokens"] + settings["thinking_budget"]
    if total >= 1_000_000:
        raise planning.ProgressivePlanError(f"full_pool_context_too_large:{total}")
    missing = [str(r.get("card_path")) for r in rows if not Path(str(r.get("card_path") or "")).is_file()]
    return {"status": "preflight_only", "network_calls": 0, "paper_count": len(rows),
            "local_input_token_estimate": count, "input_with_estimation_margin": reserved_input,
            "total_context_with_output_and_thinking": total, "missing_card_paths": missing,
            "first_call_reservation_estimate_cny": estimated_cost_cny({"prompt_tokens": reserved_input,
                "completion_tokens": settings["output_tokens"] + settings["thinking_budget"]}, model=cfg.planner_model, conservative=True),
            "budget": budget_snapshot(args.budget_ledger), "deep_read_limit": cfg.shared_deep_read_budget,
            "planner_model": cfg.planner_model, "reader_model": cfg.reader_model,
            "planner_runtime_config": settings, "directed_reader_runtime_config": planning._directed_reader_settings(cfg),
            "material_parser_mode": material_parser_mode(args),
            "planning_revision_enabled": cfg.planning_revision_enabled,
            "chapter_material_view": "full_A_B",
            "notes": ["本地分词估算，真实用量以服务返回为准。", "首次调用成本不是完整流程费用。", "各级与章内按具体问题补充，先查本地、再小批外部检索；所有章节共享精读额度。"]}


def post_planning_handoff(cfg, args, plan_result):
    """Reuse the formal strengthening entry and publish a complete-plan pointer.

    ProgressiveReviewPlanner.run remains the planning API. This CLI composes its
    complete packets with run_pipeline; both keep their original stage caches.
    """
    enabled = bool(args.outline_strengthening)
    if args.stop_after:
        return {"outline_strengthening_enabled": enabled,
                "outline_strengthening": {"status": "not_run_stage_stop"}}
    if enabled:
        output = (args.outline_strengthening_output.resolve() if args.outline_strengthening_output else
                  cfg.output_dir.with_name(cfg.output_dir.name + ".outline_strengthening"))
        report = strengthening_pipeline.run_pipeline(argparse.Namespace(
            packet_root=str(cfg.output_dir), output=str(output), run=True,
            profile=strengthening_pipeline.cli.DEFAULT_PROFILE,
            access_profile=strengthening_pipeline.cli.DEFAULT_ACCESS_PROFILE,
            selection_profile=strengthening_pipeline.cli.DEFAULT_SELECTION_PROFILE,
            include_selection_material_index=False,
            on_demand_stream=True, on_demand_request_timeout=1800.0,
            on_demand_stream_overall_timeout=3600.0,
            tokenizer=str(cfg.tokenizer_path), budget_ledger=str(args.budget_ledger.resolve()),
            budget_limit=args.budget_limit_cny, key_file=str(args.key_file.resolve()),
            retry_unresolved=bool(args.retry_unresolved),
        ))
        pointer = strengthening_pipeline._load(Path(report["current_plan"]))
    else:
        # A partial scientific revision can still supply every valid chapter;
        # genuinely missing chapters/tasks must not become a usable handoff.
        base_plan, packets = strengthening_pipeline.load_complete_packets(cfg.output_dir)
        output = cfg.output_dir.with_name(cfg.output_dir.name + ".unstrengthened")
        target = strengthening_pipeline.export_complete_plan(base_plan, packets, output,
            provenance={"status": "original_valid_plan", "groups": [], "outline_strengthening_enabled": False})
        report = {"status": "disabled", "packet_root": str(target.resolve()),
                  "arrangement_not_run": True}
        pointer = strengthening_pipeline._load(output / "CURRENT_PLAN.json")
    pointer.update(planning_status=plan_result.get("status"), outline_strengthening_status=report["status"])
    pointer_path = cfg.output_dir / "CURRENT_PLAN.json"
    strengthening_pipeline._dump(pointer_path, pointer)
    return {"outline_strengthening_enabled": enabled, "outline_strengthening": report,
            "packet_root": pointer["packet_root"], "current_plan": str(pointer_path)}


def main(argv=None, *, regional_client_constructor=None):
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    args = parser().parse_args(argv)
    try:
        selected_material_parser_mode = material_parser_mode(args)
    except ValueError as exc:
        parser().error(str(exc))
    if not args.planning_revision:
        print("Warning: --no-planning-revision selects legacy orchestration without M1 navigation; chapter planning still receives full A/B material.", file=sys.stderr)
    cfg = planning.ProgressivePlannerConfig(
        topic_id=args.topic_id, pool_path=args.pool.resolve(), plan_path=args.plan.resolve(),
        output_dir=args.output_dir.resolve(), shared_deep_read_budget=args.deep_read_limit,
        chapter_workers=args.chapter_workers, reader_workers=args.reader_workers,
        chapter_model=args.chapter_model,
        timeout_seconds=args.timeout_seconds, thinking_budget=args.thinking_budget,
        planner_output_tokens=args.output_tokens, tokenizer_path=args.tokenizer.resolve(),
        reader_thinking_budget=args.reader_thinking_budget, chapter_output_tokens=args.reader_output_tokens,
        local_formula_config_path=(args.local_formula_config.resolve() if args.local_formula_config else None),
        planning_revision_enabled=bool(args.planning_revision),
        local_material_index_path=(args.local_material_index.resolve() if args.local_material_index else None),
        recovery_from=(args.recover_chapters_from.resolve() if args.recover_chapters_from else None),
        recovery_chapters=tuple(args.recover_chapter or ()),
    )
    try:
        if not args.run:
            result = preflight(cfg, args)
            result.update(outline_strengthening_enabled=bool(args.outline_strengthening),
                          outline_strengthening={"status": "not_run_preflight"})
            out = cfg.output_dir.parent / (cfg.output_dir.name + ".PREFLIGHT.json")
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        else:
            if not args.key_file or not args.budget_ledger or args.budget_limit_cny is None:
                raise planning.ProgressivePlanError("run_requires_key_file_and_shared_budget")
            before = budget_snapshot(args.budget_ledger)
            # Always use the shared acquisition factory. With no explicit
            # Docling flags it is the fast native route; basic Docling is
            # constructed only when a local saved/batch source is supplied.
            acquirer_factory = make_material_acquirer_factory(
                args, regional_client_constructor=regional_client_constructor
            )
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
                acquirer_factory=acquirer_factory,
            )
            local_formula_enhancer = None
            if args.local_formula_config:
                from optomind_research.runtime.upgrade3.local_formula_materials import make_local_formula_enhancer
                local_formula_enhancer = make_local_formula_enhancer(args.local_formula_config,
                    cache_root=args.local_formula_cache_root or cfg.output_dir / "local_formula_cache")
            reader = planning.make_directed_reading_runner(cfg, key_file=args.key_file, budget_ledger_path=args.budget_ledger, budget_limit_cny=args.budget_limit_cny, prior_readings=prior,
                **({"local_formula_enhancer": local_formula_enhancer} if local_formula_enhancer is not None else {}))
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
            handoff = post_planning_handoff(cfg, args, result)
            after = budget_snapshot(args.budget_ledger)
            usage = {"budget_before": before, "budget_after": after, "increment_cny": after["actual_cny"] - before["actual_cny"] if before and after else None}
            usage_path = cfg.output_dir / ("CALL_COST" + ("_" + args.stop_after if args.stop_after else "") + ".json")
            usage_path.write_text(json.dumps(usage, ensure_ascii=False, indent=2), encoding="utf-8")
            planning_status = result.get("status")
            strengthening_incomplete = (args.outline_strengthening and not args.stop_after and
                                        handoff["outline_strengthening"]["status"] != "complete")
            result = {"status": "outline_strengthening_partial" if strengthening_incomplete else planning_status,
                      "planning_status": planning_status,
                      "completed_through": result.get("completed_through", "chapter_details" if planning_status == "initial_draft" else "complete"),
                      "planning_revision_enabled": cfg.planning_revision_enabled,
                      "material_parser_mode": selected_material_parser_mode,
                      "chapter_material_view": "full_A_B",
                      "output_dir": str(cfg.output_dir), "chapter_count": len(result.get("chapters") or []),
                      "candidate_screening": {k: v for k, v in (result.get("candidate_screening") or {}).items() if k != "candidate_rows"},
                      "deep_read_budget": result.get("deep_read_budget"), **handoff, **usage}
            strengthening_pipeline._dump(cfg.output_dir / "PLANNING_RUN_REPORT.json", result)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 2 if args.run and not args.stop_after and result.get("status") != "complete" else 0
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
