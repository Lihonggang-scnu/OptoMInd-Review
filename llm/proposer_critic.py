"""Proposer-Critic two-step LLM verification chain.

Pattern:
  Proposer (advanced_model / B-tier) → structured JSON draft
  Critic   (standard_model / C-tier) → {"accepted": bool, "revised": {...}, "flags": [...]}

Both steps use call_qwen_json so all outputs are structured JSON with _llm_usage tracking.
"""

from __future__ import annotations

import json
from typing import Any

from llm.qwen_llm_client import call_qwen_json

# Schema the Critic must always return
_CRITIC_OUTPUT_SCHEMA = {
    "accepted": "boolean — true if proposer output is correct as-is",
    "revised": "object — corrected version of proposer output if accepted=false, else null",
    "flags": "array of strings — issues found; empty list if accepted=true",
}


class ProposerCriticChain:
    """Run a Proposer → Critic verification chain."""

    def run(
        self,
        proposer_prompt: str,
        critic_prompt_template: str,
        proposer_tier: str = "advanced_model",
        critic_tier: str = "standard_model",
        agent_name: str = "proposer_critic",
        task_type: str = "proposer_critic",
        proposer_output_schema: dict[str, Any] | None = None,
        force_mock: bool | None = None,
    ) -> dict[str, Any]:
        """
        Args:
            proposer_prompt: Full prompt text for the proposer step.
            critic_prompt_template: Prompt template for the critic step.
                Must contain a {proposer_output} placeholder that receives
                the proposer JSON as a string.
            proposer_tier: Model tier for proposer ("advanced_model" = qwen3.7-flash).
            critic_tier: Model tier for critic ("standard_model" = qwen3.6-flash).
            agent_name: Used for LLM usage tracking.
            task_type: Used for LLM usage tracking.
            proposer_output_schema: Optional schema hint for the proposer output.

        Returns:
            {
              "proposer_output": dict,
              "critic_output": dict,
              "accepted": bool,
              "final": dict,      # revised if critic rejected, else proposer_output
              "flags": list[str],
              "_proposer_usage": dict,
              "_critic_usage": dict,
            }
        """
        # --- Proposer step ---
        proposer_resp = call_qwen_json(
            agent_name=f"{agent_name}_proposer",
            task_type=f"{task_type}_proposer",
            input_payload={"prompt": proposer_prompt},
            output_schema=proposer_output_schema,
            model_tier=proposer_tier,
            force_mock=force_mock,
        )
        proposer_usage = proposer_resp.pop("_llm_usage", {})
        proposer_output: dict[str, Any] = proposer_resp

        # --- Critic step ---
        critic_prompt = critic_prompt_template.format(
            proposer_output=json.dumps(proposer_output, ensure_ascii=False, indent=2)
        )
        critic_resp = call_qwen_json(
            agent_name=f"{agent_name}_critic",
            task_type=f"{task_type}_critic",
            input_payload={"prompt": critic_prompt},
            output_schema=_CRITIC_OUTPUT_SCHEMA,
            model_tier=critic_tier,
            force_mock=force_mock,
        )
        critic_usage = critic_resp.pop("_llm_usage", {})

        accepted = bool(critic_resp.get("accepted", True))
        flags: list[str] = list(critic_resp.get("flags") or [])
        revised = critic_resp.get("revised")
        final = (revised if isinstance(revised, dict) else proposer_output) if not accepted else proposer_output

        return {
            "proposer_output": proposer_output,
            "critic_output": critic_resp,
            "accepted": accepted,
            "final": final,
            "flags": flags,
            "_proposer_usage": proposer_usage,
            "_critic_usage": critic_usage,
        }
