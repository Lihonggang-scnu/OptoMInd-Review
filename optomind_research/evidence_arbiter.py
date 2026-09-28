"""Chapter-level evidence-type arbitration for M2a claims.

The arbiter intentionally makes one bounded model call for a whole section.
This keeps the call/usage ledger honest and lets the model compare claims
within the same argument rather than classifying them in isolation.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from optomind_research.claim_schema import VALID_EVIDENCE_TYPES, Claim

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_ARBITER_PROMPT = PROJECT_ROOT / "prompts" / "Evidence Type Arbiter.txt"


def _safe_json(text: str) -> dict[str, Any]:
    try:
        value = json.loads(text)
        return value if isinstance(value, dict) else {}
    except Exception:
        match = re.search(r"\{.*\}", str(text or ""), re.S)
        if not match:
            return {}
        try:
            value = json.loads(match.group(0))
            return value if isinstance(value, dict) else {}
        except Exception:
            return {}


class EvidenceTypeArbiter:
    """Re-judge evidence types in one chapter-level batch."""

    def __init__(
        self,
        prompt_path: Path = DEFAULT_ARBITER_PROMPT,
        model_tier: str = "standard_model",
        workers: int = 4,
    ) -> None:
        self.prompt_path = Path(prompt_path)
        self.model_tier = model_tier
        # Kept for API compatibility.  Section arbitration is deliberately
        # single-call and does not use per-claim worker threads.
        self.workers = max(1, int(workers))
        self._system_prompt: str | None = None
        self.last_audit: dict[str, Any] = {
            "call_count": 0,
            "attempts": [],
            "batch_size": 0,
            "failed_claim_ids": [],
        }

    def _load_prompt(self) -> str:
        if self._system_prompt is None:
            self._system_prompt = self.prompt_path.read_text(encoding="utf-8").strip()
        return self._system_prompt

    @staticmethod
    def _claim_payload(claim: Claim, section: dict[str, Any]) -> dict[str, Any]:
        text_chunks = section.get("candidate_text_chunks") or []
        chunk_map = {
            str(c.get("chunk_id")): c
            for c in text_chunks
            if isinstance(c, dict) and c.get("chunk_id")
        }
        text_previews = []
        for cid in claim.supporting_text_chunk_ids[:3]:
            row = chunk_map.get(str(cid))
            if not row:
                continue
            text_previews.append({
                "chunk_id": str(cid),
                "text_preview": str(
                    row.get("text_preview") or row.get("text") or row.get("search_text") or ""
                )[:500],
                "use_permission": row.get("use_permission", "discovery_only"),
                "scope_fit": row.get("scope_fit", "unreviewed"),
                "source_kind": row.get("source_kind", "unknown"),
                "content_depth": row.get("content_depth", "metadata"),
                "retrieval_role": row.get("retrieval_role", ""),
            })
        visual_chunks = section.get("candidate_visual_chunks") or []
        visual_map = {
            str(v.get("chunk_id")): v
            for v in visual_chunks
            if isinstance(v, dict) and v.get("chunk_id")
        }
        visual_previews = [
            {
                "chunk_id": str(vid),
                "visual_argument_type": visual_map[vid].get("visual_argument_type", ""),
                "caption": str(visual_map[vid].get("caption", ""))[:240],
            }
            for vid in claim.supporting_visual_chunk_ids[:2]
            if str(vid) in visual_map
        ]
        return {
            "claim_id": claim.claim_id,
            "statement": claim.statement,
            "current_evidence_type": claim.evidence_type,
            "importance": getattr(claim, "importance", "supporting"),
            "section_title": str(section.get("title", "")),
            "section_argument_role": str(section.get("argument_role", ""))[:400],
            "supporting_text_previews": text_previews,
            "supporting_visual_info": visual_previews,
        }

    def _build_user_payload(self, claim: Claim, section: dict[str, Any]) -> str:
        """Compatibility helper for callers that inspect a single payload."""
        return json.dumps(
            {"section": {"section_id": section.get("section_id", "")}, "claims": [self._claim_payload(claim, section)]},
            ensure_ascii=False,
        )

    @staticmethod
    def _normalize_result(parsed: dict[str, Any], claim: Claim) -> dict[str, Any]:
        # Accept the old single-claim shape for compatibility with cached or
        # hand-written fixtures, while the production path uses claims[].
        row = parsed
        primary = str(row.get("primary_evidence_type") or claim.evidence_type).lower().strip()
        if primary not in VALID_EVIDENCE_TYPES:
            primary = claim.evidence_type
        raw_secondary = row.get("secondary_evidence_types") or []
        if isinstance(raw_secondary, str):
            raw_secondary = [raw_secondary]
        secondary = [
            str(value).lower().strip()
            for value in raw_secondary
            if str(value).lower().strip() in VALID_EVIDENCE_TYPES
            and str(value).lower().strip() != primary
        ]
        confidence = str(row.get("confidence") or "low").lower().strip()
        if confidence not in {"high", "medium", "low"}:
            confidence = "low"
        return {
            "primary_evidence_type": primary,
            "secondary_evidence_types": list(dict.fromkeys(secondary)),
            "confidence": confidence,
            "reason": str(row.get("reason") or "")[:700],
        }

    @staticmethod
    def _apply(claim: Claim, result: dict[str, Any]) -> Claim:
        claim.decomposer_evidence_type = claim.evidence_type
        claim.evidence_type_confidence = result["confidence"]
        claim.evidence_type_arbiter_reason = result["reason"]
        claim.secondary_evidence_types = result.get("secondary_evidence_types", [])
        if result["confidence"] in ("high", "medium"):
            new_type = result["primary_evidence_type"]
            if new_type != claim.evidence_type:
                claim.critic_flags.append(
                    f"evidence_type_reclassified: {claim.evidence_type}->{new_type}"
                )
                claim.evidence_type = new_type
        else:
            claim.critic_flags.append("evidence_type_low_confidence_arbiter")
        return claim

    def _call_arbiter(self, claim: Claim, section: dict[str, Any]) -> dict[str, Any]:
        """Single-claim compatibility path; section arbitration is batched."""
        from llm.qwen_chat_client import call_qwen_chat

        result = call_qwen_chat(
            "EvidenceTypeArbiterAgent",
            [
                {"role": "system", "content": self._load_prompt()},
                {"role": "user", "content": self._build_user_payload(claim, section)},
            ],
            model_tier=self.model_tier,
            temperature=0,
            max_tokens=500,
            response_format={"type": "json_object"},
            force_mock=False,
            max_retries=1,
        )
        return self._normalize_result(_safe_json(str(result.get("content") or "")), claim)

    def arbitrate(self, claim: Claim, section: dict[str, Any]) -> Claim:
        result = self._call_arbiter(claim, section)
        return self._apply(claim, result)

    def arbitrate_section(self, claims: list[Claim], section: dict[str, Any]) -> list[Claim]:
        """Classify all claims with one bounded chapter-level call."""
        claims = list(claims or [])
        self.last_audit = {
            "call_count": 0,
            "attempts": [],
            "batch_size": len(claims),
            "failed_claim_ids": [],
        }
        if not claims:
            return claims
        from llm.qwen_chat_client import call_qwen_chat

        payload = {
            "section": {
                "section_id": section.get("section_id", ""),
                "title": section.get("title", ""),
                "argument_role": section.get("argument_role", ""),
            },
            "claims": [self._claim_payload(claim, section) for claim in claims],
        }
        max_tokens = min(5200, max(1200, 500 + 360 * len(claims)))
        estimated_input_tokens = max(
            1, len(json.dumps(payload, ensure_ascii=False)) // 4
        )
        try:
            response = call_qwen_chat(
                "EvidenceTypeArbiterAgent",
                [
                    {"role": "system", "content": self._load_prompt()},
                    {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
                ],
                model_tier=self.model_tier,
                temperature=0,
                max_tokens=max_tokens,
                response_format={"type": "json_object"},
                force_mock=False,
                max_retries=1,
            )
            usage = response.get("_llm_usage")
            if not isinstance(usage, dict):
                usage = {}
            attempt = {
                "model": self.model_tier,
                "model_tier": self.model_tier,
                "max_tokens": max_tokens,
                "estimated_input_tokens": estimated_input_tokens,
                "estimated_output_tokens": max(1, len(str(response.get("content") or "")) // 4),
                "requested_claim_ids": [claim.claim_id for claim in claims],
                "raw_chars": len(str(response.get("content") or "")),
                "usage": dict(usage),
                "usage_recorded": bool(usage),
                "retries": int(usage.get("retry_count") or 0),
                "max_retries": 1,
                "failed": bool(response.get("error") or response.get("failure")),
            }
            self.last_audit["attempts"].append(attempt)
            self.last_audit["call_count"] = 1
            parsed = _safe_json(str(response.get("content") or ""))
            rows = parsed.get("claims") if isinstance(parsed.get("claims"), list) else []
            if not rows and len(claims) == 1 and parsed.get("primary_evidence_type"):
                rows = [{"claim_id": claims[0].claim_id, **parsed}]
            by_id = {
                str(row.get("claim_id")): row
                for row in rows
                if isinstance(row, dict) and str(row.get("claim_id") or "")
            }
            missing: list[str] = []
            for claim in claims:
                row = by_id.get(claim.claim_id)
                if row is None:
                    missing.append(claim.claim_id)
                    claim.evidence_type_confidence = "not_run"
                    claim.critic_flags.append("arbiter_missing_claim")
                    continue
                self._apply(claim, self._normalize_result(row, claim))
            self.last_audit["failed_claim_ids"] = missing
            self.last_audit["complete"] = not missing
            return claims
        except Exception as exc:
            self.last_audit["call_count"] = 1
            self.last_audit["attempts"].append({
                "model": self.model_tier,
                "model_tier": self.model_tier,
                "requested_claim_ids": [claim.claim_id for claim in claims],
                "usage": {},
                "estimated_input_tokens": estimated_input_tokens,
                "estimated_output_tokens": 0,
                "usage_recorded": False,
                "retries": 1,
                "max_retries": 1,
                "failed": True,
                "error": type(exc).__name__,
            })
            self.last_audit["failed_claim_ids"] = [claim.claim_id for claim in claims]
            for claim in claims:
                claim.evidence_type_confidence = "not_run"
                claim.critic_flags.append(f"arbiter_error: {type(exc).__name__}")
            return claims


__all__ = ["EvidenceTypeArbiter"]
