"""CLI for the independent M1-M3 review chapter blueprint planner."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

# Direct script execution does not automatically put the repository root on
# sys.path.  Keep the CLI independently runnable from PowerShell and tests.
if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from optomind_research.runtime.upgrade3.review_blueprint import (
    CONTEXT_LIMIT,
    DEFAULT_MAX_OUTPUT_TOKENS,
    DEFAULT_MAX_SNIPPETS_PER_PAPER,
    DEFAULT_PACKET_BYTES,
    DEFAULT_THINKING_BUDGET,
    MODEL,
    BlueprintError,
    BlueprintValidationError,
    ContextBudgetError,
    PacketSizeError,
    QwenTransportError,
    _conservative_prompt_token_upper_bound,
    _json_bytes,
    _text,
    build_input_packet,
    context_gate,
    estimated_cost_cny,
    offline_blueprint,
    packet_messages,
    plan_blueprint,
    render_blueprint_markdown,
    sha256_value,
)


def _json_write(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".tmp")
    temp.write_text(json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    temp.replace(path)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Build a traceable review chapter taskbook from existing M1-M3 artifacts.")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--preflight", action="store_true", help="build packet and budget/context plan without a model call")
    mode.add_argument("--offline", action="store_true", help="run an explicitly marked offline fake for contract checks")
    mode.add_argument("--run", action="store_true", help="call Qwen3.5Plus thinking directly")
    parser.add_argument("--plan", required=True, type=Path)
    parser.add_argument("--candidate-corpus", required=True, type=Path)
    parser.add_argument("--skeleton", type=Path)
    parser.add_argument("--snippet-store", type=Path)
    parser.add_argument("--user-question-file", type=Path)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--budget-ledger", type=Path)
    parser.add_argument("--qwen-key-file", type=Path)
    parser.add_argument("--budget-cny", type=float)
    parser.add_argument("--max-papers", type=int)
    parser.add_argument("--max-packet-bytes", type=int, default=DEFAULT_PACKET_BYTES)
    parser.add_argument("--max-snippets-per-paper", type=int, default=DEFAULT_MAX_SNIPPETS_PER_PAPER)
    parser.add_argument("--max-output-tokens", type=int, default=DEFAULT_MAX_OUTPUT_TOKENS)
    parser.add_argument("--thinking-budget", type=int, default=DEFAULT_THINKING_BUDGET)
    parser.add_argument("--timeout-seconds", type=float, default=300.0)
    parser.add_argument("--max-retries", type=int, default=1)
    return parser


def _preflight(packet: dict[str, Any], args: argparse.Namespace) -> dict[str, Any]:
    messages = packet_messages(packet, max_output_tokens=args.max_output_tokens, thinking_budget=args.thinking_budget)
    gate = context_gate(messages, max_output_tokens=args.max_output_tokens, thinking_budget=args.thinking_budget)
    body = {
        "model": MODEL,
        "messages": [dict(item) for item in messages],
        "max_tokens": args.max_output_tokens,
        "enable_thinking": True,
        "thinking_budget": args.thinking_budget,
        "stream": False,
    }
    prompt_upper = _conservative_prompt_token_upper_bound(_json_bytes(body), messages)
    reserved = estimated_cost_cny(
        {"prompt_tokens": prompt_upper, "completion_tokens": args.max_output_tokens + args.thinking_budget},
        model=MODEL,
        conservative=True,
    )
    return {
        "status": "preflight_only",
        "model": MODEL,
        "network_call": False,
        "packet_sha256": (packet.get("packet_audit") or {}).get("packet_sha256"),
        "packet_bytes": (packet.get("packet_audit") or {}).get("packet_bytes"),
        "context_gate": gate,
        "prompt_sha256": sha256_value(messages),
        "estimated_reserved_cost_cny": reserved,
        "budget_ledger": str((args.budget_ledger or (args.output_dir / "budget.sqlite")).resolve()),
        "config": {
            "max_output_tokens": args.max_output_tokens,
            "thinking_budget": args.thinking_budget,
            "json_mode": False,
            "context_limit": CONTEXT_LIMIT,
        },
    }


def _error_record(exc: BaseException, packet: dict[str, Any], args: argparse.Namespace) -> dict[str, Any]:
    record = {
        "status": "failed_before_or_during_model_call",
        "error_type": type(exc).__name__,
        "error": str(exc),
        "model": MODEL if args.run else None,
        "input_sha256": (packet.get("packet_audit") or {}).get("packet_sha256"),
        "config": {
            "max_output_tokens": args.max_output_tokens,
            "thinking_budget": args.thinking_budget,
            "json_mode": False,
            "network_call": bool(args.run),
        },
    }
    telemetry = getattr(exc, "telemetry", None)
    if isinstance(telemetry, dict):
        record["received_telemetry"] = telemetry
    if isinstance(exc, BlueprintValidationError):
        record["validation_issues"] = exc.issues
    return record


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if not (args.preflight or args.offline or args.run):
        args.preflight = True
    if args.run and not args.qwen_key_file:
        print("--run requires --qwen-key-file", file=sys.stderr)
        return 2
    if args.run and args.budget_cny is None:
        print("--run requires explicit --budget-cny", file=sys.stderr)
        return 2
    if args.output_dir.exists():
        protected = [
            args.output_dir / "REVIEW_BLUEPRINT.json",
            args.output_dir / "REVIEW_BLUEPRINT_TELEMETRY.json",
            args.output_dir / "raw_responses",
        ]
        if any(path.exists() for path in protected):
            print("refuses an output directory containing prior model output or raw responses; use a new directory", file=sys.stderr)
            return 2
    try:
        packet = build_input_packet(
            args.plan,
            args.candidate_corpus,
            skeleton_path=args.skeleton,
            snippet_store_path=args.snippet_store,
            user_question_file=args.user_question_file,
            max_papers=args.max_papers,
            max_packet_bytes=args.max_packet_bytes,
            max_snippets_per_paper=args.max_snippets_per_paper,
        )
        args.output_dir.mkdir(parents=True, exist_ok=True)
        _json_write(args.output_dir / "INPUT_PACKET.json", packet)
        _json_write(args.output_dir / "PROMPT_MESSAGES.json", packet_messages(
            packet, max_output_tokens=args.max_output_tokens, thinking_budget=args.thinking_budget
        ))
        if args.preflight:
            receipt = _preflight(packet, args)
            _json_write(args.output_dir / "PREFLIGHT.json", receipt)
            print(json.dumps(receipt, ensure_ascii=False, sort_keys=True, indent=2))
            return 0
        if args.offline:
            result, telemetry = offline_blueprint(packet)
        else:
            ledger_path = args.budget_ledger or (args.output_dir / "budget.sqlite")
            result, telemetry = plan_blueprint(
                packet,
                key_file=args.qwen_key_file,
                budget_ledger_path=ledger_path,
                raw_response_dir=args.output_dir / "raw_responses",
                budget_cny=args.budget_cny,
                max_output_tokens=args.max_output_tokens,
                thinking_budget=args.thinking_budget,
                timeout_seconds=args.timeout_seconds,
                max_retries=args.max_retries,
            )
        _json_write(args.output_dir / "REVIEW_BLUEPRINT.json", result)
        _json_write(args.output_dir / "REVIEW_BLUEPRINT_TELEMETRY.json", telemetry)
        (args.output_dir / "REVIEW_BLUEPRINT.md").write_text(
            render_blueprint_markdown(result, validation=telemetry.get("validation") or {}, telemetry=telemetry),
            encoding="utf-8",
        )
        print(json.dumps({"status": telemetry.get("status"), "output_dir": str(args.output_dir.resolve()), "telemetry": telemetry}, ensure_ascii=False, sort_keys=True, indent=2))
        return 0
    except (BlueprintError, BlueprintValidationError, ContextBudgetError, PacketSizeError, QwenTransportError, OSError) as exc:
        try:
            args.output_dir.mkdir(parents=True, exist_ok=True)
            _json_write(args.output_dir / "FAILURE.json", _error_record(exc, packet if "packet" in locals() else {}, args))
        except OSError:
            pass
        print(f"review_blueprint failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
