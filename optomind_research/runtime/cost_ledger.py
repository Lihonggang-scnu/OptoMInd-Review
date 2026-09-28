"""Token and monetary cost accounting for a Research Harness task.

The billing currency is CNY because OptoMind uses Alibaba Cloud Model Studio.
USD is retained only as a compatibility/display conversion. Prices live in
``config/model_pricing.json`` so model changes do not require code edits.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Dict

from .artifact_store import atomic_write_json

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_PRICING_PATH = _PROJECT_ROOT / "config" / "model_pricing.json"


def load_model_pricing(path: Path | None = None) -> Dict[str, Any]:
    """Load the auditable price table or a conservative built-in fallback."""

    source = path or DEFAULT_PRICING_PATH
    try:
        data = json.loads(source.read_text(encoding="utf-8"))
        if not isinstance(data.get("models"), dict):
            raise ValueError("models must be an object")
        return data
    except Exception:
        # Cost-control failures must over-estimate rather than silently
        # under-estimate an unknown model.
        return {
            "schema_version": "optomind.model_pricing.fallback",
            "currency": "CNY",
            "usd_to_cny": 7.2,
            "pricing_source": "conservative_builtin_fallback",
            "default": {
                "input_cny_per_million": 12.0,
                "output_cny_per_million": 54.0,
            },
            "models": {},
        }


def _rate_for_call(
    pricing: Dict[str, Any],
    model_name: str,
    input_tokens: int,
) -> tuple[float, float, str]:
    brackets = list(pricing.get("models", {}).get(model_name, []))
    if brackets:
        brackets.sort(key=lambda item: int(item.get("max_input_tokens", 10**18)))
        chosen = brackets[-1]
        for bracket in brackets:
            if input_tokens <= int(bracket.get("max_input_tokens", 10**18)):
                chosen = bracket
                break
        return (
            float(chosen["input_cny_per_million"]),
            float(chosen["output_cny_per_million"]),
            "configured_model_rate",
        )
    default = dict(pricing.get("default", {}))
    return (
        float(default.get("input_cny_per_million", 12.0)),
        float(default.get("output_cny_per_million", 54.0)),
        "conservative_unknown_model_rate",
    )


def estimate_call_cost_cny(
    model_name: str,
    input_tokens: int,
    output_tokens: int,
    pricing_path: Path | None = None,
) -> float:
    """Estimate one call using its request-size pricing bracket."""

    pricing = load_model_pricing(pricing_path)
    rate_in, rate_out, _ = _rate_for_call(pricing, model_name, input_tokens)
    return (
        max(0, input_tokens) / 1_000_000 * rate_in
        + max(0, output_tokens) / 1_000_000 * rate_out
    )


class CostLedger:
    """Accumulate exact token counts and estimated list-price cost."""

    def __init__(
        self,
        work_dir: Path,
        run_id: str,
        task_id: str,
        pricing_path: Path | None = None,
    ) -> None:
        self._path = work_dir / "COST.json"
        self._run_id = run_id
        self._task_id = task_id
        self._t0 = time.monotonic()
        self._prior_wall_time_seconds: float = 0.0
        self.total_input_tokens: int = 0
        self.total_output_tokens: int = 0
        self.model_call_count: int = 0
        self.tool_call_count: int = 0
        self._pricing_path = pricing_path or DEFAULT_PRICING_PATH
        self._pricing = load_model_pricing(self._pricing_path)
        self._estimated_cost_cny = 0.0
        self._per_model: Dict[str, Dict[str, Any]] = {}
        self._restore_previous_ledger()
        self._recover_interrupted_events_if_needed(work_dir)

    def _restore_previous_ledger(self) -> None:
        """Continue an interrupted task without erasing its earlier spend.

        ResearchWorker can restore ``AGENT_STATE.json`` across processes.  The
        cost ledger must follow the same rule; otherwise a failed task could be
        restarted repeatedly while each restart appears to cost zero.
        """

        if not self._path.exists():
            return
        try:
            previous = json.loads(self._path.read_text(encoding="utf-8"))
            if not isinstance(previous, dict):
                return
            if str(previous.get("run_id") or "") != self._run_id:
                return
            if str(previous.get("task_id") or "") != self._task_id:
                return
            self.total_input_tokens = max(
                0, int(previous.get("total_input_tokens", 0) or 0)
            )
            self.total_output_tokens = max(
                0, int(previous.get("total_output_tokens", 0) or 0)
            )
            self.model_call_count = max(
                0, int(previous.get("model_call_count", 0) or 0)
            )
            self.tool_call_count = max(
                0, int(previous.get("tool_call_count", 0) or 0)
            )
            self._estimated_cost_cny = max(
                0.0, float(previous.get("estimated_cost_cny", 0.0) or 0.0)
            )
            self._prior_wall_time_seconds = max(
                0.0, float(previous.get("wall_time_seconds", 0.0) or 0.0)
            )
            per_model = previous.get("per_model", {})
            if isinstance(per_model, dict):
                self._per_model = {
                    str(name): dict(values)
                    for name, values in per_model.items()
                    if isinstance(values, dict)
                }
        except Exception:
            # A corrupt ledger must not prevent recovery.  The new run will
            # write a valid replacement and the event log still records the
            # recovery incident.
            return

    def _recover_interrupted_events_if_needed(self, work_dir: Path) -> None:
        """Recover paid model calls when a process died before COST.json save."""

        if self.model_call_count or self._path.exists():
            return
        events_path = work_dir / "EVENTS.jsonl"
        if not events_path.exists():
            return
        try:
            for line in events_path.read_text(
                encoding="utf-8", errors="replace"
            ).splitlines():
                if not line.strip():
                    continue
                event = json.loads(line)
                if event.get("event") == "model_call_end":
                    self.record_call(
                        str(event.get("model") or "unknown"),
                        int(event.get("input_tokens", 0) or 0),
                        int(event.get("output_tokens", 0) or 0),
                    )
                elif event.get("event") == "tool_call":
                    self.record_tool_call()
        except Exception:
            # Recovery is best effort; malformed trailing JSON from an abrupt
            # process exit must not make the task unrecoverable.
            return

    def record_call(
        self,
        model_name: str,
        input_tokens: int,
        output_tokens: int,
    ) -> None:
        input_tokens = max(0, int(input_tokens))
        output_tokens = max(0, int(output_tokens))
        self.total_input_tokens += input_tokens
        self.total_output_tokens += output_tokens
        self.model_call_count += 1

        rate_in, rate_out, rate_source = _rate_for_call(
            self._pricing, model_name, input_tokens
        )
        call_cost = (
            input_tokens / 1_000_000 * rate_in
            + output_tokens / 1_000_000 * rate_out
        )
        self._estimated_cost_cny += call_cost

        entry = self._per_model.setdefault(
            model_name,
            {
                "input": 0,
                "output": 0,
                "calls": 0,
                "estimated_cost_cny": 0.0,
                "rate_source": rate_source,
            },
        )
        entry["input"] += input_tokens
        entry["output"] += output_tokens
        entry["calls"] += 1
        entry["estimated_cost_cny"] = round(
            float(entry["estimated_cost_cny"]) + call_cost,
            6,
        )

    def record_tool_call(self) -> None:
        self.tool_call_count += 1

    def estimated_cost_cny(self) -> float:
        return round(self._estimated_cost_cny, 6)

    def estimated_cost_usd(self) -> float:
        usd_to_cny = max(float(self._pricing.get("usd_to_cny", 7.2)), 0.01)
        return round(self._estimated_cost_cny / usd_to_cny, 6)

    def save(self, status: str, stop_reason: str | None) -> None:
        data = {
            "run_id": self._run_id,
            "task_id": self._task_id,
            "status": status,
            "stop_reason": stop_reason,
            "wall_time_seconds": round(
                self._prior_wall_time_seconds + time.monotonic() - self._t0,
                2,
            ),
            "total_input_tokens": self.total_input_tokens,
            "total_output_tokens": self.total_output_tokens,
            "model_call_count": self.model_call_count,
            "tool_call_count": self.tool_call_count,
            "estimated_cost_cny": self.estimated_cost_cny(),
            "estimated_cost_usd": self.estimated_cost_usd(),
            "billing_currency": "CNY",
            "pricing_source": self._pricing.get("pricing_source"),
            "pricing_config_path": str(self._pricing_path),
            "per_model": self._per_model,
        }
        atomic_write_json(self._path, data)
