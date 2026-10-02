from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

RUN = Path(__file__).resolve().parent
WORKTREE = Path(r"F:\OptoMind-Review-2\outputs\serial_parts_preflight_20261002\worktree")
if str(WORKTREE) not in sys.path:
    sys.path.insert(0, str(WORKTREE))

from optomind_research.runtime.upgrade3.module4.runtime import GlobalBudgetLedger, QwenDirectClient
from optomind_research.runtime.upgrade3.serial_manuscript_parts import (
    STAGE_ORDER,
    build_stage_messages,
    messages_sha256,
    normalize_context,
    parse_stage_response,
)
from optomind_research.runtime.upgrade3.serial_parts_application import extract_body

KEY_FILE = Path(r"F:\OptoMind-Review-2\api_keys\qwen-api-key.txt")
MODEL = "qwen3.5-plus"
OUTPUT_TOKENS = 12000
THINKING_BUDGET = 4000


def write_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")


def read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def load_inputs():
    body_input = (RUN / "BODY_INPUT.md").read_text(encoding="utf-8")
    context = read_json(RUN / "CONTEXT.json")
    body = extract_body(body_input, [])
    normalized = normalize_context(context, research_question=context["research_question"], body_text=body_input, chapter_roles=[])
    return body, context, normalized


def update_state(**updates):
    path = RUN / "RUN_STATE.json"
    state = read_json(path) if path.exists() else {}
    state.update(updates)
    write_json(path, state)
    return state


def quality_record(stage: str, parsed, messages, body: str, normalized):
    checks = {
        "response_schema_valid": True,
        "body_sha256_in_prompt_context": __import__("hashlib").sha256(body.encode("utf-8")).hexdigest(),
        "allowed_source_handles": len(normalized["allowed_source_handles"]),
        "message_sha256": messages_sha256(messages),
    }
    if stage == "conception":
        plan = parsed["manuscript_parts_plan"]
        checks.update({
            "parts": sorted(k for k in plan if k != "context"),
            "all_standalone": all(plan[k]["placement"]["mode"] == "standalone" for k in ("abstract", "introduction", "conclusion")),
            "focus_counts": {k: len(plan[k]["focus"]) for k in ("abstract", "introduction", "conclusion")},
            "boundary_counts": {k: len(plan[k]["boundary"]) for k in ("abstract", "introduction", "conclusion")},
        })
    else:
        checks["nonempty_output"] = all(bool(str(v).strip()) for v in parsed.values() if isinstance(v, str))
    return {
        "stage": stage,
        "recorded_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "quality_status": "structural_checks_passed_root_content_review_required",
        "checks": checks,
        "review_prompt": "Root must inspect whether this stage follows the actual BODY, preserves conditions, avoids single-gap domination, and has a distinct duty before authorizing the next stage.",
    }


def run_stage(stage: str) -> int:
    if stage not in STAGE_ORDER:
        raise SystemExit(f"unknown stage: {stage}")
    body, context, normalized = load_inputs()
    prior_plan = None
    prior_parts = {}
    if stage != "conception":
        plan_path = RUN / "MANUSCRIPT_PARTS_PLAN.json"
        if not plan_path.exists():
            raise SystemExit("missing MANUSCRIPT_PARTS_PLAN.json; conception gate not passed")
        prior_plan = read_json(plan_path)
        prior_parts_path = RUN / "GENERATED_PARTS.json"
        if prior_parts_path.exists():
            prior_parts = read_json(prior_parts_path)
    messages = build_stage_messages(
        stage,
        body_text=body,
        research_question=context["research_question"],
        chapter_roles=[],
        context=context,
        manuscript_parts_plan=prior_plan,
        prior_parts=prior_parts,
        language="zh",
        normalized_context=normalized,
    )
    write_json(RUN / "messages" / f"{stage}.json", messages)
    ledger = GlobalBudgetLedger(limit_cny=30.0, path=RUN / "budget.sqlite")
    client = QwenDirectClient(
        model=MODEL,
        key_file=KEY_FILE,
        max_retries=2,
        timeout_seconds=900,
        max_output_tokens=OUTPUT_TOKENS,
        thinking=True,
        thinking_budget=THINKING_BUDGET,
        json_mode=False,
        raw_response_dir=RUN / "raw_responses",
        budget_ledger=ledger,
    )
    call_id = f"serial_parts:{stage}"
    update_state(stage=stage, status="running", pid=__import__("os").getpid(), call_id=call_id, started_at=time.strftime("%Y-%m-%dT%H:%M:%S%z"), model=MODEL, output_tokens=OUTPUT_TOKENS, thinking_budget=THINKING_BUDGET, budget_snapshot=ledger.as_dict())
    try:
        response = client(messages, call_id=call_id, model=MODEL, max_output_tokens=OUTPUT_TOKENS, thinking_budget=THINKING_BUDGET)
        write_json(RUN / "responses" / f"{stage}.json", response)
        parsed = parse_stage_response(stage, response, allowed_source_handles=normalized["allowed_source_handles"])
        if stage == "conception":
            write_json(RUN / "CONCEPTION.json", parsed)
            write_json(RUN / "MANUSCRIPT_PARTS_PLAN.json", parsed["manuscript_parts_plan"])
        else:
            parts = dict(prior_parts)
            parts.update(parsed)
            write_json(RUN / "GENERATED_PARTS.json", parts)
        review = quality_record(stage, parsed, messages, body, normalized)
        write_json(RUN / f"{stage.upper()}_QUALITY_RECORD.json", review)
        state = update_state(stage=stage, status="completed_waiting_root_review", finished_at=time.strftime("%Y-%m-%dT%H:%M:%S%z"), generated=[stage], budget_snapshot=ledger.as_dict(), response_path=str(RUN / "responses" / f"{stage}.json"), quality_record_path=str(RUN / f"{stage.upper()}_QUALITY_RECORD.json"))
        print(json.dumps({"stage":stage,"status":state["status"],"call_id":call_id,"budget":state["budget_snapshot"]},ensure_ascii=False))
        return 0
    except Exception as exc:
        state = update_state(stage=stage, status="failed", finished_at=time.strftime("%Y-%m-%dT%H:%M:%S%z"), error=f"{type(exc).__name__}:{exc}", budget_snapshot=ledger.as_dict())
        print(json.dumps({"stage":stage,"status":"failed","error":state["error"],"budget":state["budget_snapshot"]},ensure_ascii=False))
        return 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--stage", choices=STAGE_ORDER, required=True)
    args = parser.parse_args()
    raise SystemExit(run_stage(args.stage))
