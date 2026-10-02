"""Small isolated experiments for a single neutral opening component.

This module does not alter the production four-stage prompt or BODY.  It reads
one already fixed introduction request, makes a prompt-only neutral copy, and
can optionally send exactly one explicit qwen3.5-plus request.  The model sees
``opening_text``; the local response adapter restores the production
``introduction`` key only after the raw response is persisted.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import time
from pathlib import Path
from typing import Any, Mapping

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from optomind_research.runtime.upgrade3.module4.runtime import GlobalBudgetLedger, QwenDirectClient
from optomind_research.runtime.upgrade3 import serial_manuscript_parts as serial
from optomind_research.runtime.upgrade3.serial_parts_application import apply_serial_parts, extract_body


TARGET_RE = re.compile(r"引言|introduction", flags=re.IGNORECASE)
DEFAULT_MODEL = "qwen3.5-plus"
DEFAULT_OUTPUT_TOKENS = 12000
DEFAULT_THINKING_BUDGET = 4000

OPENING_TASK = """为尚未阅读所附正文、但具备邻近领域基础的读者写一段可直接放在文章开头的连贯学术文字，段落数按表达需要。让读者理解这个问题为何重要、已有认识已帮助解决什么，以及哪些具体的理解或实践困难使得把这些研究放在一起讨论有价值。以本文实际采用的组织视角解释这些困难之间的联系，说明这种视角能帮助读者作出什么区分或判断，并准确限定本文讨论范围。按需要解释进入问题所必需的概念，用有依据的事实支撑动机；不要为了制造价值虚构空白、首创或已完成的检索方法。文章组织若需交代，就说明讨论为何这样衔接，而非逐章宣读工作安排；无需预先报完正文的结果和未来议程。内容与措辞以实际正文为准，保留改变含义的条件。不得逐章报目录，也不得把正文发现或研究缺口串成清单式复述。输出JSON仅含opening_text。"""


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")


def _neutral_text(value: str) -> str:
    value = value.replace("引言", "开篇正文")
    return re.sub(r"introduction", "opening_text", value, flags=re.IGNORECASE)


def _neutral_copy(value: Any, *, preserve_body: bool = False, key: str = "") -> Any:
    if preserve_body and key == "actual_body":
        return value
    if isinstance(value, Mapping):
        result = {}
        for raw_key, raw_value in value.items():
            new_key = _neutral_text(raw_key) if isinstance(raw_key, str) else raw_key
            result[new_key] = _neutral_copy(raw_value, preserve_body=preserve_body, key=str(raw_key))
        return result
    if isinstance(value, list):
        return [_neutral_copy(item, preserve_body=preserve_body) for item in value]
    if isinstance(value, str):
        return _neutral_text(value)
    return value


def _target_counts(value: Any) -> dict[str, int]:
    text = json.dumps(value, ensure_ascii=False, sort_keys=True)
    return {"引言": len(re.findall("引言", text)), "introduction": len(re.findall("introduction", text, flags=re.IGNORECASE))}


def load_source_messages(source_attempt: Path) -> tuple[list[dict[str, str]], dict[str, Any]]:
    path = source_attempt / "messages" / "introduction.json"
    messages = read_json(path)
    if not isinstance(messages, list) or len(messages) != 2:
        raise ValueError("source_introduction_messages_must_have_two_messages")
    user = messages[1]
    if not isinstance(user, Mapping) or not isinstance(user.get("content"), str):
        raise ValueError("source_introduction_user_message_missing")
    marker = "【本轮输入】\n"
    end_marker = "\n【本轮任务】\n"
    content = user["content"]
    if marker not in content or end_marker not in content:
        raise ValueError("source_introduction_message_contract_missing")
    payload_text = content.split(marker, 1)[1].split(end_marker, 1)[0]
    payload = json.loads(payload_text)
    if not isinstance(payload, Mapping) or not isinstance(payload.get("actual_body"), str):
        raise ValueError("source_introduction_payload_missing_body")
    return [dict(message) for message in messages], dict(payload)


def build_neutral_messages(source_attempt: Path) -> tuple[list[dict[str, str]], dict[str, Any]]:
    """Build B's messages from the fixed request while preserving actual BODY bytes."""
    original_messages, original_payload = load_source_messages(source_attempt)
    payload = _neutral_copy(original_payload, preserve_body=True)
    # B explicitly has no preceding generated component, even if the source
    # request was copied from a full-mode attempt.
    payload.pop("prior_generated_parts_secondary_not_evidence", None)
    system = _neutral_text(original_messages[0].get("content", ""))
    user = "【本轮输入】\n" + json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n【本轮任务】\n" + OPENING_TASK
    messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
    if TARGET_RE.search(json.dumps(messages, ensure_ascii=False)):
        raise ValueError("neutral_experiment_label_remains; BODY must not be rewritten")
    body = original_payload["actual_body"]
    body_counts = _target_counts(body)
    prompt_counts = _target_counts({"system": system, "user": user.replace(body, "")})
    return messages, {
        "payload": payload,
        "body_sha256": hashlib.sha256(body.encode("utf-8")).hexdigest(),
        "body_target_counts": body_counts,
        "prompt_target_counts_excluding_body": prompt_counts,
        "source_message_target_counts": _target_counts(original_messages),
        "source_attempt": str(source_attempt.resolve()),
    }


def normalize_opening_response(response: Any, *, allowed_source_handles: list[str] | tuple[str, ...] = ()) -> dict[str, str]:
    """Parse neutral output, then restore only the production response key."""
    data = serial._decode_response(response, "introduction")
    if set(data) != {"opening_text"}:
        raise serial.SerialPartsError("response_wrong_keys:opening_text")
    serial._text(data["opening_text"], "opening_text")
    canonical = {"introduction": data["opening_text"]}
    return serial.parse_stage_response("introduction", canonical, allowed_source_handles=allowed_source_handles)


def preview(*, source_attempt: Path, out_dir: Path) -> dict[str, Any]:
    messages, info = build_neutral_messages(source_attempt)
    out_dir.mkdir(parents=True, exist_ok=True)
    write_json(out_dir / "INPUT_PAYLOAD.json", info["payload"])
    write_json(out_dir / "MESSAGES.json", messages)
    result = {
        "status": "preview",
        "mode": "neutral_opening_only",
        "model": DEFAULT_MODEL,
        "messages_sha256": serial.messages_sha256(messages),
        "message_chars": sum(len(item["content"]) for item in messages),
        "body_sha256": info["body_sha256"],
        "body_target_counts": info["body_target_counts"],
        "prompt_target_counts_excluding_body": info["prompt_target_counts_excluding_body"],
        "source_message_target_counts": info["source_message_target_counts"],
        "prior_parts": "none",
        "provider_calls": 0,
        "composition_status": "not_assembled",
        "quality_status": "pending_manual_review",
    }
    write_json(out_dir / "PREVIEW.json", result)
    return result


def live(*, source_attempt: Path, out_dir: Path, key_file: Path, ledger: Path,
         ledger_limit_cny: float = 30.0, timeout_seconds: float = 900.0) -> dict[str, Any]:
    messages, info = build_neutral_messages(source_attempt)
    out_dir.mkdir(parents=True, exist_ok=True)
    write_json(out_dir / "INPUT_PAYLOAD.json", info["payload"])
    write_json(out_dir / "MESSAGES.json", messages)
    params = {"model": DEFAULT_MODEL, "max_output_tokens": DEFAULT_OUTPUT_TOKENS,
              "thinking": True, "thinking_budget": DEFAULT_THINKING_BUDGET,
              "json_mode": False, "timeout_seconds": timeout_seconds,
              "max_retries": 0, "max_keys": 1}
    write_json(out_dir / "MODEL_PARAMS.json", params)
    budget = GlobalBudgetLedger(limit_cny=ledger_limit_cny, path=ledger)
    client = QwenDirectClient(model=DEFAULT_MODEL, key_file=key_file, max_retries=0,
        timeout_seconds=timeout_seconds, max_output_tokens=DEFAULT_OUTPUT_TOKENS,
        thinking=True, thinking_budget=DEFAULT_THINKING_BUDGET, json_mode=False,
        max_keys=1, raw_response_dir=out_dir / "raw_responses", budget_ledger=budget)
    call_id = f"focused_b_neutral_opening:{out_dir.name}:opening_text"
    response = client(messages, call_id=call_id, model=DEFAULT_MODEL,
                      max_output_tokens=DEFAULT_OUTPUT_TOKENS, thinking_budget=DEFAULT_THINKING_BUDGET)
    # Persist the complete client envelope before any parser/schema decision.
    write_json(out_dir / "OPENING_RAW_RESPONSE.json", response)
    allowed = list(info["payload"].get("context", {}).get("allowed_source_handles", []))
    canonical = normalize_opening_response(response, allowed_source_handles=allowed)
    write_json(out_dir / "OPENING_NORMALIZED_RESPONSE.json", canonical)
    source_input = source_attempt / "INPUT.json"
    input_record = read_json(source_input) if source_input.exists() else {}
    source_draft = Path(input_record.get("source_draft") or source_attempt / "INPUT_BODY.md")
    manuscript = source_draft.read_bytes().decode("utf-8")
    roles = info["payload"].get("context", {}).get("chapter_roles", [])
    mixed_text, application_log = apply_serial_parts(manuscript, canonical, roles, language="zh")
    if extract_body(mixed_text, roles) != info["payload"]["actual_body"]:
        raise ValueError("neutral_experiment_body_changed")
    (out_dir / "MIXED_COMPOSITION_MANUSCRIPT.md").write_bytes(mixed_text.encode("utf-8"))
    result = {
        "status": "generated_opening_only",
        "mode": "neutral_opening_only",
        "call_id": call_id,
        "model": DEFAULT_MODEL,
        "body_sha256": info["body_sha256"],
        "provider_calls": 1,
        "body_preserved": True,
        "composition_status": "mixed_composition",
        "new_parts": ["introduction"],
        "other_parts": "unchanged_or_absent; no complete four-stage quality claim",
        "application_log": application_log,
        "quality_status": "pending_manual_review",
    }
    write_json(out_dir / "RESULT.json", result)
    return result


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=("preview", "live"), default="preview")
    parser.add_argument("--source-attempt", type=Path, required=True)
    parser.add_argument("--out-dir", type=Path, required=True)
    parser.add_argument("--key-file", type=Path)
    parser.add_argument("--ledger", type=Path)
    parser.add_argument("--ledger-limit-cny", type=float, default=30.0)
    parser.add_argument("--timeout-seconds", type=float, default=900.0)
    args = parser.parse_args(argv)
    if args.mode == "live" and (args.key_file is None or args.ledger is None):
        parser.error("live requires --key-file and --ledger")
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    result = preview(source_attempt=args.source_attempt, out_dir=args.out_dir) if args.mode == "preview" else live(
        source_attempt=args.source_attempt, out_dir=args.out_dir, key_file=args.key_file,
        ledger=args.ledger, ledger_limit_cny=args.ledger_limit_cny, timeout_seconds=args.timeout_seconds)
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
