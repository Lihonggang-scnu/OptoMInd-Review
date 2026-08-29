"""Production Qwen adapter for one bounded fresh-evidence judgment batch."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from config.qwen_config import get_model_name
from llm import qwen_chat_client

from .cost_ledger import estimate_call_cost_cny


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_PROMPT_PATH = (
    PROJECT_ROOT / "prompts" / "Phase 3 Fresh Evidence Semantic Judge.txt"
)


def _integer_metric(usage: dict[str, Any], keys: tuple[str, ...]) -> tuple[int, str]:
    for key in keys:
        value = usage.get(key)
        if value in (None, ""):
            continue
        try:
            return max(0, int(value)), "provider_reported"
        except (TypeError, ValueError):
            continue
    return 0, "unavailable"


class QwenFreshEvidenceSemanticJudge:
    """Call the cheap Qwen tier once and retain auditable call telemetry."""

    def __init__(
        self,
        *,
        model_tier: str = "cheap_model",
        prompt_path: Path | None = None,
    ) -> None:
        self.model_tier = str(model_tier or "cheap_model")
        self.prompt_path = Path(prompt_path or DEFAULT_PROMPT_PATH)
        self.last_telemetry: dict[str, Any] = self._empty_telemetry()

    def _empty_telemetry(self) -> dict[str, Any]:
        return {
            "provider": "qwen",
            "model_tier": self.model_tier,
            "actual_model": "",
            "model_provenance": "unavailable",
            "call_count": 0,
            "api_call_count": 0,
            "input_tokens": 0,
            "output_tokens": 0,
            "token_provenance": "unavailable",
            "estimated_cost_cny": 0.0,
            "cost_provenance": "unavailable",
            "fallback_used": False,
            "error": "",
            "usage": {},
        }

    @staticmethod
    def _max_tokens(payload: dict[str, Any]) -> int:
        component_count = len(payload.get("components") or [])
        return min(1800, max(512, 320 + 220 * component_count))

    def __call__(self, payload: dict[str, Any]) -> dict[str, Any]:
        self.last_telemetry = self._empty_telemetry()
        try:
            prompt = self.prompt_path.read_text(encoding="utf-8").strip()
            if not prompt:
                raise ValueError("fresh-evidence semantic judge prompt is empty")
            messages = [
                {"role": "system", "content": prompt},
                {
                    "role": "user",
                    "content": json.dumps(
                        payload,
                        ensure_ascii=True,
                        separators=(",", ":"),
                    ),
                },
            ]
            configured_model = str(get_model_name(self.model_tier))
            estimated_input_tokens = max(
                1,
                len(json.dumps(messages, ensure_ascii=True)) // 4,
            )
            estimated_cost = estimate_call_cost_cny(
                configured_model,
                estimated_input_tokens,
                0,
            )
            self.last_telemetry.update({
                "actual_model": configured_model,
                "model_provenance": "configured_request",
                "input_tokens": estimated_input_tokens,
                "token_provenance": "estimated",
                "estimated_cost_cny": estimated_cost,
                "cost_provenance": "estimated_list_price",
                "usage": {
                    "model_name": configured_model,
                    "estimated_input_tokens": estimated_input_tokens,
                    "estimated_output_tokens": 0,
                    "estimated_cost_cny": estimated_cost,
                    "cost_provenance": "estimated_list_price",
                    "failed": True,
                },
            })
            self.last_telemetry["call_count"] = 1
            self.last_telemetry["api_call_count"] = 1
            result = qwen_chat_client.call_qwen_chat(
                agent_name="FreshEvidenceSemanticJudge",
                messages=messages,
                model_tier=self.model_tier,
                max_retries=0,
                temperature=0,
                max_tokens=self._max_tokens(payload),
                response_format={"type": "json_object"},
                stream=False,
                # This remains one logical batch.  The shared Qwen client may
                # rotate a failed/quota-limited key or use the configured
                # model fallback, and reports every physical attempt in the
                # returned usage telemetry.
                allow_model_fallback=True,
                enable_thinking=False,
            )
            if not isinstance(result, dict):
                raise TypeError("Qwen semantic judge returned a non-object response")
            usage = (
                dict(result.get("_llm_usage"))
                if isinstance(result.get("_llm_usage"), dict)
                else {}
            )
            actual_model = str(
                usage.get("model_name")
                or usage.get("model")
                or usage.get("model_tier")
                or self.model_tier
            )
            provider_input, input_source = _integer_metric(
                usage,
                ("input_tokens", "prompt_tokens", "input_token_count"),
            )
            provider_output, output_source = _integer_metric(
                usage,
                ("output_tokens", "completion_tokens", "output_token_count"),
            )
            input_tokens, estimated_input_source = _integer_metric(
                usage,
                ("estimated_input_tokens",),
            )
            output_tokens, estimated_output_source = _integer_metric(
                usage,
                ("estimated_output_tokens",),
            )
            if estimated_input_source != "unavailable":
                estimated_input_source = "estimated"
            if estimated_output_source != "unavailable":
                estimated_output_source = "estimated"
            if input_source == "provider_reported":
                input_tokens = provider_input
            else:
                input_source = estimated_input_source
            if output_source == "provider_reported":
                output_tokens = provider_output
            else:
                output_source = estimated_output_source
            token_provenance = (
                input_source
                if input_source == output_source
                else "mixed"
                if "unavailable" not in {input_source, output_source}
                else input_source if output_source == "unavailable"
                else output_source
            )
            if (
                bool(usage.get("failure") or not usage.get("success", True))
                and input_source == "estimated"
            ):
                input_tokens = max(
                    input_tokens,
                    int(self.last_telemetry.get("input_tokens") or 0),
                )
            if usage.get("mock_llm"):
                api_call_count = 0
            elif "request_attempt_count" in usage:
                api_call_count = max(
                    0, int(usage.get("request_attempt_count") or 0)
                )
            else:
                api_call_count = 1
            cost = estimate_call_cost_cny(
                actual_model,
                input_tokens,
                output_tokens,
            )
            usage_record = {
                **usage,
                "model_name": actual_model,
                "failed": bool(usage.get("failure") or not usage.get("success", True)),
                "estimated_cost_cny": cost,
                "cost_provenance": "estimated_list_price",
            }
            self.last_telemetry.update({
                "actual_model": actual_model,
                "model_provenance": "client_reported",
                "call_count": api_call_count,
                "api_call_count": api_call_count,
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "token_provenance": token_provenance,
                "estimated_cost_cny": cost,
                "cost_provenance": "estimated_list_price",
                "fallback_used": bool(
                    usage.get("fallback_used")
                    or usage.get("failure")
                    or not usage.get("success", True)
                ),
                "usage": usage_record,
            })
            if self.last_telemetry["fallback_used"]:
                raise RuntimeError(
                    str(usage.get("error_type") or "Qwen semantic judge failed")
                )
            content = result.get("content")
            if not isinstance(content, str):
                raise TypeError("Qwen semantic judge content is not a string")
            parsed = json.loads(content)
            if not isinstance(parsed, dict):
                raise ValueError("Qwen semantic judge content is not a JSON object")
            return parsed
        except Exception as exc:
            self.last_telemetry["fallback_used"] = True
            self.last_telemetry["error"] = f"{type(exc).__name__}: {exc}"
            usage = self.last_telemetry.get("usage")
            if isinstance(usage, dict):
                usage["failed"] = True
                usage["fallback_used"] = True
                usage["error_type"] = type(exc).__name__
            raise
