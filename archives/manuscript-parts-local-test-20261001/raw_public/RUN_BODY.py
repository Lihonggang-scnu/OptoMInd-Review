"""Small, fail-closed BODY acceptance driver.

This file deliberately lives beside the acceptance worktree.  It orchestrates
the checked-in arrangement, unit-writer, and assembler CLIs; it does not add a
second planner or a second writer implementation.

The ``arrange`` and ``write`` stages require ``--allow-paid``.  This guard is
intentional: a preview or an accidental invocation cannot spend from the
shared ledger.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Mapping


ACCEPTANCE_ROOT = Path(__file__).resolve().parent
REPO_ROOT = ACCEPTANCE_ROOT / "worktree"
DEFAULT_PLAN_ROOT = ACCEPTANCE_ROOT / "new_plan"
DEFAULT_OUTPUT_ROOT = ACCEPTANCE_ROOT / "body_run"
DEFAULT_LEDGER = ACCEPTANCE_ROOT / "budget.sqlite"
DEFAULT_KEY_FILE = REPO_ROOT / "api_keys" / "<local-key-file>"
DEFAULT_TOKENIZER = Path(r"F:\OptoMind-Review-2\data\tokenizers\qwen3_5_9b\tokenizer.json")
ARRANGEMENT_CLI = REPO_ROOT / "scripts" / "upgrade3" / "chapter_arrangement.py"
WRITER_CLI = REPO_ROOT / "scripts" / "upgrade3" / "review_unit_writer.py"
ASSEMBLER_CLI = REPO_ROOT / "scripts" / "upgrade3" / "full_review_draft.py"

if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

PLAN_SCHEMA = "optomind.progressive_review_plan.v2"
PARTS_CONTRACT = "optomind.manuscript_parts_plan.v1"


class DriverError(RuntimeError):
    pass


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise DriverError(f"missing_json:{path}") from exc
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise DriverError(f"invalid_json:{path}:{type(exc).__name__}") from exc


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str), encoding="utf-8")


def _chapter_id(row: Mapping[str, Any]) -> str:
    direct = str(row.get("chapter_id") or "").strip()
    if direct:
        return direct
    nested = row.get("chapter")
    return str(nested.get("chapter_id") or "").strip() if isinstance(nested, Mapping) else ""


def _plan_path(plan_root: Path) -> Path:
    path = plan_root.resolve()
    return path if path.suffix.lower() == ".json" else path / "DETAILED_REVIEW_PLAN.json"


def _planning_context(plan: Mapping[str, Any]) -> dict[str, Any]:
    fields = (
        "research_question", "review_argument", "shared_scope",
        "material_theme_inventory", "source_identity_map", "manuscript_parts_plan",
        "material_records", "material_access", "status",
        "manuscript_parts_plan_frozen", "manuscript_parts_plan_revision",
        "manuscript_parts_contract_version",
    )
    context = {key: plan[key] for key in fields if key in plan}
    context.setdefault("material_records", [])
    return context


def _validate_plan(path: Path) -> dict[str, Any]:
    """Validate the final frozen v2 plan and every packet locator/boundary."""

    plan = _read_json(path)
    if not isinstance(plan, dict):
        raise DriverError("final_plan_not_object:" + str(path))
    checks = {
        "schema_version": PLAN_SCHEMA,
        "manuscript_parts_contract_version": PARTS_CONTRACT,
        "manuscript_parts_plan_revision": "v2",
        "status": "complete",
    }
    for key, expected in checks.items():
        if plan.get(key) != expected:
            raise DriverError(f"final_plan_gate:{key}={plan.get(key)!r}:expected={expected!r}")
    if plan.get("manuscript_parts_plan_frozen") is not True:
        raise DriverError("final_plan_gate:manuscript_parts_plan_frozen!=true")

    try:
        from optomind_research.runtime.upgrade3.manuscript_parts import (
            boundary_projection,
            validate_body_tasks,
            validate_manuscript_parts_plan,
        )
        parts = validate_manuscript_parts_plan(plan.get("manuscript_parts_plan"))
    except Exception as exc:  # noqa: BLE001 - expose the contract failure clearly
        raise DriverError(f"final_plan_gate:manuscript_parts_plan:{type(exc).__name__}:{exc}") from exc

    shared_outline = plan.get("shared_outline")
    if not isinstance(shared_outline, list) or not shared_outline:
        raise DriverError("final_plan_gate:shared_outline_missing")
    chapter_ids = [_chapter_id(row) for row in shared_outline if isinstance(row, Mapping)]
    if not chapter_ids or any(not item for item in chapter_ids) or len(set(chapter_ids)) != len(chapter_ids):
        raise DriverError("final_plan_gate:shared_outline_chapter_ids_invalid")

    chapters = plan.get("chapters")
    packet_rows = plan.get("writer_packets")
    if not isinstance(chapters, list) or not chapters or not isinstance(packet_rows, list):
        raise DriverError("final_plan_gate:chapters_or_writer_packets_missing")
    packet_root = path.parent / "writer_packets"
    boundary = boundary_projection(parts)
    packet_ids: list[str] = []
    packet_paths: dict[str, Path] = {}
    for row in packet_rows:
        if not isinstance(row, Mapping):
            raise DriverError("final_plan_gate:writer_packet_not_object")
        chapter_id = str(row.get("chapter_id") or "").strip()
        relative = str(row.get("json_path") or "").strip()
        packet_path = (path.parent / relative).resolve() if relative else packet_root / f"{chapter_id}.json"
        if not chapter_id or not packet_path.is_file():
            raise DriverError(f"final_plan_gate:writer_packet_missing:{chapter_id}:{packet_path}")
        packet = _read_json(packet_path)
        if not isinstance(packet, dict):
            raise DriverError(f"final_plan_gate:writer_packet_not_object:{packet_path}")
        packet_chapter_id = _chapter_id(packet)
        if packet_chapter_id != chapter_id:
            raise DriverError(f"final_plan_gate:packet_identity:{chapter_id}:{packet_chapter_id}")
        if packet.get("schema_version") != PLAN_SCHEMA:
            raise DriverError(f"final_plan_gate:packet_schema:{chapter_id}")
        if packet.get("manuscript_parts_contract_version") != PARTS_CONTRACT:
            raise DriverError(f"final_plan_gate:packet_contract:{chapter_id}")
        locator = str(packet.get("planning_result_path") or "").strip()
        resolved_locator = (packet_path.parent / locator).resolve() if locator else None
        if resolved_locator != path.resolve():
            raise DriverError(f"final_plan_gate:packet_plan_locator:{chapter_id}")
        if packet.get("manuscript_parts_boundary") != boundary:
            raise DriverError(f"final_plan_gate:packet_boundary:{chapter_id}")
        chapter_plan = packet.get("chapter_plan")
        if not isinstance(chapter_plan, Mapping):
            raise DriverError(f"final_plan_gate:chapter_plan_missing:{chapter_id}")
        try:
            validate_body_tasks([chapter_plan])
        except Exception as exc:  # noqa: BLE001
            raise DriverError(f"final_plan_gate:body_tasks:{chapter_id}:{exc}") from exc
        packet_ids.append(chapter_id)
        packet_paths[chapter_id] = packet_path
    if set(packet_ids) != set(chapter_ids):
        raise DriverError("final_plan_gate:shared_outline_packets_mismatch")

    return {
        "path": str(path.resolve()),
        "packet_root": str(packet_root.resolve()),
        "chapter_ids": chapter_ids,
        "plan": plan,
        "planning_context": _planning_context(plan),
        "manuscript_parts_plan": parts,
        "packet_paths": {key: str(value) for key, value in packet_paths.items()},
    }


def _paths(args: argparse.Namespace) -> dict[str, Path]:
    root = Path(args.output_root).resolve()
    return {
        "root": root,
        "arrangement": Path(args.arrangement_root).resolve() if args.arrangement_root else root / "arrangement",
        "writer": Path(args.writer_root).resolve() if args.writer_root else root / "writer",
        "batch": Path(args.batch_root).resolve() if args.batch_root else root / "batch",
        "assembly": Path(args.assembly_root).resolve() if args.assembly_root else root / "assembly",
    }


def _base_environment(args: argparse.Namespace) -> dict[str, str]:
    env = dict(os.environ)
    env.setdefault("PYTHONUTF8", "1")
    return env


def _run_command(command: list[str], *, args: argparse.Namespace, label: str) -> dict[str, Any]:
    result = subprocess.run(
        command,
        cwd=REPO_ROOT,
        env=_base_environment(args),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    record = {
        "label": label,
        "command": command,
        "returncode": result.returncode,
        "stdout_tail": result.stdout[-4000:],
        "stderr_tail": result.stderr[-4000:],
    }
    if result.returncode:
        raise DriverError(f"subprocess_failed:{label}:returncode={result.returncode}")
    return record


def _common_arrangement_args(args: argparse.Namespace, info: Mapping[str, Any], paths: Mapping[str, Path]) -> list[str]:
    return [
        sys.executable, str(ARRANGEMENT_CLI),
        "--all-chapters", "--packet-root", str(Path(info["path"]).parent),
        "--output-root", str(paths["arrangement"]),
        "--model", args.arrangement_model,
        "--key-file", str(Path(args.key_file).resolve()),
        "--budget-ledger", str(Path(args.ledger).resolve()),
        "--round-cap-cny", str(args.round_cap_cny),
        "--output-tokens", "18000",
        "--planning-revision",
        "--no-retry",
    ]


def _load_arrangements(info: Mapping[str, Any], arrangement_root: Path) -> dict[tuple[str, str], dict[str, Any]]:
    arrangements: dict[tuple[str, str], dict[str, Any]] = {}
    for chapter_id in info["chapter_ids"]:
        path = arrangement_root / chapter_id / "CHAPTER_ARRANGEMENT.json"
        arrangement = _read_json(path)
        if not isinstance(arrangement, dict) or arrangement.get("chapter_id") != chapter_id:
            raise DriverError(f"arrangement_identity_invalid:{chapter_id}:{path}")
        validation = arrangement.get("validation")
        if (not isinstance(validation, Mapping)
                or validation.get("ok") is not True
                or validation.get("status") != "arranged"):
            status = validation.get("status") if isinstance(validation, Mapping) else "missing"
            raise DriverError(f"arrangement_not_ready:{chapter_id}:{status}")
        if not isinstance(arrangement.get("source_catalog"), Mapping) or not arrangement["source_catalog"]:
            raise DriverError(f"arrangement_source_catalog_missing:{chapter_id}")
        units = arrangement.get("units")
        if not isinstance(units, list) or not units:
            raise DriverError(f"arrangement_units_missing:{chapter_id}")
        view_path = arrangement_root / chapter_id / "ARRANGEMENT_INPUT.json"
        if not view_path.is_file():
            raise DriverError(f"arrangement_input_missing:{chapter_id}:{view_path}")
        unit_ids: set[str] = set()
        for unit in units:
            unit_id = str(unit.get("unit_id") or "").strip() if isinstance(unit, Mapping) else ""
            if not unit_id or unit_id in unit_ids:
                raise DriverError(f"arrangement_unit_id_invalid:{chapter_id}:{unit_id}")
            unit_ids.add(unit_id)
            arrangements[(chapter_id, unit_id)] = {
                "chapter_id": chapter_id,
                "unit_id": unit_id,
                "arrangement": path,
                "view": view_path,
                "output": None,
            }
    return arrangements


def _verify_arrangement_run(info: Mapping[str, Any], arrangement_root: Path) -> dict[str, Any]:
    """Reject a zero-exit arrangement run with failed chapter entries.

    The arrangement CLI records per-chapter failures and still returns zero so
    that its standalone report can retain earlier chapters.  The BODY driver
    must not mistake an older exported arrangement for a successful new run.
    """

    report_path = arrangement_root / "ARRANGEMENT_RUN.json"
    report = _read_json(report_path)
    rows = report.get("chapters") if isinstance(report, Mapping) else None
    if not isinstance(rows, list):
        raise DriverError(f"arrangement_run_chapters_missing:{report_path}")
    expected = list(info["chapter_ids"])
    by_id: dict[str, Mapping[str, Any]] = {}
    for row in rows:
        if isinstance(row, Mapping):
            chapter_id = str(row.get("chapter_id") or "").strip()
            if chapter_id:
                by_id[chapter_id] = row
    if set(by_id) != set(expected) or len(by_id) != len(rows):
        raise DriverError(
            "arrangement_run_chapters_mismatch:"
            + json.dumps({"expected": expected, "reported": sorted(by_id)}, ensure_ascii=False)
        )
    allowed = {"arranged", "reused_arranged"}
    blocked = {
        chapter_id: str(by_id[chapter_id].get("status") or "")
        for chapter_id in expected
        if str(by_id[chapter_id].get("status") or "") not in allowed
    }
    if blocked:
        raise DriverError(
            "arrangement_run_not_ready:" + json.dumps(blocked, ensure_ascii=False, sort_keys=True)
        )
    return dict(report)


def _result_state(result_path: Path, *, chapter_id: str, unit_id: str, arrangement_path: Path) -> tuple[bool, str]:
    if not result_path.is_file():
        return False, "missing"
    try:
        result = _read_json(result_path)
    except DriverError as exc:
        return False, str(exc)
    if not isinstance(result, dict):
        return False, "not_object"
    if result.get("chapter_id") != chapter_id or result.get("unit_id") != unit_id:
        return False, "identity_mismatch"
    if result.get("simulated") is True or str(result.get("mode") or "").casefold() == "fake":
        return False, "simulated_result"
    recorded_arrangement = str(result.get("arrangement_path") or "").strip()
    if recorded_arrangement and Path(recorded_arrangement).resolve() != arrangement_path.resolve():
        return False, "arrangement_mismatch"
    if result.get("complete") is not True:
        return False, "incomplete"
    if result.get("issues") or result.get("unknown_citations"):
        return False, "writer_issues_or_unknown_citations"
    input_path = str(result.get("input_path") or "").strip()
    if input_path:
        input_file = Path(input_path)
        if not input_file.is_absolute():
            input_file = result_path.parent / input_file
        if not input_file.is_file():
            return False, "input_missing"
    body_path = str(result.get("body_path") or "").strip()
    body = Path(body_path) if body_path else result_path.parent / "UNIT_BODY.md"
    if not body.is_absolute():
        body = result_path.parent / body
    if not body.is_file() or not body.read_text(encoding="utf-8", errors="replace").strip():
        return False, "body_missing"
    return True, "complete"


def _writer_command(
    args: argparse.Namespace,
    paths: Mapping[str, Path],
    item: Mapping[str, Any],
    packet_root: Path,
) -> list[str]:
    return [
        sys.executable, str(WRITER_CLI),
        "--arrangement", str(item["arrangement"]),
        "--unit", str(item["unit_id"]),
        "--view", str(item["view"]),
        "--packet-root", str(packet_root.resolve()),
        "--output-root", str(paths["writer"]),
        "--model", args.writer_model,
        "--output-tokens", str(args.writer_output_tokens),
        "--thinking-budget", str(args.writer_thinking_budget),
        "--key-file", str(Path(args.key_file).resolve()),
        "--budget-ledger", str(Path(args.ledger).resolve()),
        "--global-budget-cny", str(args.global_budget_cny),
        "--planning-revision",
        "--run",
    ]


def _manifest(info: Mapping[str, Any], paths: Mapping[str, Path], arrangements: Mapping[tuple[str, str], Mapping[str, Any]]) -> tuple[Path, Path]:
    batch_root = paths["batch"]
    manifest_path = batch_root / "MANIFEST.json"
    jobs: list[dict[str, Any]] = []
    chapters: list[dict[str, Any]] = []
    plan_chapters = info["plan"].get("chapters")
    if not isinstance(plan_chapters, list):
        raise DriverError("chapter_titles_missing_from_final_plan")
    title_by_id: dict[str, str] = {}
    for row in plan_chapters:
        if not isinstance(row, Mapping):
            continue
        nested = row.get("chapter")
        chapter = nested if isinstance(nested, Mapping) else row
        chapter_id = str(chapter.get("chapter_id") or row.get("chapter_id") or "").strip()
        title = str(chapter.get("title") or row.get("title") or "").strip()
        if chapter_id and title:
            title_by_id[chapter_id] = title
    arrangement_by_chapter: dict[str, Path] = {}
    for chapter_id in info["chapter_ids"]:
        title = title_by_id.get(chapter_id)
        if not title:
            raise DriverError(f"chapter_title_missing_from_final_plan:{chapter_id}")
        source_path = paths["arrangement"] / chapter_id / "CHAPTER_ARRANGEMENT.json"
        if not source_path.is_file():
            raise DriverError(f"arrangement_missing_for_manifest:{chapter_id}:{source_path}")
        source = _read_json(source_path)
        if not isinstance(source, Mapping):
            raise DriverError(f"arrangement_invalid_for_manifest:{chapter_id}:{source_path}")
        # The checked-in assembler consumes arrangement.title; keep the shared
        # BODY arrangement and Flash products untouched by materializing a
        # title-enriched copy inside this task-local Plus batch.
        arrangement_path = batch_root / "arrangements" / chapter_id / "CHAPTER_ARRANGEMENT.json"
        enriched = dict(source)
        enriched["title"] = title
        _write_json(arrangement_path, enriched)
        arrangement_by_chapter[chapter_id] = arrangement_path
        chapters.append({
            "chapter_id": chapter_id,
            "title": title,
            "arrangement_path": str(arrangement_path.resolve()),
        })
    for (chapter_id, unit_id), item in arrangements.items():
        output = paths["writer"] / f"{chapter_id}_{unit_id}"
        jobs.append({
            "chapter_id": chapter_id,
            "unit_id": unit_id,
            "arrangement": str(arrangement_by_chapter[chapter_id].resolve()),
            "output": str(output.resolve()),
        })
    manifest = {
        "schema_version": "optomind.manuscript_parts.body_batch.v1",
        "review_title": info["plan"].get("review_title") or info["plan"].get("research_question") or "",
        "planning_status": "complete",
        "planning_result_path": str(Path(info["path"]).resolve()),
        "planning_context": info["planning_context"],
        "manuscript_parts_plan": info["manuscript_parts_plan"],
        "chapters": chapters,
    }
    _write_json(manifest_path, manifest)
    _write_json(batch_root / "BATCH_JOBS.json", jobs)
    return manifest_path, batch_root / "BATCH_JOBS.json"


def _require_live(args: argparse.Namespace) -> None:
    if not args.allow_paid:
        raise DriverError("live_stage_requires_--allow-paid")
    key = Path(args.key_file).resolve()
    if not key.is_file():
        raise DriverError("api_key_missing:" + str(key))
    tokenizer = Path(args.tokenizer).resolve()
    if not tokenizer.is_file():
        raise DriverError("tokenizer_missing:" + str(tokenizer))
    ledger = Path(args.ledger).resolve()
    if not ledger.is_file():
        raise DriverError("budget_ledger_missing:" + str(ledger))
    if args.round_cap_cny > 10.0:
        raise DriverError(f"round_cap_above_authorised_ceiling:{args.round_cap_cny}")


def _stage_preview(args: argparse.Namespace, info: Mapping[str, Any], paths: Mapping[str, Path]) -> list[dict[str, Any]]:
    # Preview writes only the current arrangement inputs and estimates.  Do
    # not inspect an older CHAPTER_ARRANGEMENT.json and present its units as
    # products of this preview.
    return [_run_command(
        _common_arrangement_args(args, info, paths), args=args, label="arrangement-preview")]


def _stage_arrange(args: argparse.Namespace, info: Mapping[str, Any], paths: Mapping[str, Path]) -> list[dict[str, Any]]:
    _require_live(args)
    record = _run_command(
        _common_arrangement_args(args, info, paths) + ["--run"],
        args=args, label="arrangement-live")
    _verify_arrangement_run(info, paths["arrangement"])
    _load_arrangements(info, paths["arrangement"])
    return [record]


def _stage_write(args: argparse.Namespace, info: Mapping[str, Any], paths: Mapping[str, Path]) -> list[dict[str, Any]]:
    if not args.dry_run:
        _require_live(args)
    arrangements = _load_arrangements(info, paths["arrangement"])
    pending: list[tuple[dict[str, Any], Path]] = []
    for item in arrangements.values():
        output = paths["writer"] / f"{item['chapter_id']}_{item['unit_id']}"
        result_path = output / "UNIT_RESULT.json"
        item["output"] = output
        valid, reason = _result_state(result_path, chapter_id=item["chapter_id"], unit_id=item["unit_id"], arrangement_path=item["arrangement"])
        if result_path.is_file() and not valid:
            raise DriverError(f"existing_writer_result_not_reusable:{item['chapter_id']}:{item['unit_id']}:{reason}")
        if not valid:
            pending.append((item, result_path))

    if args.dry_run:
        return [
            {
                "label": f"writer-dry-run:{item['chapter_id']}:{item['unit_id']}",
                "command": _writer_command(args, paths, item, Path(info["packet_root"])),
                "returncode": None,
                "dry_run": True,
            }
            for item, _ in pending
        ]

    records: list[dict[str, Any]] = []
    def run_one(pair: tuple[dict[str, Any], Path]) -> dict[str, Any]:
        item, _ = pair
        return _run_command(
            _writer_command(args, paths, item, Path(info["packet_root"])),
            args=args,
            label=f"writer-live:{item['chapter_id']}:{item['unit_id']}",
        )

    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, min(args.max_writers, 3))) as pool:
        futures = [pool.submit(run_one, pair) for pair in pending]
        for future in futures:
            records.append(future.result())
    for item in arrangements.values():
        result_path = paths["writer"] / f"{item['chapter_id']}_{item['unit_id']}" / "UNIT_RESULT.json"
        valid, reason = _result_state(result_path, chapter_id=item["chapter_id"], unit_id=item["unit_id"], arrangement_path=item["arrangement"])
        if not valid:
            raise DriverError(f"writer_result_not_ready:{item['chapter_id']}:{item['unit_id']}:{reason}")
    return records


def _stage_assemble(args: argparse.Namespace, info: Mapping[str, Any], paths: Mapping[str, Path]) -> list[dict[str, Any]]:
    arrangements = _load_arrangements(info, paths["arrangement"])
    for item in arrangements.values():
        result_path = paths["writer"] / f"{item['chapter_id']}_{item['unit_id']}" / "UNIT_RESULT.json"
        valid, reason = _result_state(result_path, chapter_id=item["chapter_id"], unit_id=item["unit_id"], arrangement_path=item["arrangement"])
        if not valid:
            raise DriverError(f"assembly_writer_gate:{item['chapter_id']}:{item['unit_id']}:{reason}")
    manifest_path, _ = _manifest(info, paths, arrangements)
    check = _run_command([
        sys.executable, str(ASSEMBLER_CLI),
        "--manifest", str(manifest_path),
        "--batch-root", str(paths["batch"]),
        "--output-root", str(paths["assembly"]),
        "--check-only",
    ], args=args, label="assembler-check-only")
    records = [check]
    if args.write_assembly:
        records.append(_run_command([
            sys.executable, str(ASSEMBLER_CLI),
            "--manifest", str(manifest_path),
            "--batch-root", str(paths["batch"]),
            "--output-root", str(paths["assembly"]),
        ], args=args, label="assembler-write"))
    return records


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the frozen-v2 BODY acceptance stages offline or with explicit live opt-in.")
    parser.add_argument("stage", choices=("preview", "arrange", "write", "assemble"))
    parser.add_argument("--plan-root", default=str(DEFAULT_PLAN_ROOT), help="final-plan directory or DETAILED_REVIEW_PLAN.json")
    parser.add_argument("--output-root", default=str(DEFAULT_OUTPUT_ROOT))
    parser.add_argument("--arrangement-root", default="")
    parser.add_argument("--writer-root", default="")
    parser.add_argument("--batch-root", default="")
    parser.add_argument("--assembly-root", default="")
    parser.add_argument("--ledger", default=str(DEFAULT_LEDGER))
    parser.add_argument("--key-file", default=str(DEFAULT_KEY_FILE))
    parser.add_argument("--tokenizer", default=str(DEFAULT_TOKENIZER))
    parser.add_argument("--arrangement-model", default="qwen3.5-plus")
    # Keep the original acceptance driver default for the completed Flash
    # comparison artifact; the restore launcher passes the historical Plus
    # writer settings explicitly.
    parser.add_argument("--writer-model", default="qwen3.7-flash")
    parser.add_argument("--writer-output-tokens", type=int, default=10000)
    parser.add_argument("--writer-thinking-budget", type=int, default=0)
    parser.add_argument("--round-cap-cny", type=float, default=10.0)
    parser.add_argument("--global-budget-cny", type=float, default=100.0,
                        help="writer CLI ledger cap; keep aligned with the shared 100 CNY ledger")
    parser.add_argument("--max-writers", type=int, default=3)
    parser.add_argument("--allow-paid", action="store_true", help="required for arrange/write; root-controlled live execution")
    parser.add_argument("--dry-run", action="store_true", help="write stage: print the exact writer commands without model calls")
    parser.add_argument("--write-assembly", action="store_true", help="after assembler check-only, write the assembled draft")
    return parser


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    args = _parser().parse_args(argv)
    paths = _paths(args)
    report: dict[str, Any] = {
        "schema_version": "optomind.manuscript_parts.body_driver.run.v1",
        "stage": args.stage,
        "status": "failed",
        "paths": {key: str(value) for key, value in paths.items()},
        "ledger": str(Path(args.ledger).resolve()),
        "tokenizer": str(Path(args.tokenizer).resolve()),
        "model_routes": {"arrangement": args.arrangement_model, "writer": args.writer_model},
        "writer_settings": {
            "output_tokens": args.writer_output_tokens,
            "thinking_budget": args.writer_thinking_budget,
            "max_writers": args.max_writers,
        },
        "dry_run": bool(args.dry_run),
        "commands": [],
    }
    report_path = paths["root"] / "RUN_BODY_REPORT.json"
    try:
        if args.max_writers < 1 or args.max_writers > 3:
            raise DriverError("max_writers_must_be_between_1_and_3")
        if args.dry_run and args.stage != "write":
            raise DriverError("dry_run_only_supported_for_write_stage")
        if args.writer_output_tokens < 1 or args.writer_thinking_budget < 0:
            raise DriverError("writer_generation_settings_invalid")
        plan_path = _plan_path(Path(args.plan_root))
        info = _validate_plan(plan_path)
        report["plan"] = {
            "path": info["path"],
            "chapter_ids": info["chapter_ids"],
            "planning_revision": info["plan"].get("manuscript_parts_plan_revision"),
            "frozen": info["plan"].get("manuscript_parts_plan_frozen"),
        }
        report["commands"] = {
            "records": (_stage_preview(args, info, paths) if args.stage == "preview" else
                        _stage_arrange(args, info, paths) if args.stage == "arrange" else
                        _stage_write(args, info, paths) if args.stage == "write" else
                        _stage_assemble(args, info, paths))
        }
        report["status"] = "complete"
        _write_json(report_path, report)
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0
    except (DriverError, OSError, subprocess.SubprocessError) as exc:
        report["error"] = f"{type(exc).__name__}:{exc}"
        _write_json(report_path, report)
        print(json.dumps(report, ensure_ascii=False, indent=2), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
