"""Pre-writing scientific and manuscript-role audit for a selected blueprint.

This critic does not establish facts and does not use M1 as evidence.  It asks
whether the proposed article architecture is scientifically well-posed enough
to send to claim decomposition: terms have bounded meanings, body chapters do
not double as introductions, and planned theses are questions to test rather
than conclusions smuggled in before evidence mapping.
"""

from __future__ import annotations

import copy
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from optomind_research.intermediate_language_guard import ensure_english_payload


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PROMPT = PROJECT_ROOT / "prompts" / "Blueprint Scientific Critic.txt"

_STRONG_MARKERS = (
    "irreducible",
    "fundamental limit",
    "fundamentally impossible",
    "most studies fail",
    "most approaches fail",
    "universally",
    "always",
    "cannot be overcome",
    "has been solved",
    "is the core unsolved problem",
    "fundamentally limited",
    "unavoidable",
    "cannot resolve",
    "requires a staged approach",
)


def _safe_json(text: str) -> dict[str, Any]:
    try:
        value = json.loads(str(text or ""))
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


def _claim_text(section: dict[str, Any]) -> str:
    value = section.get("planned_thesis") or section.get("central_claim") or ""
    if isinstance(value, dict):
        value = value.get("text") or ""
    return re.sub(r"\s+", " ", str(value)).strip()


def _numeric_literals_untrimmed(value: Any) -> set[str]:
    return set(re.findall(r"(?<![A-Za-z])\d+(?:\.\d+)?(?:\s*[-–—]\s*\d+(?:\.\d+)?)?\s*%?", str(value)))


def _numeric_literals(value: Any) -> set[str]:
    # Manuscript structure references are not scientific measurements.  Remove
    # forms such as S09, Section 9, Stage 3, and Step 2 before auditing numeric
    # claims, while retaining wavelengths, percentages, layer counts, etc.
    serialized = json.dumps(value, ensure_ascii=False) if not isinstance(value, str) else value
    serialized = re.sub(
        r"\b(?:S|Section|Stage|Step)\s*0*\d+\b",
        " ",
        serialized,
        flags=re.IGNORECASE,
    )
    return {
        match.strip()
        for match in _numeric_literals_untrimmed(serialized)
        if str(match).strip()
    }


def deterministic_blueprint_audit(
    blueprint: dict[str, Any], *, allowed_numbers: set[str] | None = None
) -> list[dict[str, Any]]:
    sections = list(blueprint.get("sections") or [])
    issues: list[dict[str, Any]] = []
    if len(sections) < 2:
        issues.append({
            "severity": "block",
            "section_id": "",
            "issue_type": "manuscript_structure",
            "description": "A full review requires distinct opening and closing roles.",
        })
        return issues
    roles = [str(row.get("section_role") or "").lower() for row in sections]
    expected = ["introduction"] + ["body"] * max(0, len(sections) - 2) + ["synthesis"]
    if roles != expected:
        issues.append({
            "severity": "block",
            "section_id": "",
            "issue_type": "section_role_sequence",
            "description": f"Expected role sequence {expected}; received {roles}.",
        })
    for index, section in enumerate(sections):
        sid = str(section.get("section_id") or f"S{index + 1:02d}")
        thesis = _claim_text(section)
        lowered = thesis.lower()
        matched = [marker for marker in _STRONG_MARKERS if marker in lowered]
        if matched:
            issues.append({
                "severity": "major",
                "section_id": sid,
                "issue_type": "premature_strong_thesis",
                "description": (
                    "The planned thesis contains conclusion-strength language before evidence "
                    f"mapping: {matched}. Reframe it as a bounded proposition to test."
                ),
            })
        unsupported_numbers = _numeric_literals(thesis) - set(allowed_numbers or set())
        if unsupported_numbers:
            issues.append({
                "severity": "major",
                "section_id": sid,
                "issue_type": "unverified_numeric_thesis",
                "description": (
                    "A planned thesis contains numerical findings that must be established later from evidence: "
                    f"{sorted(unsupported_numbers)}."
                ),
            })
        if index > 0 and str(section.get("section_role") or "") == "body":
            purpose = str(section.get("purpose") or "").lower()
            if any(term in purpose for term in ("introduce the field", "define the field", "overview the review")):
                issues.append({
                    "severity": "major",
                    "section_id": sid,
                    "issue_type": "introduction_leakage",
                    "description": "A body section is assigned an introduction-level task.",
                })
    return issues


def _apply_bounded_section_updates(
    source: dict[str, Any], parsed: dict[str, Any], charter: dict[str, Any]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Apply critic edits without allowing numbers to migrate across fields."""
    sections = list(source.get("sections") or [])
    by_id = {str(row.get("section_id") or ""): row for row in sections}
    charter_scope_numbers = _numeric_literals({
        "title": charter.get("title"),
        "central_question": charter.get("central_question"),
        "scope_statement": charter.get("scope_statement"),
    })
    applied: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    for update in parsed.get("section_updates") or []:
        if not isinstance(update, dict):
            continue
        sid = str(update.get("section_id") or "")
        target = by_id.get(sid)
        if target is None:
            rejected.append({"section_id": sid, "reason": "unknown_section_id"})
            continue
        safe_update: dict[str, Any] = {"section_id": sid}
        for key in (
            "section_title", "purpose", "argument_role", "key_questions",
            "scope_guardrails", "transition_from_previous", "transition_to_next",
        ):
            if key in update:
                safe_update[key] = update[key]
        if isinstance(update.get("planned_thesis"), dict):
            safe_update["planned_thesis"] = copy.deepcopy(update["planned_thesis"])
            safe_update["planned_thesis"]["claim_status"] = "planned"

        existing_values = {key: target.get(key) for key in safe_update if key != "section_id"}
        permitted_numbers = charter_scope_numbers | _numeric_literals(existing_values)
        numeric_payload = {key: value for key, value in safe_update.items() if key != "section_id"}
        introduced = _numeric_literals(numeric_payload) - permitted_numbers
        if introduced:
            rejected.append({
                "section_id": sid,
                "reason": "introduced_numeric_claim",
                "numeric_literals": sorted(introduced),
            })
            continue
        immutable = {
            "section_id": target.get("section_id"),
            "section_index": target.get("section_index"),
            "section_role": target.get("section_role"),
            "estimated_word_budget": target.get("estimated_word_budget"),
        }
        target.update(safe_update)
        if isinstance(safe_update.get("planned_thesis"), dict):
            target["central_claim"] = copy.deepcopy(safe_update["planned_thesis"])
        target.update(immutable)
        applied.append(safe_update)
    return applied, rejected


@dataclass
class BlueprintScientificCritic:
    prompt_path: Path = field(default_factory=lambda: DEFAULT_PROMPT)
    model_tier: str = "premium_model"
    real_llm: bool = False

    def review(self, *, charter: dict[str, Any], blueprint: dict[str, Any]) -> dict[str, Any]:
        source = copy.deepcopy(blueprint)
        # ``planned_thesis`` is authoritative. Keep the compatibility alias
        # synchronized before the model sees the payload; otherwise a stale
        # legacy claim can resurrect an overclaim already repaired upstream.
        for section in source.get("sections") or []:
            if isinstance(section.get("planned_thesis"), dict):
                section["central_claim"] = copy.deepcopy(section["planned_thesis"])
        charter_scope_numbers = _numeric_literals({
            "title": charter.get("title"),
            "central_question": charter.get("central_question"),
            "scope_statement": charter.get("scope_statement"),
        })
        deterministic_issues = deterministic_blueprint_audit(
            source, allowed_numbers=charter_scope_numbers
        )
        if not self.real_llm:
            blocked = any(row.get("severity") == "block" for row in deterministic_issues)
            return {
                "schema_version": "blueprint_scientific_review.v1",
                "mode": "deterministic",
                "verdict": "block" if blocked else "revise" if deterministic_issues else "pass",
                "issues": deterministic_issues,
                "applied_updates": [],
                "blueprint": source,
            }

        if not self.prompt_path.exists():
            raise FileNotFoundError(f"Blueprint critic prompt not found: {self.prompt_path}")
        from llm.qwen_chat_client import call_qwen_chat

        system_prompt = self.prompt_path.read_text(encoding="utf-8")
        all_model_issues: list[dict[str, Any]] = []
        applied: list[dict[str, Any]] = []
        rejected_updates: list[dict[str, Any]] = []
        parsed: dict[str, Any] = {}
        final_issues = deterministic_issues
        revision_rounds = 0
        for revision_rounds in range(1, 3):
            result = call_qwen_chat(
                "BlueprintScientificCritic",
                [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": json.dumps({
                        "charter": charter,
                        "blueprint": source,
                        "deterministic_precheck": final_issues,
                        "revision_round": revision_rounds,
                        "previous_rejected_updates": rejected_updates,
                    }, ensure_ascii=False)},
                ],
                model_tier=self.model_tier,
                temperature=0.1,
                max_tokens=6000,
                response_format={"type": "json_object"},
                stream=True,
                force_mock=False,
                max_retries=1,
            )
            parsed = _safe_json(str(result.get("content") or ""))
            if not parsed or str(parsed.get("verdict") or "") not in {"pass", "revise", "block"}:
                raise RuntimeError("BlueprintScientificCritic returned an invalid review object.")
            all_model_issues.extend(
                row for row in (parsed.get("issues") or []) if isinstance(row, dict)
            )
            round_applied, round_rejected = _apply_bounded_section_updates(source, parsed, charter)
            applied.extend(round_applied)
            rejected_updates.extend(round_rejected)
            final_issues = deterministic_blueprint_audit(
                source, allowed_numbers=charter_scope_numbers
            )
            hard = [row for row in final_issues if row.get("severity") in {"block", "major"}]
            if not hard:
                break

        hard = [row for row in final_issues if row.get("severity") in {"block", "major"}]
        # A real pre-writing gate may not let unresolved major issues flow into
        # claim decomposition.  "Revise" is informative for the editor, but
        # unresolved major/block findings are operationally blocking.
        verdict = "block" if hard else str(parsed.get("verdict") or "pass")
        payload = {
            "schema_version": "blueprint_scientific_review.v1",
            "mode": "real_llm",
            "verdict": verdict,
            "issues": all_model_issues + final_issues,
            "applied_updates": applied,
            "rejected_updates": rejected_updates,
            "revision_rounds": revision_rounds,
            "review_rationale": str(parsed.get("review_rationale") or ""),
            "blueprint": source,
        }
        return ensure_english_payload(payload)
