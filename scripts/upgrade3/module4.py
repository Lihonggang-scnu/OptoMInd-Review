"""Executable Module 4 entry points.

All commands are local.  ``read`` calls a paid model only when explicitly
given ``--real`` (and a key file); ``inspect-input``, ``prepare``, ``verify``,
``render``, ``handoff`` and ``feedback-view`` are offline.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Any, Mapping

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from optomind_research.runtime.upgrade3.local_materials import PreparedSnapshotProvider
from optomind_research.runtime.upgrade3.module4.contracts import assemble_reading_input, validate_input
from optomind_research.runtime.upgrade3.module4.dossier import render_dossier, validate_dossier
from optomind_research.runtime.upgrade3.module4.feedback import build_post_reading_view
from optomind_research.runtime.upgrade3.module4.reader import read_paper
from optomind_research.runtime.upgrade3.module4.runtime import GlobalBudgetLedger, QwenDirectClient


def _json(path: str | Path) -> Any:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _json_pointer(value: Any, pointer: str) -> Any:
    if not pointer or pointer == "/":
        return value
    current = value
    for token in pointer.lstrip("/").split("/"):
        token = token.replace("~1", "/").replace("~0", "~")
        if isinstance(current, Mapping):
            current = current[token]
        elif isinstance(current, list):
            current = current[int(token)]
        else:
            raise KeyError(pointer)
    return current


def _write(path: str | Path, value: Any) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")


def _snapshot(path: str | Path):
    return PreparedSnapshotProvider(path).load()


def _plan_payload(value: Mapping[str, Any]) -> Mapping[str, Any]:
    if isinstance(value.get("plan"), Mapping):
        return value["plan"]
    return value


def _prepare_task(args: argparse.Namespace) -> dict[str, Any]:
    def source_ref(path: str, pointer: str = "") -> dict[str, Any]:
        result = {"path": str(Path(path).resolve()), "sha256": hashlib.sha256(Path(path).read_bytes()).hexdigest()}
        if pointer:
            result["json_pointer"] = pointer
        return result
    plan_raw = _json(args.plan)
    plan = _plan_payload(plan_raw)
    question = _json(args.question_ref) if args.question_ref else {}
    if isinstance(question, Mapping):
        try:
            pointed = _json_pointer(question, args.question_pointer)
        except (KeyError, IndexError, ValueError, TypeError):
            pointed = None
        if isinstance(pointed, Mapping):
            original = str(pointed.get("original_text") or pointed.get("original_question") or pointed.get("research_question") or pointed.get("question") or pointed.get("locked_question") or "")
        else:
            original = str(pointed if isinstance(pointed, str) else question.get("original_text") or question.get("original_question") or question.get("research_question") or question.get("question") or question.get("locked_question") or "")
    else:
        original = str(question)
    if not original:
        original = str(plan.get("question_original") or plan.get("original_question") or "")
    if not original:
        raise ValueError("original_question_missing")
    facets = []
    for facet_index, facet in enumerate(plan.get("facets") or ()):
        if isinstance(facet, Mapping):
            facets.append({"facet_id": facet.get("facet_id") if facet.get("facet_id") is not None else facet.get("id"), "ask_original": facet.get("ask_original") if facet.get("ask_original") is not None else facet.get("ask"), "filters_original": facet.get("filters_original") if "filters_original" in facet else facet.get("filters"), "must_exclude_original": facet.get("must_exclude_original") if "must_exclude_original" in facet else facet.get("must_exclude"), "source_ref": "PLAN.json"})
            facets[-1]["source_ref"] = source_ref(args.plan, ("/plan" if "plan" in plan_raw else "") + f"/facets/{facet_index}")
    cards = _json(args.cards) if args.cards else {}
    navigation = {"source_ref": str(args.cards or ""), "card_context": [], "portfolio_membership": "not_asserted", "pending_items": []}
    if isinstance(cards, Mapping):
        rows = cards.get("cards") or cards.get("items") or cards
        if isinstance(rows, Mapping):
            row = rows.get(args.canonical_paper_id, {})
            if isinstance(row, Mapping):
                navigation["card_context"] = [{"reason": row.get("selection_reason") or row.get("reason"), "pending_items": row.get("pending_verification_items") or row.get("pending_items") or []}]
        elif isinstance(rows, list):
            navigation["card_context"] = [{"reason": row.get("selection_reason") or row.get("reason"), "pending_items": row.get("pending_verification_items") or row.get("pending_items") or []} for row in rows if isinstance(row, Mapping) and str(row.get("canonical_paper_id") or row.get("paper_id")) == args.canonical_paper_id]
    if str(plan_raw.get("status") or "ok") not in {"ok", "complete", ""}:
        raise ValueError("plan_status_not_ready")
    if isinstance(plan_raw.get("degradation"), Mapping) and str(plan_raw["degradation"].get("plan_state") or "complete") != "complete":
        raise ValueError("plan_state_not_complete")
    task = {
        "task_id": args.task_id or "module4-" + str(args.canonical_paper_id), "project_run_id": args.project_run_id or "",
        "paper_identity": {"canonical_paper_id": args.canonical_paper_id, "publication_version": args.publication_version or ""},
        "research_question": {"original_text": original, "question_en": plan.get("question_en"), "source_ref": source_ref(args.question_ref or args.plan, args.question_pointer if args.question_ref else "")}, "facets": facets,
        "global_constraints": {key: plan.get(key) for key in ("criteria", "seeds", "additional_constraints")},
        "navigation_context": {**navigation, "source_snapshot_refs": [{"path": str(args.cards)}] if args.cards else []}, "runtime_profile_ref": {"model": args.model, "thinking": False},
        "output_preferences": {"analysis_language": "zh", "quote_language": "original"}, "reading_policy": {"scope": "full_available_paper"},
        "upstream_snapshot_refs": [source_ref(path) for path in dict.fromkeys(path for path in (args.plan, args.question_ref, args.cards, args.corpus) if path)],
    }
    return task


def cmd_inspect(args: argparse.Namespace) -> int:
    snapshot = _snapshot(args.snapshot)
    task = _json(args.input)
    value = task if task.get("schema_version") else assemble_reading_input(task, snapshot, {"allow_background_material": args.allow_background_material})
    validation = validate_input(value, snapshot)
    print(json.dumps(validation, ensure_ascii=False, indent=2))
    return 0 if validation["valid"] else 2


def cmd_prepare(args: argparse.Namespace) -> int:
    snapshot = _snapshot(args.snapshot)
    task = _prepare_task(args)
    value = assemble_reading_input(task, snapshot, {"allow_background_material": args.allow_background_material})
    _write(Path(args.output_dir) / "READING_INPUT.json", value)
    validation = validate_input(value, snapshot)
    _write(Path(args.output_dir) / "INPUT_VALIDATION.json", validation)
    print(json.dumps({"output_dir": str(args.output_dir), "input_fingerprint": value["input_fingerprint"]}, ensure_ascii=False))
    return 0 if validation["valid"] else 2


def cmd_read(args: argparse.Namespace) -> int:
    snapshot = _snapshot(args.snapshot)
    value = _json(args.input)
    validation = validate_input(value, snapshot)
    if args.dry_run:
        print(json.dumps({"dry_run": True, "strategy": args.strategy, "model": args.model, "snapshot_id": snapshot.snapshot_id, "validation": validation}, ensure_ascii=False, indent=2))
        return 0 if validation.get("valid") else 2
    if not args.real:
        raise ValueError("read_requires_explicit_real_or_dry_run")
    if args.global_budget_cny is None or args.global_budget_cny <= 0 or not args.budget_ledger_path:
        raise ValueError("positive_global_budget_and_budget_ledger_path_required")
    if not args.key_file or Path(args.key_file).resolve() != (ROOT / "api_keys/qwen-api-key.txt").resolve():
        raise ValueError("real_run_requires_explicit_api_keys_qwen_api_key_txt")
    ledger = GlobalBudgetLedger(limit_cny=args.global_budget_cny, path=args.budget_ledger_path)
    client = QwenDirectClient(model=args.model, key_file=args.key_file, max_output_tokens=args.max_output_tokens, thinking=args.thinking, thinking_budget=args.thinking_budget, json_mode=not args.free_json, raw_response_dir=Path(args.output_dir) / "raw_responses", budget_ledger=ledger)
    profile = {"model": args.model, "thinking": args.thinking, "thinking_budget": args.thinking_budget, "json_mode": not args.free_json, "source_binding": args.source_binding, "semantic_verifier": not args.no_verifier, "max_output_tokens": args.max_output_tokens, "verifier_output_tokens": args.verifier_output_tokens}
    if args.revision_context:
        profile["revision_context"] = _json(args.revision_context)
    result = read_paper(value, snapshot, profile, args.strategy, client, verifier_client=client if not args.no_verifier else False, output_dir=args.output_dir, budget_ledger=ledger)
    print(json.dumps({"dossier_id": result.dossier["dossier_id"], "status": result.dossier["status"]}, ensure_ascii=False, indent=2))
    return 0 if result.dossier["status"]["delivery_state"] in {"ready", "ready_with_limits"} else 2


def cmd_handoff(args: argparse.Namespace) -> int:
    from optomind_research.runtime.upgrade3.module4.writing_handoff import build_writing_handoff
    dossier = _json(args.dossier)
    snapshot = _snapshot(args.snapshot)
    validation = validate_input(dossier.get("input", {}), snapshot)
    if args.dry_run:
        print(json.dumps({"dry_run": True, "validation": validation}, ensure_ascii=False, indent=2))
        return 0 if validation.get("valid") else 2
    result = build_writing_handoff(dossier, snapshot, output_dir=args.output_dir)
    print(json.dumps({"handoff": str(Path(args.output_dir) / "WRITING_HANDOFF.json"),
        "summary": result["summary"], "issues": result["issues"]}, ensure_ascii=False))
    return 0 if not result["issues"] else 2


def cmd_verify(args: argparse.Namespace) -> int:
    dossier = _json(args.dossier)
    snapshot = _snapshot(args.snapshot)
    validation = validate_dossier(dossier, snapshot)
    print(json.dumps(validation, ensure_ascii=False, indent=2))
    return 0 if validation["valid"] else 2


def cmd_render(args: argparse.Namespace) -> int:
    dossier = _json(args.dossier)
    text = render_dossier(dossier)
    if args.output:
        Path(args.output).write_text(text, encoding="utf-8")
    else:
        print(text, end="")
    return 0


def cmd_feedback(args: argparse.Namespace) -> int:
    cards = _json(args.cards)
    feedback = [json.loads(line) for line in Path(args.feedback).read_text(encoding="utf-8").splitlines() if line.strip()]
    dossiers = [_json(path) for path in args.dossier]
    print(json.dumps(build_post_reading_view(cards, feedback, dossiers), ensure_ascii=False, indent=2))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="module4", description="OptoMind Module 4 full available-text reader; only read --real calls Qwen.")
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("inspect-input", help="validate a prepared input and snapshot without a model call")
    p.add_argument("--input", required=True); p.add_argument("--snapshot", required=True); p.add_argument("--allow-background-material", action="store_true"); p.set_defaults(func=cmd_inspect)
    p = sub.add_parser("prepare", help="assemble PLAN, original question, cards, and snapshot references")
    for name, required in (("plan", True), ("snapshot", True), ("canonical-paper-id", True), ("question-ref", False), ("cards", False), ("corpus", False)):
        p.add_argument("--" + name, required=required)
    p.add_argument("--question-pointer", default="/research_question/original_text"); p.add_argument("--output-dir", required=True); p.add_argument("--task-id"); p.add_argument("--project-run-id"); p.add_argument("--publication-version"); p.add_argument("--model", choices=("qwen3.7-flash", "qwen3.5-plus"), required=True); p.add_argument("--allow-background-material", action="store_true"); p.set_defaults(func=cmd_prepare)
    p = sub.add_parser("read", help="run explicit Qwen full-text reading (paid; use --dry-run for planning)")
    p.add_argument("--input", required=True); p.add_argument("--snapshot", required=True); p.add_argument("--output-dir", required=True); p.add_argument("--strategy", choices=("whole_text", "section"), default="whole_text"); p.add_argument("--model", choices=("qwen3.7-flash", "qwen3.5-plus"), required=True); p.add_argument("--key-file"); p.add_argument("--max-output-tokens", type=int, default=32768); p.add_argument("--global-budget-cny", type=float); p.add_argument("--budget-ledger-path"); p.add_argument("--source-binding", choices=("exact_quote", "whole_block"), default="exact_quote"); p.add_argument("--thinking", action="store_true"); p.add_argument("--thinking-budget", type=int, default=8192); p.add_argument("--free-json", action="store_true"); p.add_argument("--revision-context"); p.add_argument("--no-verifier", action="store_true"); p.add_argument("--real", action="store_true"); p.add_argument("--dry-run", action="store_true"); p.set_defaults(func=cmd_read)
    p.add_argument("--verifier-output-tokens", type=int, default=16384)
    p = sub.add_parser("handoff", help="package complete claims and conditions for writing, without a model call")
    p.add_argument("--dossier", required=True); p.add_argument("--snapshot", required=True); p.add_argument("--output-dir", required=True)
    p.add_argument("--dry-run", action="store_true"); p.set_defaults(func=cmd_handoff)
    p = sub.add_parser("verify", help="validate dossier anchors and references without a model call"); p.add_argument("--dossier", required=True); p.add_argument("--snapshot", required=True); p.set_defaults(func=cmd_verify)
    p = sub.add_parser("render", help="deterministically render a dossier"); p.add_argument("--dossier", required=True); p.add_argument("--output"); p.set_defaults(func=cmd_render)
    p = sub.add_parser("feedback-view", help="build a post-reading view from M3 cards and feedback"); p.add_argument("--cards", required=True); p.add_argument("--feedback", required=True); p.add_argument("--dossier", required=True, action="append"); p.set_defaults(func=cmd_feedback)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
