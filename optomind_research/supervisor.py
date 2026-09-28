"""T10: Supervisor — proposes suggestions only, never silently rewrites (spec T10).

Design rules:
- Supervisor reads outputs and proposes improvements via structured suggestions.
- Every suggestion requires explicit human approval before modification.
- Each accepted revision creates a new version with full change provenance.
- Forbidden: silent rewrites, skipping quality gate, marking complete without audit.
"""

from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from llm.qwen_chat_client import call_qwen_chat
from optomind_research.scientific_text_english_normalizer import repair_likely_scientific_mojibake

PROJECT_ROOT = Path(__file__).resolve().parents[1]
_PROMPTS = PROJECT_ROOT / "prompts"
SUPERVISOR_REVIEW_PROMPT = _PROMPTS / "Supervisor Review.txt"

VALID_SUGGESTION_TARGETS = frozenset({
    "blueprint",
    "claims",
    "evidence_relations",
    "dag",
    "gap_resolution",
    "section_draft",
    "citations",
    "figures",
    "hypothesis",
    "research_plan",
})


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _compact(text: Any, limit: int = 400) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip()[:limit]


def _read_prompt(path: Path) -> str:
    if path.exists():
        return path.read_text(encoding="utf-8").strip()
    return "You are a scientific review supervisor. Propose improvements without rewriting."


# --------------------------------------------------------------------------- #
# Data structures
# --------------------------------------------------------------------------- #

@dataclass
class SupervisorSuggestion:
    suggestion_id: str = field(default_factory=lambda: "SUG-" + str(uuid.uuid4())[:6])
    target: str = ""                    # member of VALID_SUGGESTION_TARGETS
    target_id: str = ""                 # e.g. section_id, claim_id
    issue_type: str = ""               # overclaim|missing_evidence|structure|citation|etc.
    severity: str = "medium"           # low|medium|high|critical
    description: str = ""
    proposed_change: str = ""
    new_evidence_ids: list[str] = field(default_factory=list)
    triggered_by_human_feedback: str = ""
    status: str = "pending"            # pending|accepted|rejected
    human_decision_at: str = ""
    human_decision_by: str = ""
    created_at: str = field(default_factory=_utc_now)

    def to_dict(self) -> dict[str, Any]:
        return {
            "suggestion_id": self.suggestion_id,
            "target": self.target,
            "target_id": self.target_id,
            "issue_type": self.issue_type,
            "severity": self.severity,
            "description": self.description,
            "proposed_change": self.proposed_change,
            "new_evidence_ids": self.new_evidence_ids,
            "triggered_by_human_feedback": self.triggered_by_human_feedback,
            "status": self.status,
            "human_decision_at": self.human_decision_at,
            "human_decision_by": self.human_decision_by,
            "created_at": self.created_at,
        }


@dataclass
class RevisionRecord:
    """Provenance record for a single accepted revision."""
    revision_id: str = field(default_factory=lambda: "REV-" + str(uuid.uuid4())[:6])
    suggestion_id: str = ""
    changed_fields: list[str] = field(default_factory=list)
    change_reason: str = ""
    new_evidence_ids: list[str] = field(default_factory=list)
    triggered_by_human_feedback: str = ""
    quality_delta: dict[str, Any] = field(default_factory=dict)
    breaks_falsification_contract: bool = False
    applied_at: str = field(default_factory=_utc_now)
    applied_by: str = "system"

    def to_dict(self) -> dict[str, Any]:
        return {
            "revision_id": self.revision_id,
            "suggestion_id": self.suggestion_id,
            "changed_fields": self.changed_fields,
            "change_reason": self.change_reason,
            "new_evidence_ids": self.new_evidence_ids,
            "triggered_by_human_feedback": self.triggered_by_human_feedback,
            "quality_delta": self.quality_delta,
            "breaks_falsification_contract": self.breaks_falsification_contract,
            "applied_at": self.applied_at,
            "applied_by": self.applied_by,
        }


# --------------------------------------------------------------------------- #
# Supervisor
# --------------------------------------------------------------------------- #

class Supervisor:
    """Reviews pipeline outputs and proposes improvements (spec T10).

    The Supervisor NEVER directly modifies formal artifacts.
    All changes require explicit human approval. Each approved revision
    creates a new version with full change provenance.
    """

    def __init__(
        self,
        model_tier: str = "advanced_model",
        real_llm: bool = False,
    ) -> None:
        self.model_tier = model_tier
        self.real_llm = real_llm
        self.suggestions: list[SupervisorSuggestion] = []
        self.revision_history: list[RevisionRecord] = []

    def review_blueprint(
        self,
        blueprint: dict[str, Any],
        *,
        run_manifest_summary: dict[str, Any] | None = None,
    ) -> list[SupervisorSuggestion]:
        """Review a blueprint and return improvement suggestions."""
        if not self.real_llm:
            return self._mock_suggestions("blueprint", blueprint.get("run_id", ""))

        system = _read_prompt(SUPERVISOR_REVIEW_PROMPT)
        def _is_grounded(claim: dict[str, Any]) -> bool:
            requirement = str(claim.get("evidence_requirement") or "factual")
            if requirement in {"open_question", "normative", "none"}:
                return True
            state = str(claim.get("claim_state") or "").lower()
            binding = str(claim.get("evidence_binding_status") or "").lower()
            return bool(
                state in {"grounded", "partially_grounded"}
                or binding in {"direct", "synthesized", "partial", "bound"}
                or claim.get("evidence_relations")
            )

        payload = {
            "review_target": "blueprint",
            "blueprint_summary": {
                "sections": [
                    {
                        "section_id": s.get("section_id"),
                        "title": _compact(s.get("title"), 100),
                        "claim_count": len(s.get("claims") or []),
                        "ungrounded_claims": sum(
                            1 for c in (s.get("claims") or [])
                            if str(c.get("evidence_requirement") or "factual") == "factual"
                            and not _is_grounded(c)
                        ),
                        "evidence_bound_claims": sum(
                            1 for c in (s.get("claims") or []) if _is_grounded(c)
                        ),
                    }
                    for s in (blueprint.get("sections") or [])
                ],
                "dag_edge_count": blueprint.get("argument_dag", {}).get(
                    "edge_count", len(blueprint.get("argument_dag", {}).get("edges") or [])
                ),
            },
            "run_manifest_summary": run_manifest_summary or {},
        }
        return self._call_llm_for_suggestions(payload)

    def review_section_draft(
        self,
        section_id: str,
        draft_text: str,
        claims: list[dict[str, Any]],
        human_feedback: str = "",
        citation_map: dict[str, list[str]] | None = None,
        overclaim_flags: list[dict[str, Any]] | None = None,
        evaluation_context: dict[str, Any] | None = None,
    ) -> list[SupervisorSuggestion]:
        """Review a section draft and return improvement suggestions."""
        if not self.real_llm:
            return self._mock_suggestions("section_draft", section_id)

        system = _read_prompt(SUPERVISOR_REVIEW_PROMPT)
        draft_limit = 24000
        payload = {
            "review_target": "section_draft",
            "section_id": section_id,
            "draft_text": _compact(draft_text, draft_limit),
            "draft_is_complete": len(str(draft_text or "")) <= draft_limit,
            "evaluation_context": evaluation_context or {},
            "citation_map": citation_map or {},
            "overclaim_flags": overclaim_flags or [],
            "claims": [
                {
                    "claim_id": c.get("claim_id"),
                    "statement": _compact(
                        c.get("statement_for_writing") or c.get("statement"), 300
                    ),
                    "writing_permission": c.get("writing_permission", ""),
                    "supported_rewrite": _compact(c.get("supported_rewrite"), 300),
                    "missing_evidence_components": list(
                        c.get("missing_evidence_components") or []
                    )[:5],
                    "claim_state": c.get("claim_state"),
                    "evidence_requirement": c.get("evidence_requirement", "factual"),
                    "closure_disposition": c.get("closure_disposition", ""),
                    "evidence_binding_status": c.get("evidence_binding_status"),
                    "supporting_text_chunk_count": len(c.get("supporting_text_chunk_ids") or []),
                }
                for c in claims[:10]
            ],
            "human_feedback": human_feedback,
        }
        return self._call_llm_for_suggestions(payload)

    def review_hypothesis_portfolio(
        self,
        portfolio: dict[str, Any],
        human_feedback: str = "",
    ) -> list[SupervisorSuggestion]:
        """Review hypothesis portfolio and return improvement suggestions."""
        if not self.real_llm:
            return self._mock_suggestions("hypothesis", "portfolio")

        system = _read_prompt(SUPERVISOR_REVIEW_PROMPT)
        payload = {
            "review_target": "hypothesis",
            "candidates_summary": [
                {
                    "hypothesis_id": c.get("hypothesis_id"),
                    "core_claim": _compact(c.get("core_claim"), 200),
                    "falsifiability_score": c.get("falsifiability_score"),
                    "overall_novelty": (c.get("collision_check") or {}).get("overall_novelty"),
                    "status": c.get("status"),
                }
                for c in (portfolio.get("candidates") or [])[:8]
            ],
            "human_feedback": human_feedback,
        }
        return self._call_llm_for_suggestions(payload)

    def accept_suggestion(
        self,
        suggestion_id: str,
        *,
        operator: str = "user",
        quality_delta: dict[str, Any] | None = None,
        breaks_contract: bool = False,
    ) -> RevisionRecord:
        """Mark a suggestion as accepted and create a revision record."""
        for sug in self.suggestions:
            if sug.suggestion_id == suggestion_id:
                sug.status = "accepted"
                sug.human_decision_at = _utc_now()
                sug.human_decision_by = operator
                rev = RevisionRecord(
                    suggestion_id=suggestion_id,
                    changed_fields=[sug.target_id],
                    change_reason=sug.description,
                    new_evidence_ids=list(sug.new_evidence_ids),
                    triggered_by_human_feedback=sug.triggered_by_human_feedback,
                    quality_delta=quality_delta or {},
                    breaks_falsification_contract=breaks_contract,
                    applied_by=operator,
                )
                self.revision_history.append(rev)
                return rev
        raise ValueError(f"Suggestion {suggestion_id!r} not found")

    def reject_suggestion(
        self,
        suggestion_id: str,
        *,
        operator: str = "user",
        reason: str = "",
    ) -> None:
        for sug in self.suggestions:
            if sug.suggestion_id == suggestion_id:
                sug.status = "rejected"
                sug.human_decision_at = _utc_now()
                sug.human_decision_by = operator
                if reason:
                    sug.description += f" [Rejected: {reason}]"
                return
        raise ValueError(f"Suggestion {suggestion_id!r} not found")

    def status_summary(self) -> dict[str, Any]:
        """Return a concise status summary for UI display (spec T10)."""
        pending = [s for s in self.suggestions if s.status == "pending"]
        critical = [s for s in pending if s.severity == "critical"]
        high = [s for s in pending if s.severity == "high"]
        process_errors = [
            s for s in self.suggestions if s.issue_type == "supervisor_error"
        ]
        return {
            "total_suggestions": len(self.suggestions),
            "pending": len(pending),
            "accepted": sum(1 for s in self.suggestions if s.status == "accepted"),
            "rejected": sum(1 for s in self.suggestions if s.status == "rejected"),
            "critical_pending": len(critical),
            "high_pending": len(high),
            "revision_count": len(self.revision_history),
            "needs_human_action": bool(critical or high),
            "process_error_count": len(process_errors),
            "review_complete": not process_errors,
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "suggestions": [s.to_dict() for s in self.suggestions],
            "revision_history": [r.to_dict() for r in self.revision_history],
            "status_summary": self.status_summary(),
        }

    # ------------------------------------------------------------------ #
    # Internal helpers
    # ------------------------------------------------------------------ #

    def _call_llm_for_suggestions(
        self, payload: dict[str, Any]
    ) -> list[SupervisorSuggestion]:
        system = _read_prompt(SUPERVISOR_REVIEW_PROMPT)
        try:
            messages = [
                {"role": "system", "content": system},
                {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
            ]
            parsed: dict[str, Any] = {}
            tiers = [self.model_tier]
            if self.model_tier != "b_plus_model":
                tiers.append("b_plus_model")
            for attempt_index, tier in enumerate(tiers, 1):
                result = call_qwen_chat(
                    f"SupervisorAgent:attempt_{attempt_index}",
                    messages,
                    model_tier=tier,
                    temperature=0.1,
                    max_tokens=2400,
                    response_format={"type": "json_object"},
                    force_mock=False,
                    max_retries=0,
                    timeout_seconds=150,
                    max_transport_key_candidates=1,
                    allow_model_fallback=False,
                )
                raw = str(result.get("content") or "")
                try:
                    candidate = json.loads(raw)
                except Exception:
                    m = re.search(r"\{.*\}", raw, re.S)
                    candidate = json.loads(m.group(0)) if m else {}
                if isinstance(candidate, dict) and isinstance(
                    candidate.get("suggestions"), list
                ):
                    parsed = candidate
                    break
            if not isinstance(parsed, dict) or not isinstance(parsed.get("suggestions"), list):
                raise ValueError("Supervisor returned no valid suggestions array")
            new_suggestions: list[SupervisorSuggestion] = []
            for item in (parsed.get("suggestions") or []):
                if not isinstance(item, dict):
                    continue
                target = str(item.get("target", ""))
                if target not in VALID_SUGGESTION_TARGETS:
                    continue
                sug = SupervisorSuggestion(
                    target=target,
                    target_id=str(item.get("target_id", "")),
                    issue_type=str(item.get("issue_type", "")),
                    severity=str(item.get("severity", "medium")),
                    description=_compact(
                        repair_likely_scientific_mojibake(str(item.get("description") or "")), 400
                    ),
                    proposed_change=_compact(
                        repair_likely_scientific_mojibake(str(item.get("proposed_change") or "")), 600
                    ),
                    new_evidence_ids=list(item.get("new_evidence_ids") or []),
                    triggered_by_human_feedback=str(item.get("triggered_by_human_feedback", "")),
                )
                self.suggestions.append(sug)
                new_suggestions.append(sug)
            return new_suggestions
        except Exception as exc:
            fallback = SupervisorSuggestion(
                target=str(payload.get("review_target", "blueprint")),
                issue_type="supervisor_error",
                severity="low",
                description=f"Supervisor LLM call failed: {type(exc).__name__}",
                proposed_change="",
            )
            self.suggestions.append(fallback)
            return [fallback]

    def _mock_suggestions(self, target: str, target_id: str) -> list[SupervisorSuggestion]:
        sug = SupervisorSuggestion(
            target=target,
            target_id=target_id,
            issue_type="mock_review",
            severity="low",
            description="Mock supervisor review — no LLM call made.",
            proposed_change="[No change proposed in mock mode]",
        )
        self.suggestions.append(sug)
        return [sug]
