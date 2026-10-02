"""Reusable staged post-BODY runner.

Preview is the default.  ``--mode live`` is explicit and runs the production
``run_serial_parts`` path one attempt at a time.  Responses are cached by the
exact messages hash plus the actual model/output parameters; a later attempt
can therefore stop after review and resume only the missing or changed stages.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Callable, Mapping

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from optomind_research.runtime.upgrade3.module4.runtime import GlobalBudgetLedger, QwenDirectClient
from optomind_research.runtime.upgrade3.serial_manuscript_parts import (
    STAGE_ORDER,
    build_stage_messages,
    messages_sha256,
    normalize_context,
    run_serial_parts,
)
from optomind_research.runtime.upgrade3.serial_parts_application import extract_body

DEFAULT_MODEL = "qwen3.5-plus"
DEFAULT_OUTPUT_TOKENS = 12000
DEFAULT_THINKING_BUDGET = 4000


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")


def model_params(*, model: str, output_tokens: int, thinking_budget: int, timeout_seconds: float, max_retries: int) -> dict[str, Any]:
    return {
        "model": model,
        "max_output_tokens": output_tokens,
        "thinking": True,
        "thinking_budget": thinking_budget,
        "json_mode": False,
        "timeout_seconds": timeout_seconds,
        "max_retries": max_retries,
    }


def next_attempt(run_root: Path) -> tuple[str, Path]:
    attempts = run_root / "attempts"
    attempts.mkdir(parents=True, exist_ok=True)
    numbers = []
    for path in attempts.glob("attempt-*"):
        try:
            numbers.append(int(path.name.rsplit("-", 1)[1]))
        except (ValueError, IndexError):
            continue
    attempt_id = f"attempt-{max(numbers, default=0) + 1:03d}"
    path = attempts / attempt_id
    path.mkdir()
    return attempt_id, path


class ResponseCacheProvider:
    """Cache only exact request/model pairs; persist before parser validation."""

    def __init__(self, *, run_root: Path, attempt_id: str, params: Mapping[str, Any],
                 reuse_cache: bool, client: Callable[[str, list[dict[str, str]], str], Any] | None):
        self.run_root = run_root
        self.attempt_id = attempt_id
        self.params = dict(params)
        self.reuse_cache = reuse_cache
        self.client = client
        self.events: list[dict[str, Any]] = []
        self.run_name = run_root.name or "run"

    def _key(self, stage: str, messages: list[dict[str, str]]) -> tuple[str, str]:
        message_hash = messages_sha256(messages)
        key_payload = json.dumps({"stage": stage, "messages_sha256": message_hash, "model_params": self.params}, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return message_hash, hashlib.sha256(key_payload.encode("utf-8")).hexdigest()

    def __call__(self, stage: str, messages: list[dict[str, str]]) -> Any:
        message_hash, cache_key = self._key(stage, messages)
        cache_path = self.run_root / "cache" / stage / f"{cache_key}.json"
        if self.reuse_cache and cache_path.exists():
            record = read_json(cache_path)
            if record.get("messages_sha256") == message_hash and record.get("model_params") == self.params:
                self.events.append({"stage": stage, "action": "reused", "messages_sha256": message_hash, "cache": str(cache_path)})
                return record["response"]
        if self.client is None:
            raise RuntimeError("live_client_not_configured")
        # Include the run name because one budget ledger can serve several
        # independent experiments whose attempt numbers both start at 001.
        call_id = f"serial_parts:{self.run_name}:{self.attempt_id}:{stage}"
        response = self.client(stage, messages, call_id)
        # This is deliberately before parse_stage_response: malformed output
        # remains recoverable without charging the same request again.
        record = {
            "stage": stage,
            "call_id": call_id,
            "messages_sha256": message_hash,
            "model_params": self.params,
            "response": response,
            "saved_before_parse": True,
            "saved_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        }
        write_json(cache_path, record)
        write_json(self.run_root / "requests" / f"{self.attempt_id}-{stage}.json", {
            "stage": stage, "call_id": call_id, "messages_sha256": message_hash,
            "model_params": self.params, "messages": messages,
        })
        self.events.append({"stage": stage, "action": "called", "messages_sha256": message_hash, "cache": str(cache_path), "call_id": call_id})
        return response


def _prior_parts_for_preview(stage: str, generated: Mapping[str, Any]) -> dict[str, Any]:
    allowed = {"introduction": ("conclusion",), "abstract": ("conclusion", "introduction")}.get(stage, ())
    return {key: generated[key] for key in allowed if key in generated}


def _context_roles(context: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    roles = context.get("chapter_roles", [])
    return list(roles) if isinstance(roles, list) else []


def _context_without_roles(context: Mapping[str, Any]) -> dict[str, Any]:
    """Keep chapter roles on the explicit runtime argument, not prompt context."""
    return {key: value for key, value in context.items() if key != "chapter_roles"}


def preview(*, draft: Path, context_path: Path, out_dir: Path, stage: str,
            prior_parts_mode: str, language: str = "zh") -> dict[str, Any]:
    manuscript = draft.read_bytes().decode("utf-8")
    context = read_json(context_path)
    chapter_roles = _context_roles(context)
    prompt_context = _context_without_roles(context)
    body = extract_body(manuscript, chapter_roles)
    normalized = normalize_context(prompt_context, research_question=context["research_question"], body_text=body, chapter_roles=chapter_roles)
    plan = None
    generated: Mapping[str, Any] = {}
    if stage != "conception":
        plan = read_json(out_dir / "MANUSCRIPT_PARTS_PLAN.json")
        generated = read_json(out_dir / "GENERATED_PARTS.json") if (out_dir / "GENERATED_PARTS.json").exists() else {}
    messages = build_stage_messages(stage, body_text=body, research_question=context["research_question"],
        chapter_roles=chapter_roles, context=prompt_context, manuscript_parts_plan=plan,
        prior_parts=_prior_parts_for_preview(stage, generated), language=language,
        normalized_context=normalized, include_prior_parts=(prior_parts_mode == "full"))
    result = {"status": "preview", "stage": stage, "prior_parts_mode": prior_parts_mode,
              "messages_sha256": messages_sha256(messages),
              "body_sha256": hashlib.sha256(body.encode("utf-8")).hexdigest(),
              "provider_calls": 0}
    write_json(out_dir / "PREVIEW" / f"{stage}-{prior_parts_mode}.json", result)
    write_json(out_dir / "PREVIEW" / f"{stage}-{prior_parts_mode}.messages.json", messages)
    return result


def execute_live(*, draft: Path, context_path: Path, run_root: Path, stop_after_stage: str | None,
                 prior_parts_mode: str, model: str, output_tokens: int, thinking_budget: int,
                 timeout_seconds: float, max_retries: int, key_file: Path | None,
                 ledger: Path | None, ledger_limit_cny: float, reuse_cache: bool,
                 language: str = "zh",
                 client_factory: Callable[[str, list[dict[str, str]], str], Any] | None = None) -> dict[str, Any]:
    if key_file is None or ledger is None:
        if client_factory is None:
            raise RuntimeError("live_requires_key_file_and_ledger")
    run_root.mkdir(parents=True, exist_ok=True)
    attempt_id, attempt_dir = next_attempt(run_root)
    params = model_params(model=model, output_tokens=output_tokens, thinking_budget=thinking_budget,
                          timeout_seconds=timeout_seconds, max_retries=max_retries)
    if client_factory is None:
        budget = GlobalBudgetLedger(limit_cny=ledger_limit_cny, path=ledger)
        client = QwenDirectClient(model=model, key_file=key_file, max_retries=max_retries,
            timeout_seconds=timeout_seconds, max_output_tokens=output_tokens, thinking=True,
            thinking_budget=thinking_budget, json_mode=False,
            raw_response_dir=run_root / "raw_responses", budget_ledger=budget)
        def invoke(stage: str, messages: list[dict[str, str]], call_id: str) -> Any:
            return client(messages, call_id=call_id, model=model,
                          max_output_tokens=output_tokens, thinking_budget=thinking_budget)
        client_fn = invoke
    else:
        client_fn = client_factory
    cache = ResponseCacheProvider(run_root=run_root, attempt_id=attempt_id, params=params,
                                  reuse_cache=reuse_cache, client=client_fn)
    context = read_json(context_path)
    prompt_context = _context_without_roles(context)
    report = run_serial_parts(draft_path=draft, research_question=context["research_question"],
        chapter_roles=_context_roles(context), out_dir=attempt_dir, context=prompt_context, language=language,
        provider=cache, prior_parts_mode=prior_parts_mode, stop_after_stage=stop_after_stage)
    report["actual_model_calls"] = len([e for e in cache.events if e["action"] == "called"])
    report["cache_reuses"] = len([e for e in cache.events if e["action"] == "reused"])
    write_json(attempt_dir / "FRONT_BACK_REPORT.json", report)
    write_json(attempt_dir / "RUN_EVENTS.json", {"attempt_id": attempt_id, "cache_events": cache.events,
        "model_params": params, "reuse_cache": reuse_cache, "manual_quality_status": "pending_review"})
    write_json(run_root / "RUN_STATE.json", {"attempt_id": attempt_id, "attempt_dir": str(attempt_dir),
        "status": report["status"], "generated": report["generated"], "stop_after_stage": stop_after_stage,
        "prior_parts_mode": prior_parts_mode, "cache_events": cache.events,
        "manual_quality_status": "pending_review", "provider_calls": len([e for e in cache.events if e["action"] == "called"])})
    return report


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=("preview", "live"), default="preview")
    parser.add_argument("--stage", choices=STAGE_ORDER)
    parser.add_argument("--stop-after-stage", choices=STAGE_ORDER)
    parser.add_argument("--draft", type=Path, required=True)
    parser.add_argument("--context", type=Path, required=True)
    parser.add_argument("--out-dir", type=Path, required=True)
    parser.add_argument("--prior-parts-mode", choices=("full", "none"), default="full")
    parser.add_argument("--language", default="zh")
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--output-tokens", type=int, default=DEFAULT_OUTPUT_TOKENS)
    parser.add_argument("--thinking-budget", type=int, default=DEFAULT_THINKING_BUDGET)
    parser.add_argument("--timeout-seconds", type=float, default=900)
    parser.add_argument("--max-retries", type=int, default=2)
    parser.add_argument("--key-file", type=Path)
    parser.add_argument("--ledger", type=Path)
    parser.add_argument("--ledger-limit-cny", type=float, default=30.0)
    parser.add_argument("--no-reuse-cache", action="store_true")
    args = parser.parse_args(argv)
    if args.mode == "preview" and not args.stage:
        parser.error("preview requires --stage")
    if args.mode == "live" and args.stop_after_stage is None and args.stage is not None:
        parser.error("live uses --stop-after-stage or full run; --stage is preview-only")
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.mode == "preview":
        result = preview(draft=args.draft, context_path=args.context, out_dir=args.out_dir,
                         stage=args.stage, prior_parts_mode=args.prior_parts_mode, language=args.language)
    else:
        result = execute_live(draft=args.draft, context_path=args.context, run_root=args.out_dir,
            stop_after_stage=args.stop_after_stage, prior_parts_mode=args.prior_parts_mode,
            model=args.model, output_tokens=args.output_tokens, thinking_budget=args.thinking_budget,
            timeout_seconds=args.timeout_seconds, max_retries=args.max_retries, key_file=args.key_file,
            ledger=args.ledger, ledger_limit_cny=args.ledger_limit_cny, reuse_cache=not args.no_reuse_cache,
            language=args.language)
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result.get("status") in {"preview", "generated", "awaiting_review"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
