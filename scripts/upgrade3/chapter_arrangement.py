"""CLI for chapter-internal arrangement.

Work order 03 of ``outputs/work_orders/20260926_chapter_arrangement``.

Default mode is an offline preview: it builds the arrangement view for a chapter
and writes it out without calling any model.  ``--run`` performs the bounded
model call under this round's own ceiling and writes the downstream artifacts.

The command never touches the upstream plan: it reads the writer packet and
writes only under its own output root.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
from pathlib import Path
from typing import Mapping

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from optomind_research.runtime.upgrade3.chapter_arrangement import (
    ROUND_CALL_PREFIX,
    ChapterArrangementError,
    RoundBudgetExceeded,
    RoundCappedLedger,
    arrangement_messages,
    build_chapter_view,
    build_source_catalog,
    compact_chapter_tool_materials,
    estimate_arrangement_cost,
    load_editor_prompt,
    parse_arrangement_response,
    render_arrangement_markdown,
    run_arrangement,
    source_usage_summary,
    validate_arrangement,
    write_view,
)

DEFAULT_PACKET_ROOT = PROJECT_ROOT / "outputs/progressive_review_plan/20260926_astra_repair/run585"
DEFAULT_OUTPUT_ROOT = PROJECT_ROOT / "outputs/chapter_arrangement/20260926_deepseek"
DEFAULT_LEDGER = PROJECT_ROOT / "outputs/review_blueprint/20260922_phase1/budget.sqlite"
DEFAULT_KEY_FILE = PROJECT_ROOT / "api_keys/qwen-api-key.txt"
DEFAULT_MODEL = "qwen3.5-plus"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Arrange one chapter's writing tasks from its writer packet (offline preview by default)."
    )
    parser.add_argument("--chapter", default="", help="Chapter id, e.g. CH05; may be repeated via --chapter")
    parser.add_argument("--chapters", default="", help="Comma-separated chapter ids")
    parser.add_argument("--all-chapters", action="store_true", help="Arrange every chapter in the plan")
    parser.add_argument("--packet-root", default=str(DEFAULT_PACKET_ROOT))
    parser.add_argument("--output-root", default=str(DEFAULT_OUTPUT_ROOT))
    parser.add_argument("--run", action="store_true", help="Perform the model call; default is offline preview")
    parser.add_argument(
        "--reexport-from",
        action="append",
        default=[],
        metavar="PATH",
        help="Offline re-export: re-validate a saved response/cache/arrangement with the current program "
             "and write the products. Never calls the model. May be repeated; each path pairs with a --chapter.",
    )
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--key-file", default=str(DEFAULT_KEY_FILE))
    parser.add_argument("--budget-ledger", default=str(DEFAULT_LEDGER))
    parser.add_argument("--round-cap-cny", type=float, default=10.0,
                        help="This round's own ceiling; never above 10.0")
    parser.add_argument("--output-tokens", type=int, default=20000)
    parser.add_argument("--thinking-budget", type=int, default=12000)
    parser.add_argument("--timeout-seconds", type=float, default=900.0)
    parser.add_argument("--max-source-chars", type=int, default=240)
    parser.add_argument("--no-retry", action="store_true", help="Disable transport retries (each attempt is billed)")
    parser.add_argument("--force", action="store_true", help="Ignore the saved result for the same input")
    parser.add_argument(
        "--planning-revision", action="store_true",
        help="启用 opt-in 章节论证模式：负责人建立判断，编排只组织表达",
    )
    return parser


def _chapter_ids(args: argparse.Namespace, plan: dict) -> list[str]:
    if args.all_chapters:
        return [str(row.get("chapter_id")) for row in plan.get("shared_outline") or [] if row.get("chapter_id")]
    requested = [item for item in [args.chapter, *args.chapters.split(",")] if str(item or "").strip()]
    if not requested:
        raise ChapterArrangementError("chapter_required")
    return [item.strip() for item in requested]


def _load_plan(packet_root: Path) -> dict:
    plan_path = packet_root / "DETAILED_REVIEW_PLAN.json"
    if not plan_path.is_file():
        raise ChapterArrangementError("plan_missing:" + str(plan_path))
    return json.loads(plan_path.read_text(encoding="utf-8"))


def _signature(payload: dict, config: dict) -> str:
    blob = json.dumps({"payload": payload, "config": config}, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def _cached_result(cache_dir: Path, signature: str) -> dict | None:
    path = cache_dir / (signature + ".json")
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def _save_result(cache_dir: Path, signature: str, value: dict) -> None:
    """Cache the *model output*, not the program's annotations.

    Caching the validated object meant a restored run carried the old program's
    validation and export fields with it.  What is stored now is the parsed
    model payload plus honest bookkeeping; every restore re-validates with the
    code that is running right now.
    """

    cache_dir.mkdir(parents=True, exist_ok=True)
    (cache_dir / (signature + ".json")).write_text(
        json.dumps(value, ensure_ascii=False, indent=2, default=str), encoding="utf-8")


def _cache_entry(payload: dict, *, model: str, signature: str, raw_response: str = "") -> dict:
    return {
        "schema_version": "optomind.chapter_arrangement.cache.v1",
        "saved_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "model": model,
        "signature": signature,
        "raw_response": raw_response,
        "payload": payload,
    }


def _payload_from_saved(saved: dict) -> tuple[dict, dict]:
    """Read a cache file (new or legacy) and return the model payload + meta.

    New files wrap the payload in ``payload``.  Legacy files (written before
    this repair) are validated arrangements: their task fields are re-validated
    from scratch, so restored runs are produced by the current program.
    """

    if isinstance(saved.get("payload"), dict):
        return dict(saved["payload"]), {
            "cache_schema": saved.get("schema_version") or "unknown",
            "legacy_cache": False,
            "saved_at": saved.get("saved_at") or "",
            "model": saved.get("model") or "",
            "raw_response": saved.get("raw_response") or "",
        }
    payload = {
        key: saved[key]
        for key in ("chapter_id", "chapter_argument", "units", "unused_sources")
        if key in saved
    }
    return payload, {
        "cache_schema": "legacy_validated_arrangement",
        "legacy_cache": True,
        "saved_at": saved.get("saved_at") or "",
        "model": "",
        "raw_response": "",
    }


def _payload_from_raw_response(raw: str) -> dict:
    """Pull the model's message content out of a saved raw HTTP body."""

    try:
        body = json.loads(raw)
    except ValueError:
        return parse_arrangement_response(raw)
    choices = body.get("choices") or []
    if not choices:
        return parse_arrangement_response(raw)
    message = choices[0].get("message") or {}
    content = message.get("content") or message.get("reasoning_content")
    if not content:
        raise ChapterArrangementError("raw_response_without_content")
    if isinstance(content, (dict, list)):
        return parse_arrangement_response(content)
    return parse_arrangement_response(str(content))


def _payload_from_saved_path(path: Path) -> tuple[dict, dict]:
    """Read an explicitly named saved artifact: raw response, cache, or export."""

    if not path.is_file():
        raise ChapterArrangementError("reexport_source_missing:" + str(path))
    text = path.read_text(encoding="utf-8")
    suffix = path.suffix.lower()
    if suffix == ".raw":
        return _payload_from_raw_response(text), {
            "cache_schema": "raw_response",
            "legacy_cache": False,
            "saved_at": "",
            "model": "",
            "raw_response": str(path),
        }
    try:
        saved = json.loads(text)
    except ValueError as exc:
        raise ChapterArrangementError("reexport_source_unreadable:" + str(path)) from exc
    if not isinstance(saved, dict):
        raise ChapterArrangementError("reexport_source_not_object:" + str(path))
    payload, meta = _payload_from_saved(saved)
    meta["raw_response"] = meta.get("raw_response") or str(path)
    return payload, meta


def _save_payload_cache(
    cache_dir: Path,
    signature: str,
    payload: dict,
    *,
    model: str,
    raw_response: str,
) -> None:
    _save_result(cache_dir, signature, _cache_entry(
        payload, model=model, signature=signature, raw_response=raw_response))


def _reexport_provenance(source: Path, meta: dict) -> dict:
    return {
        "model_call": False,
        "revalidated_from": str(source),
        "saved_at": meta.get("saved_at") or "",
        "legacy_cache": bool(meta.get("legacy_cache")),
        "note": "旧模型响应由当前本地程序离线重导（未调用模型，也不代表新提示词生成过该结果）",
    }


def _status_for(arrangement: dict, *, prefix: str = "") -> str:
    """Map the program's arrangement status onto this run's report vocabulary."""

    status = str((arrangement.get("validation") or {}).get("status") or "")
    if status == "arranged":
        return prefix + "arranged" if prefix else "arranged"
    if status == "needs_arrangement":
        return prefix + "needs_arrangement" if prefix else "needs_arrangement"
    return prefix + "contract_failed" if prefix else "contract_failed"


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    args = _parser().parse_args(argv)
    packet_root = Path(args.packet_root).resolve()
    output_root = Path(args.output_root).resolve()
    output_root.mkdir(parents=True, exist_ok=True)
    try:
        if args.round_cap_cny > 10.0:
            raise ChapterArrangementError(
                "round_cap_above_authorised_ceiling:%.2f>10.00" % args.round_cap_cny)
        if args.run and args.reexport_from:
            raise ChapterArrangementError("choose_either_run_or_reexport_from")
        plan = _load_plan(packet_root)
        shared_outline = plan.get("shared_outline") or []
        # The final coordinator's argument and the review's coverage are
        # different inputs. Older plans may have only the packet argument;
        # build_chapter_view retains that fallback and marks a true absence.
        review_argument = str(plan.get("review_argument") or "").strip()
        prompt = load_editor_prompt(planning_revision=args.planning_revision)
        reexport_mode = bool(args.reexport_from)
        chapter_ids = _chapter_ids(args, plan)
        if reexport_mode and len(args.reexport_from) != len(chapter_ids):
            raise ChapterArrangementError(
                "reexport_needs_one_source_per_chapter:%d!=%d" % (len(args.reexport_from), len(chapter_ids)))
        reexport_sources = [Path(item).resolve() for item in args.reexport_from]

        ledger = None
        if args.run:
            ledger = RoundCappedLedger(args.budget_ledger, round_cap_cny=args.round_cap_cny)
        report = {
            "mode": "reexport" if reexport_mode else ("run" if args.run else "preview"),
            "model": args.model,
            "round_cap_cny": args.round_cap_cny,
            "round_before": ledger.round_state() if ledger else None,
            "model_calls": 0,
            "chapters": [],
        }
        for index, chapter_id in enumerate(chapter_ids):
            packet_path = packet_root / "writer_packets" / (chapter_id + ".json")
            view = build_chapter_view(
                packet_path,
                shared_outline=shared_outline,
                review_argument=review_argument,
                review_argument_status=str(plan.get("review_argument_status") or "") if review_argument else "",
                review_argument_source=str(plan.get("review_argument_source") or "") if review_argument else "",
                shared_scope=plan.get("shared_scope") if isinstance(plan.get("shared_scope"), Mapping) else None,
                id_map_path=output_root / "ID_MAP.json",
            )
            payload = view.arrangement_payload(max_source_chars=args.max_source_chars)
            if args.planning_revision:
                payload["planning_revision_mode"] = True
            chapter_dir = output_root / chapter_id
            chapter_dir.mkdir(parents=True, exist_ok=True)
            write_view(view, chapter_dir / "ARRANGEMENT_INPUT.json")
            usage = source_usage_summary(view)
            (chapter_dir / "INPUT_SOURCE_USAGE.json").write_text(
                json.dumps(usage, ensure_ascii=False, indent=2), encoding="utf-8")

            config = {
                "model": args.model,
                "max_source_chars": args.max_source_chars,
                "output_tokens": args.output_tokens,
                "thinking_budget": args.thinking_budget,
                "prompt_sha256": hashlib.sha256(prompt.encode("utf-8")).hexdigest()[:16],
                "planning_revision": bool(args.planning_revision),
            }
            signature = _signature(payload, config)
            cache_dir = chapter_dir / "_cache"
            estimate = estimate_arrangement_cost(
                arrangement_messages(payload, prompt=prompt, planning_revision=args.planning_revision),
                model=args.model,
                output_tokens=args.output_tokens,
                thinking_budget=args.thinking_budget,
            )
            entry = {
                "chapter_id": chapter_id,
                "packet": str(packet_path),
                "view": str(chapter_dir / "ARRANGEMENT_INPUT.json"),
                "input_sources": usage["source_records"],
                "input_paragraph_level": usage["at_paragraph_level"],
                "input_case_only": usage["still_case_only"],
                "signature": signature,
                "estimate": estimate,
            }

            if reexport_mode:
                source = reexport_sources[index]
                saved_payload, meta = _payload_from_saved_path(source)
                arrangement = validate_arrangement(saved_payload, view, planning_revision=args.planning_revision)
                arrangement["provenance"] = _reexport_provenance(source, meta)
                entry["status"] = _status_for(arrangement, prefix="reimported_")
                entry["revalidated_from"] = str(source)
                entry["legacy_cache"] = bool(meta.get("legacy_cache"))
                entry["validation"] = arrangement["validation"]
                _export(chapter_dir, view, arrangement)
                entry["arrangement"] = str(chapter_dir / "CHAPTER_ARRANGEMENT.json")
                entry["markdown"] = str(chapter_dir / "CHAPTER_ARRANGEMENT.md")
                entry["source_usage"] = str(chapter_dir / "SOURCE_USAGE.json")
                report["chapters"].append(entry)
                continue

            if not args.run:
                entry["status"] = "preview_only"
                report["chapters"].append(entry)
                continue

            cached = None if args.force else _cached_result(cache_dir, signature)
            if cached:
                # A restored result is re-validated and re-exported by the code
                # running now; an unfinished saved result is never reported as a
                # successful reuse.
                saved_payload, meta = _payload_from_saved(cached)
                arrangement = validate_arrangement(saved_payload, view, planning_revision=args.planning_revision)
                raw_hint = meta.get("raw_response") or ""
                arrangement["provenance"] = {
                    "model_call": True,
                    "restored_from_cache": True,
                    "legacy_cache": bool(meta.get("legacy_cache")),
                    "saved_at": meta.get("saved_at") or "",
                    "raw_response": raw_hint,
                    "note": "同输入同配置命中缓存：模型输出沿用，程序侧校验与导出按当前代码重算（本次未调用模型）",
                }
                entry["status"] = _status_for(arrangement, prefix="reused_")
                entry["restored_from_cache"] = True
                entry["legacy_cache"] = bool(meta.get("legacy_cache"))
                entry["validation"] = arrangement["validation"]
                entry["arrangement"] = str(chapter_dir / "CHAPTER_ARRANGEMENT.json")
                entry["markdown"] = str(chapter_dir / "CHAPTER_ARRANGEMENT.md")
                entry["source_usage"] = str(chapter_dir / "SOURCE_USAGE.json")
                _export(chapter_dir, view, arrangement)
                report["chapters"].append(entry)
                continue

            from optomind_research.runtime.upgrade3.module4.runtime import QwenDirectClient

            retries = 0 if args.no_retry else 1
            client = QwenDirectClient(
                model=args.model,
                key_file=args.key_file,
                max_retries=retries,
                timeout_seconds=args.timeout_seconds,
                max_output_tokens=args.output_tokens,
                thinking=True,
                thinking_budget=args.thinking_budget,
                json_mode=False,
                budget_ledger=ledger,
                raw_response_dir=chapter_dir / "raw_responses",
            )
            try:
                arrangement = run_arrangement(
                    view,
                    client=client,
                    model=args.model,
                    prompt=prompt,
                    view_payload=payload,
                    call_id=f"{ROUND_CALL_PREFIX}{chapter_id}",
                    raw_response_dir=chapter_dir / "raw_responses",
                    planning_revision=args.planning_revision,
                )
                report["model_calls"] = report.get("model_calls", 0) + 1
            except RoundBudgetExceeded as exc:
                entry["status"] = "refused_round_budget"
                entry["error"] = str(exc)
                entry["round_state"] = ledger.round_state()
                report["chapters"].append(entry)
                break
            except Exception as exc:  # noqa: BLE001 - report, keep earlier results
                entry["status"] = "failed"
                entry["error"] = type(exc).__name__ + ":" + str(exc)[:200]
                entry["round_state"] = ledger.round_state()
                report["chapters"].append(entry)
                continue
            entry["status"] = _status_for(arrangement)
            entry["validation"] = arrangement["validation"]
            if arrangement["validation"]["ok"]:
                # Only a finished arrangement is cached as a success.
                raw_files = sorted((chapter_dir / "raw_responses").glob("*.raw"))
                _save_payload_cache(
                    cache_dir, signature, saved_payload_from(arrangement),
                    model=args.model,
                    raw_response=str(raw_files[-1]) if raw_files else "",
                )
            entry["round_state"] = ledger.round_state()
            _export(chapter_dir, view, arrangement)
            entry["arrangement"] = str(chapter_dir / "CHAPTER_ARRANGEMENT.json")
            entry["markdown"] = str(chapter_dir / "CHAPTER_ARRANGEMENT.md")
            entry["source_usage"] = str(chapter_dir / "SOURCE_USAGE.json")
            report["chapters"].append(entry)

        if ledger:
            report["round_after"] = ledger.round_state()
        (output_root / "ARRANGEMENT_RUN.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
        return 0
    except ChapterArrangementError as exc:
        print(json.dumps({"error": type(exc).__name__, "message": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2


def saved_payload_from(arrangement: Mapping) -> dict:
    """The model-authored part of a validated arrangement, for caching."""

    return {
        "chapter_id": arrangement.get("chapter_id"),
        "chapter_argument": arrangement.get("chapter_argument"),
        "units": [
            {
                "unit_id": unit.get("unit_id"),
                "focus": unit.get("focus"),
                **({"unit_title": str(unit.get("unit_title") or unit.get("title") or "").strip()}
                   if str(unit.get("unit_title") or unit.get("title") or "").strip() else {}),
                "unit_notes": unit.get("unit_notes") or "",
                "paragraph_tasks": [
                    {
                        "paragraph_id": task.get("paragraph_id"),
                        "point": task.get("point"),
                        "development": task.get("development"),
                        "source_uses": [
                            {"source_handle": use.get("source_handle"), "role": use.get("role"),
                             "use": use.get("use")}
                            for use in task.get("source_uses") or ()
                        ],
                    }
                    for task in unit.get("paragraph_tasks") or ()
                ],
                "table_tasks": [
                    {
                        "table_id": table.get("table_id"),
                        "purpose": table.get("purpose"),
                        "columns": list(table.get("columns") or []),
                        "row_tasks": [
                            {
                                "content": row.get("content"),
                                "source_uses": [
                                    {"source_handle": use.get("source_handle"), "role": use.get("role"),
                                     "use": use.get("use")}
                                    for use in row.get("source_uses") or ()
                                ],
                            }
                            for row in table.get("row_tasks") or ()
                        ],
                    }
                    for table in unit.get("table_tasks") or ()
                ],
            }
            for unit in arrangement.get("units") or ()
        ],
        "unused_sources": [
            {"source_handle": item.get("source_handle"), "reason": item.get("reason")}
            for item in arrangement.get("unused_sources") or ()
        ],
    }


def _export(chapter_dir: Path, view, arrangement: dict) -> None:
    # The exported product carries the locally completed source catalogue, so a
    # writer can open a task and reach its material without searching again.
    exported = dict(arrangement)
    exported["source_catalog"] = build_source_catalog(view, arrangement)
    # Multi-source tool returns travel next to the catalogue with their source
    # set intact instead of being folded into one paper's record.
    exported["chapter_tool_materials"] = compact_chapter_tool_materials(view)
    (chapter_dir / "CHAPTER_ARRANGEMENT.json").write_text(
        json.dumps(exported, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    (chapter_dir / "CHAPTER_ARRANGEMENT.md").write_text(
        render_arrangement_markdown(exported, view), encoding="utf-8")
    (chapter_dir / "SOURCE_USAGE.json").write_text(
        json.dumps(source_usage_summary(view, arrangement), ensure_ascii=False, indent=2, default=str),
        encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(main())
