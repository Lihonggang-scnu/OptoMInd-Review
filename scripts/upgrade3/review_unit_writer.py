"""CLI for unit writing (work order 03 of the 20260927 package).

Default mode is an offline preview: it assembles one arranged unit and its real
material, writes the exact messages it would send, and estimates the cost.  No
key is read and no request is made.  ``--fake-client`` runs the whole export path
with a canned response (clearly marked as simulated).  ``--run`` is the only mode
that touches the network and must be asked for explicitly.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from optomind_research.runtime.upgrade3.review_unit_writer import (
    DEFAULT_MAX_MATERIAL_CHARS_PER_SOURCE,
    DEFAULT_MODEL,
    DEFAULT_OUTPUT_TOKENS,
    DEFAULT_THINKING_BUDGET,
    LARGE_INPUT_TOKENS,
    UnitWritingError,
    _default_qwen_token_counter,
    build_unit_view,
    estimate_unit_cost,
    load_writer_prompt,
    completion_messages,
    run_unit_writing,
    run_unit_completion,
    unit_messages,
    unit_payload,
    write_unit_input,
    write_unit_completion,
    write_unit_output,
)
from optomind_research.runtime.upgrade3.portable_paths import portable_component

DEFAULT_OUTPUT_ROOT = PROJECT_ROOT / "outputs/unit_writing/20260927_deepseek"
DEFAULT_LEDGER = PROJECT_ROOT / "outputs/review_blueprint/20260922_phase1/budget.sqlite"
DEFAULT_KEY_FILE = PROJECT_ROOT / "api_keys/qwen-api-key.txt"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Write one arranged unit from its real material (offline preview by default)."
    )
    parser.add_argument("--arrangement", required=True,
                        help="Path to CHxx/CHAPTER_ARRANGEMENT.json from the arrangement step")
    parser.add_argument("--unit", action="append", default=[],
                        help="unit_id to write, e.g. CH05_U01; may be repeated")
    parser.add_argument("--existing-body", default="",
                        help="Existing body file for explicit omitted-task completion")
    parser.add_argument("--complete-task", action="append", default=[],
                        help="Task id to complete explicitly; repeatable, requires --existing-body")
    parser.add_argument("--view", default="",
                        help="ARRANGEMENT_INPUT.json (defaults to the arrangement's sibling file)")
    parser.add_argument("--packet-root", default="",
                        help="run585 root, only needed if ARRANGEMENT_INPUT.json is absent")
    parser.add_argument("--output-root", default=str(DEFAULT_OUTPUT_ROOT))
    parser.add_argument("--language", default="zh")
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--max-material-chars-per-source", type=int,
                        default=DEFAULT_MAX_MATERIAL_CHARS_PER_SOURCE,
                        help="Informational per-source capacity threshold; 0 disables it. Full material is always preserved.")
    parser.add_argument("--keep-deep-read-references", action="store_true",
                        help="Also send the source paper's own bibliography from the deep read")
    parser.add_argument("--output-tokens", type=int, default=DEFAULT_OUTPUT_TOKENS)
    parser.add_argument("--thinking-budget", type=int, default=DEFAULT_THINKING_BUDGET,
                        help="Reasoning token allocation; set 0 to explicitly disable thinking")
    parser.add_argument("--timeout-seconds", type=float, default=900.0)
    parser.add_argument("--key-file", default=str(DEFAULT_KEY_FILE))
    parser.add_argument("--budget-ledger", default=str(DEFAULT_LEDGER),
                        help="The shared ledger is reused when --run is used; no new wallet")
    parser.add_argument("--global-budget-cny", type=float, default=None,
                        help="Explicitly initialize/complete a ledger cap for --run")
    parser.add_argument("--run", action="store_true", help="Perform the real writing call")
    parser.add_argument("--fake-client", default="",
                        help="Offline simulation: a Markdown or JSON file used as the model reply")
    parser.add_argument("--print-preview", action="store_true",
                        help="Also print the messages and material summary to stdout")
    parser.add_argument(
        "--planning-revision", action="store_true",
        help="启用 opt-in 章节论证模式：写作者只执行更新后的具体任务",
    )
    return parser


def _unit_ids(args: argparse.Namespace) -> list[str]:
    ids = [str(item).strip() for item in args.unit if str(item or "").strip()]
    if not ids:
        raise UnitWritingError("unit_required:pass --unit CHxx_UNN")
    return ids


def _fake_client(path: str):
    source = Path(path)
    if not source.is_file():
        raise UnitWritingError("fake_client_file_missing:" + str(source))

    def client(messages, **kwargs):  # noqa: ANN001 - test double, mirrors the real client
        text = source.read_text(encoding="utf-8")
        if source.suffix.lower() in {".json"}:
            try:
                payload = json.loads(text)
            except ValueError:
                return {"content": text, "finish_reason": "stop", "complete": True,
                        "usage": {"prompt_tokens": 0, "completion_tokens": 0}}
            return payload if isinstance(payload, dict) else {"content": text, "complete": True}
        return {"content": text, "finish_reason": "stop", "complete": True,
                "usage": {"prompt_tokens": 0, "completion_tokens": 0}}

    return client


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    args = _parser().parse_args(argv)
    try:
        if args.run and args.fake_client:
            raise UnitWritingError("choose_either_run_or_fake_client")
        prompt = load_writer_prompt(planning_revision=args.planning_revision)
        unit_ids = _unit_ids(args)
        completion_mode = bool(args.complete_task)
        if args.existing_body and not completion_mode:
            raise UnitWritingError("existing_body_requires_complete_task")
        if completion_mode:
            if len(unit_ids) != 1:
                raise UnitWritingError("completion_requires_one_unit")
            if not args.existing_body:
                raise UnitWritingError("completion_requires_existing_body")
            existing_path = Path(args.existing_body).resolve()
            if not existing_path.is_file():
                raise UnitWritingError("existing_body_missing:" + str(existing_path))
            if args.run and args.fake_client:
                raise UnitWritingError("choose_either_run_or_fake_client")

        arrangement_path = Path(args.arrangement).resolve()
        output_root = Path(args.output_root).resolve()
        mode = "run" if args.run else ("fake" if args.fake_client else "preview")
        report = {
            "mode": mode,
            "model": args.model,
            "language": args.language,
            "arrangement": str(arrangement_path),
            "output_root": str(output_root),
            "model_calls": 0,
            "units": [],
        }
        token_counter = _default_qwen_token_counter()
        if completion_mode:
            unit_id = unit_ids[0]
            view = build_unit_view(
                arrangement_path,
                unit_id,
                view_path=args.view or None,
                packet_root=args.packet_root or None,
                max_material_chars_per_source=args.max_material_chars_per_source,
                keep_deep_read_references=args.keep_deep_read_references,
            )
            existing_body = existing_path.read_bytes().decode("utf-8")
            messages = completion_messages(
                view, existing_body, args.complete_task, prompt=prompt,
                language=args.language, planning_revision=args.planning_revision,
            )
            estimate = estimate_unit_cost(
                messages, model=args.model, output_tokens=args.output_tokens,
                thinking_budget=args.thinking_budget,
            )
            unit_dir = output_root / portable_component(f"{view.chapter_id}_{view.unit_id}_completion")
            # Never write the completion artifacts over the existing BODY file.
            if output_root.resolve() == existing_path.parent.resolve() or unit_dir.resolve() == existing_path.parent.resolve():
                raise UnitWritingError("completion_output_overlaps_existing_body")
            unit_dir.mkdir(parents=True, exist_ok=True)
            (unit_dir / "COMPLETION_MESSAGES.json").write_text(
                json.dumps(messages, ensure_ascii=False, indent=2), encoding="utf-8")
            (unit_dir / "COMPLETION_INPUT.json").write_text(json.dumps({
                "chapter_id": view.chapter_id,
                "unit_id": view.unit_id,
                "existing_body_path": str(existing_path),
                "task_ids": list(args.complete_task),
                "payload": json.loads(messages[-1]["content"]),
                "estimate": estimate,
                "mode": mode,
            }, ensure_ascii=False, indent=2), encoding="utf-8")
            entry = {
                "chapter_id": view.chapter_id,
                "unit_id": view.unit_id,
                "task_ids": list(args.complete_task),
                "existing_body_path": str(existing_path),
                "output_dir": str(unit_dir),
                "estimate": estimate,
                "source_handles": json.loads(messages[-1]["content"]).get("requested_source_handles", []),
                "material_summary": view.material_summary(),
                "mode": mode,
            }
            if mode == "preview":
                (unit_dir / "ORIGINAL_BODY.md").write_bytes(existing_body.encode("utf-8"))
                (unit_dir / "COMPLETION_FRAGMENT.md").write_text("", encoding="utf-8")
                (unit_dir / "COMPLETED_BODY.md").write_bytes(existing_body.encode("utf-8"))
                (unit_dir / "COMPLETION_RESULT.json").write_text(json.dumps({
                    "schema_version": "optomind.review_unit_writer.result.v1.completion",
                    "mode": "preview", "pending": True,
                    "task_ids": list(args.complete_task),
                    "note": "预览未调用模型；仅保存完整 messages 和原正文。",
                }, ensure_ascii=False, indent=2), encoding="utf-8")
                entry["status"] = "preview_only"
                report["units"].append(entry)
            else:
                client = _fake_client(args.fake_client) if mode == "fake" else _real_client(
                    args, token_counter=token_counter)
                result = run_unit_completion(
                    view, existing_body=existing_body, task_ids=args.complete_task,
                    client=client, model=args.model, prompt=prompt,
                    language=args.language, output_tokens=args.output_tokens,
                    thinking_budget=args.thinking_budget,
                    raw_response_dir=unit_dir / "raw_responses" if mode == "run" else unit_dir / "fake_response",
                    planning_revision=args.planning_revision, simulated=mode == "fake",
                )
                written = write_unit_completion(
                    view, result, unit_dir, estimate=estimate, language=args.language)
                entry.update(written)
                entry["status"] = "pending" if written["pending"] else ("simulated" if mode == "fake" else "written")
                report["model_calls"] = 1 if mode == "run" else 0
                report["units"].append(entry)
            report["completion_mode"] = True
            report["existing_body"] = str(existing_path)
            report["task_ids"] = list(args.complete_task)
            report_path = output_root / "COMPLETION_RUN.json"
            output_root.mkdir(parents=True, exist_ok=True)
            report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
            print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
            return 0
        for unit_id in unit_ids:
            view = build_unit_view(
                arrangement_path,
                unit_id,
                view_path=args.view or None,
                packet_root=args.packet_root or None,
                max_material_chars_per_source=args.max_material_chars_per_source,
                keep_deep_read_references=args.keep_deep_read_references,
            )
            payload = unit_payload(
                view, language=args.language, planning_revision=args.planning_revision)
            messages = unit_messages(
                view, prompt=prompt, language=args.language, payload=payload,
                planning_revision=args.planning_revision,
            )
            estimate = estimate_unit_cost(
                messages, model=args.model, output_tokens=args.output_tokens,
                thinking_budget=args.thinking_budget)
            unit_dir = output_root / ("_simulated" if mode == "fake" else "") / \
                portable_component(f"{view.chapter_id}_{view.unit_id}")
            write_unit_input(view, messages, unit_dir, estimate=estimate, language=args.language)
            summary = view.material_summary()
            entry = {
                "chapter_id": view.chapter_id,
                "unit_id": view.unit_id,
                "focus": view.focus,
                "paragraph_tasks": len(view.paragraph_tasks),
                "table_tasks": len(view.table_tasks),
                "source_count": summary["sources"],
                "with_study_summary_A": summary["with_study_summary_A"],
                "with_review_planning_B": summary["with_review_planning_B"],
                "with_deep_read_material": summary["with_deep_read_material"],
                "material_read_from_disk": summary["read_from_disk"],
                "material_truncated": summary["truncated"],
                "material_capacity_threshold_exceeded": summary["capacity_threshold_exceeded"],
                "input_path": str(unit_dir / "UNIT_INPUT.json"),
                "messages_path": str(unit_dir / "UNIT_MESSAGES.json"),
                "estimate": estimate,
                "input_chars": len(json.dumps(messages, ensure_ascii=False)),
                "warnings": view.warnings,
                "planning_revision": bool(args.planning_revision),
            }
            if entry["estimate"]["prompt_tokens_estimate"] > LARGE_INPUT_TOKENS:
                entry["warnings"] = list(view.warnings) + [{
                    "code": "input_too_large_consider_splitting_unit",
                    "prompt_tokens_estimate": entry["estimate"]["prompt_tokens_estimate"],
                    "threshold_tokens": LARGE_INPUT_TOKENS,
                    "input_chars": entry["input_chars"],
                    "note": "没有静默截断后续来源；建议把该单元拆成更小的写作任务",
                }]
            if mode == "preview":
                entry["status"] = "preview_only"
                report["units"].append(entry)
                if args.print_preview:
                    print(json.dumps({
                        "unit": view.unit_id,
                        "material_summary": summary,
                        "messages": messages,
                    }, ensure_ascii=False, indent=2))
                continue

            client = _fake_client(args.fake_client) if mode == "fake" else _real_client(
                args, token_counter=token_counter)
            result = run_unit_writing(
                view, client=client, model=args.model, prompt=prompt, language=args.language,
                payload=payload,
                output_tokens=args.output_tokens,
                thinking_budget=args.thinking_budget,
                raw_response_dir=None if mode == "fake" else unit_dir / "raw_responses",
                planning_revision=args.planning_revision)
            if mode == "run":
                report["model_calls"] = report.get("model_calls", 0) + 1
            written = write_unit_output(
                view, result["body_markdown"], unit_dir,
                model=result.get("model") or args.model, language=args.language, mode=mode,
                used_messages=result["messages"], estimate=estimate, usage=result["usage"],
                simulated_from=str(Path(args.fake_client).resolve()) if mode == "fake" else "",
                response_path=result["raw_response"], finish_reason=result["finish_reason"],
                complete=result.get("complete", True), partial_error=result.get("partial_error", ""),
                issues=result.get("issues") or [], citation_diagnostics=result,
                effective_request=result.get("effective_request"), cap_pressure=result.get("cap_pressure"))
            entry.update({
                "status": (
                    "written" if mode == "run" and result.get("complete", True)
                    else "partial_length" if mode == "run" and result.get("finish_reason") == "length"
                    else "partial" if mode == "run" else "simulated"
                ),
                "body_path": written["body_path"],
                "result_path": str(unit_dir / "UNIT_RESULT.json"),
                "used_source_handles": written["used_source_handles"],
                "unused_source_handles": written["unused_source_handles"],
                "unknown_citations": written["unknown_citations"],
                "finish_reason": result["finish_reason"],
                "complete": result.get("complete", True),
                "completion_status": written["completion_status"],
                "partial_error": result.get("partial_error", ""),
                "usage": result["usage"],
                **{key: result[key] for key in ("effective_request", "cap_pressure") if key in result},
                "issues": result.get("issues") or [],
                **{key: written[key] for key in (
                    "citation_problems", "unresolved_numeric_citations",
                    "numeric_citation_repairs", "bibliography_title_citation_map",
                    "citation_number_map_origin", "citation_mapping_diagnostics",
                    "known_tool_identifiers", "non_source_identifier_citations",
                ) if key in written},
            })
            report["units"].append(entry)

        (output_root / "UNIT_WRITING_RUN.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
        return 0
    except UnitWritingError as exc:
        print(json.dumps({"error": type(exc).__name__, "message": str(exc)}, ensure_ascii=False),
              file=sys.stderr)
        return 2


def _real_client(args: argparse.Namespace, *, token_counter=None):
    from optomind_research.runtime.upgrade3.module4.runtime import GlobalBudgetLedger, QwenDirectClient

    budget_path = Path(args.budget_ledger) if args.budget_ledger else None
    explicit_limit = getattr(args, "global_budget_cny", None)
    if budget_path is None and explicit_limit is None:
        raise UnitWritingError("real_run_requires_existing_budget_ledger_or_explicit_budget")
    if budget_path is not None and not budget_path.is_file() and explicit_limit is None:
        raise UnitWritingError("real_run_budget_ledger_missing:" + str(budget_path))
    ledger = GlobalBudgetLedger(
        path=budget_path,
        limit_cny=explicit_limit,
    ) if budget_path is not None else GlobalBudgetLedger(limit_cny=explicit_limit)
    # GlobalBudgetLedger loads a durable cap lazily when the first reservation
    # is made.  Refresh it here so the preflight guard sees an existing cap and
    # does not reject a valid shared ledger before the real call.
    if budget_path is not None:
        ledger._refresh_from_db()
    if ledger.limit_cny is None:
        raise UnitWritingError("real_run_budget_ledger_limit_missing:" + str(budget_path))
    return QwenDirectClient(
        model=args.model,
        key_file=args.key_file,
        max_retries=0,
        timeout_seconds=args.timeout_seconds,
        max_output_tokens=args.output_tokens,
        thinking=bool(args.thinking_budget),
        thinking_budget=args.thinking_budget,
        json_mode=False,
        budget_ledger=ledger,
        prompt_token_counter=token_counter,
    )


if __name__ == "__main__":
    raise SystemExit(main())
