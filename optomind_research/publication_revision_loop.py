"""Evidence-aware, versioned publication revision loop for scientific reviews.

This module turns one-pass review generation into an auditable author-team loop:

    reviewer reports -> normalized issues -> root-cause plan -> targeted repair
    -> citation/continuity audit -> old/new comparison -> converge or escalate

The implementation is deliberately domain-generic inside optical science.  It
does not contain topic-specific papers, keywords, numerical targets, or section
names.  LLMs propose judgments and prose; deterministic guards own identities,
citations, rollback, budgets, persistence, and stop conditions.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from llm.qwen_chat_client import call_qwen_chat
from optomind_research.scientific_text_english_normalizer import (
    ensure_english_strings,
    repair_likely_scientific_mojibake,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]
PROMPTS_DIR = PROJECT_ROOT / "prompts"
DECISION_BOARD_PROMPT = PROMPTS_DIR / "Revision Decision Board.txt"
TARGETED_REVISION_PROMPT = PROMPTS_DIR / "Targeted Review Revision.txt"
DELTA_JUDGE_PROMPT = PROMPTS_DIR / "Revision Delta Judge.txt"
ARCHITECTURE_PATCH_PROMPT = PROMPTS_DIR / "Review Architecture Patch.txt"
REVISION_LOOP_IMPLEMENTATION_VERSION = "publication_revision_loop.v1.5"

SEVERITY_RANK = {"low": 1, "medium": 2, "high": 3, "critical": 4}
VALID_SEVERITIES = frozenset(SEVERITY_RANK)
VALID_ROOT_CAUSES = frozenset({
    "missing_evidence",
    "claim_overreach",
    "writing",
    "architecture",
    "methodology_identity",
    "visual_conceptual",
    "visual_empirical",
    "scope_or_external",
    "review_process",
})
VALID_REPAIR_ROUTES = frozenset({
    "evidence_retrieval",
    "claim_narrowing",
    "section_local_rewrite",
    "cross_section_edit",
    "blueprint_contract_patch",
    "charter_method_patch",
    "conceptual_visual_generation",
    "empirical_visual_source_task",
    "human_decision",
    "reviewer_retry",
})
SAFE_AUTO_ROUTES = frozenset({
    "evidence_retrieval",
    "claim_narrowing",
    "section_local_rewrite",
    "cross_section_edit",
    "conceptual_visual_generation",
    "reviewer_retry",
    "blueprint_contract_patch",
    "charter_method_patch",
})


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace(
        "+00:00", "Z"
    )


def _compact(value: Any, limit: int = 1200) -> str:
    text = repair_likely_scientific_mojibake(str(value or ""))
    return re.sub(r"\s+", " ", text).strip()[:limit]


def _safe_json(text: str) -> dict[str, Any]:
    try:
        parsed = json.loads(str(text or ""))
        return parsed if isinstance(parsed, dict) else {}
    except Exception:
        match = re.search(r"\{.*\}", str(text or ""), re.S)
        if not match:
            return {}
        try:
            parsed = json.loads(match.group(0))
            return parsed if isinstance(parsed, dict) else {}
        except Exception:
            return {}


def _salvage_complete_array_objects(text: str, key: str) -> list[dict[str, Any]]:
    """Recover complete objects from one named array in truncated JSON."""
    value = str(text or "")
    match = re.search(rf'"{re.escape(key)}"\s*:\s*\[', value)
    if not match:
        return []
    cursor = match.end()
    decoder = json.JSONDecoder()
    tasks: list[dict[str, Any]] = []
    while cursor < len(value):
        while cursor < len(value) and value[cursor] in " \r\n\t,":
            cursor += 1
        if cursor >= len(value) or value[cursor] == "]":
            break
        try:
            item, end = decoder.raw_decode(value, cursor)
        except json.JSONDecodeError:
            break
        if isinstance(item, dict):
            tasks.append(item)
        cursor = end
    return tasks


def _salvage_complete_tasks(text: str) -> list[dict[str, Any]]:
    """Recover only complete task objects from a truncated JSON response."""
    return _salvage_complete_array_objects(text, "tasks")


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def _json_fingerprint(payload: Any) -> str:
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _issue_report_fingerprint(report: dict[str, Any]) -> str:
    rows = []
    for issue in report.get("issues") or []:
        if not isinstance(issue, dict):
            continue
        rows.append({
            "issue_id": issue.get("issue_id"),
            "severity": issue.get("severity"),
            "root_cause": issue.get("root_cause"),
            "repair_route": issue.get("repair_route"),
            "section_ids": sorted(_unique_strings(issue.get("section_ids") or [])),
            "claim_ids": sorted(_unique_strings(issue.get("claim_ids") or [])),
        })
    return _json_fingerprint(sorted(rows, key=lambda row: str(row.get("issue_id") or "")))


def _revision_fingerprint(bundle: dict[str, Any]) -> str:
    return _json_fingerprint({
        "sections": [
            {
                "section_id": row.get("section_id"),
                "english_text": row.get("english_text"),
                "citation_map": row.get("citation_map"),
            }
            for row in (bundle.get("section_drafts") or []) if isinstance(row, dict)
        ],
        "blueprint": bundle.get("blueprint") or {},
    })


def _tokens(value: Any) -> set[str]:
    stop = {
        "about", "after", "again", "against", "also", "because", "before",
        "being", "between", "could", "from", "have", "into", "must", "only",
        "other", "should", "their", "there", "these", "this", "those", "through",
        "under", "using", "were", "which", "while", "with", "would", "review",
        "section", "manuscript", "claim", "evidence", "paper", "study", "studies",
    }
    return {
        token for token in re.findall(r"[a-z][a-z0-9_-]{3,}", _compact(value, 6000).lower())
        if token not in stop
    }


def _similarity(a: Any, b: Any) -> float:
    left, right = _tokens(a), _tokens(b)
    if not left or not right:
        return 0.0
    return len(left & right) / len(left | right)


def _match_issue_lineage(
    before_rows: list[dict[str, Any]],
    after_rows: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], set[str], set[str]]:
    """Match rephrased reviewer issues without treating wording drift as resolution."""
    candidates: list[tuple[float, int, int, str]] = []
    for before_index, before in enumerate(before_rows):
        before_id = str(before.get("issue_id") or "")
        before_sections = set(_unique_strings(before.get("section_ids") or []))
        before_claims = set(_unique_strings(before.get("claim_ids") or []))
        for after_index, after in enumerate(after_rows):
            after_id = str(after.get("issue_id") or "")
            if before_id and before_id == after_id:
                candidates.append((3.0, before_index, after_index, "stable_id"))
                continue
            after_sections = set(_unique_strings(after.get("section_ids") or []))
            after_claims = set(_unique_strings(after.get("claim_ids") or []))
            section_compatible = bool(before_sections & after_sections) or (
                not before_sections and not after_sections
            )
            claim_overlap = bool(before_claims & after_claims)
            same_root = bool(
                before.get("root_cause")
                and before.get("root_cause") == after.get("root_cause")
            )
            same_type = bool(
                before.get("issue_type")
                and before.get("issue_type") == after.get("issue_type")
            )
            description_similarity = max(
                _similarity(before.get("description"), after.get("description")),
                _similarity(before.get("requested_change"), after.get("requested_change")),
            )
            score = 0.0
            reason = ""
            if claim_overlap and (same_root or same_type):
                score = 2.0 + description_similarity
                reason = "same_claim_and_issue_family"
            elif section_compatible and same_root and description_similarity >= 0.18:
                score = 1.0 + description_similarity
                reason = "same_section_root_and_semantics"
            elif section_compatible and same_type and description_similarity >= 0.26:
                score = 0.8 + description_similarity
                reason = "same_section_type_and_semantics"
            if score:
                candidates.append((score, before_index, after_index, reason))
    matched_before: set[int] = set()
    matched_after: set[int] = set()
    lineage: list[dict[str, Any]] = []
    for score, before_index, after_index, reason in sorted(candidates, reverse=True):
        if before_index in matched_before or after_index in matched_after:
            continue
        matched_before.add(before_index)
        matched_after.add(after_index)
        lineage.append({
            "before_issue_id": str(before_rows[before_index].get("issue_id") or ""),
            "after_issue_id": str(after_rows[after_index].get("issue_id") or ""),
            "match_score": round(score, 3),
            "match_reason": reason,
        })
    resolved = {
        str(row.get("issue_id") or "")
        for index, row in enumerate(before_rows)
        if index not in matched_before and row.get("issue_id")
    }
    new = {
        str(row.get("issue_id") or "")
        for index, row in enumerate(after_rows)
        if index not in matched_after and row.get("issue_id")
    }
    return lineage, resolved, new


def _unique_strings(values: Iterable[Any], *, limit: int | None = None) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = str(value or "").strip()
        if not text or text in seen:
            continue
        seen.add(text)
        result.append(text)
        if limit is not None and len(result) >= limit:
            break
    return result


def _word_count(text: str) -> int:
    return len(re.findall(r"\b[A-Za-z][A-Za-z'-]*\b", str(text or "")))


def _paragraph_count(text: str) -> int:
    return len([p for p in re.split(r"\n\s*\n", str(text or "")) if p.strip()])


def _reference_markers(text: str) -> set[str]:
    return set(re.findall(r"\[REF:[^\]]+\]", str(text or "")))


def _stable_id(prefix: str, *parts: Any) -> str:
    raw = "|".join(_compact(part, 2000).lower() for part in parts)
    return f"{prefix}-{hashlib.sha1(raw.encode('utf-8')).hexdigest()[:10]}"


def _normalize_severity(value: Any) -> str:
    text = str(value or "medium").strip().lower()
    if text in VALID_SEVERITIES:
        return text
    if text in {"blocker", "fatal"}:
        return "critical"
    if text in {"major", "serious"}:
        return "high"
    if text in {"minor", "warning"}:
        return "low"
    return "medium"


def _section_ids(row: dict[str, Any], valid_ids: set[str]) -> list[str]:
    values: list[Any] = []
    for key in ("section_ids", "affected_section_ids"):
        raw = row.get(key)
        if isinstance(raw, list):
            values.extend(raw)
    for key in ("section_id", "target_id"):
        if row.get(key):
            values.append(row.get(key))
    text = " ".join(str(row.get(k) or "") for k in (
        "description", "recommended_action", "proposed_change", "target_id"
    ))
    values.extend(re.findall(r"\bS\d{1,3}\b", text, re.I))
    return _unique_strings(
        str(value).upper() for value in values if str(value).upper() in valid_ids
    )


def _claim_ids(row: dict[str, Any], valid_ids: set[str]) -> list[str]:
    values: list[Any] = []
    raw = row.get("claim_ids")
    if isinstance(raw, list):
        values.extend(raw)
    for key in ("claim_id", "target_id"):
        if row.get(key):
            values.append(row.get(key))
    text = " ".join(str(row.get(k) or "") for k in (
        "description", "recommended_action", "proposed_change", "target_id"
    ))
    values.extend(re.findall(r"\bS\d{1,3}-C\d{1,3}\b", text, re.I))
    return _unique_strings(
        str(value).upper() for value in values if str(value).upper() in valid_ids
    )


def _classify_issue(raw_type: Any, description: str, action: str) -> tuple[str, str, str]:
    raw = str(raw_type or "").strip().lower().replace("-", "_").replace(" ", "_")
    text = f"{raw} {description} {action}".lower()
    quantitative_visual = any(term in text for term in (
        "spectrum", "spectral curve", "quantitative plot", "data plot", "benchmark plot",
        "measurement plot", "experimental image", "micrograph", "source figure", "replot",
    ))
    if any(term in text for term in (
        "llm call failed", "judge failed", "returned invalid output", "reviewer returned invalid",
        "process error", "review process failed",
    )):
        return "review_process_error", "review_process", "reviewer_retry"
    if any(term in text for term in (
        "word budget", "under the allocation", "below its word", "shortest section",
        "compressed treatment", "expand the section", "section is too short",
    )) and not any(term in text for term in (
        "uncited", "missing citation", "unsupported fact", "citation linkage",
    )):
        return "coverage_or_depth", "writing", "section_local_rewrite"
    if raw in {"citation", "evidence", "missing_evidence", "evidence_gap"} or any(
        term in text for term in ("uncited", "missing citation", "citation linkage")
    ):
        return "evidence_gap", "missing_evidence", "evidence_retrieval"
    if raw in {"unsupported_claim", "overclaim", "unsupported_fact", "numeric_audit"} or any(
        term in text for term in ("unsupported", "unsubstantiated", "invented precision")
    ):
        return "unsupported_claim", "claim_overreach", "claim_narrowing"
    if raw in {"visual", "figure", "visual_gap"} or any(
        term in text for term in ("missing visual", "missing figure", "schematic", "diagram")
    ):
        if quantitative_visual:
            return "visual_empirical_gap", "visual_empirical", "empirical_visual_source_task"
        return "visual_conceptual_gap", "visual_conceptual", "conceptual_visual_generation"
    if raw in {"redundancy", "repetition", "style", "wording", "verbosity"}:
        return "redundancy_or_style", "writing", "section_local_rewrite"
    if raw in {"transition", "coherence", "cross_section_coherence"} or any(
        term in text for term in ("cross-section", "transition", "section ownership")
    ):
        return "cross_section_coherence", "writing", "cross_section_edit"
    if raw in {"methodology", "method_identity", "review_method"} or any(
        term in text for term in ("systematic review", "search strategy", "inclusion criteria")
    ):
        return "methodology_identity", "methodology_identity", "charter_method_patch"
    if raw in {"taxonomy", "structure", "architecture", "insufficient_novelty", "novel_synthesis"}:
        return "argument_architecture", "architecture", "blueprint_contract_patch"
    if raw in {"scope", "scope_drift", "rights", "publication_strategy"}:
        return "scope_or_external", "scope_or_external", "human_decision"
    if raw in {"mechanism", "comparison", "argument_gap", "argument"}:
        return "argument_gap", "missing_evidence", "evidence_retrieval"
    return "writing_or_argument", "writing", "section_local_rewrite"


@dataclass
class ReviewIssue:
    issue_id: str
    severity: str
    issue_type: str
    root_cause: str
    repair_route: str
    section_ids: list[str] = field(default_factory=list)
    claim_ids: list[str] = field(default_factory=list)
    description: str = ""
    requested_change: str = ""
    source_refs: list[dict[str, str]] = field(default_factory=list)
    load_bearing: bool = False
    confidence: str = "medium"
    status: str = "open"
    recurrence_count: int = 1

    def to_dict(self) -> dict[str, Any]:
        return {
            "issue_id": self.issue_id,
            "severity": self.severity,
            "issue_type": self.issue_type,
            "root_cause": self.root_cause,
            "repair_route": self.repair_route,
            "section_ids": self.section_ids,
            "claim_ids": self.claim_ids,
            "description": self.description,
            "requested_change": self.requested_change,
            "source_refs": self.source_refs,
            "load_bearing": self.load_bearing,
            "confidence": self.confidence,
            "status": self.status,
            "recurrence_count": self.recurrence_count,
        }


@dataclass
class RevisionTask:
    task_id: str
    issue_ids: list[str]
    root_cause: str
    repair_route: str
    section_ids: list[str] = field(default_factory=list)
    claim_ids: list[str] = field(default_factory=list)
    priority: str = "medium"
    auto_apply: bool = False
    repair_instruction: str = ""
    success_test: str = ""
    rationale: str = ""
    status: str = "planned"

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "issue_ids": self.issue_ids,
            "root_cause": self.root_cause,
            "repair_route": self.repair_route,
            "section_ids": self.section_ids,
            "claim_ids": self.claim_ids,
            "priority": self.priority,
            "auto_apply": self.auto_apply,
            "repair_instruction": self.repair_instruction,
            "success_test": self.success_test,
            "rationale": self.rationale,
            "status": self.status,
        }


class ReviewerIssueCompiler:
    """Merge heterogeneous reviews into one traceable, deduplicated issue ledger."""

    def compile(
        self,
        *,
        revision_bundle: dict[str, Any],
        global_bundle: dict[str, Any],
        peer_bundle: dict[str, Any],
        supervisor_bundle: dict[str, Any] | None = None,
        charter: dict[str, Any] | None = None,
        previous_issues: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        blueprint = revision_bundle.get("blueprint") or {}
        sections = [row for row in (blueprint.get("sections") or []) if isinstance(row, dict)]
        valid_sections = {
            str(row.get("section_id") or "").upper() for row in sections if row.get("section_id")
        }
        valid_claims = {
            str(claim.get("claim_id") or "").upper()
            for section in sections for claim in (section.get("claims") or [])
            if isinstance(claim, dict) and claim.get("claim_id")
        }
        load_bearing = {
            str(claim.get("claim_id") or "").upper()
            for section in sections for claim in (section.get("claims") or [])
            if isinstance(claim, dict) and claim.get("load_bearing")
        }
        raw_rows: list[tuple[str, str, dict[str, Any]]] = []

        supervisor_bundle = supervisor_bundle or {}
        unhandled_ids = {
            str(row.get("suggestion_id") or "")
            for row in (revision_bundle.get("unhandled_accepted_suggestions") or [])
            if isinstance(row, dict)
        }
        for row in (supervisor_bundle.get("suggestions") or []):
            if not isinstance(row, dict):
                continue
            status = str(row.get("status") or "pending")
            if status == "rejected" or (status == "accepted" and row.get("suggestion_id") not in unhandled_ids):
                continue
            raw_rows.append(("supervisor", "scientific_supervisor", row))

        judgment = global_bundle.get("judgment") or {}
        for row in (judgment.get("issues") or []):
            if isinstance(row, dict):
                raw_rows.append(("global_review", "global_editor", row))
        for review in (peer_bundle.get("peer_reviews") or []):
            if not isinstance(review, dict):
                continue
            role = str(review.get("reviewer_role") or "peer_reviewer")
            for row in (review.get("issues") or []):
                if isinstance(row, dict):
                    raw_rows.append(("peer_review", role, row))

        post_audit = global_bundle.get("post_revision_citation_audit") or {}
        for row in (post_audit.get("citation_audits") or []):
            if not isinstance(row, dict):
                continue
            sid = str(row.get("section_id") or "").upper()
            invalid = list(row.get("invalid_cited_chunk_ids") or [])
            uncited = list(row.get("uncited_load_bearing_claim_ids") or [])
            rejected = list(row.get("uncited_after_entailment_rejection") or [])
            if invalid:
                raw_rows.append(("deterministic_audit", "citation_integrity_gate", {
                    "issue_id": f"AUDIT-{sid}-INVALID",
                    "severity": "critical",
                    "section_ids": [sid],
                    "issue_type": "citation",
                    "description": f"Invalid canonical chunk identifiers remain: {', '.join(map(str, invalid[:8]))}.",
                    "recommended_action": "Remove invalid markers or bind them to verified canonical chunks.",
                }))
            if uncited:
                raw_rows.append(("deterministic_audit", "citation_integrity_gate", {
                    "issue_id": f"AUDIT-{sid}-UNCITED",
                    "severity": "high",
                    "section_ids": [sid],
                    "claim_ids": uncited,
                    "issue_type": "evidence_gap",
                    "description": "Load-bearing claims remain without verified citation support.",
                    "recommended_action": "Retrieve component support or narrow the affected claims.",
                }))
            if rejected or bool((row.get("section_quality_judgment") or {}).get("unsupported_fact_detected")):
                raw_rows.append(("deterministic_audit", "entailment_gate", {
                    "issue_id": f"AUDIT-{sid}-UNSUPPORTED",
                    "severity": "high",
                    "section_ids": [sid],
                    "issue_type": "unsupported_claim",
                    "description": "At least one factual statement failed citation entailment or the unsupported-fact gate.",
                    "recommended_action": "Find direct/component evidence or narrow the statement without losing the section argument.",
                }))

        raw_rows.extend(self._method_identity_issue(revision_bundle, charter or {}, valid_sections))
        raw_rows.extend(self._open_question_density_issues(revision_bundle, valid_sections))
        normalized: list[ReviewIssue] = []
        for source, role, row in raw_rows:
            description = _compact(row.get("description") or row.get("issue") or row.get("proposed_change"), 1800)
            action = _compact(row.get("recommended_action") or row.get("proposed_change"), 1600)
            if not description:
                continue
            issue_type, root, route = _classify_issue(row.get("issue_type") or row.get("category"), description, action)
            sections_for_issue = _section_ids(row, valid_sections)
            claims_for_issue = _claim_ids(row, valid_claims)
            source_id = str(row.get("issue_id") or row.get("suggestion_id") or "")
            stable = _stable_id("RI", issue_type, ",".join(sections_for_issue), description)
            normalized.append(ReviewIssue(
                issue_id=stable,
                severity=_normalize_severity(row.get("severity")),
                issue_type=issue_type,
                root_cause=root,
                repair_route=route,
                section_ids=sections_for_issue,
                claim_ids=claims_for_issue,
                description=description,
                requested_change=action,
                source_refs=[{"source": source, "reviewer_role": role, "source_issue_id": source_id}],
                load_bearing=bool(set(claims_for_issue) & load_bearing),
                confidence="high" if source == "deterministic_audit" else "medium",
            ))
        merged = self._deduplicate(normalized)
        previous = {str(row.get("issue_id") or ""): row for row in (previous_issues or [])}
        for issue in merged:
            if issue.issue_id in previous:
                issue.recurrence_count = int(previous[issue.issue_id].get("recurrence_count") or 1) + 1
                continue
            semantic_previous = next((
                row for row in (previous_issues or [])
                if isinstance(row, dict)
                and str(row.get("root_cause") or "") == issue.root_cause
                and (
                    set(row.get("section_ids") or []) == set(issue.section_ids)
                    or bool(set(row.get("section_ids") or []) & set(issue.section_ids))
                )
                and _similarity(row.get("description"), issue.description) >= 0.22
            ), None)
            if semantic_previous:
                issue.recurrence_count = int(
                    semantic_previous.get("recurrence_count") or 1
                ) + 1
        severity_distribution = {
            severity: sum(issue.severity == severity for issue in merged)
            for severity in ("critical", "high", "medium", "low")
        }
        return {
            "schema_version": "publication_revision.issue_ledger.v1",
            "created_at": _utc_now(),
            "issues": [issue.to_dict() for issue in merged],
            "raw_issue_count": len(normalized),
            "deduplicated_issue_count": len(merged),
            "severity_distribution": severity_distribution,
            "root_cause_distribution": {
                root: sum(issue.root_cause == root for issue in merged)
                for root in sorted(VALID_ROOT_CAUSES)
            },
            "blocking_issue_ids": [
                issue.issue_id for issue in merged if issue.severity == "critical"
            ],
        }

    @staticmethod
    def _deduplicate(issues: list[ReviewIssue]) -> list[ReviewIssue]:
        ordered = sorted(issues, key=lambda row: SEVERITY_RANK[row.severity], reverse=True)
        result: list[ReviewIssue] = []
        for issue in ordered:
            match: ReviewIssue | None = None
            for current in result:
                same_root = current.root_cause == issue.root_cause
                section_overlap = bool(set(current.section_ids) & set(issue.section_ids))
                same_scope = current.section_ids == issue.section_ids or section_overlap
                claim_overlap = bool(set(current.claim_ids) & set(issue.claim_ids))
                if same_root and same_scope and (claim_overlap or _similarity(
                    f"{current.description} {current.requested_change}",
                    f"{issue.description} {issue.requested_change}",
                ) >= 0.20):
                    match = current
                    break
            if match is None:
                result.append(issue)
                continue
            if SEVERITY_RANK[issue.severity] > SEVERITY_RANK[match.severity]:
                match.severity = issue.severity
            match.section_ids = _unique_strings(match.section_ids + issue.section_ids)
            match.claim_ids = _unique_strings(match.claim_ids + issue.claim_ids)
            match.source_refs.extend(
                row for row in issue.source_refs if row not in match.source_refs
            )
            match.load_bearing = match.load_bearing or issue.load_bearing
            if len(issue.requested_change) > len(match.requested_change):
                match.requested_change = issue.requested_change
            match.issue_id = _stable_id(
                "RI", match.issue_type, ",".join(sorted(match.section_ids)), match.description
            )
        return result

    @staticmethod
    def _method_identity_issue(
        revision_bundle: dict[str, Any], charter: dict[str, Any], valid_sections: set[str]
    ) -> list[tuple[str, str, dict[str, Any]]]:
        text = "\n".join(
            str(row.get("english_text") or "")
            for row in (revision_bundle.get("section_drafts") or []) if isinstance(row, dict)
        )
        claims_systematic = bool(re.search(
            r"\b(?:systematic review|systematically searched|systematic evidence review)\b",
            text, re.I,
        ))
        protocol_keys = {
            "search_strategy", "databases", "inclusion_criteria", "exclusion_criteria",
            "screening", "quality_assessment", "data_extraction",
        }
        documented = sum(bool(charter.get(key)) for key in protocol_keys)
        if not claims_systematic or documented >= 5:
            return []
        intro = [sid for sid in sorted(valid_sections)[:1]]
        return [("deterministic_audit", "method_identity_gate", {
            "issue_id": "AUDIT-METHOD-IDENTITY",
            "severity": "high",
            "section_ids": intro,
            "issue_type": "method_identity",
            "description": (
                "The manuscript claims a systematic review identity, but the charter does not "
                "document a reproducible search, screening, quality-assessment, and extraction protocol."
            ),
            "recommended_action": (
                "Either add the missing reproducible method protocol or describe the work as a "
                "critical narrative review or cross-disciplinary perspective."
            ),
        })]

    @staticmethod
    def _open_question_density_issues(
        revision_bundle: dict[str, Any], valid_sections: set[str]
    ) -> list[tuple[str, str, dict[str, Any]]]:
        result: list[tuple[str, str, dict[str, Any]]] = []
        pattern = re.compile(
            r"\b(?:remains? (?:unknown|unclear|unresolved)|open question|requires? further|"
            r"has not been (?:established|resolved)|future work is needed)\b",
            re.I,
        )
        for row in (revision_bundle.get("section_drafts") or []):
            if not isinstance(row, dict):
                continue
            sid = str(row.get("section_id") or "").upper()
            if sid not in valid_sections:
                continue
            sentences = [s for s in re.split(r"(?<=[.!?])\s+", str(row.get("english_text") or "")) if s.strip()]
            count = sum(bool(pattern.search(sentence)) for sentence in sentences)
            if len(sentences) >= 16 and count >= 5 and count / len(sentences) >= 0.22:
                result.append(("deterministic_audit", "editorial_density_gate", {
                    "issue_id": f"AUDIT-{sid}-OPEN-DENSITY",
                    "severity": "medium",
                    "section_ids": [sid],
                    "issue_type": "redundancy",
                    "description": (
                        f"{count} of {len(sentences)} sentences frame unresolved questions; "
                        "the section may be deferring synthesis instead of distinguishing supported, conditional, and open conclusions."
                    ),
                    "recommended_action": (
                        "Consolidate repeated uncertainty statements and make the evidence-supported judgment explicit."
                    ),
                }))
        return result


class RevisionDecisionBoard:
    """Choose the smallest safe repair route; LLM advice is bounded by policy."""

    def __init__(self, *, real_llm: bool, model_tier: str = "premium_model") -> None:
        self.real_llm = bool(real_llm)
        self.model_tier = model_tier
        self.last_llm_audit: dict[str, Any] = {
            "requested": bool(real_llm),
            "status": "not_run",
        }

    def plan(
        self,
        issue_report: dict[str, Any],
        *,
        charter: dict[str, Any],
        max_tasks: int = 8,
    ) -> dict[str, Any]:
        issues = [row for row in (issue_report.get("issues") or []) if isinstance(row, dict)]
        ranked = sorted(
            issues,
            key=lambda row: (
                SEVERITY_RANK.get(str(row.get("severity") or "medium"), 2),
                bool(row.get("load_bearing")),
                int(row.get("recurrence_count") or 1),
            ),
            reverse=True,
        )
        board_input = self._cluster_issues_for_board(ranked)[: max(16, max_tasks * 2)]
        proposed = self._llm_plan(board_input, charter, max_tasks) if self.real_llm else []
        tasks = self._sanitize_llm_tasks(proposed, ranked, max_tasks)
        covered = {issue_id for task in tasks for issue_id in task.issue_ids}
        for issue in ranked:
            issue_id = str(issue.get("issue_id") or "")
            if issue_id in covered:
                continue
            tasks.append(self._fallback_task(issue))
            covered.add(issue_id)
        tasks = self._select_diverse_tasks(self._coalesce_tasks(tasks), max_tasks)
        covered = {issue_id for task in tasks for issue_id in task.issue_ids}
        issue_ids = {str(row.get("issue_id") or "") for row in ranked}
        deferred = sorted(issue_ids - covered)
        human = sorted({
            issue_id for task in tasks if not task.auto_apply for issue_id in task.issue_ids
        })
        return {
            "schema_version": "publication_revision.plan.v1",
            "created_at": _utc_now(),
            "tasks": [task.to_dict() for task in tasks],
            "deferred_issue_ids": deferred,
            "human_decision_issue_ids": human,
            "auto_task_count": sum(task.auto_apply for task in tasks),
            "human_task_count": sum(not task.auto_apply for task in tasks),
            "round_strategy": (
                "Repair evidence integrity first, then local argument/writing defects, then "
                "cross-section coherence; defer scope and empirical-visual decisions to humans."
            ),
            "planner_mode": (
                "premium_llm_with_deterministic_policy"
                if proposed else "deterministic_fail_closed_fallback"
            ),
            "planner_llm_audit": self.last_llm_audit,
        }

    @staticmethod
    def _cluster_issues_for_board(issues: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Compress repeated reviewer findings before the expensive planning call."""
        groups: dict[tuple[str, tuple[str, ...]], dict[str, Any]] = {}
        for row in issues:
            key = (
                str(row.get("repair_route") or "human_decision"),
                tuple(sorted(str(sid) for sid in (row.get("section_ids") or []))),
            )
            current = groups.get(key)
            if current is None:
                groups[key] = {
                    "issue_ids": [str(row.get("issue_id") or "")],
                    "severity": str(row.get("severity") or "medium"),
                    "issue_type": str(row.get("issue_type") or ""),
                    "root_cause": str(row.get("root_cause") or ""),
                    "repair_route": str(row.get("repair_route") or "human_decision"),
                    "section_ids": list(row.get("section_ids") or []),
                    "claim_ids": list(row.get("claim_ids") or []),
                    "load_bearing": bool(row.get("load_bearing")),
                    "recurrence_count": int(row.get("recurrence_count") or 1),
                    "descriptions": [_compact(row.get("description"), 500)],
                    "requested_changes": [_compact(row.get("requested_change"), 450)],
                    "independent_source_count": len(row.get("source_refs") or []),
                }
                continue
            current["issue_ids"] = _unique_strings(
                current["issue_ids"] + [row.get("issue_id")]
            )
            current["claim_ids"] = _unique_strings(
                current["claim_ids"] + list(row.get("claim_ids") or [])
            )
            current["load_bearing"] = current["load_bearing"] or bool(row.get("load_bearing"))
            current["recurrence_count"] = max(
                int(current["recurrence_count"]), int(row.get("recurrence_count") or 1)
            )
            current["independent_source_count"] += len(row.get("source_refs") or [])
            if SEVERITY_RANK.get(str(row.get("severity")), 2) > SEVERITY_RANK.get(
                str(current["severity"]), 2
            ):
                current["severity"] = str(row.get("severity"))
            if len(current["descriptions"]) < 3:
                current["descriptions"].append(_compact(row.get("description"), 500))
            if len(current["requested_changes"]) < 3:
                current["requested_changes"].append(_compact(row.get("requested_change"), 450))
        return sorted(
            groups.values(),
            key=lambda row: (
                SEVERITY_RANK.get(str(row.get("severity")), 2),
                bool(row.get("load_bearing")),
                len(row.get("issue_ids") or []),
            ),
            reverse=True,
        )

    @staticmethod
    def _coalesce_tasks(tasks: list[RevisionTask]) -> list[RevisionTask]:
        """Combine repeated reviewer findings that require the same bounded action."""
        grouped: dict[tuple[str, tuple[str, ...]], RevisionTask] = {}
        for task in tasks:
            key = (task.repair_route, tuple(sorted(task.section_ids)))
            current = grouped.get(key)
            if current is None:
                grouped[key] = copy.deepcopy(task)
                continue
            current.issue_ids = _unique_strings(current.issue_ids + task.issue_ids)
            current.claim_ids = _unique_strings(current.claim_ids + task.claim_ids)
            if SEVERITY_RANK[task.priority] > SEVERITY_RANK[current.priority]:
                current.priority = task.priority
            current.auto_apply = current.auto_apply and task.auto_apply
            current.repair_instruction = _compact(
                f"{current.repair_instruction} {task.repair_instruction}", 2200
            )
            current.success_test = _compact(
                f"{current.success_test} {task.success_test}", 1200
            )
            current.task_id = _stable_id(
                "RT", ",".join(sorted(current.issue_ids)), current.repair_route
            )
        return list(grouped.values())

    @staticmethod
    def _select_diverse_tasks(tasks: list[RevisionTask], max_tasks: int) -> list[RevisionTask]:
        """Keep high-severity work while preventing one repair route from consuming the round."""
        ordered = sorted(
            tasks,
            key=lambda task: (
                SEVERITY_RANK.get(task.priority, 2),
                len(task.issue_ids),
                task.auto_apply,
            ),
            reverse=True,
        )
        route_caps = {
            "evidence_retrieval": max(3, max_tasks // 2),
            "claim_narrowing": 2,
            "section_local_rewrite": 2,
            "cross_section_edit": 1,
            "blueprint_contract_patch": 1,
            "charter_method_patch": 1,
            "conceptual_visual_generation": 1,
            "empirical_visual_source_task": 1,
            "human_decision": 2,
            "reviewer_retry": 1,
        }
        counts: dict[str, int] = {}
        selected: list[RevisionTask] = []
        # Critical integrity failures are never displaced by diversity caps.
        for task in ordered:
            if task.priority == "critical" and len(selected) < max_tasks:
                selected.append(task)
                counts[task.repair_route] = counts.get(task.repair_route, 0) + 1
        for task in ordered:
            if task in selected or len(selected) >= max_tasks:
                continue
            cap = route_caps.get(task.repair_route, 1)
            if counts.get(task.repair_route, 0) >= cap:
                continue
            selected.append(task)
            counts[task.repair_route] = counts.get(task.repair_route, 0) + 1
        return selected

    def _llm_plan(
        self, issues: list[dict[str, Any]], charter: dict[str, Any], max_tasks: int
    ) -> list[dict[str, Any]]:
        payload = {
            "review_charter": {
                "central_question": charter.get("central_question", ""),
                "scope_statement": charter.get("scope_statement", ""),
                "constraints": charter.get("constraints") or {},
            },
            "normalized_issue_clusters": issues,
            "max_tasks_this_round": max_tasks,
        }
        attempts: list[dict[str, Any]] = []
        # This is a bounded issue-clustering task, not manuscript synthesis.
        # Long chain-of-thought streaming can keep a whole revision round
        # blocked even though deterministic route metadata already provides a
        # safe fallback plan.  Retain an A-tier first pass and one B+ fallback,
        # but request concise non-thinking JSON under a hard transport budget.
        tiers: list[tuple[str, float]] = [(self.model_tier, 120.0)]
        for fallback_tier in ("b_plus_model",):
            if fallback_tier != self.model_tier:
                tiers.append((fallback_tier, 150.0))
        for tier, timeout in tiers:
            try:
                result = call_qwen_chat(
                    f"RevisionDecisionBoard:{tier}",
                    [
                        {"role": "system", "content": DECISION_BOARD_PROMPT.read_text(encoding="utf-8")},
                        {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
                    ],
                    model_tier=tier,
                    temperature=0,
                    max_tokens=2200,
                    response_format={"type": "json_object"},
                    stream=False,
                    force_mock=False,
                    max_retries=0,
                    timeout_seconds=timeout,
                    max_transport_key_candidates=1,
                    allow_model_fallback=False,
                    enable_thinking=False,
                )
                content = str(result.get("content") or "")
                parsed = _safe_json(content)
                tasks = [row for row in (parsed.get("tasks") or []) if isinstance(row, dict)]
                status = "success"
                if not tasks:
                    tasks = _salvage_complete_tasks(content)
                    status = "partial_json_salvaged" if tasks else "invalid_or_empty_output"
                attempt = {
                    "tier": tier,
                    "status": status,
                    "usage": dict(result.get("_llm_usage") or {}),
                    "task_count_returned": len(tasks),
                }
                attempts.append(attempt)
                if tasks:
                    self.last_llm_audit = {
                        "requested": True,
                        "status": status,
                        "selected_tier": tier,
                        "attempts": attempts,
                        "cluster_count_sent": len(issues),
                        "task_count_returned": len(tasks),
                    }
                    return tasks
            except Exception as exc:
                attempts.append({
                    "tier": tier,
                    "status": "exception",
                    "error": f"{type(exc).__name__}: {exc}",
                })
        self.last_llm_audit = {
            "requested": True,
            "status": "all_model_attempts_failed",
            "attempts": attempts,
            "cluster_count_sent": len(issues),
            "task_count_returned": 0,
        }
        return []

    def _sanitize_llm_tasks(
        self,
        rows: list[dict[str, Any]],
        issues: list[dict[str, Any]],
        max_tasks: int,
    ) -> list[RevisionTask]:
        by_id = {str(row.get("issue_id") or ""): row for row in issues}
        result: list[RevisionTask] = []
        for row in rows[:max_tasks]:
            ids = _unique_strings(
                issue_id for issue_id in (row.get("issue_ids") or []) if issue_id in by_id
            )
            if not ids:
                continue
            primary = max((by_id[i] for i in ids), key=lambda issue: SEVERITY_RANK.get(str(issue.get("severity")), 2))
            route = str(row.get("repair_route") or primary.get("repair_route") or "human_decision")
            root = str(row.get("root_cause") or primary.get("root_cause") or "scope_or_external")
            if route not in VALID_REPAIR_ROUTES or root not in VALID_ROOT_CAUSES:
                route, root = str(primary.get("repair_route")), str(primary.get("root_cause"))
            auto = self._auto_policy(route, root, str(primary.get("severity") or "medium"), row)
            result.append(RevisionTask(
                task_id=_stable_id("RT", ",".join(ids), route),
                issue_ids=ids,
                root_cause=root,
                repair_route=route,
                section_ids=_unique_strings(
                    sid for issue_id in ids for sid in (by_id[issue_id].get("section_ids") or [])
                ),
                claim_ids=_unique_strings(
                    cid for issue_id in ids for cid in (by_id[issue_id].get("claim_ids") or [])
                ),
                priority=_normalize_severity(row.get("priority") or primary.get("severity")),
                auto_apply=auto,
                repair_instruction=_compact(row.get("repair_instruction") or primary.get("requested_change"), 1600),
                success_test=_compact(row.get("success_test"), 800),
                rationale=_compact(row.get("rationale"), 800),
            ))
        return result

    def _fallback_task(self, issue: dict[str, Any]) -> RevisionTask:
        route = str(issue.get("repair_route") or "human_decision")
        root = str(issue.get("root_cause") or "scope_or_external")
        severity = _normalize_severity(issue.get("severity"))
        issue_id = str(issue.get("issue_id") or "")
        return RevisionTask(
            task_id=_stable_id("RT", issue_id, route),
            issue_ids=[issue_id],
            root_cause=root,
            repair_route=route,
            section_ids=list(issue.get("section_ids") or []),
            claim_ids=list(issue.get("claim_ids") or []),
            priority=severity,
            auto_apply=self._auto_policy(route, root, severity, {}),
            repair_instruction=_compact(issue.get("requested_change") or issue.get("description"), 1600),
            success_test=self._default_success_test(route),
            rationale="Deterministic minimum-change fallback plan.",
        )

    @staticmethod
    def _auto_policy(route: str, root: str, severity: str, proposal: dict[str, Any]) -> bool:
        if route not in SAFE_AUTO_ROUTES:
            return False
        if root in {"scope_or_external", "visual_empirical"}:
            return False
        if root == "architecture" and severity in {"high", "critical"}:
            return False
        if severity == "critical" and route not in {
            "evidence_retrieval", "claim_narrowing", "reviewer_retry"
        }:
            return False
        # Retrieval and reviewer retry are exploratory, reversible operations.
        # Their outputs still face provenance/entailment gates before any prose
        # can use them, so an LLM may not unnecessarily turn them into a human
        # bottleneck.
        if route in {"evidence_retrieval", "reviewer_retry"}:
            return True
        # The model may recommend human review, but it may never broaden the deterministic auto policy.
        if proposal and proposal.get("auto_apply_recommended") is False:
            return False
        return True

    @staticmethod
    def _default_success_test(route: str) -> str:
        return {
            "evidence_retrieval": "New support passes entailment/provenance checks or the claim is explicitly narrowed.",
            "claim_narrowing": "No unsupported factual or numerical clause remains.",
            "section_local_rewrite": "The named issue is removed without citation or information regression.",
            "cross_section_edit": "Ownership, transitions, and repetition improve without changing valid claims.",
            "conceptual_visual_generation": "A labelled, reviewed schematic is available and is not treated as empirical evidence.",
            "empirical_visual_source_task": "A traceable source figure or data replot is supplied.",
            "reviewer_retry": "The failed reviewer role returns valid structured output on retry.",
        }.get(route, "A human records an explicit disposition.")


class TargetedRevisionExecutor:
    """Execute safe tasks and roll back any section that fails post-revision gates."""

    def __init__(
        self,
        *,
        real_llm: bool,
        kb_path: Path | str | None,
        enable_external_oa: bool,
        external_output_dir: Path,
        max_external_rounds: int,
        max_external_claims: int,
        generate_conceptual_visuals: bool,
        max_generated_visuals: int,
        checkpoint_dir: Path | None = None,
        resume: bool = True,
    ) -> None:
        self.real_llm = bool(real_llm)
        self.kb_path = Path(kb_path) if kb_path else None
        self.enable_external_oa = bool(enable_external_oa)
        self.external_output_dir = Path(external_output_dir)
        self.max_external_rounds = max(0, int(max_external_rounds))
        self.max_external_claims = max(1, int(max_external_claims))
        self.generate_conceptual_visuals = bool(generate_conceptual_visuals)
        self.max_generated_visuals = max(0, int(max_generated_visuals))
        self.checkpoint_dir = Path(checkpoint_dir) if checkpoint_dir else None
        self.resume = bool(resume)

    def _progress(self, stage: str, status: str, **details: Any) -> None:
        if self.checkpoint_dir is None:
            return
        _atomic_json(self.checkpoint_dir / "execution_progress.json", {
            "schema_version": "publication_revision.execution_progress.v1",
            "updated_at": _utc_now(),
            "stage": stage,
            "status": status,
            "implementation_version": REVISION_LOOP_IMPLEMENTATION_VERSION,
            "details": details,
        })

    @staticmethod
    def _rewrite_section_ids_for_task(
        task: dict[str, Any], blueprint: dict[str, Any]
    ) -> list[str]:
        """Return the smallest auditable section set a task may rewrite.

        Evidence and claim-calibration tasks often inherit a broad list of
        sections from a reviewer cluster while naming only one or two concrete
        claims.  Rewriting every listed section would turn a local evidence
        repair into an uncontrolled manuscript-wide edit.  Such routes may
        rewrite only sections that own an explicit claim ID.  A broad task
        without claim IDs remains useful for retrieval/human triage, but is
        not authorized to mutate prose automatically.
        """
        route = str(task.get("repair_route") or "")
        listed_sections = _unique_strings(task.get("section_ids") or [])
        if route not in {"evidence_retrieval", "claim_narrowing"}:
            return listed_sections
        claim_ids = set(_unique_strings(task.get("claim_ids") or []))
        if not claim_ids:
            return []
        owners: list[str] = []
        for section in (blueprint.get("sections") or []):
            if not isinstance(section, dict):
                continue
            section_id = str(section.get("section_id") or "")
            if any(
                str(claim.get("claim_id") or "") in claim_ids
                for claim in (section.get("claims") or [])
                if isinstance(claim, dict)
            ):
                owners.append(section_id)
        return _unique_strings(owners)

    def _save_checkpoint(
        self,
        *,
        plan_fingerprint: str,
        stage: str,
        working: dict[str, Any],
        working_contracts: list[dict[str, Any]],
        execution_log: list[dict[str, Any]],
        human_tasks: list[dict[str, Any]],
        completed_routes: set[str],
        completed_section_ids: set[str],
        derived_rewrite_tasks: list[dict[str, Any]],
        status: str = "in_progress",
        citation_audit: dict[str, Any] | None = None,
        rolled_back_section_ids: list[str] | None = None,
    ) -> None:
        if self.checkpoint_dir is None:
            return
        _atomic_json(self.checkpoint_dir / "execution_checkpoint.json", {
            "schema_version": "publication_revision.execution_checkpoint.v1",
            "updated_at": _utc_now(),
            "status": status,
            "implementation_version": REVISION_LOOP_IMPLEMENTATION_VERSION,
            "last_completed_stage": stage,
            "plan_fingerprint": plan_fingerprint,
            "working_bundle": working,
            "working_contracts": working_contracts,
            "execution_log": execution_log,
            "human_tasks": human_tasks,
            "completed_routes": sorted(completed_routes),
            "completed_section_ids": sorted(completed_section_ids),
            "derived_rewrite_tasks": derived_rewrite_tasks,
            "citation_audit": citation_audit or {},
            "rolled_back_section_ids": rolled_back_section_ids or [],
        })

    def _load_checkpoint(self, plan_fingerprint: str) -> dict[str, Any]:
        if not self.resume or self.checkpoint_dir is None:
            return {}
        path = self.checkpoint_dir / "execution_checkpoint.json"
        if not path.exists():
            return {}
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return {}
        if str(data.get("plan_fingerprint") or "") != plan_fingerprint:
            return {}
        checkpoint_version = str(data.get("implementation_version") or "")
        if checkpoint_version != REVISION_LOOP_IMPLEMENTATION_VERSION:
            targeted_logs = [
                row for row in (data.get("execution_log") or [])
                if isinstance(row, dict) and row.get("route") == "targeted_section_revision"
            ]
            rolled_back_ids = set(data.get("rolled_back_section_ids") or [])
            effective_text_changes = [
                row for row in targeted_logs
                if bool(row.get("text_changed"))
                and str(row.get("section_id") or "") not in rolled_back_ids
            ]
            safe_v11_migration = bool(
                checkpoint_version in {
                    "publication_revision_loop.v1.1",
                    "publication_revision_loop.v1.2",
                    "publication_revision_loop.v1.3",
                    "publication_revision_loop.v1.4",
                }
                and not effective_text_changes
                and "evidence_retrieval" in set(data.get("completed_routes") or [])
            )
            if not safe_v11_migration:
                return {}
            data["implementation_version"] = REVISION_LOOP_IMPLEMENTATION_VERSION
            data["status"] = "in_progress"
            data["completed_section_ids"] = []
            data["completed_routes"] = [
                route for route in (data.get("completed_routes") or [])
                if route not in {"targeted_section_revision", "execution_complete"}
            ]
            data["execution_log"] = [
                row for row in (data.get("execution_log") or [])
                if not (isinstance(row, dict) and row.get("route") == "targeted_section_revision")
            ]
            data["checkpoint_migration"] = {
                "from_version": checkpoint_version,
                "to_version": REVISION_LOOP_IMPLEMENTATION_VERSION,
                "reason": "reuse_verified_evidence_after_zero-text-change_revision_failure",
            }
        if not isinstance(data.get("working_bundle"), dict):
            return {}
        return data

    def execute(
        self,
        revision_bundle: dict[str, Any],
        plan: dict[str, Any],
        *,
        charter: dict[str, Any],
        contracts: list[dict[str, Any]],
        baseline_citation_bundle: dict[str, Any],
    ) -> dict[str, Any]:
        from optomind_research.full_review_evidence import resolve_evidence_gaps
        from optomind_research.full_review_production import (
            _review_text,
            audit_citations,
            draft_from_dict,
            draft_to_dict,
            edit_cross_section,
            packet_from_dict,
        )
        from optomind_research.review_writer import CitationBinder, OverclaimAuditor

        original = copy.deepcopy(revision_bundle)
        tasks = [row for row in (plan.get("tasks") or []) if isinstance(row, dict)]
        plan_fingerprint = _json_fingerprint(tasks)
        checkpoint = self._load_checkpoint(plan_fingerprint)
        working = copy.deepcopy(checkpoint.get("working_bundle") or revision_bundle)
        working_contracts = copy.deepcopy(checkpoint.get("working_contracts") or contracts)
        auto_tasks = [row for row in tasks if bool(row.get("auto_apply"))]
        human_tasks = list(checkpoint.get("human_tasks") or [
            row for row in tasks if not bool(row.get("auto_apply"))
        ])
        execution_log: list[dict[str, Any]] = list(checkpoint.get("execution_log") or [])
        completed_routes = set(checkpoint.get("completed_routes") or [])
        completed_section_ids = set(checkpoint.get("completed_section_ids") or [])
        derived_rewrite_tasks: list[dict[str, Any]] = list(
            checkpoint.get("derived_rewrite_tasks") or []
        )
        if checkpoint.get("status") == "completed" and checkpoint.get("citation_audit"):
            self._progress("execution_complete", "resumed_completed_checkpoint")
            return {
                "revision_bundle": working,
                "citation_audit": checkpoint.get("citation_audit") or {},
                "execution_log": execution_log,
                "rolled_back_section_ids": checkpoint.get("rolled_back_section_ids") or [],
                "human_tasks": human_tasks,
            }
        self._progress(
            "execution_resume" if checkpoint else "execution_start",
            "running",
            resumed=bool(checkpoint),
            completed_routes=sorted(completed_routes),
            completed_section_ids=sorted(completed_section_ids),
        )
        retry_tasks = [row for row in auto_tasks if row.get("repair_route") == "reviewer_retry"]
        if retry_tasks and "reviewer_retry" not in completed_routes:
            execution_log.append({
                "route": "reviewer_retry",
                "task_ids": [row.get("task_id") for row in retry_tasks],
                "status": "scheduled_for_post_revision_global_and_peer_rerun",
            })
            completed_routes.add("reviewer_retry")

        architecture_tasks = [
            row for row in auto_tasks if row.get("repair_route") == "blueprint_contract_patch"
        ]
        if architecture_tasks and self.real_llm and "blueprint_contract_patch" not in completed_routes:
            self._progress("blueprint_contract_patch", "running", task_count=len(architecture_tasks))
            architecture_patch = self._plan_architecture_patch(
                working.get("blueprint") or {},
                working_contracts,
                charter,
                architecture_tasks,
            )
            if architecture_patch.get("requires_human_decision"):
                human_tasks.extend(architecture_tasks)
                execution_log.append({
                    "route": "blueprint_contract_patch",
                    "status": "needs_human",
                    "reason": architecture_patch.get("reason") or "scope_or_membership_change_required",
                })
            else:
                applied, derived_rewrite_tasks = self._apply_architecture_patch(
                    working.get("blueprint") or {}, working_contracts, architecture_patch,
                    architecture_tasks,
                )
                execution_log.append({
                    "route": "blueprint_contract_patch",
                    "status": "applied" if applied else "rejected_invalid_patch",
                    "applied_section_ids": applied,
                    "reason": architecture_patch.get("reason") or "",
                })
            completed_routes.add("blueprint_contract_patch")
            self._save_checkpoint(
                plan_fingerprint=plan_fingerprint,
                stage="blueprint_contract_patch",
                working=working,
                working_contracts=working_contracts,
                execution_log=execution_log,
                human_tasks=human_tasks,
                completed_routes=completed_routes,
                completed_section_ids=completed_section_ids,
                derived_rewrite_tasks=derived_rewrite_tasks,
            )

        method_tasks = [
            row for row in auto_tasks if row.get("repair_route") == "charter_method_patch"
        ]
        if "charter_method_patch" not in completed_routes:
            for task in method_tasks:
                for sid in (task.get("section_ids") or []):
                    derived_rewrite_tasks.append({
                    **task,
                    "task_id": f"{task.get('task_id')}-method-wording",
                    "repair_route": "section_local_rewrite",
                    "repair_instruction": (
                        "Align the review-method identity with the documented process. Remove any "
                        "unsupported claim of a systematic review and describe the article as a "
                        "critical narrative review or cross-disciplinary perspective. Do not invent "
                        "search, screening, or quality-assessment procedures."
                    ),
                    "section_ids": [sid],
                    })
            if method_tasks:
                working["review_method_identity"] = "critical_narrative_review_or_perspective"
                execution_log.append({
                    "route": "charter_method_patch",
                    "status": "bounded_wording_alignment_scheduled",
                    "task_ids": [row.get("task_id") for row in method_tasks],
                })
                completed_routes.add("charter_method_patch")
                self._save_checkpoint(
                    plan_fingerprint=plan_fingerprint,
                    stage="charter_method_patch",
                    working=working,
                    working_contracts=working_contracts,
                    execution_log=execution_log,
                    human_tasks=human_tasks,
                    completed_routes=completed_routes,
                    completed_section_ids=completed_section_ids,
                    derived_rewrite_tasks=derived_rewrite_tasks,
                )
        auto_tasks.extend(derived_rewrite_tasks)
        working["revised_section_contracts"] = working_contracts

        evidence_tasks = [row for row in auto_tasks if row.get("repair_route") == "evidence_retrieval"]
        if evidence_tasks and self.real_llm and "evidence_retrieval" not in completed_routes:
            self._progress("evidence_retrieval", "running", task_count=len(evidence_tasks))
            target_claim_ids = _unique_strings(
                claim_id for task in evidence_tasks for claim_id in (task.get("claim_ids") or [])
            )
            if not target_claim_ids:
                target_sections = {
                    str(section_id)
                    for task in evidence_tasks for section_id in (task.get("section_ids") or [])
                }
                target_claim_ids = _unique_strings(
                    claim.get("claim_id")
                    for section in ((working.get("blueprint") or {}).get("sections") or [])
                    if str(section.get("section_id") or "") in target_sections
                    for claim in (section.get("claims") or [])
                    if isinstance(claim, dict) and claim.get("load_bearing") and claim.get("claim_id")
                )
            gap = resolve_evidence_gaps(
                working,
                kb_path=self.kb_path or working.get("kb_sqlite"),
                real_llm=True,
                scope_definition=str(charter.get("scope_statement") or ""),
                enable_external_oa=self.enable_external_oa,
                external_output_dir=self.external_output_dir / "evidence_retrieval",
                max_external_rounds=self.max_external_rounds,
                max_external_claims=self.max_external_claims,
                external_download_top_n=3,
                target_claim_ids=target_claim_ids or None,
                dag_update_mode="refresh_existing_graph",
                progress_callback=lambda event, details: self._progress(
                    "evidence_retrieval",
                    "running",
                    event=event,
                    **details,
                ),
            )
            working["blueprint"] = gap.get("blueprint") or working.get("blueprint")
            working["material_packets"] = gap.get("evidence_portfolios") or working.get("material_packets")
            working["kb_sqlite"] = gap.get("kb_sqlite") or working.get("kb_sqlite")
            execution_log.append({
                "route": "evidence_retrieval",
                "task_ids": [row.get("task_id") for row in evidence_tasks],
                "target_claim_ids": target_claim_ids,
                "stop_reason": gap.get("stop_reason"),
                "unresolved_load_bearing_claim_ids": gap.get("unresolved_load_bearing_claim_ids") or [],
                "remaining_missing_components": gap.get("remaining_missing_components") or {},
            })
            completed_routes.add("evidence_retrieval")
            self._save_checkpoint(
                plan_fingerprint=plan_fingerprint,
                stage="evidence_retrieval",
                working=working,
                working_contracts=working_contracts,
                execution_log=execution_log,
                human_tasks=human_tasks,
                completed_routes=completed_routes,
                completed_section_ids=completed_section_ids,
                derived_rewrite_tasks=derived_rewrite_tasks,
            )

        visual_tasks = [
            row for row in auto_tasks if row.get("repair_route") == "conceptual_visual_generation"
        ]
        if (
            visual_tasks and self.real_llm and self.generate_conceptual_visuals
            and "conceptual_visual_generation" not in completed_routes
        ):
            self._progress(
                "conceptual_visual_generation", "running", task_count=len(visual_tasks)
            )
            from optomind_research.conceptual_visual_generator import (
                generate_conceptual_visual_gaps,
            )
            target_sections = {
                str(section_id)
                for task in visual_tasks
                for section_id in (task.get("section_ids") or [])
                if section_id
            }
            packets = [
                row for row in (working.get("material_packets") or [])
                if isinstance(row, dict)
            ]
            target_plans = [
                dict(plan)
                for packet in packets
                if str(packet.get("section_id") or "") in target_sections
                for plan in (packet.get("visual_gap_plan") or [])
                if isinstance(plan, dict)
                and str(plan.get("creation_class") or "")
                == "author_synthesized_conceptual_schematic"
            ]
            generated_plans = generate_conceptual_visual_gaps(
                visual_gap_plan=target_plans,
                blueprint=working.get("blueprint") or {},
                output_dir=self.external_output_dir / "conceptual_visuals",
                real_llm=True,
                max_assets=min(
                    self.max_generated_visuals,
                    max(1, len(visual_tasks)),
                ),
            )
            generated_by_id = {
                str(row.get("visual_plan_id") or ""): row
                for row in generated_plans
                if isinstance(row, dict) and row.get("visual_plan_id")
            }
            for packet in packets:
                revised_plans = []
                for plan in (packet.get("visual_gap_plan") or []):
                    plan_id = str((plan or {}).get("visual_plan_id") or "")
                    revised_plans.append(generated_by_id.get(plan_id, plan))
                packet["visual_gap_plan"] = revised_plans
            working["material_packets"] = packets
            working["visual_gap_plan"] = [
                plan
                for packet in packets
                for plan in (packet.get("visual_gap_plan") or [])
                if isinstance(plan, dict)
            ]
            generation_summary = {
                "target_section_ids": sorted(target_sections),
                "target_plan_count": len(target_plans),
                "generated_or_reused_count": sum(
                    str(row.get("generation_status") or "")
                    in {"model_approved_human_pending", "model_rejected_or_revision_required"}
                    for row in generated_plans
                ),
                "generation_failed_count": sum(
                    str(row.get("generation_status") or "")
                    in {"generation_failed", "download_failed"}
                    for row in generated_plans
                ),
            }
            execution_log.append({
                "route": "conceptual_visual_generation",
                "task_ids": [row.get("task_id") for row in visual_tasks],
                "quality_summary": generation_summary,
                "approval_file": "",
            })
            completed_routes.add("conceptual_visual_generation")
            self._save_checkpoint(
                plan_fingerprint=plan_fingerprint,
                stage="conceptual_visual_generation",
                working=working,
                working_contracts=working_contracts,
                execution_log=execution_log,
                human_tasks=human_tasks,
                completed_routes=completed_routes,
                completed_section_ids=completed_section_ids,
                derived_rewrite_tasks=derived_rewrite_tasks,
            )

        drafts = [draft_from_dict(row) for row in (working.get("section_drafts") or [])]
        packets = [packet_from_dict(row) for row in (working.get("material_packets") or [])]
        packet_by_id = {row.section_id: row for row in packets}
        contract_by_id = {
            str(row.get("section_id") or ""): row for row in working_contracts if isinstance(row, dict)
        }
        original_draft_by_id = {
            str(row.get("section_id") or ""): copy.deepcopy(row)
            for row in (original.get("section_drafts") or []) if isinstance(row, dict)
        }
        section_tasks: dict[str, list[dict[str, Any]]] = {}
        rewrite_routes = {
            "evidence_retrieval", "claim_narrowing", "section_local_rewrite",
        }
        for task in auto_tasks:
            if task.get("repair_route") not in rewrite_routes:
                continue
            for sid in self._rewrite_section_ids_for_task(
                task, working.get("blueprint") or {}
            ):
                section_tasks.setdefault(str(sid), []).append(task)

        binder = CitationBinder(model_tier="premium_model", real_llm=self.real_llm)
        auditor = OverclaimAuditor(model_tier="advanced_model", real_llm=self.real_llm)
        ordered_ids = [draft.section_id for draft in drafts]
        revised_ids: list[str] = []
        for index, draft in enumerate(drafts):
            assigned = section_tasks.get(draft.section_id, [])
            packet = packet_by_id.get(draft.section_id)
            if not assigned or packet is None or draft.section_id in completed_section_ids:
                continue
            self._progress(
                "targeted_section_revision",
                "running",
                section_id=draft.section_id,
                completed_section_count=len(completed_section_ids),
                total_section_count=len(section_tasks),
            )
            previous_id = ordered_ids[index - 1] if index > 0 else ""
            next_id = ordered_ids[index + 1] if index + 1 < len(ordered_ids) else ""
            before = draft.english_text
            candidate, audit = self._revise_one(
                draft=draft,
                packet=packet,
                contract=contract_by_id.get(draft.section_id) or packet.section_contract,
                tasks=assigned,
                neighbour_context={
                    "previous_section_id": previous_id,
                    "next_section_id": next_id,
                    "manuscript_context": packet.manuscript_context,
                },
            )
            draft.revision_history.append(audit)
            if candidate:
                applied_updates = self._apply_safe_claim_state_updates(
                    working.get("blueprint") or {},
                    packet,
                    list(audit.get("safe_claim_state_updates") or []),
                )
                audit["applied_claim_state_updates"] = applied_updates
                draft.english_text = candidate
                draft.status = "publication_revision_candidate"
                draft = binder.bind(draft, packet)
                audited_before = draft.english_text
                draft = auditor.audit(draft, packet)
                if draft.english_text != audited_before:
                    draft = binder.bind(draft, packet)
                revised_ids.append(draft.section_id)
            execution_log.append({
                "route": "targeted_section_revision",
                "section_id": draft.section_id,
                "task_ids": [row.get("task_id") for row in assigned],
                "candidate_accepted": bool(candidate),
                "text_changed": draft.english_text != before,
                "safety_audit": audit,
            })
            completed_section_ids.add(draft.section_id)
            working["section_drafts"] = [draft_to_dict(row) for row in drafts]
            working["material_packets"] = [row.to_dict() for row in packets]
            working["full_review_english"] = _review_text(
                drafts, working.get("blueprint") or {}
            )
            self._save_checkpoint(
                plan_fingerprint=plan_fingerprint,
                stage=f"targeted_section_revision:{draft.section_id}",
                working=working,
                working_contracts=working_contracts,
                execution_log=execution_log,
                human_tasks=human_tasks,
                completed_routes=completed_routes,
                completed_section_ids=completed_section_ids,
                derived_rewrite_tasks=derived_rewrite_tasks,
            )

        working["section_drafts"] = [draft_to_dict(row) for row in drafts]
        working["material_packets"] = [row.to_dict() for row in packets]
        working["full_review_english"] = _review_text(drafts, working.get("blueprint") or {})
        if section_tasks:
            completed_routes.add("targeted_section_revision")
        cross_tasks = [row for row in auto_tasks if row.get("repair_route") == "cross_section_edit"]
        first_audit: dict[str, Any] = {}
        if cross_tasks and self.real_llm and "cross_section_edit" not in completed_routes:
            self._progress("citation_audit_before_cross_section", "running")
            first_audit = audit_citations(working, real_llm=self.real_llm)
        if cross_tasks and self.real_llm and "cross_section_edit" not in completed_routes:
            self._progress("cross_section_edit", "running", task_count=len(cross_tasks))
            edited = edit_cross_section(working, first_audit, real_llm=True)
            working = edited
            execution_log.append({
                "route": "cross_section_edit",
                "task_ids": [row.get("task_id") for row in cross_tasks],
                "changed_section_ids": edited.get("changed_section_ids") or [],
                "continuity_audit": edited.get("manuscript_continuity_audit") or {},
            })
            completed_routes.add("cross_section_edit")
            self._save_checkpoint(
                plan_fingerprint=plan_fingerprint,
                stage="cross_section_edit",
                working=working,
                working_contracts=working_contracts,
                execution_log=execution_log,
                human_tasks=human_tasks,
                completed_routes=completed_routes,
                completed_section_ids=completed_section_ids,
                derived_rewrite_tasks=derived_rewrite_tasks,
            )

        self._progress("citation_audit_after_revision", "running")
        post_audit = audit_citations(working, real_llm=self.real_llm)
        rolled_back = self._deterministic_rollback(
            original_bundle=original,
            working_bundle=working,
            baseline_audit=baseline_citation_bundle,
            post_audit=post_audit,
            candidate_section_ids=revised_ids,
        )
        if rolled_back:
            by_id = {
                str(row.get("section_id") or ""): row
                for row in (working.get("section_drafts") or []) if isinstance(row, dict)
            }
            for sid in rolled_back:
                if sid in original_draft_by_id:
                    by_id[sid] = original_draft_by_id[sid]
            working["section_drafts"] = [
                by_id.get(str(row.get("section_id") or ""), row)
                for row in (working.get("section_drafts") or []) if isinstance(row, dict)
            ]
            fresh = [draft_from_dict(row) for row in working.get("section_drafts") or []]
            working["full_review_english"] = _review_text(fresh, working.get("blueprint") or {})
            self._progress(
                "citation_audit_after_deterministic_rollback",
                "running",
                rolled_back_section_ids=rolled_back,
            )
            post_audit = audit_citations(working, real_llm=self.real_llm)
        working["publication_revision_execution"] = {
            "executed_at": _utc_now(),
            "auto_task_count": len(auto_tasks),
            "human_task_count": len(human_tasks),
            "execution_log": execution_log,
            "rolled_back_section_ids": rolled_back,
        }
        completed_routes.add("execution_complete")
        self._save_checkpoint(
            plan_fingerprint=plan_fingerprint,
            stage="execution_complete",
            working=working,
            working_contracts=working_contracts,
            execution_log=execution_log,
            human_tasks=human_tasks,
            completed_routes=completed_routes,
            completed_section_ids=completed_section_ids,
            derived_rewrite_tasks=derived_rewrite_tasks,
            status="completed",
            citation_audit=post_audit,
            rolled_back_section_ids=rolled_back,
        )
        self._progress("execution_complete", "completed")
        return {
            "revision_bundle": working,
            "citation_audit": post_audit,
            "execution_log": execution_log,
            "rolled_back_section_ids": rolled_back,
            "human_tasks": human_tasks,
        }

    def _plan_architecture_patch(
        self,
        blueprint: dict[str, Any],
        contracts: list[dict[str, Any]],
        charter: dict[str, Any],
        tasks: list[dict[str, Any]],
    ) -> dict[str, Any]:
        affected = {
            str(sid) for task in tasks for sid in (task.get("section_ids") or [])
        }
        payload = {
            "review_charter": {
                "central_question": charter.get("central_question") or "",
                "scope_statement": charter.get("scope_statement") or "",
                "out_of_scope": charter.get("out_of_scope") or [],
            },
            "current_sections": [
                {
                    "section_id": row.get("section_id"),
                    "title": row.get("title") or row.get("section_title"),
                    "argument_role": row.get("argument_role"),
                    "planned_thesis": row.get("planned_thesis"),
                    "taxonomy_organizing_principle": row.get("taxonomy_organizing_principle"),
                }
                for row in (blueprint.get("sections") or [])
                if isinstance(row, dict) and str(row.get("section_id") or "") in affected
            ],
            "current_section_contracts": [
                row for row in contracts
                if isinstance(row, dict) and str(row.get("section_id") or "") in affected
            ],
            "approved_architecture_tasks": tasks,
        }
        try:
            response = call_qwen_chat(
                "ReviewArchitecturePatch",
                [
                    {"role": "system", "content": ARCHITECTURE_PATCH_PROMPT.read_text(encoding="utf-8")},
                    {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
                ],
                model_tier="premium_model",
                temperature=0,
                max_tokens=3600,
                response_format={"type": "json_object"},
                force_mock=False,
                max_retries=0,
                timeout_seconds=120,
                max_transport_key_candidates=1,
            )
            parsed = _safe_json(str(response.get("content") or ""))
        except Exception as exc:
            return {
                "requires_human_decision": True,
                "reason": f"architecture_patch_call_failed:{type(exc).__name__}",
                "section_patches": [],
            }
        if not isinstance(parsed.get("section_patches"), list):
            return {
                "requires_human_decision": True,
                "reason": "architecture_patch_invalid_output",
                "section_patches": [],
            }
        return parsed

    @staticmethod
    def _apply_architecture_patch(
        blueprint: dict[str, Any],
        contracts: list[dict[str, Any]],
        patch: dict[str, Any],
        source_tasks: list[dict[str, Any]],
    ) -> tuple[list[str], list[dict[str, Any]]]:
        sections = {
            str(row.get("section_id") or ""): row
            for row in (blueprint.get("sections") or []) if isinstance(row, dict)
        }
        contract_by_id = {
            str(row.get("section_id") or ""): row
            for row in contracts if isinstance(row, dict)
        }
        allowed_sections = {
            str(sid) for task in source_tasks for sid in (task.get("section_ids") or [])
        }
        allowed_blueprint = {
            "title", "section_title", "argument_role", "planned_thesis",
            "taxonomy_organizing_principle",
        }
        allowed_contract = {
            "central_thesis", "classification_framework", "scope_guardrails",
            "paragraph_functions", "transition_contract", "forbidden_overclaims",
        }
        applied: list[str] = []
        derived: list[dict[str, Any]] = []
        issue_ids = _unique_strings(
            issue_id for task in source_tasks for issue_id in (task.get("issue_ids") or [])
        )
        for row in (patch.get("section_patches") or []):
            if not isinstance(row, dict):
                continue
            sid = str(row.get("section_id") or "")
            if sid not in sections or sid not in allowed_sections:
                continue
            bp_updates = row.get("blueprint_updates") or {}
            contract_updates = row.get("contract_updates") or {}
            if not isinstance(bp_updates, dict) or not isinstance(contract_updates, dict):
                continue
            safe_bp = {key: value for key, value in bp_updates.items() if key in allowed_blueprint}
            safe_contract = {
                key: value for key, value in contract_updates.items() if key in allowed_contract
            }
            if not safe_bp and not safe_contract:
                continue
            sections[sid].update(copy.deepcopy(safe_bp))
            if sid in contract_by_id:
                contract_by_id[sid].update(copy.deepcopy(safe_contract))
            instruction = _compact(row.get("revision_instruction"), 1400)
            if not instruction:
                instruction = (
                    "Revise this section to obey the patched organizing principle and role, "
                    "while preserving verified evidence and valid citations."
                )
            derived.append({
                "task_id": _stable_id("RT", sid, "architecture-rewrite", ",".join(issue_ids)),
                "issue_ids": issue_ids,
                "root_cause": "writing",
                "repair_route": "section_local_rewrite",
                "section_ids": [sid],
                "claim_ids": [],
                "priority": "medium",
                "auto_apply": True,
                "repair_instruction": instruction,
                "success_test": "The revised section follows the patched hierarchy without citation regression.",
                "rationale": "Derived from a validated local architecture patch.",
            })
            applied.append(sid)
        return _unique_strings(applied), derived

    def _revise_one(
        self,
        *,
        draft: Any,
        packet: Any,
        contract: dict[str, Any],
        tasks: list[dict[str, Any]],
        neighbour_context: dict[str, Any],
    ) -> tuple[str, dict[str, Any]]:
        if not self.real_llm:
            return "", {"accepted": False, "reason": "mock_mode_no_prose_revision"}
        source_paragraphs = [
            row.strip() for row in re.split(r"\n\s*\n", draft.english_text) if row.strip()
        ]
        task_context = " ".join(
            str(task.get(key) or "")
            for task in tasks
            for key in ("repair_instruction", "success_test", "rationale")
        )
        task_terms = _tokens(task_context)
        paragraph_scores = []
        for paragraph_index, paragraph in enumerate(source_paragraphs):
            paragraph_terms = _tokens(paragraph)
            overlap = len(task_terms & paragraph_terms)
            density = overlap / max(1, len(task_terms))
            paragraph_scores.append((density, overlap, -paragraph_index, paragraph_index))
        editable_indices = {
            row[3] for row in sorted(paragraph_scores, reverse=True)[:6]
        }
        if len(source_paragraphs) <= 6:
            editable_indices = set(range(len(source_paragraphs)))
        target_claim_ids = {
            str(claim_id)
            for task in tasks for claim_id in (task.get("claim_ids") or [])
            if str(claim_id)
        }
        authorized_claims = [
            {
                "claim_id": row.get("claim_id"),
                "statement": _compact(row.get("statement"), 700),
                "evidence_type": row.get("evidence_type"),
                "evidence_requirement": row.get("evidence_requirement"),
                "claim_state": row.get("claim_state"),
                "load_bearing": bool(row.get("load_bearing")),
            }
            for row in packet.claims
            if isinstance(row, dict)
            and (
                not target_claim_ids
                or str(row.get("claim_id") or "") in target_claim_ids
                or bool(row.get("load_bearing"))
            )
        ][:8]
        evidence_rows = [
            row for row in packet.evidence_packets
            if not target_claim_ids or str(row.claim_id or "") in target_claim_ids
        ]
        if not evidence_rows:
            evidence_rows = list(packet.evidence_packets)
        compact_evidence = [
            {
                "claim_id": row.claim_id,
                "paper_id": row.paper_id,
                "chunk_id": row.chunk_id,
                "exact_spans": [
                    _compact(span, 500) for span in list(row.exact_spans or [])[:2]
                ],
                "scope_fit": row.scope_fit,
                "retrieval_role": row.retrieval_role,
            }
            for row in evidence_rows[:10]
        ]
        contract_keys = (
            "section_id", "title", "section_title", "purpose", "argument_role",
            "central_claim", "scope_guardrails", "transition_from_previous",
            "transition_to_next", "estimated_word_budget",
        )
        compact_contract = {
            key: (
                _compact(contract.get(key), 900)
                if isinstance(contract.get(key), (str, list, tuple, dict))
                else contract.get(key)
            )
            for key in contract_keys if contract.get(key) is not None
        }
        payload = {
            "section_id": draft.section_id,
            "section_paragraph_outline": [
                {"paragraph_index": index, "preview": _compact(text, 240)}
                for index, text in enumerate(source_paragraphs)
            ],
            "current_section_paragraphs": [
                {"paragraph_index": index, "text": text}
                for index, text in enumerate(source_paragraphs)
                if index in editable_indices
            ],
            "section_contract": compact_contract,
            "neighbour_boundaries": {
                "previous_section_id": neighbour_context.get("previous_section_id"),
                "next_section_id": neighbour_context.get("next_section_id"),
                "manuscript_context": _compact(
                    neighbour_context.get("manuscript_context"), 1200
                ),
            },
            "authorized_claims": authorized_claims,
            "verified_evidence_packets": compact_evidence,
            "approved_revision_tasks": tasks,
            "approved_visuals": [
                row for row in (draft.figure_placements or [])
                if row.get("local_image_path") and Path(str(row.get("local_image_path"))).exists()
            ],
        }
        messages = [
            {"role": "system", "content": TARGETED_REVISION_PROMPT.read_text(encoding="utf-8")},
            {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
        ]
        parsed: dict[str, Any] = {}
        raw_output = ""
        parse_mode = "unusable_output"
        result: dict[str, Any] = {}
        llm_attempts: list[dict[str, Any]] = []
        for attempt_index, timeout_seconds in enumerate((150, 120), start=1):
            try:
                result = call_qwen_chat(
                    (
                        f"TargetedReviewRevision:{draft.section_id}"
                        if attempt_index == 1
                        else f"TargetedReviewRevisionRetry:{draft.section_id}"
                    ),
                    messages,
                    model_tier="advanced_model",
                    temperature=0.1,
                    max_tokens=3200,
                    response_format={"type": "json_object"},
                    stream=True,
                    force_mock=False,
                    max_retries=0,
                    timeout_seconds=timeout_seconds,
                    max_transport_key_candidates=1,
                    allow_model_fallback=False,
                )
                raw_output = str(result.get("content") or "")
                parsed = _safe_json(raw_output)
                parse_mode = "complete_json" if parsed else "unusable_output"
                if not parsed:
                    salvaged_edits = _salvage_complete_array_objects(
                        raw_output, "paragraph_edits"
                    )
                    if salvaged_edits:
                        parsed = {
                            "paragraph_edits": salvaged_edits,
                            "resolved_issue_ids": [],
                            "unresolved_issue_ids": _unique_strings(
                                issue_id
                                for task in tasks
                                for issue_id in (task.get("issue_ids") or [])
                            ),
                            "changes": [
                                "Recovered complete paragraph edits from a truncated response."
                            ],
                            "claim_state_updates": [],
                        }
                        parse_mode = "salvaged_complete_paragraph_edits"
                llm_attempts.append({
                    "attempt": attempt_index,
                    "parse_mode": parse_mode,
                    "raw_chars": len(raw_output),
                    "usage": result.get("_llm_usage") or {},
                })
                if parsed:
                    break
            except Exception as exc:
                llm_attempts.append({
                    "attempt": attempt_index,
                    "parse_mode": "exception",
                    "error": f"{type(exc).__name__}: {exc}",
                })
        revised_paragraphs = list(source_paragraphs)
        applied_edits: list[dict[str, Any]] = []
        rejected_edits: list[dict[str, Any]] = []
        seen_indices: set[int] = set()
        for row in (parsed.get("paragraph_edits") or [])[:5]:
            if not isinstance(row, dict):
                continue
            try:
                paragraph_index = int(row.get("paragraph_index"))
            except (TypeError, ValueError):
                rejected_edits.append({"reason": "invalid_paragraph_index"})
                continue
            replacement = ensure_english_strings([
                str(row.get("replacement_text") or "").strip()
            ])[0]
            if (
                paragraph_index < 0
                or paragraph_index >= len(revised_paragraphs)
                or paragraph_index in seen_indices
                or not replacement
            ):
                rejected_edits.append({
                    "paragraph_index": paragraph_index,
                    "reason": "out_of_range_duplicate_or_empty",
                })
                continue
            old_paragraph = revised_paragraphs[paragraph_index]
            old_count = _word_count(old_paragraph)
            replacement_count = _word_count(replacement)
            if replacement_count < max(12, int(old_count * 0.45)) or replacement_count > max(
                180, int(old_count * 2.0)
            ):
                rejected_edits.append({
                    "paragraph_index": paragraph_index,
                    "reason": "paragraph_information_retention_guard",
                    "old_word_count": old_count,
                    "replacement_word_count": replacement_count,
                })
                continue
            seen_indices.add(paragraph_index)
            revised_paragraphs[paragraph_index] = replacement
            applied_edits.append({
                "paragraph_index": paragraph_index,
                "issue_ids": _unique_strings(row.get("issue_ids") or []),
                "old_word_count": old_count,
                "replacement_word_count": replacement_count,
            })
        revised = "\n\n".join(revised_paragraphs).strip() if applied_edits else ""
        # Backward compatibility for an already-running older prompt. New runs
        # use paragraph_edits so a truncated response cannot destroy the whole
        # section.
        if not revised and parsed.get("revised_text"):
            revised = ensure_english_strings([str(parsed.get("revised_text") or "").strip()])[0]
        old_refs, new_refs = _reference_markers(draft.english_text), _reference_markers(revised)
        allowed_refs = {
            marker for ep in packet.evidence_packets if ep.paper_id
            for marker in (
                f"[REF:{ep.paper_id}]",
                f"[REF:{ep.paper_id}:{ep.claim_id}]" if ep.claim_id else "",
            ) if marker
        }
        unknown_refs = sorted(new_refs - old_refs - allowed_refs)
        old_words, new_words = _word_count(draft.english_text), _word_count(revised)
        old_paragraphs, new_paragraphs = _paragraph_count(draft.english_text), _paragraph_count(revised)
        valid = bool(
            revised
            and new_words >= max(80, int(old_words * 0.65))
            and new_words <= max(200, int(old_words * 1.55))
            and new_paragraphs >= max(1, old_paragraphs - 2)
            and not unknown_refs
            and not re.search(r"[\u3400-\u9fff]", revised)
        )
        valid_claim_ids = {
            str(claim.get("claim_id") or "")
            for claim in packet.claims if isinstance(claim, dict) and claim.get("claim_id")
        }
        safe_updates: list[dict[str, str]] = []
        rejected_updates: list[dict[str, str]] = []
        for row in (parsed.get("claim_state_updates") or []):
            if not isinstance(row, dict):
                continue
            normalized = {
                "claim_id": str(row.get("claim_id") or ""),
                "evidence_requirement": str(row.get("evidence_requirement") or "").lower(),
                "claim_state": str(row.get("claim_state") or "").lower(),
                "reason": _compact(row.get("reason"), 600),
            }
            expected = {
                "open_question": "open_question",
                "normative": "reframed",
            }.get(normalized["evidence_requirement"])
            if (
                normalized["claim_id"] in valid_claim_ids
                and expected == normalized["claim_state"]
                and normalized["reason"]
            ):
                safe_updates.append(normalized)
            else:
                rejected_updates.append(normalized)
        audit = {
            "stage": "publication_targeted_revision",
            "accepted": valid,
            "reason": (
                "passed_pre_citation_safety_gate"
                if valid else f"rejected_by_pre_citation_safety_gate:{parse_mode}"
            ),
            "parse_mode": parse_mode,
            "llm_usage": result.get("_llm_usage") or {},
            "llm_attempts": llm_attempts,
            "input_payload_char_count": len(json.dumps(payload, ensure_ascii=False)),
            "editable_paragraph_indices": sorted(editable_indices),
            "raw_output_preview": _compact(raw_output, 800),
            "issue_ids": _unique_strings(i for row in tasks for i in (row.get("issue_ids") or [])),
            "resolved_issue_ids_reported": list(parsed.get("resolved_issue_ids") or []),
            "unresolved_issue_ids_reported": list(parsed.get("unresolved_issue_ids") or []),
            "changes": list(parsed.get("changes") or []),
            "old_word_count": old_words,
            "new_word_count": new_words,
            "old_paragraph_count": old_paragraphs,
            "new_paragraph_count": new_paragraphs,
            "unknown_reference_markers": unknown_refs,
            "applied_paragraph_edits": applied_edits,
            "rejected_paragraph_edits": rejected_edits,
            "safe_claim_state_updates": safe_updates if valid else [],
            "rejected_claim_state_updates": rejected_updates,
        }
        return (revised if valid else ""), audit

    @staticmethod
    def _apply_safe_claim_state_updates(
        blueprint: dict[str, Any],
        packet: Any,
        updates: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        canonical = {
            str(claim.get("claim_id") or ""): claim
            for section in (blueprint.get("sections") or [])
            for claim in (section.get("claims") or [])
            if isinstance(claim, dict) and claim.get("claim_id")
        }
        compact_claims = {
            str(claim.get("claim_id") or ""): claim
            for claim in (packet.claims or [])
            if isinstance(claim, dict) and claim.get("claim_id")
        }
        applied: list[dict[str, Any]] = []
        for update in updates:
            claim_id = str(update.get("claim_id") or "")
            requirement = str(update.get("evidence_requirement") or "")
            state = str(update.get("claim_state") or "")
            if {requirement: state} not in (
                {"open_question": "open_question"},
                {"normative": "reframed"},
            ):
                continue
            if claim_id not in canonical or claim_id not in compact_claims:
                continue
            for claim in (canonical[claim_id], compact_claims[claim_id]):
                claim["original_statement"] = str(
                    claim.get("original_statement") or claim.get("statement") or ""
                )
                claim["evidence_requirement"] = requirement
                claim["claim_state"] = state
                claim["load_bearing"] = False
                claim["closure_disposition"] = (
                    "open_question" if requirement == "open_question" else "recommendation"
                )
            applied.append(dict(update))
        return applied

    @staticmethod
    def _audit_by_section(bundle: dict[str, Any]) -> dict[str, dict[str, Any]]:
        return {
            str(row.get("section_id") or ""): row
            for row in (bundle.get("citation_audits") or []) if isinstance(row, dict)
        }

    def _deterministic_rollback(
        self,
        *,
        original_bundle: dict[str, Any],
        working_bundle: dict[str, Any],
        baseline_audit: dict[str, Any],
        post_audit: dict[str, Any],
        candidate_section_ids: list[str],
    ) -> list[str]:
        before_audit = self._audit_by_section(baseline_audit)
        after_audit = self._audit_by_section(post_audit)
        before_text = {
            str(row.get("section_id") or ""): str(row.get("english_text") or "")
            for row in (original_bundle.get("section_drafts") or []) if isinstance(row, dict)
        }
        after_text = {
            str(row.get("section_id") or ""): str(row.get("english_text") or "")
            for row in (working_bundle.get("section_drafts") or []) if isinstance(row, dict)
        }
        rollback: list[str] = []
        for sid in _unique_strings(candidate_section_ids):
            old, new = before_audit.get(sid, {}), after_audit.get(sid, {})
            old_invalid = len(old.get("invalid_cited_chunk_ids") or [])
            new_invalid = len(new.get("invalid_cited_chunk_ids") or [])
            old_uncited = len(old.get("uncited_load_bearing_claim_ids") or [])
            new_uncited = len(new.get("uncited_load_bearing_claim_ids") or [])
            old_unsupported = bool((old.get("section_quality_judgment") or {}).get("unsupported_fact_detected"))
            new_unsupported = bool((new.get("section_quality_judgment") or {}).get("unsupported_fact_detected"))
            info_loss = _word_count(after_text.get(sid, "")) < int(
                _word_count(before_text.get(sid, "")) * 0.65
            )
            if (
                new_invalid > old_invalid
                or new_uncited > old_uncited
                or (new_unsupported and not old_unsupported)
                or info_loss
            ):
                rollback.append(sid)
        return rollback


class RevisionDeltaAuditor:
    """Compare immutable versions and detect issue improvement versus regression."""

    def __init__(self, *, real_llm: bool) -> None:
        self.real_llm = bool(real_llm)

    def compare(
        self,
        *,
        before_bundle: dict[str, Any],
        after_bundle: dict[str, Any],
        before_issues: dict[str, Any],
        after_issues: dict[str, Any],
        before_citation: dict[str, Any],
        after_citation: dict[str, Any],
        plan: dict[str, Any],
    ) -> dict[str, Any]:
        before_drafts = {
            str(row.get("section_id") or ""): str(row.get("english_text") or "")
            for row in (before_bundle.get("section_drafts") or []) if isinstance(row, dict)
        }
        after_drafts = {
            str(row.get("section_id") or ""): str(row.get("english_text") or "")
            for row in (after_bundle.get("section_drafts") or []) if isinstance(row, dict)
        }
        changed = [sid for sid in before_drafts if before_drafts.get(sid) != after_drafts.get(sid)]
        before_rows = [
            row for row in (before_issues.get("issues") or []) if isinstance(row, dict)
        ]
        after_rows = [
            row for row in (after_issues.get("issues") or []) if isinstance(row, dict)
        ]
        issue_lineage, resolved_ids, new_ids = _match_issue_lineage(
            before_rows, after_rows
        )
        before_high = sum(
            str(row.get("severity")) in {"critical", "high"}
            for row in before_rows
        )
        after_high = sum(
            str(row.get("severity")) in {"critical", "high"}
            for row in after_rows
        )
        before_manuscript_high = sum(
            str(row.get("severity")) in {"critical", "high"}
            and str(row.get("root_cause") or "") != "review_process"
            for row in before_rows
        )
        after_manuscript_high = sum(
            str(row.get("severity")) in {"critical", "high"}
            and str(row.get("root_cause") or "") != "review_process"
            for row in after_rows
        )
        deterministic = {
            "changed_section_ids": changed,
            "word_count_before": sum(_word_count(text) for text in before_drafts.values()),
            "word_count_after": sum(_word_count(text) for text in after_drafts.values()),
            "invalid_citations_before": int(before_citation.get("invalid_citation_count") or 0),
            "invalid_citations_after": int(after_citation.get("invalid_citation_count") or 0),
            "uncited_load_bearing_before": int(before_citation.get("uncited_load_bearing_claim_count") or 0),
            "uncited_load_bearing_after": int(after_citation.get("uncited_load_bearing_claim_count") or 0),
            "high_or_critical_issues_before": before_high,
            "high_or_critical_issues_after": after_high,
            "manuscript_high_or_critical_issues_before": before_manuscript_high,
            "manuscript_high_or_critical_issues_after": after_manuscript_high,
            "review_process_issue_count_after": sum(
                str(row.get("root_cause") or "") == "review_process"
                for row in after_rows
            ),
            "issue_lineage": issue_lineage,
            "resolved_issue_ids": sorted(resolved_ids),
            "new_issue_ids": sorted(new_ids),
        }
        llm_judgments = self._llm_compare_sections(
            changed, before_drafts, after_drafts, plan, deterministic
        ) if self.real_llm else []
        llm_regressions = [
            row for row in llm_judgments
            if row.get("verdict") == "regressed" or row.get("rollback_recommended") is True
        ]
        hard_regression = bool(
            deterministic["invalid_citations_after"] > deterministic["invalid_citations_before"]
            or deterministic["uncited_load_bearing_after"] > deterministic["uncited_load_bearing_before"]
            or llm_regressions
        )
        issue_gain = before_manuscript_high - after_manuscript_high
        quality_delta = float(issue_gain)
        if llm_judgments:
            quality_delta += sum(float(row.get("quality_delta") or 0) for row in llm_judgments) / len(llm_judgments)
        return {
            "schema_version": "publication_revision.delta.v1",
            "deterministic": deterministic,
            "section_pairwise_judgments": llm_judgments,
            "hard_regression": hard_regression,
            "rollback_recommended_section_ids": _unique_strings(
                row.get("section_id") for row in llm_regressions
            ),
            "quality_delta": round(quality_delta, 3),
            "verdict": "regressed" if hard_regression else "improved" if quality_delta > 0 else "neutral",
        }

    def _llm_compare_sections(
        self,
        changed: list[str],
        before: dict[str, str],
        after: dict[str, str],
        plan: dict[str, Any],
        deterministic: dict[str, Any],
    ) -> list[dict[str, Any]]:
        result_rows: list[dict[str, Any]] = []
        tasks = [row for row in (plan.get("tasks") or []) if isinstance(row, dict)]
        for sid in changed[:8]:
            related = [row for row in tasks if sid in (row.get("section_ids") or [])]
            payload = {
                "section_id": sid,
                "original_text": before.get(sid, ""),
                "revised_text": after.get(sid, ""),
                "target_revision_tasks": related,
                "deterministic_audit_delta": deterministic,
            }
            try:
                response = call_qwen_chat(
                    f"RevisionDeltaJudge:{sid}",
                    [
                        {"role": "system", "content": DELTA_JUDGE_PROMPT.read_text(encoding="utf-8")},
                        {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
                    ],
                    model_tier="premium_model",
                    temperature=0,
                    max_tokens=1800,
                    response_format={"type": "json_object"},
                    force_mock=False,
                    max_retries=0,
                    timeout_seconds=90,
                    max_transport_key_candidates=1,
                    allow_model_fallback=False,
                    enable_thinking=False,
                )
                parsed = _safe_json(str(response.get("content") or ""))
            except Exception:
                parsed = {}
            if not parsed:
                parsed = {
                    "verdict": "cannot_determine",
                    "quality_delta": 0,
                    "rollback_recommended": False,
                    "rationale": "Pairwise judge failed; deterministic gates remain authoritative.",
                }
            parsed["section_id"] = sid
            result_rows.append(parsed)
        return result_rows


class ConvergenceController:
    """Choose continue, stop, or human escalation from evidence and recurrence."""

    def decide(
        self,
        *,
        round_number: int,
        max_rounds: int,
        issue_report: dict[str, Any],
        plan: dict[str, Any],
        delta: dict[str, Any] | None,
        citation_bundle: dict[str, Any],
        global_bundle: dict[str, Any],
        peer_bundle: dict[str, Any],
        prior_rounds: list[dict[str, Any]],
    ) -> dict[str, Any]:
        issues = [row for row in (issue_report.get("issues") or []) if isinstance(row, dict)]
        critical = [row for row in issues if row.get("severity") == "critical"]
        nonvisual_high = [
            row for row in issues
            if row.get("severity") == "high"
            and row.get("root_cause") not in {
                "visual_conceptual", "visual_empirical", "review_process",
            }
        ]
        review_process_high = [
            row for row in issues
            if row.get("severity") == "high" and row.get("root_cause") == "review_process"
        ]
        visual_high = [
            row for row in issues
            if row.get("severity") == "high"
            and row.get("root_cause") in {"visual_conceptual", "visual_empirical"}
        ]
        evidence_integrity = bool(
            int(citation_bundle.get("invalid_citation_count") or 0) == 0
            and int(citation_bundle.get("uncited_load_bearing_claim_count") or 0) == 0
        )
        auto_tasks = [row for row in (plan.get("tasks") or []) if row.get("auto_apply")]
        human_tasks = [row for row in (plan.get("tasks") or []) if not row.get("auto_apply")]
        hard_regression = bool((delta or {}).get("hard_regression"))
        recent_deltas = [float((row.get("delta") or {}).get("quality_delta") or 0) for row in prior_rounds[-1:]]
        stagnant = bool(
            delta is not None
            and float(delta.get("quality_delta") or 0) <= 0
            and recent_deltas
            and all(value <= 0 for value in recent_deltas)
        )
        if hard_regression:
            action, reason = "needs_human", "revision_regressed_after_automatic_rollback"
        elif not critical and not nonvisual_high and not review_process_high and evidence_integrity:
            action = "complete"
            reason = "text_publication_candidate_visuals_pending" if visual_high else "publication_candidate"
        elif round_number >= max_rounds:
            action = "needs_human"
            reason = (
                "reviewer_unavailable_after_bounded_retries"
                if review_process_high and not nonvisual_high and not critical
                else "maximum_major_revision_rounds_reached"
            )
        elif stagnant:
            action, reason = "needs_human", "revision_quality_stagnated"
        elif not auto_tasks and human_tasks:
            action, reason = "needs_human", "remaining_issues_require_human_or_external_action"
        elif not auto_tasks:
            action, reason = "complete", "no_actionable_major_issue_remains"
        else:
            action, reason = "continue", "actionable_issues_remain_with_safe_repair_routes"
        return {
            "action": action,
            "reason": reason,
            "round_number": round_number,
            "critical_issue_count": len(critical),
            "nonvisual_high_issue_count": len(nonvisual_high),
            "visual_high_issue_count": len(visual_high),
            "review_process_high_issue_count": len(review_process_high),
            "evidence_integrity_passed": evidence_integrity,
            "auto_task_count": len(auto_tasks),
            "human_task_count": len(human_tasks),
            "global_formal_readiness": str((global_bundle.get("judgment") or {}).get("formal_readiness") or ""),
            "peer_recommendations": [
                str(row.get("recommendation") or "")
                for row in (peer_bundle.get("peer_reviews") or []) if isinstance(row, dict)
            ],
        }


def run_publication_revision_loop(
    revision_bundle: dict[str, Any],
    global_bundle: dict[str, Any],
    peer_bundle: dict[str, Any],
    *,
    supervisor_bundle: dict[str, Any] | None,
    charter: dict[str, Any],
    contracts: list[dict[str, Any]],
    kb_path: Path | str | None,
    output_dir: Path,
    real_llm: bool,
    enabled: bool = True,
    max_rounds: int = 3,
    max_tasks_per_round: int = 8,
    enable_external_oa: bool = True,
    max_external_rounds: int = 2,
    max_external_claims: int = 6,
    generate_conceptual_visuals: bool = True,
    max_generated_visuals: int = 4,
    resume: bool = True,
) -> dict[str, Any]:
    """Run a versioned, bounded revision loop and return the accepted version."""
    from optomind_research.full_review_production import (
        _review_text,
        audit_citations,
        draft_from_dict,
        run_global_review,
        run_peer_review_panel,
    )

    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    compiler = ReviewerIssueCompiler()
    board = RevisionDecisionBoard(real_llm=real_llm)
    delta_auditor = RevisionDeltaAuditor(real_llm=real_llm)
    controller = ConvergenceController()
    current_revision = copy.deepcopy(revision_bundle)
    current_global = copy.deepcopy(global_bundle)
    current_peer = copy.deepcopy(peer_bundle)
    current_contracts = copy.deepcopy(contracts)
    current_citation = current_global.get("post_revision_citation_audit") or audit_citations(
        current_revision, real_llm=real_llm
    )
    rounds: list[dict[str, Any]] = []
    human_tasks: list[dict[str, Any]] = []
    previous_issues: list[dict[str, Any]] = []
    final_decision: dict[str, Any] = {}
    accepted_version_round = 0
    rejected_candidate_rounds: list[int] = []
    max_rounds = max(1, int(max_rounds))

    for round_number in range(1, max_rounds + 1):
        round_dir = output_dir / f"round_{round_number:02d}"
        # A completed revision round is immutable. On process resume, restore
        # its accepted candidate and reviews instead of recomputing pairwise
        # delta judgments or re-calling the reviewer panel. This is especially
        # important after long evidence-retrieval rounds.
        completed_round_path = round_dir / "round_summary.json"
        if resume and completed_round_path.exists():
            try:
                saved_round = json.loads(completed_round_path.read_text(encoding="utf-8"))
                saved_revision = json.loads(
                    (round_dir / "candidate_revision.json").read_text(encoding="utf-8")
                )
                saved_citation = json.loads(
                    (round_dir / "candidate_citation_audit.json").read_text(encoding="utf-8")
                )
                saved_global = json.loads(
                    (round_dir / "post_revision_global_review.json").read_text(encoding="utf-8")
                )
                saved_peer = json.loads(
                    (round_dir / "post_revision_peer_reviews.json").read_text(encoding="utf-8")
                )
                saved_valid = bool(
                    int(saved_round.get("round_number") or 0) == round_number
                    and isinstance(saved_round.get("decision"), dict)
                    and isinstance(saved_revision.get("section_drafts"), list)
                )
            except Exception:
                saved_round = {}
                saved_revision = {}
                saved_citation = {}
                saved_global = {}
                saved_peer = {}
                saved_valid = False
            if saved_valid:
                rounds.append(saved_round)
                saved_promoted = saved_round.get("candidate_promoted") is not False
                if saved_promoted:
                    current_revision = saved_revision
                    current_citation = saved_citation
                    current_global = saved_global
                    current_peer = saved_peer
                    current_contracts = list(
                        saved_revision.get("revised_section_contracts") or current_contracts
                    )
                    accepted_version_round = round_number
                else:
                    rejected_candidate_rounds.append(round_number)
                previous_issues = list(
                    ((saved_round.get("after_issue_report") or {}).get("issues") or [])
                )
                checkpoint_path = round_dir / "execution_checkpoint.json"
                if checkpoint_path.exists():
                    try:
                        checkpoint_data = json.loads(
                            checkpoint_path.read_text(encoding="utf-8")
                        )
                        human_tasks.extend(checkpoint_data.get("human_tasks") or [])
                    except Exception:
                        pass
                final_decision = dict(saved_round.get("decision") or {})
                if final_decision.get("action") != "continue":
                    break
                continue
        _atomic_json(round_dir / "round_progress.json", {
            "updated_at": _utc_now(), "stage": "issue_compilation", "status": "running"
        })
        issue_report = compiler.compile(
            revision_bundle=current_revision,
            global_bundle=current_global,
            peer_bundle=current_peer,
            supervisor_bundle=supervisor_bundle if round_number == 1 else {},
            charter=charter,
            previous_issues=previous_issues,
        )
        issue_fingerprint = _issue_report_fingerprint(issue_report)
        plan: dict[str, Any] = {}
        saved_issue_path = round_dir / "reviewer_issues.json"
        saved_plan_path = round_dir / "revision_plan.json"
        if resume and saved_issue_path.exists() and saved_plan_path.exists():
            try:
                saved_issue = json.loads(saved_issue_path.read_text(encoding="utf-8"))
                saved_plan = json.loads(saved_plan_path.read_text(encoding="utf-8"))
            except Exception:
                saved_issue, saved_plan = {}, {}
            if (
                _issue_report_fingerprint(saved_issue) == issue_fingerprint
                and isinstance(saved_plan.get("tasks"), list)
            ):
                plan = saved_plan
                plan["planner_resume"] = {
                    "reused": True,
                    "reused_at": _utc_now(),
                    "reason": "identical_compiled_issue_set",
                }
        if not plan:
            _atomic_json(round_dir / "round_progress.json", {
                "updated_at": _utc_now(), "stage": "revision_planning", "status": "running"
            })
            plan = board.plan(issue_report, charter=charter, max_tasks=max_tasks_per_round)
        plan["issue_fingerprint"] = issue_fingerprint
        _atomic_json(round_dir / "reviewer_issues.json", issue_report)
        _atomic_json(round_dir / "revision_plan.json", plan)

        if not enabled or not real_llm:
            decision = controller.decide(
                round_number=round_number,
                max_rounds=max_rounds,
                issue_report=issue_report,
                plan={"tasks": [] if not enabled else plan.get("tasks") or []},
                delta=None,
                citation_bundle=current_citation,
                global_bundle=current_global,
                peer_bundle=current_peer,
                prior_rounds=rounds,
            )
            if not real_llm:
                decision = {
                    **decision,
                    "action": "complete",
                    "reason": "mock_mode_revision_not_executed",
                }
            rounds.append({
                "round_number": round_number,
                "issue_report": issue_report,
                "plan": plan,
                "delta": {},
                "decision": decision,
            })
            final_decision = decision
            break

        before_revision = copy.deepcopy(current_revision)
        before_citation = copy.deepcopy(current_citation)
        executor = TargetedRevisionExecutor(
            real_llm=True,
            kb_path=kb_path or current_revision.get("kb_sqlite"),
            enable_external_oa=enable_external_oa,
            external_output_dir=round_dir,
            max_external_rounds=max_external_rounds,
            max_external_claims=max_external_claims,
            generate_conceptual_visuals=generate_conceptual_visuals,
            max_generated_visuals=max_generated_visuals,
            checkpoint_dir=round_dir,
            resume=resume,
        )
        execution = executor.execute(
            current_revision,
            plan,
            charter=charter,
            contracts=current_contracts,
            baseline_citation_bundle=current_citation,
        )
        candidate_revision = execution["revision_bundle"]
        candidate_contracts = list(
            candidate_revision.get("revised_section_contracts") or current_contracts
        )
        candidate_citation = execution["citation_audit"]
        revision_fingerprint = _revision_fingerprint(candidate_revision)
        _atomic_json(round_dir / "candidate_revision.json", candidate_revision)
        _atomic_json(round_dir / "candidate_citation_audit.json", candidate_citation)
        _atomic_json(round_dir / "round_progress.json", {
            "updated_at": _utc_now(), "stage": "global_review", "status": "running",
            "revision_fingerprint": revision_fingerprint,
        })
        global_path = round_dir / "post_revision_global_review.json"
        if resume and global_path.exists():
            try:
                candidate_global = json.loads(global_path.read_text(encoding="utf-8"))
            except Exception:
                candidate_global = {}
            if candidate_global.get("revision_fingerprint") != revision_fingerprint:
                candidate_global = {}
        else:
            candidate_global = {}
        if not candidate_global:
            candidate_global = run_global_review(
                candidate_revision,
                charter=charter,
                contracts=candidate_contracts,
                citation_bundle=candidate_citation,
                real_llm=True,
            )
            candidate_global["post_revision_citation_audit"] = candidate_citation
            candidate_global["revision_fingerprint"] = revision_fingerprint
            _atomic_json(global_path, candidate_global)
        peer_path = round_dir / "post_revision_peer_reviews.json"
        _atomic_json(round_dir / "round_progress.json", {
            "updated_at": _utc_now(), "stage": "peer_review", "status": "running",
            "revision_fingerprint": revision_fingerprint,
        })
        if resume and peer_path.exists():
            try:
                candidate_peer = json.loads(peer_path.read_text(encoding="utf-8"))
            except Exception:
                candidate_peer = {}
            if candidate_peer.get("revision_fingerprint") != revision_fingerprint:
                candidate_peer = {}
        else:
            candidate_peer = {}
        if not candidate_peer:
            candidate_peer = run_peer_review_panel(
                candidate_revision, candidate_global, charter=charter, real_llm=True
            )
            candidate_peer["revision_fingerprint"] = revision_fingerprint
            _atomic_json(peer_path, candidate_peer)
        after_issues = compiler.compile(
            revision_bundle=candidate_revision,
            global_bundle=candidate_global,
            peer_bundle=candidate_peer,
            supervisor_bundle={},
            charter=charter,
            previous_issues=issue_report.get("issues") or [],
        )
        delta = delta_auditor.compare(
            before_bundle=before_revision,
            after_bundle=candidate_revision,
            before_issues=issue_report,
            after_issues=after_issues,
            before_citation=before_citation,
            after_citation=candidate_citation,
            plan=plan,
        )
        _atomic_json(round_dir / "after_revision_issues.json", after_issues)
        _atomic_json(round_dir / "revision_delta_report.json", delta)

        # Pairwise review may detect a semantic regression that deterministic
        # citation checks cannot see. Roll back only the affected sections and
        # re-audit/re-review the accepted mixed version once.
        semantic_rollbacks = list(delta.get("rollback_recommended_section_ids") or [])
        applied_semantic_rollbacks: list[str] = []
        old_by_id = {
            str(row.get("section_id") or ""): row
            for row in (before_revision.get("section_drafts") or []) if isinstance(row, dict)
        }
        # A first rollback can reveal a second semantic regression that was
        # previously masked by the larger edit set. Iterate a bounded number
        # of times and re-run all gates after each pass. Never accept a
        # candidate merely because the first rollback pass completed.
        for rollback_pass in range(1, 4):
            new_rollbacks = [
                sid for sid in semantic_rollbacks
                if sid not in applied_semantic_rollbacks and sid in old_by_id
            ]
            if not new_rollbacks:
                break
            applied_semantic_rollbacks.extend(new_rollbacks)
            revised_rows = []
            for row in (candidate_revision.get("section_drafts") or []):
                sid = str(row.get("section_id") or "") if isinstance(row, dict) else ""
                revised_rows.append(old_by_id.get(sid, row) if sid in new_rollbacks else row)
            candidate_revision["section_drafts"] = revised_rows
            candidate_revision["full_review_english"] = _review_text(
                [draft_from_dict(row) for row in revised_rows if isinstance(row, dict)],
                candidate_revision.get("blueprint") or {},
            )
            revision_fingerprint = _revision_fingerprint(candidate_revision)
            _atomic_json(round_dir / "candidate_revision.json", candidate_revision)
            candidate_citation = audit_citations(candidate_revision, real_llm=True)
            _atomic_json(round_dir / "candidate_citation_audit.json", candidate_citation)
            candidate_global = run_global_review(
                candidate_revision,
                charter=charter,
                contracts=candidate_contracts,
                citation_bundle=candidate_citation,
                real_llm=True,
            )
            candidate_global["post_revision_citation_audit"] = candidate_citation
            candidate_global["revision_fingerprint"] = revision_fingerprint
            _atomic_json(round_dir / "post_revision_global_review.json", candidate_global)
            candidate_peer = run_peer_review_panel(
                candidate_revision, candidate_global, charter=charter, real_llm=True
            )
            candidate_peer["revision_fingerprint"] = revision_fingerprint
            _atomic_json(round_dir / "post_revision_peer_reviews.json", candidate_peer)
            after_issues = compiler.compile(
                revision_bundle=candidate_revision,
                global_bundle=candidate_global,
                peer_bundle=candidate_peer,
                supervisor_bundle={},
                charter=charter,
                previous_issues=issue_report.get("issues") or [],
            )
            delta = delta_auditor.compare(
                before_bundle=before_revision,
                after_bundle=candidate_revision,
                before_issues=issue_report,
                after_issues=after_issues,
                before_citation=before_citation,
                after_citation=candidate_citation,
                plan=plan,
            )
            delta["semantic_rollback_pass"] = rollback_pass
            semantic_rollbacks = list(
                delta.get("rollback_recommended_section_ids") or []
            )
        if applied_semantic_rollbacks:
            delta["semantic_rollback_applied_section_ids"] = (
                applied_semantic_rollbacks
            )

        # The controller only needs to know whether safe repair routes remain;
        # it does not need another expensive LLM plan here. The actual next
        # round performs one full planning call and persists that result.
        next_plan = RevisionDecisionBoard(real_llm=False).plan(
            after_issues, charter=charter, max_tasks=max_tasks_per_round
        )
        next_plan["decision_only"] = True
        decision = controller.decide(
            round_number=round_number,
            max_rounds=max_rounds,
            issue_report=after_issues,
            plan=next_plan,
            delta=delta,
            citation_bundle=candidate_citation,
            global_bundle=candidate_global,
            peer_bundle=candidate_peer,
            prior_rounds=rounds,
        )
        human_tasks.extend(execution.get("human_tasks") or [])
        candidate_promoted = not bool(delta.get("hard_regression"))
        # Version control is fail-closed: a regressed candidate remains fully
        # auditable in its round directory but can never replace the last
        # accepted manuscript. Human review receives the failed candidate and
        # its delta report while final delivery retains the safer prior round.
        if candidate_promoted:
            current_revision = candidate_revision
            current_citation = candidate_citation
            current_global = candidate_global
            current_peer = candidate_peer
            current_contracts = candidate_contracts
            accepted_version_round = round_number
        else:
            rejected_candidate_rounds.append(round_number)
        previous_issues = after_issues.get("issues") or []
        round_record = {
            "round_number": round_number,
            "issue_report": issue_report,
            "plan": plan,
            "execution_summary": {
                "log": execution.get("execution_log") or [],
                "rolled_back_section_ids": execution.get("rolled_back_section_ids") or [],
            },
            "after_issue_report": after_issues,
            "delta": delta,
            "decision": decision,
            "candidate_promoted": candidate_promoted,
        }
        rounds.append(round_record)
        _atomic_json(round_dir / "revision_delta_report.json", delta)
        _atomic_json(round_dir / "post_revision_global_review.json", candidate_global)
        _atomic_json(round_dir / "post_revision_peer_reviews.json", candidate_peer)
        _atomic_json(round_dir / "round_summary.json", round_record)
        _atomic_json(round_dir / "round_progress.json", {
            "updated_at": _utc_now(), "stage": "round_complete", "status": "completed",
            "decision": decision,
        })
        final_decision = decision
        if decision.get("action") != "continue":
            break

    report = {
        "schema_version": "publication_revision.loop.v1",
        "created_at": _utc_now(),
        "enabled": bool(enabled),
        "real_llm": bool(real_llm),
        "round_count": len(rounds),
        "rounds": rounds,
        "final_decision": final_decision,
        "final_revision_bundle": current_revision,
        "final_global_review": current_global,
        "final_peer_reviews": current_peer,
        "final_citation_audit": current_citation,
        "final_section_contracts": current_contracts,
        "accepted_version_round": accepted_version_round,
        "rejected_candidate_rounds": rejected_candidate_rounds,
        "final_candidate_promoted": (
            rounds[-1].get("candidate_promoted") is not False if rounds else True
        ),
        "final_manuscript_evidence_integrity_passed": bool(
            int(current_citation.get("invalid_citation_count") or 0) == 0
            and int(current_citation.get("uncited_load_bearing_claim_count") or 0) == 0
            and int(current_citation.get("quality_judge_failure_count") or 0) == 0
        ),
        "unresolved_human_tasks": human_tasks,
        "version_policy": (
            "Every round is immutable; only sections passing citation, information-retention, "
            "and semantic-delta gates enter the current accepted version."
        ),
    }
    _atomic_json(output_dir / "unresolved_human_tasks.json", {
        "tasks": human_tasks,
        "final_decision": final_decision,
    })
    _atomic_json(output_dir / "revision_loop_report.json", report)
    _atomic_json(output_dir / "current_version.json", {
        "round_count": len(rounds),
        "final_decision": final_decision,
        "revision_loop_report": str(output_dir / "revision_loop_report.json"),
    })
    return report
