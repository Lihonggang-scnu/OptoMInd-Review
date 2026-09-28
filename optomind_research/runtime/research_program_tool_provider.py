"""AgentScope tools for turning a review into a traceable research program."""

from __future__ import annotations

import ast
import copy
import json
import re
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from agentscope.tool import FunctionTool

from .artifact_store import atomic_write_json, atomic_write_text
from .research_program_schemas import (
    ResearchHypothesisPortfolio,
    ResearchOpportunityMap,
    ResearchPlan,
)
from .program_focus_gate import ProgramFocusGate
from .research_plan_quality import (
    audit_plan_quality,
    audit_plan_quality_warnings,
    build_source_terminology_ledger,
    normalize_plan_quality,
)
from .tool_provider import ToolProvider

_CJK = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]")
_ID_PATTERNS = {
    "opportunity": re.compile(r"^OP\d{2,3}$"),
    "hypothesis": re.compile(r"^H\d{2,3}$"),
    "work_package": re.compile(r"^WP\d{2,3}$"),
}
_PLACEHOLDER = re.compile(
    r"\b(mock|placeholder|tbd|to be determined|unknown paper)\b",
    re.I,
)

_PERMISSION_RANK = {
    "discovery_only": 0,
    "background_and_candidate_only": 1,
    "contextual_or_qualified_support": 1,
    "factual_support": 2,
}

_READINESS_ALIASES = {
    "ready_for_experimental_design": "ready",
    "ready_for_simulation_study": "ready",
    "experimental_validation_ready": "ready",
    "ready_for_experimental_validation": "ready",
    "simulation_validation_ready": "ready",
    "experiment_ready": "ready",
    "simulation_ready": "ready",
    "data_ready": "ready",
    # These labels are not canonical readiness states.  They describe a
    # theoretical or conceptual lead, so the safe mapping is deliberately
    # downward rather than silently upgrading it to executable readiness.
    "theory_ready": "needs_more_literature",
    "theoretical_ready": "needs_more_literature",
    "theory_validated": "needs_more_literature",
    "conceptual_ready": "needs_more_literature",
    "literature_ready": "needs_more_literature",
    "evidence_ready": "needs_more_literature",
    "requires_feasibility_study": "needs_more_literature",
    "conceptual": "needs_more_literature",
    "concept_validated": "needs_more_literature",
    "concept_supported": "needs_more_literature",
    "theoretically_supported": "needs_more_literature",
    "candidate": "needs_more_literature",
    "proposed": "needs_more_literature",
}

_CONFIDENCE_ALIASES = {
    "low": "low",
    "very_low": "low",
    "very-low": "low",
    "weak": "low",
    "uncertain": "low",
    "unknown": "low",
    "medium": "medium",
    "moderate": "medium",
    "moderate_confidence": "medium",
    "moderate-confidence": "medium",
    "med": "medium",
    "mid": "medium",
    "medium_confidence": "medium",
    "medium-confidence": "medium",
    "high": "high",
    "strong": "high",
    "very_high": "high",
    "very-high": "high",
    "high_confidence": "high",
    "high-confidence": "high",
}

_SINGLE_SPINE_ERROR_SET = frozenset(
    {
        "main_hypotheses_must_have_dependency_chain",
        "main_hypotheses_must_have_one_spine_root",
        "main_hypotheses_dependency_graph_disconnected",
    }
)
_SINGLE_SPINE_REPAIRABLE_EXACT_ERRORS = frozenset(
    {
        *_SINGLE_SPINE_ERROR_SET,
        "main_hypothesis_not_in_hypothesis_portfolio",
    }
)
_SINGLE_SPINE_REPAIRABLE_ERROR_PREFIXES = (
    "future_branch_reason_missing:",
    "opportunity_uses_nonfactual_permission:",
)
_SINGLE_SPINE_EVIDENCE_STATUS_SCORE = {
    "factual": 4,
    "supported_boundary": 3,
    "partially_supported": 2,
    "partial_support": 2,
    "open_gap": 1,
    "unknown": 0,
}
_SINGLE_SPINE_READINESS_SCORE = {
    "ready": 3,
    "needs_more_literature": 2,
    "needs_human_choice": 1,
    "future_phase": 0,
}
_SINGLE_SPINE_CONFIDENCE_SCORE = {
    "high": 3,
    "medium": 2,
    "low": 1,
}


def _canonical_id(value: Any, prefix: str) -> str:
    """Normalize unambiguous model aliases such as OPP-01 or wp_2."""

    raw = str(value or "").strip().upper()
    digits = re.findall(r"\d+", raw)
    if not digits:
        return raw
    number = int(digits[-1])
    return f"{prefix}{number:02d}"


def _as_string_list(value: Any) -> List[str]:
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    if value is None:
        return []
    text = str(value).strip()
    if not text:
        return []
    # Preserve a substantive sentence as one reasoning step. Semicolon
    # separated model output is safely expanded into multiple steps.
    values = re.split(r"\s*;\s*", text)
    return [item.strip() for item in values if item.strip()]


def _dedupe_audit_rows(rows: Any) -> List[Dict[str, Any]]:
    """Keep normalization history stable across repeated resumes."""

    result: List[Dict[str, Any]] = []
    seen: set[str] = set()
    for raw in rows if isinstance(rows, list) else []:
        if not isinstance(raw, dict):
            continue
        key = json.dumps(raw, sort_keys=True, ensure_ascii=True)
        if key in seen:
            continue
        seen.add(key)
        result.append(dict(raw))
    return result


def _new_audit_rows(
    current: Any,
    previous: Any,
) -> List[Dict[str, Any]]:
    """Return only audit rows introduced by the current normalization pass."""

    previous_keys = {
        json.dumps(row, sort_keys=True, ensure_ascii=True)
        for row in (previous if isinstance(previous, list) else [])
        if isinstance(row, dict)
    }
    return [
        dict(row)
        for row in (current if isinstance(current, list) else [])
        if isinstance(row, dict)
        and json.dumps(row, sort_keys=True, ensure_ascii=True) not in previous_keys
    ]


def _safe_literal_structure(value: Any) -> Any:
    """Parse a JSON/Python-literal container without executing code.

    Models occasionally serialize a dictionary into a string using Python's
    single-quoted representation.  ``ast.literal_eval`` is intentionally used
    instead of ``eval``: it accepts only literal containers and primitive
    values, and rejects calls, imports, attributes, and arbitrary expressions.
    The length bound keeps this formatting helper from becoming an unbounded
    parser for hostile or accidental input.
    """

    if not isinstance(value, str):
        return value if isinstance(value, (dict, list, tuple)) else None
    text = value.strip().strip("`").strip()
    if not text or len(text) > 100_000:
        return None
    candidates = [text]
    try:
        parsed = json.loads(text)
        if isinstance(parsed, (dict, list, tuple)):
            return parsed
        if isinstance(parsed, str) and parsed.strip() != text:
            candidates.append(parsed.strip())
    except (TypeError, ValueError, json.JSONDecodeError):
        pass
    for candidate in candidates:
        try:
            parsed = ast.literal_eval(candidate)
        except (SyntaxError, ValueError, TypeError, MemoryError, RecursionError):
            continue
        if isinstance(parsed, (dict, list, tuple)):
            return parsed
    return None


def _humanize_field_name(value: Any) -> str:
    text = re.sub(r"[_-]+", " ", str(value or "").strip()).strip()
    special = {
        "experiment id": "Experiment",
        "expected result id": "Expected result",
        "verification deferred": "Verification status",
        "verification status": "Verification status",
        "verification rationale": "Verification rationale",
    }
    return special.get(text.casefold(), text[:1].upper() + text[1:])


def _format_structured_value(value: Any) -> str:
    """Turn nested literal data into readable English without repr syntax."""

    if isinstance(value, dict):
        parts = []
        for key, item in value.items():
            label = _humanize_field_name(key)
            if key == "verification_deferred" and item is True:
                rendered = "verification_deferred"
            else:
                rendered = _format_structured_value(item)
            if rendered:
                parts.append(f"{label}: {rendered}")
        return "; ".join(parts)
    if isinstance(value, (list, tuple)):
        return ", ".join(
            rendered
            for item in value
            if (rendered := _format_structured_value(item))
        )
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        return ""
    return str(value).strip()


def _format_structured_narrative_item(value: Any) -> str:
    if not isinstance(value, dict):
        return str(value or "").strip()
    identifier = str(
        value.get("id")
        or value.get("experiment_id")
        or value.get("expected_result_id")
        or value.get("milestone_id")
        or value.get("decision_id")
        or ""
    ).strip()
    label = ""
    if value.get("experiment_id"):
        label = "Experiment"
    elif value.get("expected_result_id"):
        label = "Expected result"
    elif value.get("summary"):
        label = "Summary"
    elif value.get("expected_result"):
        label = "Expected result"
    description = str(
        value.get("description")
        or value.get("title")
        or value.get("decision")
        or value.get("purpose")
        or value.get("objective")
        or value.get("summary")
        or value.get("expected_result")
        or ""
    ).strip()
    excluded = {
        "id", "experiment_id", "expected_result_id", "milestone_id",
        "decision_id", "description", "title", "decision", "purpose",
        "objective", "summary", "expected_result",
    }
    extras = {
        key: item
        for key, item in value.items()
        if key not in excluded and item not in (None, "", [], {})
    }
    if description:
        prefix = f"{label} {identifier}".strip() if label or identifier else ""
        text = f"{prefix}: {description}" if prefix else description
        extra_text = _format_structured_value(extras)
        if extra_text:
            text += "; " + extra_text
        return text.strip()
    return _format_structured_value(value)


def _as_narrative_list(value: Any) -> List[str]:
    """Normalize model list items without leaking Python dict reprs.

    Milestones and decision points are stored as readable strings by the
    current schema.  Models often return small structured objects, which were
    previously converted to strings such as ``{'id': 'M1', ...}`` and rendered
    verbatim in the final plan.
    """

    if not isinstance(value, list):
        parsed = _safe_literal_structure(value)
        if isinstance(parsed, list):
            value = list(parsed)
        elif isinstance(parsed, dict):
            value = [parsed]
        else:
            return _as_string_list(value)
    result: List[str] = []
    for raw in value:
        if not isinstance(raw, dict):
            parsed = _safe_literal_structure(raw)
            if isinstance(parsed, (dict, list, tuple)):
                nested = _as_narrative_list(list(parsed) if isinstance(parsed, (list, tuple)) else [parsed])
                result.extend(nested)
                continue
            text = str(raw).strip()
            if text:
                result.append(text)
            continue
        if (
            raw.get("experiment_id")
            or raw.get("expected_result_id")
            or raw.get("summary")
            or raw.get("expected_result")
        ):
            result.append(_format_structured_narrative_item(raw))
            continue
        identifier = str(
            raw.get("id")
            or raw.get("milestone_id")
            or raw.get("decision_id")
            or ""
        ).strip()
        description = str(
            raw.get("description")
            or raw.get("title")
            or raw.get("decision")
            or raw.get("purpose")
            or ""
        ).strip()
        if not description and any(
            key in raw for key in ("experiment_id", "expected_result_id", "summary", "objective")
        ):
            result.append(_format_structured_narrative_item(raw))
            continue
        qualifiers = []
        for label, key in (
            ("timeline", "timeline"),
            ("trigger", "trigger"),
            ("owner", "owner"),
        ):
            text = str(raw.get(key) or "").strip()
            if text:
                qualifiers.append(f"{label}: {text}")
        prefix = f"{identifier}: " if identifier else ""
        suffix = f" ({'; '.join(qualifiers)})" if qualifiers else ""
        text = f"{prefix}{description}{suffix}".strip()
        if text:
            result.append(text)
    return result


_DECISION_LABEL_ONLY = re.compile(
    r"^\s*(?:HD|DP|DECISION|GATE)[-_]?\d+\s*:\s*[.!;,-]*\s*$",
    re.I,
)
_DECISION_CONDITION = re.compile(
    r"\b(?:if|when|once|after|before|upon|unless|until|condition|trigger|"
    r"threshold|review|result|completion|fails?|passes?|milestone|month)\b",
    re.I,
)
_DECISION_ACTION = re.compile(
    r"\b(?:decide|decision|choose|select|approve|reject|proceed|continue|"
    r"stop|halt|pivot|escalate|go\s*/?\s*no[- ]?go|option|otherwise|"
    r"whether|or)\b",
    re.I,
)


def _decision_point_is_substantive(value: Any) -> bool:
    """Require a decision condition plus an actionable choice or trigger."""

    text = " ".join(str(value or "").split()).strip()
    if not text or _DECISION_LABEL_ONLY.fullmatch(text) or len(text) < 20:
        return False
    return bool(_DECISION_CONDITION.search(text) and _DECISION_ACTION.search(text))


def _normalize_human_decision_points(
    value: Any,
) -> tuple[List[str], List[Dict[str, Any]]]:
    points = _as_narrative_list(value)
    kept: List[str] = []
    audit: List[Dict[str, Any]] = []
    for point in points:
        text = " ".join(str(point or "").split()).strip()
        if _decision_point_is_substantive(text):
            kept.append(text)
        else:
            audit.append(
                {
                    "action": "remove_insubstantial_human_decision_point",
                    "value": text,
                    "reason": (
                        "A decision point must state a condition or trigger "
                        "and an explicit choice, action, or pivot."
                    ),
                }
            )
    return list(dict.fromkeys(kept)), audit


_RECOVERABLE_DECISION_CONDITION = re.compile(
    r"\b(?:if|when|once|after|before|upon|unless|until|provided)\b",
    re.I,
)
_RECOVERABLE_DECISION_ACTION = re.compile(
    r"\b(?:halt|stop|pivot|escalate|choose|select|approve|reject|"
    r"proceed|continue|go\s*/?\s*no[- ]?go)\b",
    re.I,
)


def _recover_human_decision_points_from_work_packages(
    packages: Any,
) -> tuple[List[str], List[Dict[str, Any]]]:
    """Recover only explicit decisions already present in valid work packages.

    This is a lossless presentation repair: it copies an existing stop/pivot
    criterion, adds an HD label, and records its source.  It must never invent
    a condition, threshold, option, or scientific action.
    """

    recovered: List[str] = []
    audit: List[Dict[str, Any]] = []
    seen: set[str] = set()
    for package in packages if isinstance(packages, list) else []:
        if not isinstance(package, dict):
            continue
        work_package_id = str(package.get("work_package_id") or "").strip()
        if not work_package_id:
            continue
        for criterion_index, criterion in enumerate(
            _as_narrative_list(package.get("stop_or_pivot_criteria"))
        ):
            text = " ".join(str(criterion or "").split()).strip()
            if not text or text.casefold() in seen:
                continue
            if not (
                len(text) >= 20
                and _RECOVERABLE_DECISION_CONDITION.search(text)
                and _RECOVERABLE_DECISION_ACTION.search(text)
            ):
                continue
            seen.add(text.casefold())
            identifier = f"HD{len(recovered) + 1:02d}"
            recovered_text = f"{identifier}: {text}"
            recovered.append(recovered_text)
            audit.append(
                {
                    "action": "recover_human_decision_point_from_stop_criteria",
                    "source_field": "work_packages[].stop_or_pivot_criteria",
                    "source_work_package_id": work_package_id,
                    "source_index": criterion_index,
                    "source_text": text,
                    "output_id": identifier,
                    "reason": (
                        "Copied an existing conditional stop/pivot criterion; "
                        "no new threshold, conclusion, or action was added."
                    ),
                }
            )
            if len(recovered) >= 3:
                return recovered, audit
    return recovered, audit


def _ensure_hypothesis_statements_in_narrative(
    narrative: Any,
    statements: Any,
) -> str:
    """Ensure the reader sees full scientific hypotheses, not only H IDs.

    This is a deterministic rendering safeguard, not a scientific rewrite.  A
    model may mention a hypothesis only by identifier in its narrative; the
    canonical statement is then appended verbatim under a dedicated heading.
    The gate still checks the final narrative, so an omitted statement cannot
    silently disappear from the published plan.
    """

    text = str(narrative or "").strip()
    missing: list[dict[str, str]] = []
    for raw in statements if isinstance(statements, list) else []:
        if not isinstance(raw, dict):
            continue
        statement = re.sub(r"\s+", " ", str(raw.get("statement") or "")).strip()
        if statement and statement.casefold() not in re.sub(r"\s+", " ", text).casefold():
            missing.append(
                {
                    "hypothesis_id": str(raw.get("hypothesis_id") or "").strip(),
                    "title": str(raw.get("title") or "").strip(),
                    "statement": statement,
                }
            )
    if not missing:
        return text
    lines = [text, "", "## Main Hypothesis Statements", ""]
    for item in missing:
        label = item["hypothesis_id"]
        if item["title"]:
            label += ": " + item["title"]
        lines.extend([f"### {label}", "", item["statement"], ""])
    return "\n".join(lines).strip()


_QUANTITATIVE_SCIENTIFIC_PATTERN = re.compile(
    r"(?:"
    r"[~≈<>±]?\s*\d+(?:\.\d+)?\s*(?:%|ppm|ppb|ppt|nm|µm|μm|um|"
    r"cm-?1|cm\^-?1|hz|khz|mhz|ghz|thz|k|°c|db|riu|w|mw|kw)"
    r"|q(?:uality)?[-\s]?factors?"
    r"[^.!?\n]{0,40}?"
    r"[~≈<>±]?\s*\d+(?:\.\d+)?"
    r")",
    re.I,
)
_NUMERIC_TARGET_PATTERN = re.compile(
    r"(?<!\w)[<>~]?\s*\d+(?:\.\d+)?\s*"
    r"(?:%|ppm|ppb|nm|um|μm|mm|cm|m|rad|radians?|mrad|deg(?:ree)?s?|"
    r"hz|khz|mhz|ghz|thz|k|db|w|mw|kw)(?!\w)",
    re.I,
)
_NUMERIC_SOURCE_MARKER = re.compile(
    r"(?:\[REF:[^\]]+\]|\b(?:doi|source|cited|reported|measured|"
    r"literature|paper|published)\b)",
    re.I,
)
_PROPOSED_TARGET_MARKER = re.compile(
    r"\b(?:proposed|proposal|calibration target|program target|design target|"
    r"to be calibrated|verification_deferred|will test|aim|planned)\b",
    re.I,
)


def _mark_unverified_numeric_target(value: Any) -> str:
    """Expose unsupported numeric choices as proposed calibration targets."""

    text = " ".join(str(value or "").split()).strip()
    if not text or not _NUMERIC_TARGET_PATTERN.search(text):
        return text
    if _NUMERIC_SOURCE_MARKER.search(text):
        return text
    if _PROPOSED_TARGET_MARKER.search(text):
        if "calibration target" in text.casefold():
            return text
        return text + " (proposed calibration target; verification_deferred)."
    return (
        "Proposed calibration target (verification_deferred): " + text
    )


def _reader_facing_text(value: Any) -> str:
    """Remove implementation-facing wording from reader-facing Markdown."""

    text = str(value or "")
    replacements = (
        ("upstream review package", "preceding literature assessment"),
        ("upstream review pipeline", "preceding literature assessment"),
        ("this workflow", "this plan"),
        ("the workflow", "the plan"),
        ("plan-only", "planning stage"),
        ("model turn", "revision"),
        ("tool call", "planning action"),
    )
    for old, new in replacements:
        text = re.sub(re.escape(old), new, text, flags=re.I)
    return text


_INTERNAL_RENDER_TERM = re.compile(
    r"\b(?:upstream review package|upstream review pipeline|this workflow|"
    r"plan-only|model turn|tool call|prompt injection|token budget)\b",
    re.I,
)
_DICT_REPR = re.compile(r"\{\s*['\"](?:[A-Za-z_][\w -]*)['\"]\s*:")
_STANDARD_RENDER_HEADINGS = (
    "Main Hypothesis Statements",
    "Readiness Summary",
    "Future Branches",
    "Traceability Matrix",
    "Source Lineage and Limitations",
    "Verification Status",
    "Technical Details",
    "Datasets: Source",
    "Datasets: Target",
    "Methods",
    "Experiments",
    "Expected Results",
    "Deferred Verification",
    "Canonical Program Specification",
    "Milestones",
    "Human decision points",
    "Unresolved literature needs",
    "References",
)
_NARRATIVE_STANDARD_HEADING = re.compile(
    r"(?im)^\s{0,3}#{1,6}\s*(?:main hypothesis statements|work packages|"
    r"future branches|traceability matrix|source lineage and limitations|"
    r"verification status|technical details|datasets?\s*:\s*(?:source|target)|"
    r"methods|experiments|expected results|deferred verification|"
    r"canonical program specification|milestones|human decision points|"
    r"unresolved literature needs|references)\s*$"
)


def _render_authoritative_strategy(plan: Dict[str, Any]) -> str:
    """Return only a compact strategy paragraph from structured plan fields.

    ``narrative_markdown`` is intentionally not a rendering source.  It may
    contain a complete model-authored document with headings that are already
    represented by canonical fields.  The structured ``strategy`` field is
    therefore the sole source for this high-level section.
    """

    raw = str(plan.get("strategy") or plan.get("rationale") or "").strip()
    if not raw:
        return "No additional high-level strategy declared."
    heading = _NARRATIVE_STANDARD_HEADING.search(raw)
    if heading:
        raw = raw[: heading.start()]
    raw = re.sub(r"(?m)^\s{0,6}#{1,6}\s*", "", raw)
    raw = re.sub(r"(?m)^\s*[-*]\s+", "", raw)
    text = " ".join(
        _reader_facing_text(line).strip()
        for line in raw.splitlines()
        if line.strip()
    ).strip()
    return text or "No additional high-level strategy declared."


def _audit_rendered_plan_content(
    plan: Dict[str, Any],
    markdown: str = "",
    source_terminology_ledger: Optional[Dict[str, Any]] = None,
) -> List[str]:
    """Return deterministic reader-facing quality failures."""

    errors: List[str] = []
    for field in ("experiments", "expected_results"):
        if _DICT_REPR.search(json.dumps(plan.get(field), ensure_ascii=False)):
            errors.append(f"{field}_contains_dict_repr")
        for index, value in enumerate(_as_narrative_list(plan.get(field))):
            if (
                _NUMERIC_TARGET_PATTERN.search(value)
                and not _NUMERIC_SOURCE_MARKER.search(value)
                and "calibration target" not in value.casefold()
            ):
                errors.append(
                    f"{field}_{index}_numeric_target_missing_proposed_label"
                )
    decision_points = plan.get("human_decision_points") or []
    if not decision_points:
        errors.append("human_decision_points_missing_or_insubstantial")
    for index, point in enumerate(decision_points):
        if not _decision_point_is_substantive(point):
            errors.append(f"human_decision_point_{index}_insubstantial")
    summary = plan.get("readiness_summary")
    if not isinstance(summary, dict) or summary.get("scope") != "current_mainline":
        errors.append("readiness_summary_scope_not_declared_as_current_mainline")
    for package in plan.get("work_packages", []):
        if not isinstance(package, dict):
            continue
        status = str(package.get("quantitative_target_status") or "none")
        for text in _as_narrative_list(package.get("stop_or_pivot_criteria")):
            if _NUMERIC_TARGET_PATTERN.search(text) and status not in {
                "proposed_program_target",
                "evidence_anchored",
            }:
                errors.append(
                    f"{package.get('work_package_id', '')}_numeric_stop_target_unlabelled"
                )
            if (
                _NUMERIC_TARGET_PATTERN.search(text)
                and status == "proposed_program_target"
                and "calibration target" not in text.casefold()
            ):
                errors.append(
                    f"{package.get('work_package_id', '')}_numeric_stop_target_missing_proposed_label"
                )
    for index, row in enumerate(plan.get("traceability_matrix") or []):
        if not isinstance(row, dict):
            continue
        for text in _as_narrative_list(row.get("stop_or_pivot_decisions")):
            if (
                _NUMERIC_TARGET_PATTERN.search(text)
                and "calibration target" not in text.casefold()
            ):
                errors.append(
                    f"traceability_row_{index}_numeric_stop_target_missing_proposed_label"
                )
    if markdown:
        if _DICT_REPR.search(markdown):
            errors.append("rendered_plan_contains_dict_repr")
        if re.search(r"(?m)^\s*-\s*(?:HD|DP|DECISION|GATE)[-_]?\d+\s*:\s*$", markdown, re.I):
            errors.append("rendered_plan_contains_empty_decision_label")
        if _INTERNAL_RENDER_TERM.search(markdown):
            errors.append("rendered_plan_contains_internal_process_wording")
        for heading in _STANDARD_RENDER_HEADINGS:
            count = len(
                re.findall(
                    rf"(?im)^\s*##\s+{re.escape(heading)}\s*$",
                    markdown,
                )
            )
            if count > 1:
                errors.append(
                    "rendered_plan_duplicate_heading_"
                    + re.sub(r"[^a-z0-9]+", "_", heading.casefold()).strip("_")
                )
        for package in plan.get("work_packages", []):
            if not isinstance(package, dict):
                continue
            package_id = str(package.get("work_package_id") or "").strip()
            if not package_id:
                continue
            count = len(
                re.findall(
                    rf"(?im)^\s*###\s+{re.escape(package_id)}(?:\s|:|$)",
                    markdown,
                )
            )
            if count > 1:
                errors.append(f"rendered_plan_duplicate_work_package_{package_id}")
    errors.extend(audit_plan_quality(plan, source_terminology_ledger))
    return list(dict.fromkeys(errors))
_PROGRAM_TARGET_LANGUAGE = re.compile(
    r"\b(?:propos(?:e|ed|es)|program target|design target|target|aim|"
    r"hypothes(?:is|ize|ized)|success criterion|decision threshold|"
    r"sampling range|to be calibrated|we will|we aim|will test|"
    r"objective|falsification|stop or pivot)\b",
    re.I,
)
_SCHEDULE_LANGUAGE = re.compile(
    r"\b(?:month|months|timeline|work package|wp\d+|milestone)\b",
    re.I,
)


def _unsupported_narrative_quantitative_claims(
    narrative: str,
) -> List[str]:
    """Find exact scientific numbers presented as facts without disclosure.

    Review synthesis need not cite every sentence.  Exact measurements are a
    narrower case: a research-plan narrative must either identify them as a
    proposed program choice or avoid presenting them as established findings.
    """

    issues: List[str] = []
    sentences = re.split(r"(?<=[.!?])\s+", str(narrative or ""))
    for sentence in sentences:
        compact = " ".join(sentence.split())
        if not _QUANTITATIVE_SCIENTIFIC_PATTERN.search(compact):
            continue
        if _PROGRAM_TARGET_LANGUAGE.search(compact):
            continue
        if _SCHEDULE_LANGUAGE.search(compact):
            continue
        issues.append(compact[:220])
    return issues


def _parse_json_object(raw: Any) -> Dict[str, Any]:
    """Parse model JSON with a local, deterministic repair fallback.

    The repair step is deliberately local: malformed escaping must not consume
    another expensive model turn when the intended object is recoverable.
    """

    if isinstance(raw, dict):
        return dict(raw)
    text = str(raw or "").strip()
    value: Any
    try:
        value = json.loads(text)
    except Exception:
        try:
            from json_repair import repair_json  # type: ignore

            value = repair_json(text, return_objects=True)
        except Exception:
            raise
    if isinstance(value, list):
        return {"items": value}
    if not isinstance(value, dict):
        raise ValueError("payload_must_be_json_object")
    return value


def _normalize_opportunity_payload(parsed: Dict[str, Any]) -> Dict[str, Any]:
    normalized = dict(parsed)
    # Economical models often preserve the right item schema under a more
    # descriptive root key.  Accept harmless container aliases instead of
    # spending repeated calls on a zero-item map.
    if not isinstance(normalized.get("opportunities"), list):
        for alias in (
            "research_opportunities",
            "opportunity_map",
            "research_opportunity_map",
            "candidates",
            "items",
        ):
            value = normalized.get(alias)
            if isinstance(value, list):
                normalized["opportunities"] = value
                break
            if isinstance(value, dict):
                nested = value.get("opportunities") or value.get("items")
                if isinstance(nested, list):
                    normalized["opportunities"] = nested
                    break
    items = []
    for raw in normalized.get("opportunities", []):
        if not isinstance(raw, dict):
            items.append(raw)
            continue
        item = dict(raw)
        item["opportunity_id"] = _canonical_id(
            item.get("opportunity_id") or item.get("id"), "OP"
        )
        item["problem"] = str(
            item.get("problem")
            or item.get("gap_or_boundary")
            or item.get("research_gap")
            or item.get("description")
            or ""
        )
        item["why_it_matters"] = str(
            item.get("why_it_matters")
            or item.get("significance")
            or item.get("importance")
            or ""
        )
        origin_raw = str(
            item.get("origin_type")
            or item.get("origin")
            or "evidence_gap"
        ).lower()
        origin_aliases = {
            "missing_benchmark": "benchmark_gap",
            "benchmarking_gap": "benchmark_gap",
            "missing_mechanism_or_model": "method_gap",
            "mechanism_gap": "method_gap",
            "model_gap": "method_gap",
            "deployment_link": "deployment_gap",
            "boundary_of_consensus": "consensus_boundary",
            "contradictory_evidence_or_measurement": "controversy",
            "contradictory_evidence": "controversy",
        }
        item["origin_type"] = origin_aliases.get(origin_raw, origin_raw)
        evidence_basis = item.get("evidence_basis", "")
        extracted_chunks: List[str] = []
        if isinstance(evidence_basis, list):
            extracted_chunks = [str(value) for value in evidence_basis]
            evidence_basis = (
                "Canonical evidence chunks listed in supporting_chunk_ids."
            )
        elif isinstance(evidence_basis, str):
            try:
                decoded = json.loads(evidence_basis)
                if isinstance(decoded, list):
                    extracted_chunks = [
                        str(value) for value in decoded
                    ]
                    evidence_basis = (
                        "Canonical evidence chunks listed in "
                        "supporting_chunk_ids."
                    )
            except Exception:
                pass
        item["evidence_basis"] = str(evidence_basis)
        if not item.get("supporting_chunk_ids") and extracted_chunks:
            item["supporting_chunk_ids"] = extracted_chunks
        item["supporting_paper_ids"] = list(
            item.get("supporting_paper_ids") or []
        )
        item["source_section_ids"] = list(
            item.get("source_section_ids") or []
        )
        item["author_inference"] = str(
            item.get("author_inference")
            or item.get("inference")
            or item.get("reasoning")
            or ""
        )
        item["uncertainty"] = str(
            item.get("uncertainty")
            or item.get("risk_or_uncertainty")
            or item.get("limitations")
            or ""
        )
        item["recommended_next_evidence"] = _as_string_list(
            item.get("recommended_next_evidence")
            or item.get("next_evidence")
        )
        # Never allow a semantic label to imply evidence that was not actually
        # attached. Downgrading preserves the useful lead without certifying it.
        if not (
            item.get("supporting_paper_ids")
            or item.get("supporting_chunk_ids")
        ):
            item["evidence_status"] = "open_gap"
        elif not item.get("evidence_status"):
            item["evidence_status"] = "partially_supported"
        items.append(item)
    normalized["opportunities"] = items
    return normalized


def _normalize_hypothesis_payload(parsed: Dict[str, Any]) -> Dict[str, Any]:
    normalized = dict(parsed)
    items = []
    for raw in parsed.get("hypotheses", []):
        if not isinstance(raw, dict):
            items.append(raw)
            continue
        item = dict(raw)
        item["hypothesis_id"] = _canonical_id(
            item.get("hypothesis_id") or item.get("id"), "H"
        )
        item["source_opportunity_ids"] = [
            _canonical_id(value, "OP")
            for value in _as_string_list(
                item.get("source_opportunity_ids")
                or item.get("source_opportunities")
                or item.get("opportunity_ids")
            )
        ]
        for key in (
            "supporting_paper_ids",
            "supporting_chunk_ids",
            "inference_chain",
            "assumptions",
            "alternative_explanations",
            "falsification_conditions",
        ):
            item[key] = _as_string_list(item.get(key))
        readiness = str(item.get("readiness") or "").strip().lower()
        normalized_readiness = _READINESS_ALIASES.get(
            readiness, readiness or "needs_more_literature"
        )
        item["readiness"] = normalized_readiness
        raw_confidence = (
            str(item.get("confidence") or "")
            .strip()
            .casefold()
            .replace(" ", "_")
        )
        normalized_confidence = _CONFIDENCE_ALIASES.get(
            raw_confidence, raw_confidence
        )
        if normalized_confidence:
            item["confidence"] = normalized_confidence
        item["readiness_calibration"] = dict(
            item.get("readiness_calibration")
            if isinstance(item.get("readiness_calibration"), dict)
            else {}
        )
        if readiness and normalized_readiness != readiness:
            item["readiness_calibration"].setdefault(
                "alias_correction",
                {
                    "from": readiness,
                    "to": normalized_readiness,
                    "reason": "Non-canonical readiness label was mapped to the nearest safe enum without upgrading maturity.",
                },
            )
        # A precise quantitative prediction whose prior-art status is still
        # unknown is a useful lead, not an experiment-ready established result.
        # Downgrade epistemic confidence without deleting the hypothesis.
        statement = str(item.get("statement") or "")
        contains_quantitative_commitment = bool(
            re.search(
                r"(?:\d+(?:\.\d+)?|[<>]=?|[\u00b1\u2248\u221d]|"
                r"\^[+-]?\d+|lambda\s*/\s*\d+)",
                statement,
                re.I,
            )
        )
        quantitative_status = str(
            item.get("quantitative_commitment_status") or ""
        ).strip().lower()
        quantitative_status = {
            "proposed": "proposed_program_target",
            "proposed_target": "proposed_program_target",
            "program_target": "proposed_program_target",
            "design_target": "proposed_program_target",
            "supported": "evidence_anchored",
            "evidence_based": "evidence_anchored",
        }.get(quantitative_status, quantitative_status)
        if contains_quantitative_commitment and quantitative_status not in {
            "evidence_anchored",
            "proposed_program_target",
        }:
            quantitative_status = "proposed_program_target"
        if not contains_quantitative_commitment:
            quantitative_status = "none"
        item["quantitative_commitment_status"] = quantitative_status
        if quantitative_status == "proposed_program_target":
            disclosure = (
                "Any numerical threshold in this hypothesis is a proposed "
                "program-design target to be calibrated, not a value claimed "
                "as established by the cited literature."
            )
            if disclosure not in item["assumptions"]:
                item["assumptions"].append(disclosure)
            if str(item.get("confidence") or "").lower() == "high":
                item["confidence"] = "medium"
            if item["readiness"] == "ready":
                item["readiness"] = "needs_more_literature"
        if (
            item.get("novelty_status")
            == "unknown_requires_prior_art_search"
            and contains_quantitative_commitment
        ):
            item["confidence"] = "low"
            if item["readiness"] == "ready":
                item["readiness"] = "needs_more_literature"
        items.append(item)
    normalized["hypotheses"] = items
    return normalized


def _calibrate_hypothesis_readiness(
    parsed: Dict[str, Any],
    opportunities: Dict[str, Any],
    permission_map: Dict[str, Dict[str, str]],
) -> tuple[Dict[str, Any], List[Dict[str, Any]]]:
    """Downgrade hypotheses that have no factual premise, never upgrade them.

    An opportunity can be useful because it identifies a gap or a promising
    direction even when its sources are contextual.  That permission does not
    make the resulting hypothesis ``ready``.  This deterministic pass keeps
    the hypothesis and records the correction so a model does not spend a
    whole repair turn arguing with a safety gate.
    """

    normalized = dict(parsed)
    paper_permissions = permission_map.get("paper_permissions", {})
    chunk_permissions = permission_map.get("chunk_permissions", {})
    opportunity_by_id = {
        str(item.get("opportunity_id")): item
        for item in opportunities.get("opportunities", [])
        if isinstance(item, dict) and item.get("opportunity_id")
    }
    audits: List[Dict[str, Any]] = []
    items: List[Any] = []
    for raw in parsed.get("hypotheses", []):
        if not isinstance(raw, dict):
            items.append(raw)
            continue
        item = dict(raw)
        item["readiness_calibration"] = dict(
            item.get("readiness_calibration")
            if isinstance(item.get("readiness_calibration"), dict)
            else {}
        )
        readiness = str(item.get("readiness") or "needs_more_literature").strip().lower()
        if readiness == "ready":
            paper_ids = _as_string_list(item.get("supporting_paper_ids"))
            chunk_ids = _as_string_list(item.get("supporting_chunk_ids"))
            factual_papers = [
                paper_id
                for paper_id in paper_ids
                if _PERMISSION_RANK.get(str(paper_permissions.get(paper_id)), -1) >= 2
            ]
            factual_chunks = [
                chunk_id
                for chunk_id in chunk_ids
                if _PERMISSION_RANK.get(str(chunk_permissions.get(chunk_id)), -1) >= 2
            ]
            source_opportunities = [
                opportunity_by_id.get(opportunity_id, {})
                for opportunity_id in _as_string_list(item.get("source_opportunity_ids"))
            ]
            source_statuses = {
                str(opportunity.get("evidence_status") or "").strip().lower()
                for opportunity in source_opportunities
                if isinstance(opportunity, dict)
            }
            if not factual_papers and not factual_chunks:
                reasons = [
                    "No supporting paper or text chunk has factual_support permission.",
                ]
                if source_statuses & {"open_gap", "partially_supported"}:
                    reasons.append(
                        "The source opportunity is a gap or partially supported direction, so its inference cannot be treated as execution-ready."
                    )
                item["readiness"] = "needs_more_literature"
                correction = {
                    "from": "ready",
                    "to": "needs_more_literature",
                    "reason": " ".join(reasons),
                    "factual_support_paper_ids": factual_papers,
                    "factual_support_chunk_ids": factual_chunks,
                }
                item["readiness_calibration"]["permission_correction"] = correction
                audits.append(
                    {
                        "hypothesis_id": str(item.get("hypothesis_id") or ""),
                        "original_readiness": "ready",
                        "calibrated_readiness": "needs_more_literature",
                        "reason": correction["reason"],
                    }
                )
        items.append(item)
    normalized["hypotheses"] = items
    return normalized, audits


def _normalize_plan_payload(parsed: Dict[str, Any]) -> Dict[str, Any]:
    normalized = dict(parsed)
    normalization_audit: List[Dict[str, Any]] = [
        dict(item)
        for item in (parsed.get("normalization_audit") or [])
        if isinstance(item, dict)
    ]
    packages = []
    for raw in parsed.get("work_packages", []):
        if not isinstance(raw, dict):
            packages.append(raw)
            continue
        item = dict(raw)
        item["work_package_id"] = _canonical_id(
            item.get("work_package_id") or item.get("id"), "WP"
        )
        item["hypothesis_ids"] = [
            _canonical_id(value, "H")
            for value in _as_string_list(
                item.get("hypothesis_ids")
                or item.get("linked_hypotheses")
                or item.get("hypotheses")
            )
        ]
        item["opportunity_ids"] = [
            _canonical_id(value, "OP")
            for value in _as_string_list(
                item.get("opportunity_ids")
                or item.get("linked_opportunities")
                or item.get("opportunities")
            )
        ]
        dependencies = _as_string_list(item.get("dependencies"))
        if len(dependencies) == 1 and dependencies[0].lower().rstrip(".") in {
            "none",
            "n/a",
            "no dependencies",
        }:
            dependencies = []
        item["dependencies"] = [
            _canonical_id(value, "WP")
            for value in dependencies
            if re.search(r"\d", value)
        ]
        item["platform_id"] = str(
            item.get("platform_id") or item.get("shared_platform_id") or ""
        ).strip()
        item["platform_compatibility_key"] = str(
            item.get("platform_compatibility_key")
            or item.get("platform_key")
            or ""
        ).strip()
        item["metric_ids"] = _as_string_list(
            item.get("metric_ids") or item.get("metrics_ids")
        )
        item["baseline_ids"] = _as_string_list(
            item.get("baseline_ids") or item.get("baselines_ids")
        )
        item["objective"] = str(
            item.get("objective")
            or item.get("aim")
            or item.get("purpose")
            or (
                f"Execute and evaluate {item.get('title', '').strip()}."
                if str(item.get("title") or "").strip()
                else ""
            )
        )
        aliases = {
            "expected_outputs": ("expected_outputs", "outputs", "deliverables"),
            "controls_or_baselines": (
                "controls_or_baselines",
                "controls_baselines",
                "controls",
                "baselines",
            ),
            "stop_or_pivot_criteria": (
                "stop_or_pivot_criteria",
                "stop_pivot_criteria",
                "stop_criteria",
                "pivot_criteria",
            ),
        }
        for canonical, candidates in aliases.items():
            selected: Any = None
            for candidate in candidates:
                if item.get(candidate) not in (None, "", []):
                    selected = item.get(candidate)
                    break
            item[canonical] = _as_string_list(selected)
        original_stop_criteria = list(item.get("stop_or_pivot_criteria", []))
        item["stop_or_pivot_criteria"] = [
            _mark_unverified_numeric_target(value)
            for value in item["stop_or_pivot_criteria"]
        ]
        if item["stop_or_pivot_criteria"] != original_stop_criteria:
            normalization_audit.append(
                {
                    "action": "label_unverified_numeric_stop_target",
                    "work_package_id": item.get("work_package_id", ""),
                    "source_field": "stop_or_pivot_criteria",
                    "label": "proposed calibration target; verification_deferred",
                }
            )
        for key in (
            "methods",
            "inputs",
            "evaluation_metrics",
            "risks",
        ):
            item[key] = _as_string_list(item.get(key))
        quantitative_text = " ".join(
            [
                str(item.get("objective") or ""),
                *item.get("methods", []),
                *item.get("expected_outputs", []),
                *item.get("evaluation_metrics", []),
                *item.get("stop_or_pivot_criteria", []),
            ]
        )
        contains_quantitative_target = bool(
            re.search(
                r"(?:\d+(?:\.\d+)?|[<>]=?|[\u00b1\u2248\u221d]|"
                r"\^[+-]?\d+|lambda\s*/\s*\d+)",
                quantitative_text,
                re.I,
            )
        )
        target_status = str(
            item.get("quantitative_target_status") or ""
        ).strip().lower()
        target_status = {
            "proposed": "proposed_program_target",
            "proposed_target": "proposed_program_target",
            "program_target": "proposed_program_target",
            "design_target": "proposed_program_target",
            "supported": "evidence_anchored",
            "evidence_based": "evidence_anchored",
        }.get(target_status, target_status)
        if contains_quantitative_target and target_status not in {
            "evidence_anchored",
            "proposed_program_target",
        }:
            target_status = "proposed_program_target"
        if not contains_quantitative_target:
            target_status = "none"
        quantitative_source = any(
            item.get(key)
            for key in (
                "quantitative_target_source",
                "quantitative_target_source_ids",
                "source_chunk_ids",
                "supporting_chunk_ids",
                "supporting_paper_ids",
            )
        )
        if contains_quantitative_target and not quantitative_source:
            if target_status != "proposed_program_target":
                normalization_audit.append(
                    {
                        "action": "downgrade_unanchored_numeric_target",
                        "work_package_id": item.get("work_package_id", ""),
                        "from": target_status or "none",
                        "to": "proposed_program_target",
                        "reason": "No factual source identifier was supplied.",
                    }
                )
            target_status = "proposed_program_target"
        item["quantitative_target_status"] = target_status
        item["quantitative_target_provenance"] = (
            "proposed_calibration_target"
            if target_status == "proposed_program_target"
            else (
                "source_anchored"
                if target_status == "evidence_anchored"
                else "not_applicable"
            )
        )
        # This program generator plans validation; it does not execute it.
        # Normalizing the state here makes the distinction durable even when a
        # model uses optimistic prose such as "will demonstrate".
        item["verification_status"] = "verification_deferred"
        item["verification_rationale"] = (
            "Planned work only: no experiment, simulation, or data analysis "
            "has been executed in this plan."
        )
        packages.append(item)
    normalized["work_packages"] = packages
    for key in (
        "objectives",
        "unresolved_literature_needs",
    ):
        normalized[key] = _as_string_list(normalized.get(key))
    normalized["milestones"] = _as_narrative_list(normalized.get("milestones"))
    decision_points, decision_audits = (
        _normalize_human_decision_points(normalized.get("human_decision_points"))
    )
    normalization_audit.extend(decision_audits)
    if not decision_points:
        recovered_points, recovery_audits = (
            _recover_human_decision_points_from_work_packages(packages)
        )
        decision_points = recovered_points
        normalization_audit.extend(recovery_audits)
    normalized["human_decision_points"] = decision_points
    methods_summary = list(
        dict.fromkeys(
            value
            for package in packages
            if isinstance(package, dict)
            for value in package.get("methods", [])
            if value
        )
    )
    inputs = list(
        dict.fromkeys(
            value
            for package in packages
            if isinstance(package, dict)
            for value in package.get("inputs", [])
            if value
        )
    )
    expected = list(
        dict.fromkeys(
            value
            for package in packages
            if isinstance(package, dict)
            for value in package.get("expected_outputs", [])
            if value
        )
    )
    normalized["problem_statement"] = str(
        normalized.get("problem_statement")
        or normalized.get("research_question")
        or ""
    )
    normalized["rationale"] = str(
        normalized.get("rationale")
        or normalized.get("strategy")
        or ""
    )
    normalized["technical_details"] = _as_string_list(
        normalized.get("technical_details")
    ) or methods_summary[:12]
    normalized["dataset_source"] = _as_string_list(
        normalized.get("dataset_source")
        or normalized.get("source_datasets")
        or normalized.get("datasets_source")
    ) or [
        "Traceable literature evidence and the canonical text/visual chunks "
        "listed in the evidence package."
    ]
    normalized["dataset_target"] = _as_string_list(
        normalized.get("dataset_target")
        or normalized.get("target_datasets")
        or normalized.get("datasets_target")
    ) or inputs[:12] or [
        "A future validation dataset or measurement record to be specified "
        "before execution."
    ]
    normalized["methods_summary"] = _as_string_list(
        normalized.get("methods_summary") or normalized.get("methods")
    ) or methods_summary[:12]
    normalized["experiments"] = _as_narrative_list(
        normalized.get("experiments") or normalized.get("planned_experiments")
    ) or [
        "Verification route deferred: execute only after the stated inputs, "
        "controls, and ethical or operational approvals are available."
    ]
    normalized["expected_results"] = _as_narrative_list(
        normalized.get("expected_results")
        or normalized.get("anticipated_results")
    ) or expected[:12] or [
        "No result is claimed. Expected observations remain to be verified."
    ]
    normalized["experiments"] = [
        _mark_unverified_numeric_target(value)
        for value in normalized["experiments"]
    ]
    normalized["expected_results"] = [
        _mark_unverified_numeric_target(value)
        for value in normalized["expected_results"]
    ]
    normalized["paper_abstract"] = str(
        normalized.get("paper_abstract")
        or normalized.get("abstract")
        or (
            "This proposed research program translates a literature-grounded "
            "problem into falsifiable hypotheses and planned validation routes. "
            "All experimental, simulation, and data-analysis outcomes are "
            "verification_deferred."
        )
    )
    normalized["reference_paper_ids"] = _as_string_list(
        normalized.get("reference_paper_ids")
        or normalized.get("references")
    )
    normalized["results_status"] = "verification_deferred"
    normalized["verification_deferred"] = list(
        dict.fromkeys(
            _as_string_list(normalized.get("verification_deferred"))
            + [
                "No experiment, simulation, or data-analysis result has been "
                "executed in this plan-generation run.",
                "All proposed technical validation remains verification_deferred "
                "until an execution environment and approved data are supplied.",
            ]
        )
    )
    # The provider computes the canonical counts after validation. Models often
    # return prose here; it must not create a second correction turn.
    # Always recompute this from canonical work-package readiness after
    # validation.  Free-form model summaries must never trigger a repair turn.
    normalized["readiness_summary"] = {}
    normalized["program_focus_gate_id"] = str(
        normalized.get("program_focus_gate_id") or normalized.get("focus_gate_id") or ""
    ).strip()
    normalized["main_problem"] = (
        dict(normalized.get("main_problem"))
        if isinstance(normalized.get("main_problem"), dict)
        else {}
    )
    normalized["project_type"] = str(normalized.get("project_type") or "").strip().lower()
    for key in ("shared_platform", "unified_evaluation", "source_context"):
        normalized[key] = (
            dict(normalized.get(key))
            if isinstance(normalized.get(key), dict)
            else {}
        )
    normalized["boundaries"] = (
        dict(normalized.get("boundaries"))
        if isinstance(normalized.get("boundaries"), dict)
        else {}
    )
    normalized["main_hypothesis_ids"] = [
        _canonical_id(value, "H")
        for value in _as_string_list(normalized.get("main_hypothesis_ids"))
    ]
    normalized["future_hypothesis_ids"] = [
        _canonical_id(value, "H")
        for value in _as_string_list(normalized.get("future_hypothesis_ids"))
    ]
    normalized["future_branches"] = [
        dict(item) for item in (normalized.get("future_branches") or [])
        if isinstance(item, dict)
    ]
    normalized["hypothesis_dependencies"] = [
        dict(item) for item in (normalized.get("hypothesis_dependencies") or [])
        if isinstance(item, dict)
    ]
    normalized_matrix: List[Dict[str, Any]] = []
    for raw_matrix_item in normalized.get("traceability_matrix") or []:
        if not isinstance(raw_matrix_item, dict):
            continue
        matrix_item = dict(raw_matrix_item)
        for key in (
            "proposed_tests",
            "metrics",
            "baselines",
            "falsification_conditions",
            "stop_or_pivot_decisions",
        ):
            matrix_item[key] = _as_narrative_list(matrix_item.get(key))
        original_stop = list(matrix_item["stop_or_pivot_decisions"])
        matrix_item["stop_or_pivot_decisions"] = [
            _mark_unverified_numeric_target(value)
            for value in matrix_item["stop_or_pivot_decisions"]
        ]
        if matrix_item["stop_or_pivot_decisions"] != original_stop:
            normalization_audit.append(
                {
                    "action": "label_unverified_numeric_traceability_stop_target",
                    "work_package_id": matrix_item.get("work_package_id", ""),
                    "source_field": "traceability_matrix.stop_or_pivot_decisions",
                    "label": "proposed calibration target; verification_deferred",
                }
            )
        normalized_matrix.append(matrix_item)
    normalized["traceability_matrix"] = normalized_matrix
    normalized["source_limitations"] = _as_string_list(
        normalized.get("source_limitations") or normalized.get("limitations")
    )
    normalized["main_hypothesis_statements"] = [
        dict(item) for item in (normalized.get("main_hypothesis_statements") or [])
        if isinstance(item, dict)
    ]
    normalized["normalization_audit"] = normalization_audit
    return normalized


def _sanitize_plan_packages_to_focus(
    plan: Dict[str, Any],
    focus_gate: Dict[str, Any],
) -> tuple[Dict[str, Any], List[Dict[str, Any]]]:
    """Keep current work packages on the accepted project spine.

    The plan writer may still echo a future hypothesis, a non-selected
    opportunity, or an identifier it invented while composing a draft.  That
    is a structural error, not a reason to let contaminated work packages
    reach the published plan.  This pass removes those links, drops packages
    that have no current link left, and records every correction for audit.
    It never creates a new hypothesis, opportunity, platform, or evidence.
    """

    normalized = dict(plan)
    selected_hypotheses = {
        str(value)
        for value in focus_gate.get("main_hypothesis_ids", [])
        if value
    }
    selected_opportunities = {
        str(value)
        for value in focus_gate.get("selected_opportunity_ids", [])
        if value
    }
    future_hypotheses = {
        str(value)
        for value in focus_gate.get("future_hypothesis_ids", [])
        if value
    }
    future_opportunities = {
        str(item.get("opportunity_id"))
        for item in focus_gate.get("future_branches", [])
        if isinstance(item, dict) and item.get("opportunity_id")
    }
    corrections: List[Dict[str, Any]] = []
    retained: List[Dict[str, Any]] = []
    for package in normalized.get("work_packages", []):
        if not isinstance(package, dict):
            corrections.append(
                {"action": "drop_non_object_work_package"}
            )
            continue
        item = dict(package)
        package_id = str(item.get("work_package_id") or "")
        raw_hypotheses = [
            str(value) for value in item.get("hypothesis_ids", []) if value
        ]
        raw_opportunities = [
            str(value) for value in item.get("opportunity_ids", []) if value
        ]
        kept_hypotheses = [
            value for value in raw_hypotheses if value in selected_hypotheses
        ]
        kept_opportunities = [
            value
            for value in raw_opportunities
            if value in selected_opportunities
        ]
        removed_hypotheses = [
            value for value in raw_hypotheses if value not in kept_hypotheses
        ]
        removed_opportunities = [
            value
            for value in raw_opportunities
            if value not in kept_opportunities
        ]
        if removed_hypotheses or removed_opportunities:
            corrections.append(
                {
                    "action": "remove_non_current_links",
                    "work_package_id": package_id,
                    "removed_hypothesis_ids": removed_hypotheses,
                    "removed_opportunity_ids": removed_opportunities,
                    "future_hypothesis_ids": [
                        value
                        for value in removed_hypotheses
                        if value in future_hypotheses
                    ],
                    "future_opportunity_ids": [
                        value
                        for value in removed_opportunities
                        if value in future_opportunities
                    ],
                }
            )
        item["hypothesis_ids"] = list(dict.fromkeys(kept_hypotheses))
        item["opportunity_ids"] = list(dict.fromkeys(kept_opportunities))
        if not item["hypothesis_ids"] and not item["opportunity_ids"]:
            corrections.append(
                {
                    "action": "drop_future_or_unlinked_work_package",
                    "work_package_id": package_id,
                }
            )
            continue
        retained.append(item)
    normalized["work_packages"] = retained

    platform = focus_gate.get("shared_platform") or {}
    accepted_platform_id = str(platform.get("platform_id") or "").strip()
    accepted_key = str(
        platform.get("compatibility_key")
        or platform.get("platform_compatibility_key")
        or ""
    ).strip()
    for item in retained:
        package_id = str(item.get("work_package_id") or "")
        if accepted_platform_id and item.get("platform_id") != accepted_platform_id:
            corrections.append(
                {
                    "action": "normalize_platform_id_to_focus_gate",
                    "work_package_id": package_id,
                    "from": str(item.get("platform_id") or ""),
                    "to": accepted_platform_id,
                }
            )
            item["platform_id"] = accepted_platform_id
        if accepted_key and item.get("platform_compatibility_key") != accepted_key:
            corrections.append(
                {
                    "action": "normalize_platform_compatibility_key_to_focus_gate",
                    "work_package_id": package_id,
                    "from": str(item.get("platform_compatibility_key") or ""),
                    "to": accepted_key,
                }
            )
            item["platform_compatibility_key"] = accepted_key
    return normalized, corrections


def _traceability_matrix_is_complete(
    matrix: Any,
    focus_gate: Dict[str, Any],
    plan: Dict[str, Any],
) -> bool:
    """Check whether an existing matrix covers the current accepted spine.

    This is deliberately a structural check.  It does not judge scientific
    validity and it never treats a model's prose as evidence.  The matrix is
    complete only when every selected opportunity, main hypothesis, and
    current work package has a row with all required non-empty fields.
    """

    rows = [item for item in (matrix or []) if isinstance(item, dict)]
    if not rows:
        return False
    selected_opportunities = set(
        _as_string_list(focus_gate.get("selected_opportunity_ids"))
    )
    selected_hypotheses = set(
        _as_string_list(focus_gate.get("main_hypothesis_ids"))
    )
    work_package_ids = {
        str(item.get("work_package_id") or "").strip()
        for item in plan.get("work_packages", [])
        if isinstance(item, dict) and str(item.get("work_package_id") or "").strip()
    }
    if not selected_opportunities or not selected_hypotheses or not work_package_ids:
        return False
    matrix_opportunities: set[str] = set()
    matrix_hypotheses: set[str] = set()
    matrix_work_packages: set[str] = set()
    required = (
        "proposed_tests",
        "metrics",
        "baselines",
        "falsification_conditions",
        "stop_or_pivot_decisions",
    )
    problem_id = str(
        (focus_gate.get("main_problem") or {}).get("problem_id") or ""
    ).strip()
    row_keys: set[tuple[str, str, str, str]] = set()
    for row in rows:
        if str(row.get("problem_id") or "").strip() != problem_id:
            return False
        opportunity_id = str(row.get("opportunity_id") or "").strip()
        hypothesis_id = str(row.get("hypothesis_id") or "").strip()
        work_package_id = str(row.get("work_package_id") or "").strip()
        if opportunity_id not in selected_opportunities:
            return False
        if hypothesis_id not in selected_hypotheses:
            return False
        if work_package_id not in work_package_ids:
            return False
        if any(not _as_string_list(row.get(key)) for key in required):
            return False
        row_key = (problem_id, opportunity_id, hypothesis_id, work_package_id)
        if row_key in row_keys:
            return False
        row_keys.add(row_key)
        matrix_opportunities.add(opportunity_id)
        matrix_hypotheses.add(hypothesis_id)
        matrix_work_packages.add(work_package_id)
    return (
        matrix_opportunities == selected_opportunities
        and matrix_hypotheses == selected_hypotheses
        and matrix_work_packages == work_package_ids
    )


def _build_plan_only_traceability_matrix(
    plan: Dict[str, Any],
    focus_gate: Dict[str, Any],
    hypothesis_rows: Dict[str, Dict[str, Any]],
    *,
    force_rebuild: bool = False,
) -> tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """Build missing plan-only traceability rows from accepted local data.

    The model may omit the root matrix even after producing valid work
    packages.  This repair copies only IDs and text already present in the
    accepted focus gate, hypothesis portfolio, or current work packages.  It
    intentionally leaves empty fields empty; the normal validator must still
    report a real missing input instead of receiving fabricated test plans or
    results.
    """

    existing = [
        dict(item)
        for item in (focus_gate.get("traceability_matrix") or [])
        if isinstance(item, dict)
    ]
    if not force_rebuild and _traceability_matrix_is_complete(
        existing, focus_gate, plan
    ):
        return existing, {
            "action": "preserve_complete_traceability_matrix",
            "previous_row_count": len(existing),
            "generated_row_count": len(existing),
            "source_policy": "accepted focus gate and current work packages only",
        }

    selected_opportunities = set(
        _as_string_list(focus_gate.get("selected_opportunity_ids"))
    )
    selected_hypotheses = set(
        _as_string_list(focus_gate.get("main_hypothesis_ids"))
    )
    problem_id = str(
        (focus_gate.get("main_problem") or {}).get("problem_id") or ""
    ).strip()
    rows: List[Dict[str, Any]] = []
    fallback_usage: List[Dict[str, Any]] = []
    selected_hypothesis_list = sorted(selected_hypotheses)
    selected_opportunity_list = sorted(selected_opportunities)
    for package in plan.get("work_packages", []):
        if not isinstance(package, dict):
            continue
        package_id = str(package.get("work_package_id") or "").strip()
        hypothesis_ids = list(
            dict.fromkeys(
                value
                for value in _as_string_list(package.get("hypothesis_ids"))
                if value in selected_hypotheses
            )
        )
        opportunity_ids = list(
            dict.fromkeys(
                value
                for value in _as_string_list(package.get("opportunity_ids"))
                if value in selected_opportunities
            )
        )

        # A literature-maturation package may be linked only to the selected
        # opportunity.  Once the accepted focus contains exactly one main
        # hypothesis, that linkage is unambiguous and can be recorded without
        # inventing a work package or scientific claim.  With multiple main
        # hypotheses we keep the missing link visible and fail closed rather
        # than guessing which hypothesis the package serves.
        if (
            not hypothesis_ids
            and len(selected_hypothesis_list) == 1
            and opportunity_ids
        ):
            hypothesis_ids = [selected_hypothesis_list[0]]
            fallback_usage.append(
                {
                    "work_package_id": package_id,
                    "matrix_field": "hypothesis_id",
                    "source_field": "sole_selected_focus_hypothesis",
                    "reason": (
                        "Attached the sole accepted main hypothesis to an "
                        "opportunity-linked current work package; no new "
                        "identifier or work package was created."
                    ),
                }
            )
        if (
            not opportunity_ids
            and len(selected_opportunity_list) == 1
            and hypothesis_ids
        ):
            opportunity_ids = [selected_opportunity_list[0]]
            fallback_usage.append(
                {
                    "work_package_id": package_id,
                    "matrix_field": "opportunity_id",
                    "source_field": "sole_selected_focus_opportunity",
                    "reason": (
                        "Attached the sole accepted opportunity to a "
                        "hypothesis-linked current work package; no new "
                        "identifier or work package was created."
                    ),
                }
            )

        proposed_tests = _as_string_list(package.get("proposed_tests"))
        if not proposed_tests:
            proposed_tests = _as_string_list(package.get("methods"))
            if proposed_tests:
                fallback_usage.append(
                    {
                        "work_package_id": package_id,
                        "matrix_field": "proposed_tests",
                        "source_field": "methods",
                    }
                )
        metrics = _as_string_list(package.get("metric_ids"))
        if not metrics:
            metrics = _as_string_list(package.get("evaluation_metrics"))
            if metrics:
                fallback_usage.append(
                    {
                        "work_package_id": package_id,
                        "matrix_field": "metrics",
                        "source_field": "evaluation_metrics",
                        "reason": (
                            "This preparatory package has no unified metric IDs; "
                            "preserve its existing evaluation metrics as trace text."
                        ),
                    }
                )
        baselines = _as_string_list(package.get("baseline_ids"))
        if not baselines:
            baselines = _as_string_list(package.get("controls_or_baselines"))
            if baselines:
                fallback_usage.append(
                    {
                        "work_package_id": package_id,
                        "matrix_field": "baselines",
                        "source_field": "controls_or_baselines",
                        "reason": (
                            "No baseline IDs were present; preserve the package's "
                            "existing control descriptions without inventing IDs."
                        ),
                    }
                )
        falsification = _as_string_list(
            package.get("falsification_conditions")
        )
        if not falsification:
            for hypothesis_id in hypothesis_ids:
                falsification.extend(
                    _as_string_list(
                        hypothesis_rows.get(hypothesis_id, {}).get(
                            "falsification_conditions"
                        )
                    )
                )
            falsification = list(dict.fromkeys(falsification))
            if falsification:
                fallback_usage.append(
                    {
                        "work_package_id": package_id,
                        "matrix_field": "falsification_conditions",
                        "source_field": "linked_hypothesis.falsification_conditions",
                    }
                )
        stop_or_pivot = _as_string_list(
            package.get("stop_or_pivot_decisions")
        ) or _as_string_list(package.get("stop_or_pivot_criteria"))
        if stop_or_pivot and not package.get("stop_or_pivot_decisions"):
            fallback_usage.append(
                {
                    "work_package_id": package_id,
                    "matrix_field": "stop_or_pivot_decisions",
                    "source_field": "stop_or_pivot_criteria",
                }
            )

        # Empty link lists are kept as empty placeholders so the validator
        # reports the missing link.  They are never replaced with guessed IDs.
        hypothesis_values = hypothesis_ids or [""]
        opportunity_values = opportunity_ids or [""]
        for hypothesis_id in hypothesis_values:
            for opportunity_id in opportunity_values:
                rows.append(
                    {
                        "problem_id": problem_id,
                        "opportunity_id": opportunity_id,
                        "hypothesis_id": hypothesis_id,
                        "work_package_id": package_id,
                        "proposed_tests": list(proposed_tests),
                        "metrics": list(metrics),
                        "baselines": list(baselines),
                        "falsification_conditions": list(falsification),
                        "stop_or_pivot_decisions": list(stop_or_pivot),
                    }
                )

    audit = {
        "action": "rebuild_traceability_matrix",
        "reason": (
            "plan-only canonical matrix was missing or incomplete; rebuild "
            "from the accepted focus gate, hypothesis portfolio, and current "
            "work packages"
        ),
        "previous_row_count": len(existing),
        "generated_row_count": len(rows),
        "stale_rows_discarded": len(existing),
        "forced_rebuild": force_rebuild,
        "stale_rows_are_not_reused": True,
        "source_policy": {
            "problem_id": "PROGRAM_FOCUS_GATE.main_problem.problem_id",
            "opportunity_ids": "PROGRAM_FOCUS_GATE.selected_opportunity_ids",
            "hypothesis_ids": "PROGRAM_FOCUS_GATE.main_hypothesis_ids",
            "work_package_fields": [
                "work_package_id",
                "hypothesis_ids",
                "opportunity_ids",
                "proposed_tests_or_methods",
                "metric_ids_or_evaluation_metrics",
                "baseline_ids_or_control_descriptions",
                "stop_or_pivot_decisions_or_criteria",
            ],
            "falsification_source": "linked HYPOTHESIS_PORTFOLIO.falsification_conditions",
            "forbidden": [
                "invented identifiers",
                "invented papers",
                "invented experimental results",
            ],
        },
        "fallback_field_sources": fallback_usage,
        "empty_fields_are_preserved_for_validation": True,
    }
    return rows, audit


_CANONICAL_PLAN_READINESS = frozenset(
    {"ready", "needs_more_literature", "needs_human_choice", "future_phase"}
)


def _normalize_plan_work_package_readiness(
    plan: Dict[str, Any],
    focus_gate: Dict[str, Any],
    hypothesis_rows: Dict[str, Dict[str, Any]],
) -> tuple[Dict[str, Any], List[Dict[str, Any]]]:
    """Conservatively normalize work-package maturity before plan validation.

    ``verification_deferred`` is a verification-status value, not a plan
    readiness value.  Unknown readiness labels are therefore mapped downward
    to ``needs_more_literature``.  A package linked to selected hypotheses
    cannot claim ``ready`` while all of those hypotheses remain unready.  No
    branch is ever upgraded by this helper.
    """

    normalized = copy.deepcopy(plan)
    selected_hypotheses = set(
        _as_string_list(focus_gate.get("main_hypothesis_ids"))
    )
    corrections: List[Dict[str, Any]] = []
    packages: List[Any] = []
    for raw_package in plan.get("work_packages", []):
        if not isinstance(raw_package, dict):
            packages.append(raw_package)
            continue
        package = dict(raw_package)
        package_id = str(package.get("work_package_id") or "")
        original = str(package.get("readiness") or "").strip().lower()
        canonical = _READINESS_ALIASES.get(original, original)
        if canonical not in _CANONICAL_PLAN_READINESS:
            canonical = "needs_more_literature"
        if original != canonical:
            package["readiness"] = canonical
            corrections.append(
                {
                    "action": "normalize_work_package_readiness",
                    "work_package_id": package_id,
                    "from": original or "missing",
                    "to": canonical,
                    "reason": (
                        "Invalid or non-canonical plan readiness was mapped "
                        "downward; verification status remains separate."
                    ),
                }
            )

        linked_selected = [
            hypothesis_id
            for hypothesis_id in _as_string_list(package.get("hypothesis_ids"))
            if hypothesis_id in selected_hypotheses
        ]
        linked_readiness = [
            str(hypothesis_rows.get(hypothesis_id, {}).get("readiness") or "")
            .strip()
            .lower()
            for hypothesis_id in linked_selected
        ]
        if (
            package.get("readiness") == "ready"
            and linked_readiness
            and all(value != "ready" for value in linked_readiness)
        ):
            package["readiness"] = "needs_more_literature"
            corrections.append(
                {
                    "action": "downgrade_work_package_readiness_to_hypothesis",
                    "work_package_id": package_id,
                    "from": "ready",
                    "to": "needs_more_literature",
                    "linked_selected_hypothesis_ids": linked_selected,
                    "linked_hypothesis_readiness": linked_readiness,
                    "reason": (
                        "Every linked selected hypothesis is not ready; the "
                        "work package cannot be more mature than its premise."
                    ),
                }
            )
        packages.append(package)
    normalized["work_packages"] = packages
    return normalized, corrections


def _rehydrate_main_hypothesis_statements(
    plan: Dict[str, Any],
    focus_gate: Dict[str, Any],
    hypothesis_rows: Dict[str, Dict[str, Any]],
) -> tuple[Dict[str, Any], Optional[Dict[str, Any]], List[str]]:
    """Copy exact accepted statements into a missing or stale plan field."""

    normalized = copy.deepcopy(plan)
    selected_ids = _as_string_list(focus_gate.get("main_hypothesis_ids"))
    canonical: List[Dict[str, str]] = []
    missing: List[str] = []
    for hypothesis_id in selected_ids:
        source = hypothesis_rows.get(hypothesis_id) or {}
        statement = str(source.get("statement") or "").strip()
        if not statement:
            missing.append(hypothesis_id)
            continue
        canonical.append(
            {
                "hypothesis_id": hypothesis_id,
                "title": str(source.get("title") or "").strip(),
                "statement": statement,
            }
        )
    if missing:
        return normalized, None, [
            "main_hypothesis_statements_source_missing:" + ",".join(missing)
        ]
    existing = [
        dict(item)
        for item in (plan.get("main_hypothesis_statements") or [])
        if isinstance(item, dict)
    ]
    if existing == canonical:
        return normalized, None, []
    normalized["main_hypothesis_statements"] = canonical
    return normalized, {
        "action": "rehydrate_main_hypothesis_statements",
        "source": "HYPOTHESIS_PORTFOLIO.json",
        "selected_hypothesis_ids": selected_ids,
        "reason": (
            "Copied only the exact accepted hypothesis IDs, titles, and "
            "statements; no statement was generated."
        ),
    }, []


def _normalize_focus_gate_payload(parsed: Dict[str, Any]) -> Dict[str, Any]:
    """Normalize the small, explicit project-convergence contract.

    This is intentionally conservative: aliases are accepted for harmless
    naming differences, but missing scientific decisions are not filled with
    generic defaults.  The deterministic gate must see the model's actual
    focus choice and can then fail closed.
    """

    normalized = dict(parsed)
    normalized["schema_version"] = (
        normalized.get("schema_version")
        or "research_harness.program_focus_gate.v1"
    )
    normalized["gate_id"] = str(
        normalized.get("gate_id") or normalized.get("program_focus_gate_id") or "PFG01"
    ).strip()
    main_problem = normalized.get("main_problem") or normalized.get("problem")
    if isinstance(main_problem, list):
        normalized["main_problem"] = main_problem
    elif isinstance(main_problem, dict):
        normalized["main_problem"] = dict(main_problem)
    else:
        normalized["main_problem"] = {}
    if isinstance(normalized["main_problem"], dict):
        problem = normalized["main_problem"]
        problem["problem_id"] = str(problem.get("problem_id") or "P01")
        problem["statement"] = str(
            problem.get("statement")
            or problem.get("problem_statement")
            or problem.get("description")
            or ""
        ).strip()
        problem["scope"] = str(problem.get("scope") or "").strip()
        problem["boundary"] = str(problem.get("boundary") or "").strip()
    normalized["project_type"] = str(
        normalized.get("project_type") or normalized.get("program_type") or ""
    ).strip().lower()
    platform = normalized.get("shared_platform") or normalized.get("platform")
    normalized["shared_platform"] = dict(platform) if isinstance(platform, dict) else {}
    if isinstance(normalized["shared_platform"], dict):
        platform = normalized["shared_platform"]
        platform["platform_id"] = str(
            platform.get("platform_id") or platform.get("id") or ""
        ).strip()
        platform["name"] = str(platform.get("name") or platform.get("title") or "").strip()
        platform["description"] = str(
            platform.get("description") or platform.get("model_or_system") or ""
        ).strip()
        platform["compatibility_key"] = str(
            platform.get("compatibility_key")
            or platform.get("platform_compatibility_key")
            or platform.get("platform_key")
            or ""
        ).strip()
        platform["platform_compatibility_key"] = platform["compatibility_key"]
    boundaries = normalized.get("boundaries") or normalized.get("resource_boundaries")
    normalized["boundaries"] = dict(boundaries) if isinstance(boundaries, dict) else {}
    for key in ("personnel", "equipment", "data", "timeline", "budget"):
        normalized["boundaries"][key] = _as_string_list(normalized["boundaries"].get(key))
    evaluation = normalized.get("unified_evaluation") or normalized.get("evaluation_framework")
    normalized["unified_evaluation"] = dict(evaluation) if isinstance(evaluation, dict) else {}
    evaluation = normalized["unified_evaluation"]
    evaluation["metrics"] = evaluation.get("metrics") or evaluation.get("core_metrics") or []
    evaluation["baselines"] = evaluation.get("baselines") or evaluation.get("core_baselines") or []
    evaluation["comparison_protocol"] = str(
        evaluation.get("comparison_protocol") or evaluation.get("protocol") or ""
    ).strip()
    normalized["selected_opportunity_ids"] = [
        _canonical_id(value, "OP")
        for value in _as_string_list(
            normalized.get("selected_opportunity_ids")
            or normalized.get("main_opportunity_ids")
            or normalized.get("selected_opportunities")
        )
    ]
    normalized["main_hypothesis_ids"] = [
        _canonical_id(value, "H")
        for value in _as_string_list(
            normalized.get("main_hypothesis_ids")
            or normalized.get("selected_hypothesis_ids")
            or normalized.get("main_hypotheses")
        )
    ]
    normalized["future_hypothesis_ids"] = [
        _canonical_id(value, "H")
        for value in _as_string_list(normalized.get("future_hypothesis_ids"))
    ]
    normalization_audit = _dedupe_audit_rows(
        normalized.get("normalization_audit")
    )
    dependencies = []
    dependency_keys = set()
    for raw in normalized.get("hypothesis_dependencies") or []:
        if not isinstance(raw, dict):
            continue
        item = dict(raw)
        item["upstream_hypothesis_id"] = _canonical_id(
            item.get("upstream_hypothesis_id") or item.get("from_hypothesis_id"), "H"
        )
        item["downstream_hypothesis_id"] = _canonical_id(
            item.get("downstream_hypothesis_id") or item.get("to_hypothesis_id"), "H"
        )
        item["reason"] = str(item.get("reason") or item.get("rationale") or "").strip()
        edge_key = (
            item["upstream_hypothesis_id"],
            item["downstream_hypothesis_id"],
        )
        if edge_key[0] == edge_key[1]:
            # A self-edge cannot describe a dependency.  Remove it before
            # validation so it cannot masquerade as the dependency spine; if
            # no distinct edge remains for multiple main hypotheses, the gate
            # still fails honestly below.
            normalization_audit.append(
                {
                    "field": "hypothesis_dependencies",
                    "from": f"{edge_key[0]}->{edge_key[1]}",
                    "to": "removed",
                    "reason": "Removed a self-dependency; it cannot form a valid dependency edge.",
                }
            )
            continue
        if edge_key in dependency_keys:
            normalization_audit.append(
                {
                    "field": "hypothesis_dependencies",
                    "from": f"{edge_key[0]}->{edge_key[1]}",
                    "to": "deduplicated",
                    "reason": "Removed a duplicate dependency edge.",
                }
            )
            continue
        dependency_keys.add(edge_key)
        dependencies.append(item)
    normalized["hypothesis_dependencies"] = dependencies
    if normalization_audit:
        normalized["normalization_audit"] = normalization_audit
    future_branches = []
    for raw in normalized.get("future_branches") or []:
        if not isinstance(raw, dict):
            continue
        item = dict(raw)
        item["opportunity_id"] = _canonical_id(item.get("opportunity_id") or item.get("id"), "OP")
        item["reason"] = str(item.get("reason") or item.get("rationale") or "").strip()
        item["excluded_from_current_work_packages"] = bool(
            item.get("excluded_from_current_work_packages", True)
        )
        future_branches.append(item)
    normalized["future_branches"] = future_branches
    matrix = []
    for raw in normalized.get("traceability_matrix") or []:
        if not isinstance(raw, dict):
            continue
        item = dict(raw)
        item["problem_id"] = str(item.get("problem_id") or "P01").strip()
        item["opportunity_id"] = _canonical_id(item.get("opportunity_id"), "OP")
        item["hypothesis_id"] = _canonical_id(item.get("hypothesis_id"), "H")
        item["work_package_id"] = _canonical_id(item.get("work_package_id"), "WP")
        for key in (
            "proposed_tests", "metrics", "baselines",
            "falsification_conditions", "stop_or_pivot_decisions",
        ):
            item[key] = _as_string_list(item.get(key))
        matrix.append(item)
    normalized["traceability_matrix"] = matrix
    return normalized


def _normalize_focus_gate_against_opportunities(
    parsed: Dict[str, Any],
    opportunities: List[Dict[str, Any]],
    hypotheses: Optional[List[Dict[str, Any]]] = None,
) -> tuple[Dict[str, Any], List[Dict[str, str]]]:
    """Apply deterministic, audit-only focus repairs before validation.

    The model still chooses the mainline.  This helper only completes the
    bookkeeping required by the gate: a one-component hybrid is downgraded by
    ``ProgramFocusGate``; every nonselected opportunity is represented as an
    explicitly excluded future branch; and every accepted hypothesis that is
    not on the selected spine is represented in ``future_hypothesis_ids``.
    These are bookkeeping repairs only.  The exclusion text reports existing
    identifiers/status values and does not invent a scientific rationale.
    """

    previous_audit = [
        dict(item)
        for item in (parsed.get("normalization_audit") or [])
        if isinstance(item, dict)
    ]
    normalized = _normalize_focus_gate_payload(parsed)
    payload_corrections = _new_audit_rows(
        normalized.get("normalization_audit"), previous_audit
    )
    normalized, compatibility_corrections = (
        ProgramFocusGate.normalize_compatibility(normalized)
    )
    corrections = [
        *payload_corrections,
        *[dict(item) for item in compatibility_corrections],
    ]
    opportunity_rows = [
        item for item in opportunities if isinstance(item, dict)
    ]
    opportunity_by_id = {
        _canonical_id(item.get("opportunity_id"), "OP"): item
        for item in opportunity_rows
        if item.get("opportunity_id")
    }
    selected_ids = set(
        _canonical_id(value, "OP")
        for value in _as_string_list(normalized.get("selected_opportunity_ids"))
    )
    branches: List[Dict[str, Any]] = []
    seen_branch_ids: set[str] = set()
    for raw in normalized.get("future_branches") or []:
        if not isinstance(raw, dict):
            continue
        item = dict(raw)
        opportunity_id = _canonical_id(
            item.get("opportunity_id") or item.get("id"), "OP"
        )
        item["opportunity_id"] = opportunity_id
        if not str(item.get("reason") or "").strip():
            opportunity = opportunity_by_id.get(opportunity_id, {})
            title = str(
                opportunity.get("title") or "untitled opportunity"
            ).strip()
            status = str(
                opportunity.get("evidence_status")
                or opportunity.get("status")
                or "unspecified"
            ).strip()
            item["reason"] = (
                f"Opportunity {opportunity_id} ({title}; status={status}) "
                "is not selected for the current program focus and is "
                "excluded from current work packages."
            )
            corrections.append(
                {
                    "field": "future_branches.reason",
                    "from": "missing",
                    "to": opportunity_id,
                    "reason": (
                        "Filled missing exclusion bookkeeping from the "
                        "accepted opportunity title and evidence status; no "
                        "scientific rationale was generated."
                    ),
                }
            )
        if opportunity_id in seen_branch_ids:
            corrections.append(
                {
                    "field": "future_branches",
                    "from": opportunity_id,
                    "to": opportunity_id,
                    "reason": "Duplicate future-branch bookkeeping row removed.",
                }
            )
            continue
        seen_branch_ids.add(opportunity_id)
        branches.append(item)

    for opportunity_id, opportunity in opportunity_by_id.items():
        if opportunity_id in selected_ids or opportunity_id in seen_branch_ids:
            continue
        title = str(opportunity.get("title") or "untitled opportunity").strip()
        status = str(
            opportunity.get("status")
            or opportunity.get("evidence_status")
            or "unspecified"
        ).strip()
        branches.append(
            {
                "opportunity_id": opportunity_id,
                "reason": (
                    f"Opportunity {opportunity_id} ({title}; status={status}) "
                    "is not selected for the current program focus and is "
                    "excluded from current work packages."
                ),
                "excluded_from_current_work_packages": True,
            }
        )
        corrections.append(
            {
                "field": "future_branches",
                "from": "missing",
                "to": opportunity_id,
                "reason": (
                    "Added deterministic audit-only exclusion for a nonselected "
                    "opportunity; no scientific rationale was generated."
                ),
            }
        )
    normalized["future_branches"] = branches

    # The focus gate receives the model's selected main hypothesis IDs, but a
    # model may omit the complementary future_hypothesis_ids list.  That is a
    # schema/branch-isolation error, not a reason to spend another model turn
    # asking it to repeat the same portfolio.  Complete the bookkeeping only
    # from the accepted hypothesis portfolio supplied by the caller.  When the
    # portfolio is unavailable (legacy direct library use), leave the field
    # untouched and let normal validation fail closed.
    if hypotheses is not None:
        accepted_hypothesis_ids = list(
            dict.fromkeys(
                _canonical_id(
                    item.get("hypothesis_id") or item.get("id"), "H"
                )
                for item in hypotheses
                if isinstance(item, dict)
                and (item.get("hypothesis_id") or item.get("id"))
            )
        )
        accepted_hypothesis_set = set(accepted_hypothesis_ids)
        raw_selected_hypothesis_ids = list(
            dict.fromkeys(
                _canonical_id(value, "H")
                for value in _as_string_list(
                    normalized.get("main_hypothesis_ids")
                )
            )
        )
        valid_selected_hypothesis_ids = [
            value
            for value in raw_selected_hypothesis_ids
            if value in accepted_hypothesis_set
        ]
        for invalid_id in (
            value
            for value in raw_selected_hypothesis_ids
            if value not in accepted_hypothesis_set
        ):
            corrections.append(
                {
                    "field": "main_hypothesis_ids",
                    "from": invalid_id,
                    "to": "",
                    "reason": (
                        "Removed a selected hypothesis ID that is not present "
                        "in the accepted HYPOTHESIS_PORTFOLIO."
                    ),
                }
            )
        normalized["main_hypothesis_ids"] = valid_selected_hypothesis_ids
        selected_hypothesis_ids = set(valid_selected_hypothesis_ids)
        future_hypothesis_ids: List[str] = []
        for value in _as_string_list(
            normalized.get("future_hypothesis_ids")
        ):
            hypothesis_id = _canonical_id(value, "H")
            if hypothesis_id not in accepted_hypothesis_set:
                corrections.append(
                    {
                        "field": "future_hypothesis_ids",
                        "from": hypothesis_id,
                        "to": "",
                        "reason": (
                            "Removed a future hypothesis ID that is not present "
                            "in the accepted HYPOTHESIS_PORTFOLIO."
                        ),
                    }
                )
                continue
            if (
                hypothesis_id not in selected_hypothesis_ids
                and hypothesis_id not in future_hypothesis_ids
            ):
                future_hypothesis_ids.append(hypothesis_id)
        for hypothesis_id in accepted_hypothesis_ids:
            if (
                hypothesis_id not in selected_hypothesis_ids
                and hypothesis_id not in future_hypothesis_ids
            ):
                future_hypothesis_ids.append(hypothesis_id)
                corrections.append(
                    {
                        "field": "future_hypothesis_ids",
                        "from": "missing",
                        "to": hypothesis_id,
                        "reason": (
                            "Added an audit-only future branch for an accepted "
                            "nonselected hypothesis; no scientific rationale "
                            "was generated."
                        ),
                    }
                )
        normalized["future_hypothesis_ids"] = future_hypothesis_ids

    if corrections:
        normalized["normalization_audit"] = _dedupe_audit_rows(
            [
                *(
                    normalized.get("normalization_audit")
                    if isinstance(normalized.get("normalization_audit"), list)
                    else []
                ),
                *corrections,
            ]
        )
    return normalized, corrections


def _try_evidence_calibrated_single_spine(
    gate: Dict[str, Any],
    errors: List[str],
    opportunities: List[Dict[str, Any]],
    hypotheses: List[Dict[str, Any]],
    permission_map: Optional[Dict[str, Dict[str, str]]] = None,
) -> tuple[Optional[Dict[str, Any]], Optional[Dict[str, Any]]]:
    """Collapse a malformed multi-main focus only when the evidence ranking is unique.

    This is bookkeeping, not scientific reasoning.  It is deliberately
    fail-closed: the repair is considered only when the complete validation
    error set is the known disconnected-spine family, and it refuses to pick
    a winner when the deterministic evidence tuple is tied.  The returned
    audit records every candidate score and explicitly states that the result
    is an evidence-calibrated convergence decision rather than a scientific
    truth claim.
    """

    error_set = {
        str(item).strip()
        for item in errors
        if str(item).strip()
    }
    repairable = all(
        item in _SINGLE_SPINE_REPAIRABLE_EXACT_ERRORS
        or item.startswith(_SINGLE_SPINE_REPAIRABLE_ERROR_PREFIXES)
        for item in error_set
    )
    has_spine_or_identity_error = any(
        item in _SINGLE_SPINE_ERROR_SET
        or item == "main_hypothesis_not_in_hypothesis_portfolio"
        for item in error_set
    )
    if not error_set or not repairable or not has_spine_or_identity_error:
        return None, None

    opportunity_rows = [
        item for item in opportunities if isinstance(item, dict)
    ]
    opportunity_by_id = {
        _canonical_id(item.get("opportunity_id"), "OP"): item
        for item in opportunity_rows
        if item.get("opportunity_id")
    }
    hypothesis_rows = [
        item for item in hypotheses if isinstance(item, dict)
    ]
    explicit_future_ids = {
        _canonical_id(value, "H")
        for value in _as_string_list(gate.get("future_hypothesis_ids"))
    }
    candidate_rows = []
    for hypothesis in hypothesis_rows:
        hypothesis_id = _canonical_id(
            hypothesis.get("hypothesis_id") or hypothesis.get("id"), "H"
        )
        if not hypothesis_id or hypothesis_id in explicit_future_ids:
            continue
        source_ids = [
            _canonical_id(value, "OP")
            for value in _as_string_list(
                hypothesis.get("source_opportunity_ids")
                or hypothesis.get("source_opportunity_id")
            )
        ]
        source_ids = list(
            dict.fromkeys(
                value for value in source_ids if value in opportunity_by_id
            )
        )
        if not source_ids:
            continue
        source_rows = [opportunity_by_id[value] for value in source_ids]
        permission_map = permission_map or {}
        paper_permissions = permission_map.get("paper_permissions") or {}
        source_permission_valid = True
        for source in source_rows:
            evidence_status = str(
                source.get("evidence_status")
                or source.get("status")
                or "unknown"
            ).strip().casefold().replace("-", "_").replace(" ", "_")
            has_nonfactual_paper = any(
                _PERMISSION_RANK.get(
                    str(paper_permissions.get(value) or "discovery_only"), 0
                )
                < _PERMISSION_RANK["factual_support"]
                for value in _as_string_list(
                    source.get("supporting_paper_ids")
                )
            )
            if not has_nonfactual_paper:
                continue
            if evidence_status not in {"open_gap", "partially_supported"}:
                source_permission_valid = False
                break
            if not str(source.get("author_inference") or "").strip() or not str(
                source.get("uncertainty") or ""
            ).strip():
                source_permission_valid = False
                break
        if not source_permission_valid:
            continue
        statuses = [
            str(
                row.get("evidence_status")
                or row.get("status")
                or "unknown"
            )
            .strip()
            .casefold()
            .replace("-", "_")
            .replace(" ", "_")
            for row in source_rows
        ]
        evidence_status_score = max(
            (
                _SINGLE_SPINE_EVIDENCE_STATUS_SCORE.get(status, 0)
                for status in statuses
            ),
            default=0,
        )
        readiness = str(hypothesis.get("readiness") or "")
        readiness = readiness.strip().casefold().replace("-", "_").replace(" ", "_")
        readiness = _READINESS_ALIASES.get(readiness, readiness)
        readiness_score = _SINGLE_SPINE_READINESS_SCORE.get(readiness, 0)
        confidence = str(hypothesis.get("confidence") or "")
        confidence = confidence.strip().casefold().replace("-", "_").replace(" ", "_")
        confidence = _CONFIDENCE_ALIASES.get(confidence, confidence)
        confidence_score = _SINGLE_SPINE_CONFIDENCE_SCORE.get(confidence, 0)

        support_paper_ids = {
            str(value).strip()
            for value in _as_string_list(hypothesis.get("supporting_paper_ids"))
            if str(value).strip()
        }
        support_chunk_ids = {
            str(value).strip()
            for value in _as_string_list(hypothesis.get("supporting_chunk_ids"))
            if str(value).strip()
        }
        for source in source_rows:
            support_paper_ids.update(
                str(value).strip()
                for value in _as_string_list(source.get("supporting_paper_ids"))
                if str(value).strip()
            )
            support_chunk_ids.update(
                str(value).strip()
                for value in _as_string_list(source.get("supporting_chunk_ids"))
                if str(value).strip()
            )
        chunk_permissions = permission_map.get("chunk_permissions") or {}
        factual_papers = sorted(
            value
            for value in support_paper_ids
            if _PERMISSION_RANK.get(
                str(paper_permissions.get(value) or "discovery_only"), 0
            )
            >= _PERMISSION_RANK["factual_support"]
        )
        contextual_papers = sorted(
            value
            for value in support_paper_ids
            if _PERMISSION_RANK.get(
                str(paper_permissions.get(value) or "discovery_only"), 0
            )
            == _PERMISSION_RANK["contextual_or_qualified_support"]
        )
        factual_chunks = sorted(
            value
            for value in support_chunk_ids
            if _PERMISSION_RANK.get(
                str(chunk_permissions.get(value) or "discovery_only"), 0
            )
            >= _PERMISSION_RANK["factual_support"]
        )
        contextual_chunks = sorted(
            value
            for value in support_chunk_ids
            if _PERMISSION_RANK.get(
                str(chunk_permissions.get(value) or "discovery_only"), 0
            )
            == _PERMISSION_RANK["contextual_or_qualified_support"]
        )
        support_counts = {
            "factual_papers": len(factual_papers),
            "contextual_papers": len(contextual_papers),
            "factual_chunks": len(factual_chunks),
            "contextual_chunks": len(contextual_chunks),
        }
        legal_support_count = sum(support_counts.values())
        rank_tuple = (
            evidence_status_score,
            readiness_score,
            confidence_score,
            legal_support_count,
        )
        candidate_rows.append(
            {
                "hypothesis_id": hypothesis_id,
                "source_opportunity_ids": source_ids,
                "evidence_statuses": statuses,
                "evidence_status_score": evidence_status_score,
                "readiness": readiness,
                "readiness_score": readiness_score,
                "confidence": confidence,
                "confidence_score": confidence_score,
                "support_counts": support_counts,
                "legal_support_count": legal_support_count,
                "rank_tuple": list(rank_tuple),
            }
        )

    if not candidate_rows:
        return None, None
    highest = max(tuple(item["rank_tuple"]) for item in candidate_rows)
    winners = [
        item for item in candidate_rows if tuple(item["rank_tuple"]) == highest
    ]
    if len(winners) != 1:
        return None, None
    winner = winners[0]
    winner_id = str(winner["hypothesis_id"])
    selected_opportunity_ids = list(winner["source_opportunity_ids"])
    if not selected_opportunity_ids:
        return None, None

    normalized = copy.deepcopy(gate)
    all_hypothesis_ids = list(
        dict.fromkeys(
            _canonical_id(
                item.get("hypothesis_id") or item.get("id"), "H"
            )
            for item in hypothesis_rows
            if item.get("hypothesis_id") or item.get("id")
        )
    )
    all_opportunity_ids = list(opportunity_by_id)
    normalized["main_hypothesis_ids"] = [winner_id]
    normalized["future_hypothesis_ids"] = [
        value for value in all_hypothesis_ids if value != winner_id
    ]
    normalized["selected_opportunity_ids"] = selected_opportunity_ids
    normalized["hypothesis_dependencies"] = []
    # A focus gate is not the place where work packages are designed.  Older
    # model submissions sometimes carry a plan-level matrix from before the
    # single-spine decision, including rows for hypotheses that are now future
    # branches or rows with no hypothesis at all.  Do not carry that stale
    # matrix across the convergence boundary.  The plan-only phase will rebuild
    # it from the accepted spine and the work packages it actually receives.
    stale_focus_matrix = [
        dict(item)
        for item in (normalized.get("traceability_matrix") or [])
        if isinstance(item, dict)
    ]
    normalized["traceability_matrix"] = []
    future_branches = []
    for opportunity_id in all_opportunity_ids:
        if opportunity_id in selected_opportunity_ids:
            continue
        opportunity = opportunity_by_id[opportunity_id]
        title = str(opportunity.get("title") or "untitled opportunity").strip()
        status = str(
            opportunity.get("evidence_status")
            or opportunity.get("status")
            or "unspecified"
        ).strip()
        future_branches.append(
            {
                "opportunity_id": opportunity_id,
                "reason": (
                    f"Opportunity {opportunity_id} ({title}; status={status}) "
                    "is excluded from the current single-spine focus."
                ),
                "excluded_from_current_work_packages": True,
            }
        )
    normalized["future_branches"] = future_branches
    convergence_audit = {
        "field": "focus_convergence",
        "action": "evidence_calibrated_single_spine",
        "trigger_errors": sorted(error_set),
        "winner_hypothesis_id": winner_id,
        "winner_source_opportunity_ids": selected_opportunity_ids,
        "candidate_scores": candidate_rows,
        "ranking_order": [
            "source_opportunity_evidence_status",
            "hypothesis_readiness",
            "hypothesis_confidence",
            "legal_factual_or_contextual_support_count",
        ],
        "tie_policy": "A tied highest tuple is not auto-resolved; request human review.",
        "focus_traceability_rows_cleared": len(stale_focus_matrix),
        "focus_traceability_policy": (
            "Focus-stage traceability rows are non-authoritative and are not "
            "used to create work packages; plan-only rebuilds rows from the "
            "accepted selected IDs and current work packages."
        ),
        "scientific_interpretation": (
            "This is evidence-calibrated convergence for a single research "
            "spine, not a claim that the selected hypothesis is scientific truth."
        ),
    }
    audit_rows = [
        dict(item)
        for item in (normalized.get("normalization_audit") or [])
        if isinstance(item, dict)
    ]
    audit_rows.append(convergence_audit)
    normalized["normalization_audit"] = audit_rows
    return normalized, convergence_audit


def _reset_focus_traceability_for_plan_rebuild(
    gate: Dict[str, Any],
) -> tuple[Dict[str, Any], Optional[Dict[str, Any]]]:
    """Remove non-authoritative plan rows from a focus artifact.

    Focus selection can be validated without a work-package matrix.  Keeping a
    matrix supplied by the model at that boundary is unsafe because it may
    describe a pre-convergence hypothesis set.  The plan-only worker owns the
    matrix and reconstructs it from its current work packages.  This function
    never creates a work package or a traceability row.
    """

    rows = [
        item
        for item in (gate.get("traceability_matrix") or [])
        if isinstance(item, dict)
    ]
    if not rows:
        return gate, None
    normalized = copy.deepcopy(gate)
    normalized["traceability_matrix"] = []
    return normalized, {
        "field": "traceability_matrix",
        "action": "clear_focus_matrix_for_plan_only_rebuild",
        "previous_row_count": len(rows),
        "selected_opportunity_ids": list(
            _as_string_list(normalized.get("selected_opportunity_ids"))
        ),
        "main_hypothesis_ids": list(
            _as_string_list(normalized.get("main_hypothesis_ids"))
        ),
        "reason": (
            "Focus-stage rows are not authoritative after convergence; no "
            "work package was created or inferred. Plan-only will rebuild "
            "traceability from accepted IDs and current work packages."
        ),
    }


def _focus_traceability_has_stale_selected_ids(gate: Dict[str, Any]) -> bool:
    """Detect focus rows that cannot belong to the currently selected spine."""

    rows = [
        item
        for item in (gate.get("traceability_matrix") or [])
        if isinstance(item, dict)
    ]
    if not rows:
        return False
    selected_opportunities = set(
        _as_string_list(gate.get("selected_opportunity_ids"))
    )
    selected_hypotheses = set(_as_string_list(gate.get("main_hypothesis_ids")))
    return any(
        str(row.get("opportunity_id") or "").strip()
        not in selected_opportunities
        or str(row.get("hypothesis_id") or "").strip()
        not in selected_hypotheses
        for row in rows
    )


def _recover_last_focus_gate_from_agent_state(work_dir: Path) -> Dict[str, Any]:
    """Recover the latest rejected focus candidate for offline reconciliation."""

    state_path = work_dir / "AGENT_STATE.json"
    try:
        payload = json.loads(state_path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    recovered: Dict[str, Any] = {}

    def visit(value: Any) -> None:
        nonlocal recovered
        if isinstance(value, dict):
            if value.get("type") == "tool_call" and value.get(
                "name"
            ) == "submit_program_focus_gate":
                raw_input = value.get("input")
                try:
                    outer = (
                        json.loads(raw_input)
                        if isinstance(raw_input, str)
                        else raw_input
                    )
                    raw_gate = (
                        outer.get("program_focus_gate_json")
                        if isinstance(outer, dict)
                        else None
                    )
                    candidate = (
                        json.loads(raw_gate)
                        if isinstance(raw_gate, str)
                        else raw_gate
                    )
                    if isinstance(candidate, dict):
                        recovered = candidate
                except Exception:
                    pass
            for child in value.values():
                visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)

    visit(payload)
    return recovered


_READINESS_RANK = {
    "ready": 0,
    "needs_more_literature": 1,
    "needs_human_choice": 2,
    "future_phase": 3,
}


def _align_plan_readiness(
    plan: Dict[str, Any],
    hypotheses: Dict[str, Any],
) -> Dict[str, Any]:
    """Prevent a work package from outrunning its linked hypothesis."""

    readiness_by_hypothesis = {
        str(item.get("hypothesis_id") or ""): str(
            item.get("readiness") or "needs_more_literature"
        )
        for item in hypotheses.get("hypotheses", [])
        if isinstance(item, dict) and item.get("hypothesis_id")
    }
    for package in plan.get("work_packages", []):
        if not isinstance(package, dict):
            continue
        linked = [
            readiness_by_hypothesis[hypothesis_id]
            for hypothesis_id in package.get("hypothesis_ids", [])
            if hypothesis_id in readiness_by_hypothesis
        ]
        if not linked:
            continue
        required = max(
            linked,
            key=lambda value: _READINESS_RANK.get(value, 1),
        )
        current = str(package.get("readiness") or "needs_more_literature")
        if _READINESS_RANK.get(current, 1) < _READINESS_RANK.get(required, 1):
            package["readiness"] = required
    return plan


def _read_json(path: Path) -> Dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except Exception:
        return {}


def _split_review(review: str) -> Dict[str, str]:
    result: Dict[str, str] = {}
    current = ""
    lines: List[str] = []
    for line in review.splitlines():
        match = re.match(r"^##\s+(.+?)\s*$", line)
        if match:
            if current:
                result[current] = "\n".join(lines).strip()
            current = match.group(1).strip()
            lines = []
        elif current:
            lines.append(line)
    if current:
        result[current] = "\n".join(lines).strip()
    return result


def _paragraphs(text: str) -> List[str]:
    return [
        value.strip()
        for value in re.split(r"\n\s*\n", text)
        if value.strip()
    ]


def _build_readiness_summary(
    plan: Dict[str, Any],
    focus_gate: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Count readiness without confusing future branches with the main line."""

    gate = focus_gate or plan
    selected_hypotheses = set(
        _as_string_list(gate.get("main_hypothesis_ids"))
    )
    selected_opportunities = set(
        _as_string_list(gate.get("selected_opportunity_ids"))
    )
    future_hypotheses = set(_as_string_list(gate.get("future_hypothesis_ids")))
    future_opportunities = {
        str(item.get("opportunity_id") or "").strip()
        for item in gate.get("future_branches", [])
        if isinstance(item, dict) and str(item.get("opportunity_id") or "").strip()
    }
    states = (
        "ready",
        "needs_more_literature",
        "needs_human_choice",
        "future_phase",
    )

    def empty_counts() -> Dict[str, int]:
        return {state: 0 for state in states}

    all_counts = empty_counts()
    current_counts = empty_counts()
    future_counts = empty_counts()
    current_count = 0
    future_count = 0
    packages = [
        item
        for item in plan.get("work_packages", [])
        if isinstance(item, dict)
    ]
    for package in packages:
        readiness = str(package.get("readiness") or "needs_more_literature")
        if readiness not in all_counts:
            readiness = "needs_more_literature"
        all_counts[readiness] += 1
        hypotheses = set(_as_string_list(package.get("hypothesis_ids")))
        opportunities = set(_as_string_list(package.get("opportunity_ids")))
        is_current = bool(
            (hypotheses & selected_hypotheses)
            or (opportunities & selected_opportunities)
        )
        is_future = bool(
            (hypotheses & future_hypotheses)
            or (opportunities & future_opportunities)
        )
        if is_current:
            current_count += 1
            current_counts[readiness] += 1
        elif is_future:
            future_count += 1
            future_counts[readiness] += 1

    return {
        "scope": "current_mainline",
        "current_mainline": {
            "package_count": current_count,
            "readiness_counts": current_counts,
        },
        "all_submitted_packages": {
            "package_count": len(packages),
            "readiness_counts": all_counts,
        },
        "future_branch_packages_excluded_from_current": {
            "package_count": future_count,
            "readiness_counts": future_counts,
        },
        "note": (
            "Only current_mainline counts describe present program maturity; "
            "all_submitted_packages is diagnostic and future branches are "
            "excluded from the current maturity judgment."
        ),
    }


def _render_research_plan_markdown(plan: Dict[str, Any]) -> str:
    """Render a complete plan from the validated structure.

    Structured fields are the sole authoritative rendering source.  The raw
    model narrative is retained in the JSON audit surface but is never
    injected wholesale into Markdown, because it may already contain the
    canonical sections rendered below.
    """

    lines = [
        "# " + _reader_facing_text(plan.get("title") or "Research Program"),
        "",
        "## Abstract",
        "",
        _reader_facing_text(plan.get("paper_abstract") or "").strip(),
        "",
        "## Problem Statement",
        "",
        _reader_facing_text(
            plan.get("problem_statement") or plan.get("research_question") or ""
        ).strip(),
        "",
        "## Rationale",
        "",
        _reader_facing_text(
            plan.get("rationale") or plan.get("strategy") or ""
        ).strip(),
        "",
        "## Program Focus Gate",
        "",
        "**Main problem.** "
        + _reader_facing_text(
            (plan.get("main_problem") or {}).get("statement")
            or plan.get("problem_statement")
            or ""
        ).strip(),
        "",
        "**Project type.** " + _reader_facing_text(plan.get("project_type") or "").strip(),
        "",
        "**Shared platform.** "
        + _reader_facing_text((plan.get("shared_platform") or {}).get("name") or "").strip()
        + ". "
        + _reader_facing_text(
            (plan.get("shared_platform") or {}).get("description") or ""
        ).strip(),
        "",
    ]
    boundaries = plan.get("boundaries") or {}
    lines.extend(["**Project boundaries.**", ""])
    for key in ("personnel", "equipment", "data", "timeline", "budget"):
        values = (boundaries.get(key) or []) if isinstance(boundaries, dict) else []
        lines.append(
            f"- {key}: "
            + "; ".join(
                _reader_facing_text(value)
                for value in _as_narrative_list(values)
                if value
            )
        )
    evaluation = plan.get("unified_evaluation") or {}
    lines.extend(
        [
            "",
            "**Unified evaluation.**",
            "",
            "- Metrics: "
            + "; ".join(
                _reader_facing_text(row.get("metric_id") or row.get("name") or "")
                if isinstance(row, dict) else str(row)
                for row in evaluation.get("metrics", [])
            ),
            "- Baselines: "
            + "; ".join(
                _reader_facing_text(row.get("baseline_id") or row.get("name") or "")
                if isinstance(row, dict) else str(row)
                for row in evaluation.get("baselines", [])
            ),
            "- Comparison protocol: "
            + _reader_facing_text(evaluation.get("comparison_protocol") or ""),
            "",
            "## Main Hypothesis Statements",
            "",
        ]
    )
    summary = plan.get("readiness_summary") or {}
    lines.extend(["## Readiness Summary", ""])
    lines.append(
        "- Scope: current mainline; future branches are excluded from maturity."
    )
    current_summary = summary.get("current_mainline", {}) if isinstance(summary, dict) else {}
    all_summary = summary.get("all_submitted_packages", {}) if isinstance(summary, dict) else {}
    future_summary = (
        summary.get("future_branch_packages_excluded_from_current", {})
        if isinstance(summary, dict)
        else {}
    )
    lines.append(
        "- Current mainline packages: "
        + str(current_summary.get("package_count", 0))
        + "; readiness: "
        + "; ".join(
            f"{key}={value}"
            for key, value in (current_summary.get("readiness_counts", {}) or {}).items()
        )
    )
    lines.append(
        "- All submitted packages (diagnostic only): "
        + str(all_summary.get("package_count", 0))
        + "; readiness: "
        + "; ".join(
            f"{key}={value}"
            for key, value in (all_summary.get("readiness_counts", {}) or {}).items()
        )
    )
    lines.append(
        "- Future-branch packages excluded from current maturity: "
        + str(future_summary.get("package_count", 0))
    )
    lines.append("")
    for item in plan.get("main_hypothesis_statements", []):
        if not isinstance(item, dict):
            continue
        label = str(item.get("hypothesis_id") or "")
        title = str(item.get("title") or "")
        if title:
            label += ": " + title
        lines.extend(
            [
                f"### {label}",
                "",
                _reader_facing_text(item.get("statement") or ""),
                "",
            ]
        )
    lines.extend(
        [
            "## Program Strategy",
            "",
            _render_authoritative_strategy(plan),
            "",
        ]
    )
    future_branches = plan.get("future_branches") or []
    lines.extend(["## Future Branches", ""])
    if future_branches:
        for item in future_branches:
            if isinstance(item, dict):
                lines.append(
                    f"- {item.get('opportunity_id', '')}: "
                    f"{_reader_facing_text(item.get('reason', ''))} "
                    "(excluded from current work packages)"
                )
    else:
        lines.append("- None declared.")
    lines.extend(["", "## Traceability Matrix", ""])
    matrix = plan.get("traceability_matrix") or []
    for row in matrix:
        if not isinstance(row, dict):
            continue
        lines.extend(
            [
                (
                    f"- {row.get('problem_id')} → {row.get('opportunity_id')} → "
                    f"{row.get('hypothesis_id')} → {row.get('work_package_id')}"
                ),
                "  - Proposed tests: "
                + "; ".join(
                    _reader_facing_text(value)
                    for value in _as_narrative_list(row.get("proposed_tests", []))
                ),
                "  - Metrics: "
                + "; ".join(
                    _reader_facing_text(value)
                    for value in _as_narrative_list(row.get("metrics", []))
                ),
                "  - Baselines: "
                + "; ".join(
                    _reader_facing_text(value)
                    for value in _as_narrative_list(row.get("baselines", []))
                ),
                "  - Falsification: "
                + "; ".join(
                    _reader_facing_text(value)
                    for value in _as_narrative_list(row.get("falsification_conditions", []))
                ),
                "  - Stop/Pivot: "
                + "; ".join(
                    _reader_facing_text(value)
                    for value in _as_narrative_list(row.get("stop_or_pivot_decisions", []))
                ),
            ]
        )
    if not matrix:
        lines.append("- No traceability rows declared.")
    lines.extend(
        [
            "",
            "## Source Lineage and Limitations",
            "",
            "This plan is based on a documented review scope, literature relationship map, technical audit, and source-permission record.",
        ]
    )
    for item in plan.get("source_limitations", []):
        lines.append("- " + _reader_facing_text(item))
    lines.extend(
        [
            "",
        ]
    )
    lines.extend(
        [
        "## Verification Status",
        "",
        "**verification_deferred.** This document is a research-planning "
        "artifact. No proposed experiment, simulation, dataset collection, or "
        "data analysis has been executed in this plan.",
        "",
        ]
    )
    for heading, key in (
        ("Technical Details", "technical_details"),
        ("Datasets: Source", "dataset_source"),
        ("Datasets: Target", "dataset_target"),
        ("Methods", "methods_summary"),
        ("Experiments", "experiments"),
        ("Expected Results", "expected_results"),
        ("Deferred Verification", "verification_deferred"),
    ):
        lines.extend([f"## {heading}", ""])
        values = plan.get(key, [])
        lines.extend(
            f"- {_reader_facing_text(value)}"
            for value in _as_narrative_list(values)
            if value
        )
        lines.append("")
    lines.extend(
        [
            "## Canonical Program Specification",
            "",
            f"**Research question.** {_reader_facing_text(plan.get('research_question', ''))}",
            "",
            f"**Strategy.** {_reader_facing_text(plan.get('strategy', ''))}",
            "",
            "### Objectives",
            "",
        ]
    )
    lines.extend(
        f"- {_reader_facing_text(item)}"
        for item in _as_narrative_list(plan.get("objectives", []))
        if item
    )
    for package in plan.get("work_packages", []):
        if not isinstance(package, dict):
            continue
        lines.extend(
            [
                "",
                (
                    f"### {package.get('work_package_id', '')}: "
                    f"{_reader_facing_text(package.get('title', ''))}"
                ),
                "",
                f"**Objective.** {_reader_facing_text(package.get('objective', ''))}",
                "",
                (
                    "**Traceability.** Hypotheses: "
                    + ", ".join(package.get("hypothesis_ids", []))
                    + "; opportunities: "
                    + ", ".join(package.get("opportunity_ids", []))
                    + "."
                ),
                "",
                f"**Readiness.** {_reader_facing_text(package.get('readiness', ''))}",
                "",
                (
                    "**Quantitative target status.** "
                    + (
                        "Proposed calibration target (verification_deferred; "
                        "not a reported result)."
                        if package.get("quantitative_target_status")
                        == "proposed_program_target"
                        else (
                            "Source-anchored target; verify against the cited "
                            "record before execution."
                            if package.get("quantitative_target_status")
                            == "evidence_anchored"
                            else "No quantitative target declared."
                        )
                    )
                ),
                "",
                (
                    "**Verification status.** "
                    +
                    _reader_facing_text(
                        package.get("verification_status", "verification_deferred")
                    )
                ),
                "",
                (
                    "**Verification rationale.** "
                    +
                    _reader_facing_text(package.get("verification_rationale", ""))
                ),
                "",
            ]
        )
        for heading, key in (
            ("Methods", "methods"),
            ("Required inputs", "inputs"),
            ("Expected outputs", "expected_outputs"),
            ("Controls and baselines", "controls_or_baselines"),
            ("Evaluation metrics", "evaluation_metrics"),
            ("Dependencies", "dependencies"),
            ("Risks", "risks"),
            ("Stop or pivot criteria", "stop_or_pivot_criteria"),
        ):
            lines.extend([f"**{heading}.**", ""])
            values = package.get(key, [])
            if values:
                lines.extend(
                    f"- {_reader_facing_text(value)}"
                    for value in _as_narrative_list(values)
                )
            else:
                lines.append("- None declared.")
            lines.append("")
    for heading, key in (
        ("Milestones", "milestones"),
        ("Human decision points", "human_decision_points"),
        ("Unresolved literature needs", "unresolved_literature_needs"),
    ):
        lines.extend([f"## {heading}", ""])
        lines.extend(
            f"- {_reader_facing_text(value)}"
            for value in _as_narrative_list(plan.get(key, []))
            if value
        )
        lines.append("")
    lines.extend(["## References", ""])
    references = plan.get("reference_paper_ids", [])
    if references:
        lines.extend(f"- [REF:{value}]" for value in references if value)
    else:
        lines.append("- No additional plan-only references declared; see traceable work-package links above.")
    lines.append("")
    return "\n".join(lines).strip() + "\n"


@dataclass
class ResearchProgramContext:
    blueprint_path: Path
    final_review_path: Path
    coverage_root: Path
    work_dir: Path
    phase3_artifacts_root: Optional[Path] = None
    base_kb_sqlite: Optional[Path] = None
    staging_kb_sqlite: Optional[Path] = None
    quality_report_path: Optional[Path] = None
    literature_portfolio_path: Optional[Path] = None
    visual_plan_path: Optional[Path] = None
    query_plan_path: Optional[Path] = None
    review_scope_map_path: Optional[Path] = None
    relation_graph_path: Optional[Path] = None
    technical_audit_path: Optional[Path] = None
    source_permissions_path: Optional[Path] = None
    max_section_reads: int = 3
    max_evidence_chunks: int = 24
    max_section_batch_size: int = 8
    max_initial_detail_calls: int = 1
    # A plan-only resume is a deliberately smaller protocol.  It reuses the
    # already accepted focus artifacts and does not reopen discovery or
    # evidence exploration.
    plan_only_resume: bool = False
    # Discovery resumes are stage-aware.  The runner sets this to one of
    # opportunity/hypothesis/focus so the provider itself cannot expose tools
    # from completed stages.
    # Direct library users that construct a provider without the runner retain
    # the legacy all-stage inspection surface.  The production runner always
    # overwrites this with a concrete stage before constructing the worker.
    discovery_stage: str = "legacy_full"


class ResearchProgramToolProvider(ToolProvider):
    TOOL_NAMES = [
        "load_research_program_context",
        "read_review_section",
        "read_review_sections_batch",
        "inspect_research_evidence",
        "inspect_research_evidence_batch",
        "submit_research_opportunity_map",
        "submit_hypothesis_portfolio",
        "submit_program_focus_gate",
        "submit_research_plan",
        "validate_research_program_package",
    ]

    def __init__(self, ctx: ResearchProgramContext) -> None:
        self.ctx = ctx
        self.ctx.work_dir.mkdir(parents=True, exist_ok=True)
        self.blueprint = _read_json(ctx.blueprint_path)
        self.review = ctx.final_review_path.read_text(
            encoding="utf-8", errors="replace"
        )
        self.review_sections = _split_review(self.review)
        self._section_reads = 0
        self._evidence_reads = 0
        self._section_batch_calls = 0
        self._section_batch_cache: Optional[Dict[str, Any]] = None
        self._evidence_batch_calls = 0
        self._evidence_batch_cache: Optional[Dict[str, Any]] = None
        self._plan_only_context_loads = 0
        self._plan_only_submission_count = 0
        self._plan_only_validation_count = 0
        self._plan_only_context_cache: Optional[Dict[str, Any]] = None
        self._plan_only_human_stop_requested = False
        self._focus_submission_count = 0
        self._focus_last_error_signature = ""
        self._focus_error_repeat_count = 0
        self._focus_human_stop_requested = False
        self._section_ids = {
            str(section.get("section_id") or "")
            for section in self.blueprint.get("sections", [])
            if isinstance(section, dict) and section.get("section_id")
        }
        self._paper_ids: set[str] = set()
        self._paper_to_chunks: Dict[str, List[str]] = {}
        self._chunk_to_paper: Dict[str, str] = {}
        self._chunk_to_sections: Dict[str, set[str]] = {}
        self._source_records: Dict[str, Dict[str, Any]] = {}
        self._paper_permissions: Dict[str, str] = {}
        self._chunk_permissions: Dict[str, str] = {}
        self._build_allowlist()
        self.problem_frame = self._build_problem_frame()
        self.gap_map = self._build_gap_map()
        self.shared_review_context = self._build_shared_review_context()
        self.source_terminology_ledger = build_source_terminology_ledger(
            self.blueprint,
            self.review,
            self.shared_review_context,
        )
        self.shared_review_context["source_terminology_ledger"] = (
            self.source_terminology_ledger
        )
        atomic_write_json(
            self.ctx.work_dir / "RESEARCH_PROBLEM_FRAME.json",
            self.problem_frame,
        )
        atomic_write_json(
            self.ctx.work_dir / "RESEARCH_GAP_MAP.json",
            self.gap_map,
        )
        atomic_write_json(
            self.ctx.work_dir / "PROGRAM_SHARED_CONTEXT.json",
            self.shared_review_context,
        )
        atomic_write_json(
            self.ctx.work_dir / "SOURCE_TERMINOLOGY_LEDGER.json",
            self.source_terminology_ledger,
        )

    def try_auto_finalize(self) -> Optional[str]:
        """Return a trusted phase-completion signal when safe to stop.

        The worker checks this hook after every tool result.  Returning the
        canonical validation prefix is important: a plain JSON status from a
        submit tool is not treated as a terminal validation result by the
        generic worker loop.
        """

        if self._focus_human_stop_requested:
            return (
                "VALIDATION_AWAITING_HUMAN_REVIEW: repeated focus-gate "
                "error signature made no progress; stop automatic repair and "
                "request human review."
            )
        if not self.ctx.plan_only_resume:
            if self.ctx.discovery_stage == "opportunity":
                opportunity_map = _read_json(
                    self.ctx.work_dir / "RESEARCH_OPPORTUNITY_MAP.json"
                )
                if opportunity_map.get("opportunities"):
                    return (
                        "VALIDATION_PASSED: opportunity map accepted; "
                        "discovery is paused before hypothesis generation. "
                        "Resume with the hypothesis-stage protocol."
                    )
            # An accepted focus is the discovery hand-off boundary.  The
            # discovery worker must stop here so the model cannot spend a
            # further turn calling the generic validate_task_result tool,
            # which is intentionally not part of this domain provider.  The
            # runner starts a fresh plan-only worker from durable artifacts.
            focus = _read_json(self.ctx.work_dir / "PROGRAM_FOCUS_GATE.json")
            if focus.get("status") == "passed" and _as_string_list(
                focus.get("main_hypothesis_ids")
            ):
                return (
                    "VALIDATION_PASSED: accepted program focus gate; "
                    "discovery phase is complete and plan-only is the next "
                    "bounded phase."
                )
        if self.ctx.plan_only_resume and self._plan_only_human_stop_requested:
            return (
                "VALIDATION_AWAITING_HUMAN_REVIEW: plan-only submission "
                "protocol is closed; human review is required before another "
                "submission."
            )
        return None

    def _build_problem_frame(self) -> Dict[str, Any]:
        """Create a stable research-plan branch from the original question.

        This is not an LLM summary of the finished manuscript.  It preserves
        the upstream question, understanding, and scope as the independent
        starting point for hypothesis generation.
        """

        query = _read_json(self.ctx.query_plan_path) if self.ctx.query_plan_path else {}
        query_output = query.get("output", query) if isinstance(query, dict) else {}
        if not isinstance(query_output, dict):
            query_output = {}
        input_context = self.blueprint.get("input_context", {})
        return {
            "schema_version": "research_harness.research_problem_frame.v1",
            "user_question": str(
                input_context.get("user_question")
                or query_output.get("user_question")
                or query.get("user_question", "")
            ),
            "problem_understanding": str(
                input_context.get("problem_understanding")
                or query_output.get("problem_understanding")
                or query_output.get("problem_interpretation", "")
            ),
            "scope_definition": str(
                input_context.get("scope_definition")
                or query_output.get("scope_definition")
                or query_output.get("scope", "")
            ),
            "topic_identity": self.blueprint.get("topic_identity", {}),
            "review_thesis": str(self.blueprint.get("review_thesis") or ""),
            "planning_scope": (
                "Textual research planning only. Any experiment, simulation, "
                "dataset collection, or data analysis remains verification_deferred."
            ),
        }

    def _build_gap_map(self) -> Dict[str, Any]:
        gaps: list[Dict[str, Any]] = []
        for card in self._section_cards():
            for item in card.get("open_gaps", []):
                if not isinstance(item, dict):
                    continue
                gaps.append(
                    {
                        "gap_id": f"{card.get('section_id', 'S00')}:{len(gaps) + 1:02d}",
                        "section_id": card.get("section_id", ""),
                        "section_title": card.get("title", ""),
                        "gap_role": item.get("role", "evidence_gap"),
                        "description": item.get("description", ""),
                        "stop_reason": item.get("stop_reason", ""),
                        "status": "open_for_research_planning",
                    }
                )
        return {
            "schema_version": "research_harness.research_gap_map.v1",
            "gap_count": len(gaps),
            "gaps": gaps,
            "policy": (
                "A gap is a planning opportunity, not evidence that a proposed "
                "hypothesis or result is already established."
            ),
        }

    def _build_allowlist(self) -> None:
        for ledger_path in self.ctx.coverage_root.glob(
            "sections/*/SECTION_SOURCE_LEDGER.json"
        ):
            ledger = _read_json(ledger_path)
            section_id = ledger_path.parent.name
            for source in ledger.get("sources", []):
                if not isinstance(source, dict):
                    continue
                paper_id = str(source.get("paper_id") or "")
                if not paper_id:
                    continue
                permission = self._infer_source_permission(source)
                self._paper_ids.add(paper_id)
                existing = self._source_records.setdefault(
                    paper_id,
                    {
                        "paper_id": paper_id,
                        "title": str(source.get("title") or ""),
                        "year": source.get("year"),
                        "venue": str(source.get("venue") or ""),
                        "scope_fit": str(source.get("scope_fit") or ""),
                        "not_usable_for": list(source.get("not_usable_for") or []),
                        "use_permission": permission,
                        "content_depth": str(source.get("content_depth") or ""),
                        "acquisition_status": str(source.get("acquisition_status") or ""),
                    },
                )
                if _PERMISSION_RANK.get(permission, -1) < _PERMISSION_RANK.get(
                    str(existing.get("use_permission") or ""), -1
                ):
                    existing["use_permission"] = permission
                existing["not_usable_for"] = list(
                    dict.fromkeys(
                        [
                            *existing.get("not_usable_for", []),
                            *(source.get("not_usable_for") or []),
                        ]
                    )
                )
                self._paper_permissions[paper_id] = min(
                    permission,
                    self._paper_permissions.get(paper_id, permission),
                    key=lambda value: _PERMISSION_RANK.get(value, -1),
                )
                for chunk_id in source.get("canonical_chunk_ids", []):
                    if chunk_id:
                        canonical_chunk_id = str(chunk_id)
                        self._chunk_to_paper[canonical_chunk_id] = paper_id
                        self._paper_to_chunks.setdefault(
                            paper_id,
                            [],
                        ).append(canonical_chunk_id)
                        self._chunk_to_sections.setdefault(
                            canonical_chunk_id, set()
                        ).add(section_id)
                        current_chunk_permission = self._chunk_permissions.get(
                            canonical_chunk_id, permission
                        )
                        self._chunk_permissions[canonical_chunk_id] = min(
                            permission,
                            current_chunk_permission,
                            key=lambda value: _PERMISSION_RANK.get(value, -1),
                        )

    @staticmethod
    def _infer_source_permission(source: Dict[str, Any]) -> str:
        """Use the Phase-3 permission when present; otherwise infer safely."""

        explicit = str(source.get("use_permission") or "").strip()
        if explicit in _PERMISSION_RANK:
            return explicit
        depth = str(source.get("content_depth") or "").casefold()
        acquisition = str(source.get("acquisition_status") or "").casefold()
        if depth in {"fulltext", "structured_fulltext", "s2_body", "body_snippet"}:
            return "factual_support"
        if "abstract" in depth or "snippet" in depth or acquisition == "abstract_only":
            return "contextual_or_qualified_support"
        # Older ledgers predate the explicit permission field.  A canonical
        # chunk in those ledgers was materialized as readable text, so retain
        # compatibility for that legacy route; metadata-only records have no
        # canonical chunks and remain discovery-only.
        if source.get("canonical_chunk_ids"):
            return "factual_support"
        return "discovery_only"

    def _permission_map(self) -> Dict[str, Dict[str, str]]:
        return {
            "paper_permissions": dict(self._paper_permissions),
            "chunk_permissions": dict(self._chunk_permissions),
        }

    def _rehydrate_plan_backed_traceability_matrix(
        self,
        focus_gate: Dict[str, Any],
        opportunities: Dict[str, Any],
        hypotheses: Dict[str, Any],
    ) -> tuple[Optional[List[Dict[str, Any]]], Dict[str, Any]]:
        """Revalidate and recover the canonical matrix from a durable plan.

        ``PROGRAM_FOCUS_GATE.json`` is intentionally allowed to lose its
        pre-plan matrix during focus reconciliation.  Once a durable plan
        exists, however, that plan is the authoritative owner of the rows.
        This method never trusts an old audit or cleanup report as proof.  It
        rechecks the current plan, current opportunity/hypothesis artifacts,
        current selected spine, current work packages, and the rendered plan
        before allowing the rows back into the focus artifact.

        Returning ``None`` is fail-closed: the caller must clear any focus
        rows and the normal package validator will expose the inconsistency.
        """

        work_dir = self.ctx.work_dir
        plan_path = work_dir / "RESEARCH_PLAN.json"
        markdown_path = work_dir / "RESEARCH_PLAN.md"
        prior_plan_audit = _read_json(work_dir / "RESEARCH_PLAN_AUDIT.json")
        prior_cleanup_audit = _read_json(
            work_dir / "RESEARCH_PLAN_CLEANUP_AUDIT.json"
        )
        audit_base: Dict[str, Any] = {
            "action": "reject_plan_traceability_matrix",
            "status": "blocked",
            "source": "RESEARCH_PLAN.json",
            "independent_revalidation": True,
            "prior_plan_audit_status": str(
                prior_plan_audit.get("status") or "missing"
            ),
            "prior_cleanup_audit_status": str(
                prior_cleanup_audit.get("status") or "missing"
            ),
            "errors": [],
        }
        if not plan_path.exists() or not markdown_path.exists():
            audit_base["errors"] = [
                "durable_research_plan_or_markdown_missing"
            ]
            return None, audit_base

        plan = _read_json(plan_path)
        markdown = markdown_path.read_text(
            encoding="utf-8", errors="replace"
        )
        errors: List[str] = []
        plan_matrix = [
            dict(item)
            for item in (plan.get("traceability_matrix") or [])
            if isinstance(item, dict)
        ]
        selected_opportunities = set(
            _as_string_list(focus_gate.get("selected_opportunity_ids"))
        )
        selected_hypotheses = set(
            _as_string_list(focus_gate.get("main_hypothesis_ids"))
        )
        if set(_as_string_list(plan.get("main_hypothesis_ids"))) != selected_hypotheses:
            errors.append("plan_main_hypothesis_ids_do_not_match_focus")
        if str(plan.get("program_focus_gate_id") or "").strip() and str(
            plan.get("program_focus_gate_id")
        ).strip() != str(focus_gate.get("gate_id") or "").strip():
            errors.append("plan_focus_gate_id_does_not_match_focus")
        if not _traceability_matrix_is_complete(plan_matrix, focus_gate, plan):
            errors.append("plan_traceability_matrix_incomplete_or_inconsistent")

        opportunity_rows = [
            item
            for item in opportunities.get("opportunities", [])
            if isinstance(item, dict)
        ]
        hypothesis_rows = [
            item
            for item in hypotheses.get("hypotheses", [])
            if isinstance(item, dict)
        ]
        opportunity_ids = {
            str(item.get("opportunity_id") or "")
            for item in opportunity_rows
            if item.get("opportunity_id")
        }
        hypothesis_ids = {
            str(item.get("hypothesis_id") or "")
            for item in hypothesis_rows
            if item.get("hypothesis_id")
        }
        errors.extend(self._validate_opportunities(opportunity_rows))
        errors.extend(self._validate_hypotheses(hypothesis_rows, opportunity_ids))
        try:
            ResearchPlan.model_validate(plan)
        except Exception as exc:
            errors.append(f"plan_schema_revalidation_failed:{exc}")
        errors.extend(
            self._validate_plan(
                plan,
                opportunity_ids,
                hypothesis_ids,
                hypothesis_readiness={
                    str(item.get("hypothesis_id")): str(
                        item.get("readiness") or ""
                    )
                    for item in hypothesis_rows
                    if item.get("hypothesis_id")
                },
            )
        )

        candidate_gate = copy.deepcopy(focus_gate)
        candidate_gate["traceability_matrix"] = [
            dict(item) for item in plan_matrix
        ]
        package_result = ProgramFocusGate().validate_package(
            candidate_gate,
            opportunity_rows,
            hypothesis_rows,
            plan,
            shared_context=self.shared_review_context,
            permission_map=self._permission_map(),
        )
        errors.extend(package_result.errors)
        errors.extend(
            _audit_rendered_plan_content(
                plan,
                markdown,
                self.source_terminology_ledger,
            )
        )
        errors = list(dict.fromkeys(str(item) for item in errors if str(item)))
        if errors:
            audit_base["errors"] = errors[:30]
            audit_base["row_count"] = len(plan_matrix)
            return None, audit_base

        return plan_matrix, {
            "action": "rehydrate_plan_traceability_matrix",
            "status": "passed",
            "source": "RESEARCH_PLAN.json",
            "row_count": len(plan_matrix),
            "independent_revalidation": True,
            "prior_plan_audit_status": str(
                prior_plan_audit.get("status") or "missing"
            ),
            "prior_cleanup_audit_status": str(
                prior_cleanup_audit.get("status") or "missing"
            ),
            "policy": (
                "The current plan, focus, opportunity, hypothesis, work-package, "
                "and rendered-content contracts were revalidated; prior audit "
                "files were retained only as provenance."
            ),
        }

    def reconcile_existing_r5_artifacts(self) -> Dict[str, Any]:
        """Reconcile an interrupted R5 run without invoking a model.

        This is the plan-writing resume boundary: opportunities, hypotheses,
        and the focus choice are read from disk, readiness is only calibrated
        downward, and a single-platform ``hybrid`` label may be normalized to
        the declared platform type.  No discovery, hypothesis generation, or
        focus search is repeated.
        """

        work_dir = self.ctx.work_dir
        opportunities = _read_json(work_dir / "RESEARCH_OPPORTUNITY_MAP.json")
        hypotheses = _read_json(work_dir / "HYPOTHESIS_PORTFOLIO.json")
        focus_gate = _read_json(work_dir / "PROGRAM_FOCUS_GATE.json")
        focus_recovered_from_state = False
        if not focus_gate:
            focus_gate = _recover_last_focus_gate_from_agent_state(work_dir)
            focus_recovered_from_state = bool(focus_gate)
        readiness_audits: List[Dict[str, Any]] = []
        errors: List[str] = []
        changed = False

        if hypotheses.get("hypotheses"):
            try:
                normalized = _normalize_hypothesis_payload(hypotheses)
                normalized, readiness_audits = _calibrate_hypothesis_readiness(
                    normalized,
                    opportunities,
                    self._permission_map(),
                )
                model = ResearchHypothesisPortfolio.model_validate(normalized)
                hypothesis_errors = self._validate_hypotheses(
                    [item.model_dump() for item in model.hypotheses],
                    {
                        str(item.get("opportunity_id"))
                        for item in opportunities.get("opportunities", [])
                        if isinstance(item, dict)
                    },
                )
                if hypothesis_errors:
                    errors.extend(hypothesis_errors)
                else:
                    canonical = model.model_dump()
                    if canonical != hypotheses:
                        atomic_write_json(work_dir / "HYPOTHESIS_PORTFOLIO.json", canonical)
                        changed = True
                    atomic_write_json(
                        work_dir / "HYPOTHESIS_READINESS_AUDIT.json",
                        {
                            "schema_version": "research_harness.hypothesis_readiness_audit.v1",
                            "corrections": readiness_audits,
                            "correction_count": len(readiness_audits),
                            "policy": "Readiness may be downgraded by source permission checks, never upgraded.",
                            "resume_reconciliation": True,
                        },
                    )
            except Exception as exc:
                errors.append(f"hypothesis_reconciliation_error:{exc}")

        focus_status = "missing"
        focus_corrections: List[Dict[str, Any]] = []
        if focus_recovered_from_state:
            focus_corrections.append(
                {
                    "field": "program_focus_gate",
                    "from": "AGENT_STATE.json",
                    "to": "offline_reconciliation_candidate",
                    "reason": (
                        "Recovered the latest rejected focus submission for "
                        "deterministic reconciliation; no model call was made."
                    ),
                }
            )
        if focus_gate and opportunities.get("opportunities") and hypotheses.get("hypotheses"):
            try:
                normalized_gate, focus_corrections = (
                    _normalize_focus_gate_against_opportunities(
                        focus_gate,
                        [
                            item
                            for item in opportunities.get("opportunities", [])
                            if isinstance(item, dict)
                        ],
                        [
                            item
                            for item in _read_json(
                                work_dir / "HYPOTHESIS_PORTFOLIO.json"
                            ).get("hypotheses", hypotheses.get("hypotheses", []))
                            if isinstance(item, dict)
                        ],
                    )
                )
                normalized_gate["source_context"] = self.shared_review_context
                decision = ProgramFocusGate().validate_focus_decision(
                    normalized_gate,
                    opportunities.get("opportunities", []),
                    _read_json(work_dir / "HYPOTHESIS_PORTFOLIO.json").get("hypotheses", hypotheses.get("hypotheses", [])),
                    shared_context=self.shared_review_context,
                    permission_map=self._permission_map(),
                )
                if not decision.passed:
                    repaired_gate, convergence_audit = (
                        _try_evidence_calibrated_single_spine(
                            normalized_gate,
                            decision.errors,
                            [
                                item
                                for item in opportunities.get("opportunities", [])
                                if isinstance(item, dict)
                            ],
                            [
                                item
                                for item in _read_json(
                                    work_dir / "HYPOTHESIS_PORTFOLIO.json"
                                ).get(
                                    "hypotheses", hypotheses.get("hypotheses", [])
                                )
                                if isinstance(item, dict)
                            ],
                            self._permission_map(),
                        )
                    )
                    if repaired_gate is not None:
                        repaired_gate["source_context"] = self.shared_review_context
                        repaired_decision = ProgramFocusGate().validate_focus_decision(
                            repaired_gate,
                            opportunities.get("opportunities", []),
                            _read_json(work_dir / "HYPOTHESIS_PORTFOLIO.json").get(
                                "hypotheses", hypotheses.get("hypotheses", [])
                            ),
                            shared_context=self.shared_review_context,
                            permission_map=self._permission_map(),
                        )
                        if repaired_decision.passed:
                            normalized_gate = repaired_gate
                            decision = repaired_decision
                            if convergence_audit is not None:
                                focus_corrections.append(convergence_audit)
                # Focus-stage rows are non-authoritative before a plan exists,
                # but a durable independently valid plan owns a canonical
                # matrix that must survive this reconciliation boundary.  Do
                # the plan-backed revalidation before deciding whether to
                # clear the focus copy.  This is deliberately fail-closed:
                # an invalid or stale plan never restores its rows.
                plan_matrix, plan_matrix_audit = (
                    self._rehydrate_plan_backed_traceability_matrix(
                        normalized_gate,
                        opportunities,
                        _read_json(work_dir / "HYPOTHESIS_PORTFOLIO.json"),
                    )
                )
                if plan_matrix is not None:
                    current_matrix = [
                        dict(item)
                        for item in (normalized_gate.get("traceability_matrix") or [])
                        if isinstance(item, dict)
                    ]
                    normalized_gate["traceability_matrix"] = [
                        dict(item) for item in plan_matrix
                    ]
                    focus_corrections.append(plan_matrix_audit)
                    if current_matrix != plan_matrix:
                        changed = True
                elif normalized_gate.get("traceability_matrix"):
                    normalized_gate, matrix_correction = (
                        _reset_focus_traceability_for_plan_rebuild(normalized_gate)
                    )
                    if matrix_correction is not None:
                        matrix_correction["reason"] = (
                            "No current durable plan passed independent "
                            "matrix revalidation; pre-plan focus rows were "
                            "cleared and will not be treated as plan evidence."
                        )
                        focus_corrections.append(matrix_correction)
                focus_status = decision.status
                if decision.passed:
                    normalized_gate["status"] = "passed"
                    normalized_gate["validation"] = decision.as_dict()
                    if normalized_gate != focus_gate:
                        atomic_write_json(work_dir / "PROGRAM_FOCUS_GATE.json", normalized_gate)
                        changed = True
                else:
                    errors.extend(decision.errors)
            except Exception as exc:
                focus_status = "error"
                errors.append(f"focus_reconciliation_error:{exc}")

        atomic_write_json(
            work_dir / "PROGRAM_FOCUS_NORMALIZATION_AUDIT.json",
            {
                "schema_version": "research_harness.focus_normalization_audit.v1",
                "status": "passed" if focus_status == "passed" else "blocked",
                "submission_count": 0,
                "same_error_repeat_count": 0,
                "normalization_corrections": focus_corrections,
                "errors": list(dict.fromkeys(errors)),
                "offline_reconciliation": True,
                "recovered_from_agent_state": focus_recovered_from_state,
                "policy": (
                    "Mechanical evidence-calibrated convergence may select a "
                    "unique highest-ranked spine, but never resolves a tie or "
                    "an unrelated validation error."
                ),
            },
        )

        report = {
            "schema_version": "research_harness.r5_reconciliation.v1",
            "status": "ready_for_plan_resume" if not errors else "blocked",
            "recomputed_opportunities": False,
            "recomputed_hypotheses": False,
            "recomputed_focus": False,
            "readiness_correction_count": len(readiness_audits),
            "focus_normalization_correction_count": len(focus_corrections),
            "focus_recovered_from_agent_state": focus_recovered_from_state,
            "focus_status": focus_status,
            "changed_existing_artifacts": changed,
            "errors": list(dict.fromkeys(errors)),
            "model_next_step": "plan_only",
        }
        atomic_write_json(work_dir / "R5_RECONCILIATION.json", report)
        return report

    def _first_existing(self, *candidates: Optional[Path]) -> Optional[Path]:
        for candidate in candidates:
            if candidate is None:
                continue
            try:
                if candidate.exists():
                    return candidate.resolve()
            except OSError:
                continue
        return None

    @staticmethod
    def _artifact_reference(path: Optional[Path]) -> str:
        return str(path) if path is not None else ""

    def _build_shared_review_context(self) -> Dict[str, Any]:
        """Expose Phase-3/R4 lineage without duplicating large artifacts."""

        root = self.ctx.coverage_root
        phase3_root = self._first_existing(self.ctx.phase3_artifacts_root, root) or root
        parent = root.parent
        phase3_parent = phase3_root.parent
        review_roots: list[Path] = []
        if self.ctx.final_review_path is not None:
            review_root = self.ctx.final_review_path
            if not review_root.is_dir():
                review_root = review_root.parent
            # A final review may be at the run root or below a section folder.
            # Walk only a bounded number of parents so unrelated desktop files
            # cannot become implicit lineage inputs.
            for _ in range(6):
                if review_root not in review_roots:
                    review_roots.append(review_root)
                if review_root.parent == review_root:
                    break
                review_root = review_root.parent
        scope_path = self._first_existing(
            self.ctx.review_scope_map_path,
            phase3_root / "REVIEW_SCOPE_MAP.json",
            phase3_parent / "REVIEW_SCOPE_MAP.json",
            root / "REVIEW_SCOPE_MAP.json",
            parent / "REVIEW_SCOPE_MAP.json",
            self.ctx.blueprint_path.parent / "REVIEW_SCOPE_MAP.json",
        )
        scope = _read_json(scope_path) if scope_path else {}
        embedded_scope = self.blueprint.get("review_scope_map")
        if not scope and isinstance(embedded_scope, dict):
            scope = dict(embedded_scope)
        dimensions = []
        for item in scope.get("research_dimensions", []) if isinstance(scope, dict) else []:
            if not isinstance(item, dict):
                continue
            dimensions.append(
                {
                    "dimension_id": str(item.get("dimension_id") or ""),
                    "title": str(item.get("title") or ""),
                    "argument_task": str(item.get("argument_task") or "")[:900],
                    "literature_roles": list(item.get("literature_roles") or []),
                }
            )
        if not dimensions:
            dimensions = [
                {
                    "dimension_id": str(section.get("section_id") or ""),
                    "title": str(section.get("title") or ""),
                    "argument_task": str(
                        section.get("argument_role")
                        or section.get("chapter_argument")
                        or ""
                    )[:900],
                    "literature_roles": list(section.get("required_roles") or []),
                }
                for section in self.blueprint.get("sections", [])
                if isinstance(section, dict) and section.get("section_id")
            ]
        scope_summary = {
            "schema_version": str(scope.get("schema_version") or "review_scope_map.v1"),
            "artifact_ref": self._artifact_reference(scope_path),
            "user_question": str(scope.get("user_question") or self.problem_frame.get("user_question") or ""),
            "core_question": str(scope.get("core_question") or ""),
            "central_judgment": str(scope.get("central_judgment") or "")[:1600],
            "review_mode": str(scope.get("review_mode") or self.blueprint.get("review_mode") or ""),
            "dimensions": dimensions,
        }

        relation_path = self._first_existing(
            self.ctx.relation_graph_path,
            phase3_root / "RELATION_GRAPH_MIGRATED.json",
            phase3_root / "RELATION_GRAPH.json",
            root / "RELATION_GRAPH_MIGRATED.json",
            root / "RELATION_GRAPH.json",
            parent / "RELATION_GRAPH_MIGRATED.json",
            parent / "RELATION_GRAPH.json",
        )
        relation = _read_json(relation_path) if relation_path else {}
        relation_rows = relation.get("edges") or relation.get("relations") or []
        if not isinstance(relation_rows, list):
            relation_rows = []
        relation_summary = {
            "schema_version": str(relation.get("schema_version") or "literature_relation_graph.v1"),
            "artifact_ref": self._artifact_reference(relation_path),
            "edge_count": len([item for item in relation_rows if isinstance(item, dict)]),
            "observed_relation_counts": relation.get("observed_relation_counts") or {},
            "semantic_relation_counts": relation.get("semantic_relation_counts") or {},
            "observed_edges_are_not_semantic_relations": bool(
                relation.get("observed_edges_are_not_semantic_relations", True)
            ),
            "inferred_edges_require_basis": True,
        }

        technical_candidates: list[Optional[Path]] = [self.ctx.technical_audit_path]
        # R4's final package is the authoritative handoff status when the
        # program is launched from an existing review run.  Prefer it over a
        # generic Phase-3 acceptance file so awaiting-human-review cannot be
        # silently flattened into a passed technical audit.
        for review_root in review_roots:
            technical_candidates.extend(
                [
                    review_root / "FULL_REVIEW_PACKAGE.json",
                    review_root / "R4_REAL_ACCEPTANCE_SUMMARY.json",
                    review_root / "R4_PHASE3_HANDOFF.json",
                    review_root / "REVIEW_STATE.json",
                ]
            )
        technical_candidates.extend(
            [
            phase3_root / "PHASE3_ACCEPTANCE.json",
            phase3_root / "R4_PHASE3_HANDOFF.json",
            parent / "R4_PHASE3_HANDOFF.json",
            parent / "PHASE3_ACCEPTANCE.json",
            root / "PHASE3_ACCEPTANCE.json",
            self.ctx.quality_report_path,
            ]
        )
        technical_path = self._first_existing(*technical_candidates)
        technical = _read_json(technical_path) if technical_path else {}
        limitations: list[str] = []
        for key in (
            "limitations",
            "warnings",
            "blocking_issues",
            "open_gaps",
            "unresolved_issues",
        ):
            values = technical.get(key, []) if isinstance(technical, dict) else []
            if isinstance(values, list):
                limitations.extend(str(value).strip() for value in values if str(value).strip())
        technical_status = str(
            technical.get("status")
            or technical.get("state")
            or technical.get("review_status")
            or ""
        ).strip().casefold()
        if technical_status in {
            "awaiting_human_review",
            "needs_more_literature",
            "candidate",
            "pending_human_review",
        }:
            limitations.append(
                "The upstream review package is not final (status="
                f"{technical_status}); its material is a candidate and retains unresolved review limitations."
            )
        if technical.get("r4_handoff_ready") is False:
            limitations.append("The upstream R4 handoff was not marked ready; candidate material retains its recorded limitations.")
        for key, label in (
            ("blocking_flags", "blocking review flags"),
            ("total_flags", "total review flags"),
            ("section_flags_total", "section review flags"),
        ):
            try:
                count = int(technical.get(key) or 0)
            except (TypeError, ValueError):
                count = 0
            if count > 0:
                limitations.append(
                    f"The upstream review package records {count} {label}; do not treat the candidate as an executed result."
                )
        awaiting_sections = technical.get("sections_awaiting_human_review")
        if isinstance(awaiting_sections, list) and awaiting_sections:
            limitations.append(
                "Sections awaiting human review: "
                + ", ".join(str(item).strip() for item in awaiting_sections if str(item).strip())
                + "."
            )
        synthesis = _read_json(phase3_root / "SYNTHESIS_BUNDLES.json")
        for bundle in synthesis.get("bundles", []) if isinstance(synthesis, dict) else []:
            if not isinstance(bundle, dict):
                continue
            if (
                bundle.get("r4_handoff_allowed") is False
                or str(bundle.get("status") or "") in {"needs_more_literature", "awaiting_human_review"}
            ):
                section_id = str(bundle.get("section_id") or "unknown_section")
                limitations.append(
                    f"R4 section {section_id} is a candidate or literature-limited handoff; retain its recorded limitations and do not treat it as an executed result."
                )
        technical_summary = {
            "schema_version": str(technical.get("schema_version") or "phase3_r4_technical_audit.v1"),
            "artifact_ref": self._artifact_reference(technical_path),
            "status": str(
                technical.get("status")
                or technical.get("state")
                or technical.get("engineering_status")
                or "not_provided"
            ),
            "r4_handoff_ready": technical.get("r4_handoff_ready"),
            "limitations": list(dict.fromkeys(limitations))[:24],
            "source_permissions_are_inherited": True,
        }
        permission_counts: Dict[str, int] = {}
        for value in self._paper_permissions.values():
            permission_counts[value] = permission_counts.get(value, 0) + 1
        chunk_permission_counts: Dict[str, int] = {}
        for value in self._chunk_permissions.values():
            chunk_permission_counts[value] = chunk_permission_counts.get(value, 0) + 1
        permissions = {
            "schema_version": "research_harness.source_permissions_summary.v1",
            "artifact_ref": self._artifact_reference(self.ctx.source_permissions_path),
            "paper_permission_counts": dict(sorted(permission_counts.items())),
            "chunk_permission_counts": dict(sorted(chunk_permission_counts.items())),
            "direct_fact_permission": "factual_support",
            "background_permission": "contextual_or_qualified_support",
            "discovery_permission": "discovery_only",
            "discovery_only_cannot_support_hypothesis_facts": True,
        }
        return {
            "schema_version": "research_harness.shared_review_context.v1",
            "review_scope_map": scope_summary,
            "literature_relation_graph": relation_summary,
            "technical_audit": technical_summary,
            "source_permissions": permissions,
            "r4_candidate_limitations": technical_summary["limitations"],
            "phase3_artifact_root": self._artifact_reference(phase3_root),
            "lineage_policy": (
                "Review scope, literature relations, technical audit, and source permissions "
                "are shared inputs. They are not themselves experimental results."
            ),
        }

    def _evidence_identifier_catalog(
        self,
        *,
        limit: int = 24,
    ) -> List[Dict[str, str]]:
        """Expose real identifiers before the agent requests evidence.

        This is intentionally a compact routing catalogue, not evidence text.
        It prevents an agent from guessing DOI-like chunk IDs when the
        canonical chunk IDs use route-specific prefixes.
        """

        catalog: List[Dict[str, str]] = []
        seen: set[tuple[str, str, str]] = set()
        for chunk_id, paper_id in self._chunk_to_paper.items():
            sections = sorted(self._chunk_to_sections.get(chunk_id, set()))
            section_id = sections[0] if sections else ""
            key = (section_id, paper_id, chunk_id)
            if key in seen:
                continue
            catalog.append(
                {
                    "section_id": section_id,
                    "paper_id": paper_id,
                    "chunk_id": chunk_id,
                }
            )
            seen.add(key)
            if len(catalog) >= max(1, int(limit)):
                break
        return catalog

    def _resolve_evidence_identifiers(
        self,
        identifiers: List[str],
    ) -> tuple[List[str], List[str], Dict[str, List[str]]]:
        """Resolve canonical chunk IDs or allowlisted paper identifiers."""

        resolved: List[str] = []
        unknown: List[str] = []
        resolution: Dict[str, List[str]] = {}
        for raw in identifiers:
            identifier = str(raw or "").strip()
            if not identifier:
                continue
            candidates = [identifier]
            if identifier.startswith("REF:"):
                candidates.append(identifier[4:])
            if identifier.startswith("[REF:") and identifier.endswith("]"):
                candidates.append(identifier[5:-1])
            if re.match(r"^10\.\d{4,9}/", identifier, re.I):
                candidates.append(f"doi:{identifier.lower()}")
            if identifier.isdigit():
                candidates.append(f"CorpusId:{identifier}")

            matched: List[str] = []
            for candidate in dict.fromkeys(candidates):
                if candidate in self._chunk_to_paper:
                    matched.append(candidate)
                    break
                paper_chunks = self._paper_to_chunks.get(candidate, [])
                if paper_chunks:
                    matched.extend(paper_chunks[:2])
                    break
            if not matched:
                unknown.append(identifier)
                continue
            resolution[identifier] = matched
            for chunk_id in matched:
                if chunk_id not in resolved:
                    resolved.append(chunk_id)
        return resolved, unknown, resolution

    def get_allowed_tool_names(self) -> List[str]:
        return list(self.TOOL_NAMES)

    def _section_cards(self) -> List[Dict[str, Any]]:
        cards = []
        for section in self.blueprint.get("sections", []):
            if not isinstance(section, dict):
                continue
            section_id = str(section.get("section_id") or "")
            title = str(section.get("title") or "")
            text = self.review_sections.get(title, "")
            paras = _paragraphs(text)
            gap = _read_json(
                self.ctx.coverage_root
                / "sections"
                / section_id
                / "SECTION_GAP_REPORT.json"
            )
            ledger = _read_json(
                self.ctx.coverage_root
                / "sections"
                / section_id
                / "SECTION_SOURCE_LEDGER.json"
            )
            papers = {
                str(source.get("paper_id"))
                for source in ledger.get("sources", [])
                if isinstance(source, dict) and source.get("paper_id")
            }
            candidate_chunks = []
            for source in ledger.get("sources", []):
                if not isinstance(source, dict):
                    continue
                for chunk_id in source.get("canonical_chunk_ids", []):
                    if chunk_id and chunk_id not in candidate_chunks:
                        candidate_chunks.append(str(chunk_id))
            cards.append(
                {
                    "section_id": section_id,
                    "title": title,
                    "argument_role": section.get("argument_role", ""),
                    "chapter_argument": section.get("chapter_argument", ""),
                    "synthesis_task": section.get("synthesis_task", ""),
                    "closing_preview": paras[-1][:900] if paras else "",
                    "open_gaps": [
                        {
                            "role": item.get("role", ""),
                            "description": item.get("description", ""),
                            "stop_reason": item.get("stop_reason", ""),
                        }
                        for item in gap.get("gaps", [])
                        if isinstance(item, dict)
                    ][:6],
                    "source_count": len(papers),
                    "candidate_chunk_ids": candidate_chunks[:10],
                }
            )
        return cards

    @staticmethod
    def _compact_digest_text(value: Any, limit: int = 360) -> str:
        text = " ".join(str(value or "").split()).strip()
        if len(text) <= limit:
            return text
        return text[: max(1, limit - 1)].rstrip() + "…"

    def _section_digests(self) -> List[Dict[str, Any]]:
        """Build bounded section-level routing records for initial R5 discovery.

        Digests deliberately omit full review text and canonical chunk IDs.
        They give the architect enough structure to choose where a single
        batch read may help without turning section browsing into the default
        workflow.
        """

        digests: List[Dict[str, Any]] = []
        for card in self._section_cards():
            section_id = str(card.get("section_id") or "")
            ledger = _read_json(
                self.ctx.coverage_root
                / "sections"
                / section_id
                / "SECTION_SOURCE_LEDGER.json"
            )
            gap = _read_json(
                self.ctx.coverage_root
                / "sections"
                / section_id
                / "SECTION_GAP_REPORT.json"
            )
            source_rows = [
                item
                for item in ledger.get("sources", [])
                if isinstance(item, dict)
            ]
            permission_counts: Dict[str, int] = {}
            role_counts: Dict[str, int] = {}
            direct_paper_count = 0
            for source in source_rows:
                role = str(source.get("literature_role") or "unknown")
                role_counts[role] = role_counts.get(role, 0) + 1
                if str(source.get("scope_fit") or "").lower() == "direct":
                    direct_paper_count += 1
                for chunk_id in source.get("canonical_chunk_ids", []):
                    permission = self._chunk_permissions.get(
                        str(chunk_id),
                        self._infer_source_permission(source),
                    )
                    permission_counts[permission] = (
                        permission_counts.get(permission, 0) + 1
                    )
            section = next(
                (
                    item
                    for item in self.blueprint.get("sections", [])
                    if isinstance(item, dict)
                    and str(item.get("section_id") or "") == section_id
                ),
                {},
            )
            key_judgments = []
            for value in (
                section.get("chapter_argument"),
                section.get("synthesis_task"),
                section.get("mentor_guidance"),
                card.get("closing_preview"),
            ):
                compact = self._compact_digest_text(value)
                if compact and compact not in key_judgments:
                    key_judgments.append(compact)
            coverage_status = str(
                gap.get("overall_coverage_status")
                or section.get("status")
                or "unknown"
            )
            digests.append(
                {
                    "section_id": section_id,
                    "title": card.get("title", ""),
                    "argument_role": self._compact_digest_text(
                        card.get("argument_role"), 220
                    ),
                    "argument_task": self._compact_digest_text(
                        card.get("chapter_argument")
                        or card.get("synthesis_task"),
                        320,
                    ),
                    "status": coverage_status,
                    "key_judgments": key_judgments[:3],
                    "permission_counts": dict(sorted(permission_counts.items())),
                    "candidate_counts": {
                        "papers": len(
                            {
                                str(source.get("paper_id"))
                                for source in source_rows
                                if source.get("paper_id")
                            }
                        ),
                        "direct_papers": direct_paper_count,
                        "chunks": len(card.get("candidate_chunk_ids", [])),
                        "candidates_found": sum(
                            int(item.get("candidates_found", 0) or 0)
                            for item in gap.get("gaps", [])
                            if isinstance(item, dict)
                        ),
                        "candidates_materialized": sum(
                            int(item.get("candidates_materialized", 0) or 0)
                            for item in gap.get("gaps", [])
                            if isinstance(item, dict)
                        ),
                    },
                    "role_counts": dict(sorted(role_counts.items())),
                    "open_gap_count": int(
                        gap.get("open_gap_count", len(gap.get("gaps", [])))
                        or 0
                    ),
                    "blocking_gap_count": int(
                        gap.get("blocking_gap_count", 0) or 0
                    ),
                }
            )
        return digests

    def _lookup_chunks(
        self,
        chunk_ids: List[str],
        *,
        max_items: int = 12,
    ) -> List[Dict[str, Any]]:
        remaining = max(
            0, self.ctx.max_evidence_chunks - self._evidence_reads
        )
        requested = [
            chunk_id
            for chunk_id in dict.fromkeys(chunk_ids)
            if chunk_id in self._chunk_to_paper
        ][: min(max(1, int(max_items)), remaining)]
        if not requested:
            return []
        results: Dict[str, Dict[str, Any]] = {}
        for db_path in (
            self.ctx.base_kb_sqlite,
            self.ctx.staging_kb_sqlite,
        ):
            if db_path is None or not db_path.exists():
                continue
            try:
                with sqlite3.connect(str(db_path)) as conn:
                    placeholders = ",".join("?" for _ in requested)
                    rows = conn.execute(
                        "SELECT chunk_id, paper_id, text FROM text_chunks "
                        f"WHERE chunk_id IN ({placeholders})",
                        requested,
                    ).fetchall()
                for chunk_id, paper_id, text in rows:
                    if (
                        str(chunk_id) in self._chunk_to_paper
                        and self._chunk_to_paper[str(chunk_id)]
                        == str(paper_id)
                    ):
                        results[str(chunk_id)] = {
                            "chunk_id": str(chunk_id),
                            "paper_id": str(paper_id),
                            "text": str(text or "")[:3500],
                        }
            except Exception:
                continue
        self._evidence_reads += len(results)
        return [results[item] for item in requested if item in results]

    def _build_discovery_resume_context(self) -> Dict[str, Any]:
        """Build a compact context for a hypothesis or focus-stage resume."""

        stage = self.ctx.discovery_stage
        opportunities = _read_json(
            self.ctx.work_dir / "RESEARCH_OPPORTUNITY_MAP.json"
        )
        hypotheses = _read_json(
            self.ctx.work_dir / "HYPOTHESIS_PORTFOLIO.json"
        )
        opportunity_records = [
            item
            for item in opportunities.get("opportunities", [])
            if isinstance(item, dict)
        ]
        hypothesis_records = [
            item
            for item in hypotheses.get("hypotheses", [])
            if isinstance(item, dict)
        ]
        records: List[Dict[str, Any]] = []
        if stage == "hypothesis":
            records = list(opportunity_records)
        elif stage == "focus":
            # Focus selection needs both sides of the accepted mapping: the
            # hypothesis records and the opportunities from which they came.
            # Previously this branch returned an empty persisted_opportunities
            # list, forcing the model to infer the opportunity context from
            # hypotheses or repeat discovery.
            records = [*opportunity_records, *hypothesis_records]
        chunk_ids: List[str] = []
        for record in records:
            for key in ("supporting_chunk_ids", "basis_chunk_ids"):
                chunk_ids.extend(
                    str(value)
                    for value in record.get(key, [])
                    if value
                )
        evidence = self._lookup_chunks(
            list(dict.fromkeys(chunk_ids)),
            max_items=min(12, max(1, int(self.ctx.max_evidence_chunks))),
        )
        return {
            "status": "ok",
            "mode": "discovery_stage_resume",
            "stage": stage,
            "instruction": (
                "Resume only the current stage. Do not regenerate artifacts from "
                "completed stages, reopen full review text, or call tools that are "
                "not present in the current toolkit."
            ),
            "persisted_opportunities": (
                opportunity_records
                if stage in {"hypothesis", "focus"}
                else []
            ),
            "persisted_opportunity_ids": [
                str(item.get("opportunity_id"))
                for item in opportunity_records
                if item.get("opportunity_id")
            ],
            "persisted_hypotheses": (
                hypothesis_records if stage == "focus" else []
            ),
            "persisted_hypothesis_ids": [
                str(item.get("hypothesis_id") or item.get("id"))
                for item in hypothesis_records
                if item.get("hypothesis_id") or item.get("id")
            ],
            "section_digests": self._section_digests(),
            "bounded_evidence": evidence,
            "evidence_policy": (
                "Use only returned canonical chunk IDs. These records are bounded "
                "context, not permission to expand the completed stage."
            ),
            "next_actions": (
                ["submit_hypothesis_portfolio", "submit_program_focus_gate"]
                if stage == "hypothesis"
                else ["submit_program_focus_gate"]
            ),
        }

    def _build_plan_only_context(self) -> Dict[str, Any]:
        """Build the bounded context used by a plan-only resume.

        The previous implementation exposed the whole shared review context
        and the generic evidence tools.  That made a resumed plan writer
        behave like a discovery agent again.  This payload contains only the
        accepted project spine, the selected opportunity/hypotheses, a small
        evidence digest, and a machine-readable submission scaffold.
        """

        focus_gate = _read_json(
            self.ctx.work_dir / "PROGRAM_FOCUS_GATE.json"
        )
        opportunity_map = _read_json(
            self.ctx.work_dir / "RESEARCH_OPPORTUNITY_MAP.json"
        )
        hypothesis_portfolio = _read_json(
            self.ctx.work_dir / "HYPOTHESIS_PORTFOLIO.json"
        )
        selected_opportunity_ids = [
            str(value)
            for value in focus_gate.get("selected_opportunity_ids", [])
            if value
        ]
        selected_hypothesis_ids = [
            str(value)
            for value in focus_gate.get("main_hypothesis_ids", [])
            if value
        ]
        opportunities = [
            item
            for item in opportunity_map.get("opportunities", [])
            if isinstance(item, dict)
            and str(item.get("opportunity_id")) in selected_opportunity_ids
        ]
        hypotheses = [
            item
            for item in hypothesis_portfolio.get("hypotheses", [])
            if isinstance(item, dict)
            and str(item.get("hypothesis_id")) in selected_hypothesis_ids
        ]
        evidence_ids: List[str] = []
        for item in [*opportunities, *hypotheses]:
            evidence_ids.extend(
                str(value)
                for value in item.get("supporting_chunk_ids", [])
                if value
            )
        evidence_rows = self._lookup_chunks(
            list(dict.fromkeys(evidence_ids))[:8]
        )
        evidence_summary = [
            {
                "chunk_id": row.get("chunk_id", ""),
                "paper_id": row.get("paper_id", ""),
                "permission": self._chunk_permissions.get(
                    str(row.get("chunk_id") or ""), ""
                ),
                "text_preview": str(row.get("text") or "")[:1200],
            }
            for row in evidence_rows
        ]
        platform = dict(focus_gate.get("shared_platform") or {})
        platform_key = str(
            platform.get("compatibility_key")
            or platform.get("platform_compatibility_key")
            or ""
        )
        focus_snapshot = {
            "schema_version": focus_gate.get("schema_version", ""),
            "gate_id": focus_gate.get("gate_id", ""),
            "main_problem": focus_gate.get("main_problem", {}),
            "project_type": focus_gate.get("project_type", ""),
            "shared_platform": {
                key: platform.get(key, "")
                for key in (
                    "platform_id",
                    "platform_type",
                    "name",
                    "description",
                    "compatibility_key",
                    "platform_compatibility_key",
                )
            },
            "boundaries": focus_gate.get("boundaries", {}),
            "unified_evaluation": focus_gate.get(
                "unified_evaluation", {}
            ),
            "selected_opportunity_ids": selected_opportunity_ids,
            "main_hypothesis_ids": selected_hypothesis_ids,
            "future_hypothesis_ids": list(
                focus_gate.get("future_hypothesis_ids", [])
            ),
            "future_branch_opportunity_ids": [
                str(item.get("opportunity_id"))
                for item in focus_gate.get("future_branches", [])
                if isinstance(item, dict) and item.get("opportunity_id")
            ],
        }
        root_fields = [
            "schema_version", "title", "research_question", "strategy",
            "objectives", "work_packages", "milestones",
            "human_decision_points", "unresolved_literature_needs",
            "readiness_summary", "narrative_markdown", "paper_abstract",
            "problem_statement", "rationale", "technical_details",
            "dataset_source", "dataset_target", "methods_summary",
            "experiments", "expected_results", "results_status",
            "reference_paper_ids", "verification_deferred",
            "main_problem", "project_type", "shared_platform", "boundaries",
            "unified_evaluation", "main_hypothesis_ids",
            "future_hypothesis_ids", "hypothesis_dependencies",
            "future_branches", "traceability_matrix", "source_context",
            "source_limitations", "main_hypothesis_statements",
            "normalization_audit",
        ]
        work_package_fields = [
            "work_package_id", "title", "objective", "hypothesis_ids",
            "opportunity_ids", "methods", "inputs", "expected_outputs",
            "controls_or_baselines", "evaluation_metrics", "dependencies",
            "risks", "readiness", "quantitative_target_status",
            "quantitative_target_provenance",
            "platform_id", "platform_compatibility_key", "metric_ids",
            "baseline_ids", "stop_or_pivot_criteria",
        ]
        scaffold = {
            "schema_version": "research_harness.research_plan.v2",
            "required_root_fields": root_fields,
            "required_work_package_fields": work_package_fields,
            "work_package_template": {
                field: ([] if field.endswith("_ids") or field in {
                    "methods", "inputs", "expected_outputs",
                    "controls_or_baselines", "evaluation_metrics",
                    "dependencies", "risks", "metric_ids", "baseline_ids",
                    "stop_or_pivot_criteria",
                } else (
                    "needs_more_literature"
                    if field == "readiness"
                    else (
                        "none"
                        if field == "quantitative_target_status"
                        else (
                            "not_applicable"
                            if field == "quantitative_target_provenance"
                            else ""
                        )
                    )
                ))
                for field in work_package_fields
            },
            "fixed_values": {
                "results_status": "verification_deferred",
                "verification_status": "verification_deferred",
                "verification_rationale": (
                    "Planned work only; no new experiment, simulation, "
                    "or data-analysis result has been executed."
                ),
                "platform_compatibility_key": platform_key,
            },
        }
        source_limitations = list(
            self.shared_review_context.get("r4_candidate_limitations", [])
        )
        return {
            "status": "ok",
            "mode": "plan_only_resume",
            "instruction": (
                "The opportunity map, hypothesis portfolio, and focus gate "
                "are already accepted. Write only RESEARCH_PLAN.json. Do "
                "not recompute discovery, hypotheses, focus, or literature."
            ),
            "focus_gate": focus_snapshot,
            "selected_opportunities": opportunities,
            "main_hypotheses": hypotheses,
            "selected_evidence_summary": evidence_summary,
            "source_terminology_ledger": self.source_terminology_ledger,
            "source_limitations": source_limitations,
            "plan_schema_and_scaffold": scaffold,
            "submission_protocol": [
                "Submit one plan root object, never an opportunity or hypothesis container.",
                "Current work packages may reference only selected opportunity and main hypothesis IDs.",
                "Future branches and future hypothesis IDs must stay outside work_packages.",
                "Use the focus-gate platform_id and compatibility_key exactly; do not invent a replacement.",
                "Call the validator after submission. At most one semantic correction is allowed.",
            ],
        }

    def get_tools(self, work_dir: Path) -> list:
        provider = self

        def load_research_program_context() -> str:
            """Load the compact review, evidence, and gap context."""

            if provider.ctx.plan_only_resume:
                provider._plan_only_context_loads += 1
                if provider._plan_only_context_cache is None:
                    provider._plan_only_context_cache = (
                        provider._build_plan_only_context()
                    )
                    atomic_write_json(
                        provider.ctx.work_dir / "RESEARCH_PLAN_SCAFFOLD.json",
                        provider._plan_only_context_cache.get(
                            "plan_schema_and_scaffold", {}
                        ),
                    )
                if provider._plan_only_context_loads > 1:
                    return json.dumps(
                        {
                            "status": "already_loaded",
                            "mode": "plan_only_resume",
                            "next_action": "submit_research_plan",
                            "instruction": (
                                "Do not reload context. Submit the plan root "
                                "object using the scaffold."
                            ),
                        },
                        ensure_ascii=True,
                    )
                return json.dumps(
                    provider._plan_only_context_cache,
                    ensure_ascii=True,
                )
            if provider.ctx.discovery_stage in {"hypothesis", "focus"}:
                provider._plan_only_context_loads += 1
                if provider._plan_only_context_cache is None:
                    provider._plan_only_context_cache = (
                        provider._build_discovery_resume_context()
                    )
                    atomic_write_json(
                        provider.ctx.work_dir / "R5_DISCOVERY_CONTEXT.json",
                        provider._plan_only_context_cache,
                    )
                if provider._plan_only_context_loads > 1:
                    return json.dumps(
                        {
                            "status": "already_loaded",
                            "mode": "discovery_stage_resume",
                            "stage": provider.ctx.discovery_stage,
                            "next_actions": provider._plan_only_context_cache.get(
                                "next_actions", []
                            ),
                            "instruction": (
                                "The compact stage context is cached. Do not reload; "
                                "use the current stage submission tools."
                            ),
                        },
                        ensure_ascii=True,
                    )
                return json.dumps(
                    provider._plan_only_context_cache,
                    ensure_ascii=True,
                )

            input_context = provider.blueprint.get("input_context", {})
            quality = (
                _read_json(provider.ctx.quality_report_path)
                if provider.ctx.quality_report_path
                else {}
            )
            portfolio = (
                _read_json(provider.ctx.literature_portfolio_path)
                if provider.ctx.literature_portfolio_path
                else {}
            )
            visual = (
                _read_json(provider.ctx.visual_plan_path)
                if provider.ctx.visual_plan_path
                else {}
            )
            return json.dumps(
                {
                    "status": "ok",
                    "user_question": input_context.get(
                        "user_question", ""
                    ),
                    "problem_understanding": input_context.get(
                        "problem_understanding", ""
                    ),
                    "scope_definition": input_context.get(
                        "scope_definition", ""
                    ),
                    "research_problem_frame": provider.problem_frame,
                    "research_gap_map": provider.gap_map,
                    "shared_review_context": provider.shared_review_context,
                    "source_terminology_ledger": provider.source_terminology_ledger,
                    "review_thesis": provider.blueprint.get(
                        "review_thesis", ""
                    ),
                    "full_review_argument": provider.blueprint.get(
                        "full_review_argument", ""
                    ),
                    "methodology_identity": provider.blueprint.get(
                        "methodology_identity", ""
                    ),
                    "section_digests": provider._section_digests(),
                    "quality_gate": {
                        "status": quality.get("status", ""),
                        "blocking_issues": quality.get(
                            "blocking_issues", []
                        ),
                        "warnings": quality.get("warnings", []),
                    },
                    "literature_portfolio": {
                        key: portfolio.get(key)
                        for key in (
                            "article_unique_sources",
                            "article_direct_sources",
                            "recommended_minimum_unique_sources",
                            "article_breadth_target_met",
                            "sections_needing_expansion",
                        )
                    },
                    "visual_summary": {
                        "verified_existing": len(
                            visual.get("placements", [])
                        ),
                        "conceptual_requests": len(
                            visual.get(
                                "conceptual_figure_requests", []
                            )
                        ),
                    },
                    "allowlist": {
                        "paper_count": len(provider._paper_ids),
                        "chunk_count": len(provider._chunk_to_paper),
                        "sample_sources": list(
                            provider._source_records.values()
                        )[:24],
                    },
                    "evidence_identifier_catalog": (
                        provider._evidence_identifier_catalog()
                    ),
                    "inspection_budget": {
                        "initial_max_iters": 8,
                        "review_section_reads": 0,
                        "review_section_batch_size": (
                            provider.ctx.max_section_batch_size
                        ),
                        "initial_detail_batch_calls": (
                            provider.ctx.max_initial_detail_calls
                        ),
                        "evidence_chunks": (
                            provider.ctx.max_evidence_chunks
                        ),
                        "evidence_batch_calls": (
                            provider.ctx.max_initial_detail_calls
                        ),
                    },
                    "initial_discovery_protocol": {
                        "ordered_steps": [
                            "load_research_program_context",
                            "optional read_review_sections_batch once",
                            "optional inspect_research_evidence_batch once",
                            "submit_research_opportunity_map",
                            "submit_hypothesis_portfolio",
                            "submit_program_focus_gate",
                        ],
                        "single_section_reads_disabled_for_initial_discovery": True,
                        "section_ids_are_not_chunk_ids": True,
                        "focus_gate_required_before_plan_only": True,
                        "honest_stop_if_focus_missing": (
                            "needs_more_literature or awaiting_human"
                        ),
                    },
                    "evidence_policy": (
                        "Scientific sources establish facts and constraints. "
                        "Hypotheses may go beyond the literature only when the "
                        "inference, assumptions, alternatives, and falsification "
                        "conditions are explicit."
                    ),
                    "focus_gate_policy": (
                        "Before submitting a plan, choose one main problem, one "
                        "compatible platform, one to three dependent main hypotheses, "
                        "and explicitly route all other opportunities to future_branches."
                    ),
                    "resume_state": {
                        "opportunity_map_exists": (provider.ctx.work_dir / "RESEARCH_OPPORTUNITY_MAP.json").exists(),
                        "hypothesis_portfolio_exists": (provider.ctx.work_dir / "HYPOTHESIS_PORTFOLIO.json").exists(),
                        "focus_gate_exists": (provider.ctx.work_dir / "PROGRAM_FOCUS_GATE.json").exists(),
                        "plan_exists": (provider.ctx.work_dir / "RESEARCH_PLAN.json").exists(),
                        "plan_only_resume_supported": True,
                        "policy": "When the first three artifacts exist and the plan is missing, reuse them and write only the plan; do not regenerate opportunities, hypotheses, or focus.",
                    },
                },
                ensure_ascii=True,
            )

        def read_review_section(section_id: str) -> str:
            """Read one complete review section, with a strict read budget."""

            if provider.ctx.plan_only_resume:
                return json.dumps(
                    {
                        "status": "error",
                        "error": "plan_only_review_section_read_disabled",
                        "instruction": "Use the bounded plan-only context and submit the plan.",
                    }
                )

            return json.dumps(
                {
                    "status": "error",
                    "error": "single_section_read_disabled",
                    "instruction": (
                        "Initial discovery receives section_digests. If detail is "
                        "necessary, call read_review_sections_batch once; do not "
                        "read sections one at a time."
                    ),
                    "section_id": str(section_id or ""),
                },
                ensure_ascii=True,
            )

        def read_review_sections_batch(section_ids_json: str) -> str:
            """Read a bounded batch of complete review sections once."""

            if provider.ctx.plan_only_resume:
                return json.dumps(
                    {
                        "status": "error",
                        "error": "plan_only_review_section_read_disabled",
                        "instruction": "Use the bounded plan-only context and submit the plan.",
                    },
                    ensure_ascii=True,
                )
            # A second request is a normal model-repair reflex, not a reason
            # to spend an iteration on an avoidable budget error.  Return the
            # exact first batch with an explicit proceed signal.  This is also
            # safe when the second request contains a slightly different list:
            # the first bounded batch remains the canonical detail snapshot.
            if provider._section_batch_cache is not None:
                cached = dict(provider._section_batch_cache)
                cached.update(
                    {
                        "cached": True,
                        "proceed_signal": "submit_research_opportunity_map",
                        "instruction": (
                            "The detail batch is already available. Reuse it; "
                            "do not call this tool again. Proceed to opportunity "
                            "generation."
                        ),
                    }
                )
                return json.dumps(cached, ensure_ascii=True)
            try:
                value = json.loads(section_ids_json)
            except Exception:
                value = []
            if not isinstance(value, list):
                return json.dumps(
                    {"status": "error", "error": "section_ids must be a list"},
                    ensure_ascii=True,
                )
            requested = list(
                dict.fromkeys(str(item or "").strip() for item in value if item)
            )
            if not requested:
                return json.dumps(
                    {"status": "error", "error": "section_ids must not be empty"},
                    ensure_ascii=True,
                )
            known_section_ids = [
                str(item.get("section_id") or "")
                for item in provider._section_digests()
                if isinstance(item, dict) and str(item.get("section_id") or "")
            ]
            batch_limit = max(1, int(provider.ctx.max_section_batch_size))
            # For the usual review size (at most eight planned sections), any
            # first request is expanded to the complete digest inventory.  It
            # prevents the model from paging through S01, S02, ... one by one.
            # Larger reviews remain bounded and must request a batch explicitly.
            expanded_to_all = len(known_section_ids) <= batch_limit
            effective_requested = (
                known_section_ids if expanded_to_all else requested
            )
            if len(effective_requested) > batch_limit:
                return json.dumps(
                    {
                        "status": "error",
                        "error": "section_batch_size_exceeded",
                        "max_sections": batch_limit,
                    },
                    ensure_ascii=True,
                )
            if provider._section_batch_calls >= max(
                0, int(provider.ctx.max_initial_detail_calls)
            ):
                return json.dumps(
                    {
                        "status": "error",
                        "error": "section_batch_budget_exhausted",
                        "instruction": "Proceed to opportunity generation using the digests.",
                    },
                    ensure_ascii=True,
                )
            sections = []
            unknown = []
            for requested_id in effective_requested:
                section = next(
                    (
                        item
                        for item in provider.blueprint.get("sections", [])
                        if isinstance(item, dict)
                        and str(item.get("section_id") or "") == requested_id
                    ),
                    None,
                )
                if not section:
                    unknown.append(requested_id)
                    continue
                title = str(section.get("title") or "")
                text = provider.review_sections.get(title, "")
                sections.append(
                    {
                        "section_id": requested_id,
                        "title": title,
                        "argument_role": section.get("argument_role", ""),
                        "text": text,
                        "char_count": len(text),
                    }
                )
            provider._section_batch_calls += 1
            payload = {
                "status": "ok",
                "sections": sections,
                "unknown_section_ids": list(
                    dict.fromkeys(
                        [item for item in requested if item not in effective_requested]
                        + unknown
                    )
                ),
                "requested_section_ids": requested,
                "section_ids_used": effective_requested,
                "expanded_to_all_digest_sections": expanded_to_all,
                "batch_call_count": provider._section_batch_calls,
                "detail_budget_remaining": max(
                    0,
                    int(provider.ctx.max_initial_detail_calls)
                    - provider._section_batch_calls,
                ),
                "proceed_signal": "submit_research_opportunity_map",
                "instruction": (
                    "Use this one batch for detail verification. Do not call "
                    "single-section reads or repeat the batch. Proceed to "
                    "opportunity generation."
                ),
            }
            provider._section_batch_cache = dict(payload)
            return json.dumps(payload, ensure_ascii=True)

            if provider._section_reads >= provider.ctx.max_section_reads:
                return json.dumps(
                    {"status": "error", "error": "section_read_budget_exhausted"}
                )
            section = next(
                (
                    item
                    for item in provider.blueprint.get("sections", [])
                    if isinstance(item, dict)
                    and item.get("section_id") == section_id
                ),
                None,
            )
            if not section:
                return json.dumps(
                    {"status": "error", "error": "unknown_section_id"}
                )
            provider._section_reads += 1
            text = provider.review_sections.get(
                str(section.get("title") or ""), ""
            )
            return json.dumps(
                {
                    "status": "ok",
                    "section_id": section_id,
                    "text": text,
                    "reads_remaining": max(
                        0,
                        provider.ctx.max_section_reads
                        - provider._section_reads,
                    ),
                },
                ensure_ascii=True,
            )

        def inspect_research_evidence(chunk_ids_json: str) -> str:
            """Read evidence by canonical chunk ID or allowlisted paper ID."""

            if provider.ctx.plan_only_resume:
                return json.dumps(
                    {
                        "status": "error",
                        "error": "plan_only_evidence_inspection_disabled",
                        "instruction": "Use selected_evidence_summary; do not reopen evidence inspection.",
                    }
                )

            try:
                value = json.loads(chunk_ids_json)
            except Exception:
                value = []
            if not isinstance(value, list):
                return json.dumps(
                    {"status": "error", "error": "chunk_ids must be a list"}
                )
            requested = [str(item) for item in value]
            section_ids = [
                item for item in requested if item.strip() in provider._section_ids
            ]
            if section_ids:
                return json.dumps(
                    {
                        "status": "error",
                        "error": "section_id_used_as_chunk_id",
                        "section_ids_misused_as_chunk_ids": section_ids,
                        "instruction": (
                            "Section IDs identify review sections, not evidence. "
                            "Use canonical chunk IDs from the evidence catalog."
                        ),
                    },
                    ensure_ascii=True,
                )
            resolved, unknown, resolution = (
                provider._resolve_evidence_identifiers(requested)
            )
            chunks = provider._lookup_chunks(resolved)
            return json.dumps(
                {
                    "status": "ok",
                    "chunks": chunks,
                    "unknown_chunk_ids": unknown,
                    "resolved_identifiers": resolution,
                    "instruction": (
                        "Use returned canonical chunk_id values in all "
                        "submitted artifacts. Do not guess identifier formats."
                    ),
                    "valid_identifier_examples": (
                        provider._evidence_identifier_catalog(limit=6)
                        if unknown
                        else []
                    ),
                    "remaining_budget": max(
                        0,
                        provider.ctx.max_evidence_chunks
                        - provider._evidence_reads,
                    ),
                },
                ensure_ascii=True,
            )

        def inspect_research_evidence_batch(chunk_ids_json: str) -> str:
            """Inspect several canonical chunks in one bounded evidence call."""

            if provider.ctx.plan_only_resume:
                return json.dumps(
                    {
                        "status": "error",
                        "error": "plan_only_evidence_inspection_disabled",
                        "instruction": "Use selected_evidence_summary; do not reopen evidence inspection.",
                    },
                    ensure_ascii=True,
                )
            if provider._evidence_batch_cache is not None:
                cached = dict(provider._evidence_batch_cache)
                cached.update(
                    {
                        "cached": True,
                        "proceed_signal": "submit_research_opportunity_map",
                        "instruction": (
                            "The evidence batch was already consumed. Reuse the "
                            "cached records; do not call this tool again. Proceed "
                            "to opportunity generation."
                        ),
                    }
                )
                return json.dumps(cached, ensure_ascii=True)
            try:
                value = json.loads(chunk_ids_json)
            except Exception:
                value = []
            if not isinstance(value, list):
                return json.dumps(
                    {"status": "error", "error": "chunk_ids must be a list"},
                    ensure_ascii=True,
                )
            requested = list(
                dict.fromkeys(str(item or "").strip() for item in value if item)
            )
            if not requested:
                return json.dumps(
                    {"status": "error", "error": "chunk_ids must not be empty"},
                    ensure_ascii=True,
                )
            section_ids = [
                item for item in requested if item in provider._section_ids
            ]
            if section_ids:
                return json.dumps(
                    {
                        "status": "error",
                        "error": "section_id_used_as_chunk_id",
                        "section_ids_misused_as_chunk_ids": section_ids,
                        "instruction": (
                            "Do not convert section IDs into chunk IDs. Use the "
                            "canonical chunk IDs exposed by the evidence catalog."
                        ),
                    },
                    ensure_ascii=True,
                )
            if provider._evidence_batch_calls >= max(
                0, int(provider.ctx.max_initial_detail_calls)
            ):
                return json.dumps(
                    {
                        "status": "error",
                        "error": "evidence_batch_budget_exhausted",
                        "instruction": "Proceed using the section digests and available evidence.",
                    },
                    ensure_ascii=True,
                )
            resolved, unknown, resolution = provider._resolve_evidence_identifiers(
                requested
            )
            chunks = provider._lookup_chunks(
                resolved,
                max_items=provider.ctx.max_evidence_chunks,
            )
            provider._evidence_batch_calls += 1
            payload = {
                "status": "ok",
                "chunks": chunks,
                "unknown_chunk_ids": unknown,
                "resolved_identifiers": resolution,
                "batch_call_count": provider._evidence_batch_calls,
                "proceed_signal": "submit_research_opportunity_map",
                "instruction": (
                    "Use only returned canonical chunk_id values in submitted "
                    "artifacts. This was the single permitted batch evidence check."
                ),
            }
            provider._evidence_batch_cache = dict(payload)
            return json.dumps(payload, ensure_ascii=True)

        def submit_research_opportunity_map(opportunity_map_json: str) -> str:
            """Submit 3-8 evidence-aware research opportunities."""

            try:
                parsed = _parse_json_object(opportunity_map_json)
                if "items" in parsed and "opportunities" not in parsed:
                    parsed = {"opportunities": parsed["items"]}
                parsed = _normalize_opportunity_payload(parsed)
                for item in parsed.get("opportunities", []):
                    if not isinstance(item, dict):
                        continue
                    chunks = [
                        str(value)
                        for value in item.get(
                            "supporting_chunk_ids", []
                        )
                    ]
                    if not item.get("supporting_paper_ids"):
                        item["supporting_paper_ids"] = list(
                            dict.fromkeys(
                                provider._chunk_to_paper[chunk_id]
                                for chunk_id in chunks
                                if chunk_id in provider._chunk_to_paper
                            )
                        )
                    if not item.get("source_section_ids"):
                        item["source_section_ids"] = sorted(
                            {
                                section_id
                                for chunk_id in chunks
                                for section_id in provider._chunk_to_sections.get(
                                    chunk_id, set()
                                )
                            }
                        )
                model = ResearchOpportunityMap.model_validate(parsed)
            except Exception as exc:
                return json.dumps(
                    {"status": "error", "error": f"invalid_schema: {exc}"}
                )
            errors = provider._validate_opportunities(
                [item.model_dump() for item in model.opportunities]
            )
            if errors:
                return json.dumps({"status": "error", "errors": errors})
            atomic_write_json(
                provider.ctx.work_dir / "RESEARCH_OPPORTUNITY_MAP.json",
                model.model_dump(),
            )
            return json.dumps(
                {
                    "status": "ok",
                    "opportunity_count": len(model.opportunities),
                }
            )

        def submit_hypothesis_portfolio(hypothesis_portfolio_json: str) -> str:
            """Submit 2-6 falsifiable hypotheses derived from opportunities."""

            opportunities = _read_json(
                provider.ctx.work_dir / "RESEARCH_OPPORTUNITY_MAP.json"
            )
            valid_opportunities = {
                str(item.get("opportunity_id"))
                for item in opportunities.get("opportunities", [])
                if isinstance(item, dict)
            }
            try:
                parsed = _parse_json_object(hypothesis_portfolio_json)
                if "items" in parsed and "hypotheses" not in parsed:
                    parsed = {"hypotheses": parsed["items"]}
                parsed = _normalize_hypothesis_payload(parsed)
                parsed, readiness_audits = _calibrate_hypothesis_readiness(
                    parsed,
                    opportunities,
                    provider._permission_map(),
                )
                model = ResearchHypothesisPortfolio.model_validate(parsed)
            except Exception as exc:
                return json.dumps(
                    {"status": "error", "error": f"invalid_schema: {exc}"}
                )
            errors = provider._validate_hypotheses(
                [item.model_dump() for item in model.hypotheses],
                valid_opportunities,
            )
            if errors:
                return json.dumps({"status": "error", "errors": errors})
            atomic_write_json(
                provider.ctx.work_dir / "HYPOTHESIS_PORTFOLIO.json",
                model.model_dump(),
            )
            atomic_write_json(
                provider.ctx.work_dir / "HYPOTHESIS_READINESS_AUDIT.json",
                {
                    "schema_version": "research_harness.hypothesis_readiness_audit.v1",
                    "corrections": readiness_audits,
                    "correction_count": len(readiness_audits),
                    "policy": "Readiness may be downgraded by source permission checks, never upgraded.",
                },
            )
            return json.dumps(
                {
                    "status": "ok",
                    "hypothesis_count": len(model.hypotheses),
                    "readiness_correction_count": len(readiness_audits),
                }
            )

        def submit_program_focus_gate(program_focus_gate_json: str) -> str:
            """Submit the single-project convergence decision before the plan."""

            provider._focus_submission_count += 1
            opportunities = _read_json(
                provider.ctx.work_dir / "RESEARCH_OPPORTUNITY_MAP.json"
            )
            hypotheses = _read_json(
                provider.ctx.work_dir / "HYPOTHESIS_PORTFOLIO.json"
            )

            def focus_error_response(
                errors: List[str],
                *,
                error: str = "invalid_focus_gate",
                metrics: Optional[Dict[str, Any]] = None,
                normalization_corrections: Optional[List[Dict[str, Any]]] = None,
            ) -> str:
                normalized_errors = list(
                    dict.fromkeys(str(item) for item in errors if str(item))
                )
                signature = "|".join(sorted(normalized_errors)) or error
                if signature == provider._focus_last_error_signature:
                    provider._focus_error_repeat_count += 1
                else:
                    provider._focus_last_error_signature = signature
                    provider._focus_error_repeat_count = 1
                repeated_without_progress = provider._focus_error_repeat_count >= 2
                if repeated_without_progress:
                    provider._focus_human_stop_requested = True
                status = (
                    "awaiting_human_review"
                    if repeated_without_progress
                    else "error"
                )
                audit = {
                    "schema_version": "research_harness.focus_normalization_audit.v1",
                    "status": status,
                    "submission_count": provider._focus_submission_count,
                    "error_signature": signature,
                    "same_error_repeat_count": provider._focus_error_repeat_count,
                    "normalization_corrections": list(
                        normalization_corrections or []
                    ),
                    "errors": normalized_errors,
                    "policy": (
                        "Stop after the same deterministic focus error is "
                        "returned twice without progress."
                    ),
                }
                atomic_write_json(
                    provider.ctx.work_dir / "PROGRAM_FOCUS_NORMALIZATION_AUDIT.json",
                    audit,
                )
                return json.dumps(
                    {
                        "status": status,
                        "error": (
                            "repeated_focus_error_signature"
                            if repeated_without_progress
                            else error
                        ),
                        "errors": normalized_errors,
                        "error_signature": signature,
                        "same_error_repeat_count": provider._focus_error_repeat_count,
                        "metrics": metrics or {},
                        "instruction": (
                            "Stop focus repair and request human review."
                            if repeated_without_progress
                            else "Correct the focus gate and submit it once more."
                        ),
                    },
                    ensure_ascii=True,
                )
            try:
                parsed, normalization_corrections = (
                    _normalize_focus_gate_against_opportunities(
                        _parse_json_object(program_focus_gate_json),
                        [
                            item
                            for item in opportunities.get("opportunities", [])
                            if isinstance(item, dict)
                        ],
                        [
                            item
                            for item in hypotheses.get("hypotheses", [])
                            if isinstance(item, dict)
                        ],
                    )
                )
                # Lineage is deterministic and must not be rewritten by the
                # model.  The full source context stays on disk; only its
                # compact summary enters the submitted gate.
                parsed["source_context"] = provider.shared_review_context
                decision = ProgramFocusGate().validate_focus_decision(
                    parsed,
                    opportunities.get("opportunities", []),
                    hypotheses.get("hypotheses", []),
                    shared_context=provider.shared_review_context,
                    permission_map=provider._permission_map(),
                )
            except Exception as exc:
                return focus_error_response(
                    [f"invalid_focus_gate:{exc}"],
                    error="invalid_focus_gate",
                )
            if not decision.passed:
                repaired_gate, convergence_audit = (
                    _try_evidence_calibrated_single_spine(
                        parsed,
                        decision.errors,
                        [
                            item
                            for item in opportunities.get("opportunities", [])
                            if isinstance(item, dict)
                        ],
                        [
                            item
                            for item in hypotheses.get("hypotheses", [])
                            if isinstance(item, dict)
                        ],
                        provider._permission_map(),
                    )
                )
                if repaired_gate is not None:
                    repaired_gate["source_context"] = provider.shared_review_context
                    repaired_decision = ProgramFocusGate().validate_focus_decision(
                        repaired_gate,
                        opportunities.get("opportunities", []),
                        hypotheses.get("hypotheses", []),
                        shared_context=provider.shared_review_context,
                        permission_map=provider._permission_map(),
                    )
                    if repaired_decision.passed:
                        parsed = repaired_gate
                        decision = repaired_decision
                        if convergence_audit is not None:
                            normalization_corrections.append(convergence_audit)
                if not decision.passed:
                    return focus_error_response(
                        decision.errors[:20],
                        metrics=decision.metrics,
                        normalization_corrections=normalization_corrections,
                    )
            if (
                any(
                    item.get("field") == "focus_convergence"
                    for item in normalization_corrections
                    if isinstance(item, dict)
                )
                or _focus_traceability_has_stale_selected_ids(parsed)
            ):
                parsed, matrix_correction = _reset_focus_traceability_for_plan_rebuild(
                    parsed
                )
                if matrix_correction is not None:
                    normalization_corrections.append(matrix_correction)
            parsed["status"] = "passed"
            parsed["validation"] = decision.as_dict()
            provider._focus_last_error_signature = ""
            provider._focus_error_repeat_count = 0
            provider._focus_human_stop_requested = False
            atomic_write_json(
                provider.ctx.work_dir / "PROGRAM_FOCUS_NORMALIZATION_AUDIT.json",
                {
                    "schema_version": "research_harness.focus_normalization_audit.v1",
                    "status": "passed",
                    "submission_count": provider._focus_submission_count,
                    "same_error_repeat_count": 0,
                    "normalization_corrections": normalization_corrections,
                    "errors": [],
                    "policy": "Only deterministic bookkeeping normalization is applied before focus validation.",
                },
            )
            atomic_write_json(
                provider.ctx.work_dir / "PROGRAM_FOCUS_GATE.json",
                parsed,
            )
            return json.dumps(
                    {
                        "status": "ok",
                        "gate_id": parsed["gate_id"],
                        "project_type": parsed["project_type"],
                        "main_hypothesis_count": len(parsed["main_hypothesis_ids"]),
                        "future_branch_count": len(parsed["future_branches"]),
                        "normalization_correction_count": len(normalization_corrections),
                    }
                )

        def submit_research_plan(research_plan_json: str) -> str:
            """Submit the structured and narrative research plan."""

            if provider.ctx.plan_only_resume:
                if provider._plan_only_submission_count >= 2:
                    provider._plan_only_human_stop_requested = True
                    return (
                        "VALIDATION_AWAITING_HUMAN_REVIEW: plan-only revision "
                        "limit reached; stop model repair and request human "
                        "review before another submission."
                    )
                provider._plan_only_submission_count += 1

            opportunities = _read_json(
                provider.ctx.work_dir / "RESEARCH_OPPORTUNITY_MAP.json"
            )
            hypotheses = _read_json(
                provider.ctx.work_dir / "HYPOTHESIS_PORTFOLIO.json"
            )
            valid_opportunities = {
                str(item.get("opportunity_id"))
                for item in opportunities.get("opportunities", [])
                if isinstance(item, dict)
            }
            valid_hypotheses = {
                str(item.get("hypothesis_id"))
                for item in hypotheses.get("hypotheses", [])
                if isinstance(item, dict)
            }
            focus_gate = _read_json(
                provider.ctx.work_dir / "PROGRAM_FOCUS_GATE.json"
            )
            if not focus_gate or focus_gate.get("status") != "passed":
                return json.dumps(
                    {
                        "status": "error",
                        "errors": ["program_focus_gate_required_before_plan"],
                    }
                )
            try:
                parsed = _parse_json_object(research_plan_json)
                parsed = _normalize_plan_payload(parsed)
                # Gate-owned fields are copied into the plan so the published
                # artifact cannot drift from the decision that was validated.
                for key in (
                    "main_problem",
                    "project_type",
                    "shared_platform",
                    "boundaries",
                    "unified_evaluation",
                    "main_hypothesis_ids",
                    "future_hypothesis_ids",
                    "hypothesis_dependencies",
                    "future_branches",
                    "traceability_matrix",
                    "source_context",
                ):
                    parsed[key] = focus_gate.get(key, parsed.get(key))
                parsed["program_focus_gate_id"] = focus_gate.get("gate_id", "")
                parsed["source_limitations"] = list(
                    dict.fromkeys(
                        [
                            *parsed.get("source_limitations", []),
                            *provider.shared_review_context.get(
                                "r4_candidate_limitations", []
                            ),
                        ]
                    )
                )
                selected_hypothesis_ids = set(
                    focus_gate.get("main_hypothesis_ids", [])
                )
                hypothesis_rows = {
                    str(item.get("hypothesis_id")): item
                    for item in hypotheses.get("hypotheses", [])
                    if isinstance(item, dict)
                }
                parsed["main_hypothesis_statements"] = [
                    {
                        "hypothesis_id": hypothesis_id,
                        "title": str(hypothesis_rows[hypothesis_id].get("title") or ""),
                        "statement": str(hypothesis_rows[hypothesis_id].get("statement") or ""),
                    }
                    for hypothesis_id in focus_gate.get("main_hypothesis_ids", [])
                    if hypothesis_id in hypothesis_rows
                ]
                platform = focus_gate.get("shared_platform") or {}
                evaluation = focus_gate.get("unified_evaluation") or {}
                metric_ids = [
                    str(row.get("metric_id") or row.get("name") or "")
                    if isinstance(row, dict) else str(row)
                    for row in evaluation.get("metrics", [])
                ]
                baseline_ids = [
                    str(row.get("baseline_id") or row.get("name") or "")
                    if isinstance(row, dict) else str(row)
                    for row in evaluation.get("baselines", [])
                ]
                for package in parsed.get("work_packages", []):
                    if not isinstance(package, dict):
                        continue
                    if not package.get("platform_id"):
                        package["platform_id"] = platform.get("platform_id", "")
                    if not package.get("platform_compatibility_key"):
                        package["platform_compatibility_key"] = platform.get(
                            "compatibility_key", ""
                        )
                    if not package.get("metric_ids"):
                        package["metric_ids"] = list(metric_ids)
                    if not package.get("baseline_ids"):
                        package["baseline_ids"] = list(baseline_ids)
                parsed, package_corrections = _sanitize_plan_packages_to_focus(
                    parsed,
                    focus_gate,
                )
                if provider.ctx.plan_only_resume:
                    traceability_matrix, matrix_audit = (
                        _build_plan_only_traceability_matrix(
                            parsed,
                            focus_gate,
                            hypothesis_rows,
                        )
                    )
                    parsed["traceability_matrix"] = traceability_matrix
                    # ProgramFocusGate validates the gate-owned matrix rather
                    # than trusting a second copy carried only by the plan.
                    # Persist the deterministic normalization so the later
                    # validator sees exactly the same rows.
                    focus_gate = dict(focus_gate)
                    focus_gate["traceability_matrix"] = [
                        dict(item) for item in traceability_matrix
                    ]
                    focus_gate["normalization_audit"] = [
                        dict(item)
                        for item in (focus_gate.get("normalization_audit") or [])
                        if isinstance(item, dict)
                    ] + [matrix_audit]
                    atomic_write_json(
                        provider.ctx.work_dir / "PROGRAM_FOCUS_GATE.json",
                        focus_gate,
                    )
                    package_corrections.append(matrix_audit)
                    atomic_write_json(
                        provider.ctx.work_dir
                        / "RESEARCH_PLAN_NORMALIZATION_AUDIT.json",
                        {
                            "schema_version": "research_harness.research_plan_normalization_audit.v1",
                            "submission_attempt": provider._plan_only_submission_count,
                            "corrections": [
                                *parsed.get("normalization_audit", []),
                                *package_corrections,
                            ],
                            "policy": (
                                "Only the accepted focus-gate spine may enter "
                                "current work packages; no future branch or "
                                "unknown identifier is promoted."
                            ),
                        },
                    )
                parsed = _align_plan_readiness(parsed, hypotheses)
                parsed["narrative_markdown"] = _ensure_hypothesis_statements_in_narrative(
                    parsed.get("narrative_markdown", ""),
                    parsed["main_hypothesis_statements"],
                )
                parsed, quality_corrections, quality_errors = normalize_plan_quality(
                    parsed,
                    provider.source_terminology_ledger,
                )
                parsed.setdefault("normalization_audit", []).extend(
                    quality_corrections
                )
                if quality_errors:
                    raise ValueError(
                        "source_terminology_quality_gate: "
                        + "; ".join(quality_errors)
                    )
                if provider.ctx.plan_only_resume:
                    existing_quality_audit = _read_json(
                        provider.ctx.work_dir
                        / "RESEARCH_PLAN_NORMALIZATION_AUDIT.json"
                    ) if (
                        provider.ctx.work_dir
                        / "RESEARCH_PLAN_NORMALIZATION_AUDIT.json"
                    ).exists() else {}
                    existing_quality_audit["corrections"] = [
                        *existing_quality_audit.get("corrections", []),
                        *quality_corrections,
                    ]
                    existing_quality_audit["quality_gate"] = {
                        "status": "passed",
                        "source_terminology_ledger": (
                            provider.ctx.work_dir
                            / "SOURCE_TERMINOLOGY_LEDGER.json"
                        ).name,
                    }
                    atomic_write_json(
                        provider.ctx.work_dir
                        / "RESEARCH_PLAN_NORMALIZATION_AUDIT.json",
                        existing_quality_audit,
                    )
                model = ResearchPlan.model_validate(parsed)
            except Exception as exc:
                return json.dumps(
                    {"status": "error", "error": f"invalid_schema: {exc}"}
                )
            plan = model.model_dump()
            traceable_references = list(plan.get("reference_paper_ids") or [])
            for collection in (
                opportunities.get("opportunities", []),
                hypotheses.get("hypotheses", []),
            ):
                for item in collection:
                    if isinstance(item, dict):
                        traceable_references.extend(
                            str(value)
                            for value in item.get("supporting_paper_ids", [])
                            if value
                        )
            plan["reference_paper_ids"] = list(
                dict.fromkeys(
                    value for value in traceable_references
                    if value in provider._paper_ids
                )
            )
            plan["readiness_summary"] = _build_readiness_summary(
                plan,
                focus_gate,
            )
            rendered_plan = _render_research_plan_markdown(plan)
            errors = provider._validate_plan(
                plan,
                valid_opportunities,
                valid_hypotheses,
            )
            focus_result = ProgramFocusGate().validate_package(
                focus_gate,
                opportunities.get("opportunities", []),
                hypotheses.get("hypotheses", []),
                plan,
                shared_context=provider.shared_review_context,
                permission_map=provider._permission_map(),
            )
            errors.extend(focus_result.errors)
            errors.extend(
                _audit_rendered_plan_content(
                    plan,
                    rendered_plan,
                    provider.source_terminology_ledger,
                )
            )
            if errors:
                return json.dumps({"status": "error", "errors": errors})
            atomic_write_json(
                provider.ctx.work_dir / "RESEARCH_PLAN.json",
                plan,
            )
            atomic_write_text(
                provider.ctx.work_dir / "RESEARCH_PLAN.md",
                rendered_plan,
            )
            return json.dumps(
                {
                    "status": "ok",
                    "work_package_count": len(plan["work_packages"]),
                    "readiness_summary": plan["readiness_summary"],
                    "focus_gate_status": focus_result.status,
                }
            )

        def validate_research_program_package() -> str:
            """Independently validate traceability and research-plan quality."""

            if provider.ctx.plan_only_resume:
                provider._plan_only_validation_count += 1

            errors: List[str] = []
            warnings: List[str] = []
            opportunity_path = (
                provider.ctx.work_dir / "RESEARCH_OPPORTUNITY_MAP.json"
            )
            hypothesis_path = (
                provider.ctx.work_dir / "HYPOTHESIS_PORTFOLIO.json"
            )
            plan_path = provider.ctx.work_dir / "RESEARCH_PLAN.json"
            markdown_path = provider.ctx.work_dir / "RESEARCH_PLAN.md"
            problem_frame_path = (
                provider.ctx.work_dir / "RESEARCH_PROBLEM_FRAME.json"
            )
            gap_map_path = provider.ctx.work_dir / "RESEARCH_GAP_MAP.json"
            focus_gate_path = provider.ctx.work_dir / "PROGRAM_FOCUS_GATE.json"
            shared_context_path = provider.ctx.work_dir / "PROGRAM_SHARED_CONTEXT.json"
            for path in (
                opportunity_path,
                hypothesis_path,
                plan_path,
                markdown_path,
                problem_frame_path,
                gap_map_path,
                focus_gate_path,
                shared_context_path,
            ):
                if not path.exists():
                    errors.append(f"missing_artifact:{path.name}")
            opportunities = _read_json(opportunity_path)
            hypotheses = _read_json(hypothesis_path)
            plan = _read_json(plan_path)
            problem_frame = _read_json(problem_frame_path)
            gap_map = _read_json(gap_map_path)
            focus_gate = _read_json(focus_gate_path)
            shared_context = _read_json(shared_context_path)
            focus_result = None
            rendered_plan_text = (
                markdown_path.read_text(encoding="utf-8", errors="replace")
                if markdown_path.exists()
                else ""
            )
            if not errors:
                errors.extend(
                    provider._validate_opportunities(
                        opportunities.get("opportunities", [])
                    )
                )
                opportunity_ids = {
                    str(item.get("opportunity_id"))
                    for item in opportunities.get("opportunities", [])
                    if isinstance(item, dict)
                }
                errors.extend(
                    provider._validate_hypotheses(
                        hypotheses.get("hypotheses", []),
                        opportunity_ids,
                    )
                )
                hypothesis_ids = {
                    str(item.get("hypothesis_id"))
                    for item in hypotheses.get("hypotheses", [])
                    if isinstance(item, dict)
                }
                errors.extend(
                    provider._validate_plan(
                        plan,
                        opportunity_ids,
                        hypothesis_ids,
                        hypothesis_readiness={
                            str(item.get("hypothesis_id")): str(
                                item.get("readiness") or ""
                            )
                            for item in hypotheses.get("hypotheses", [])
                            if isinstance(item, dict)
                            and item.get("hypothesis_id")
                        },
                    )
                )
                focus_result = ProgramFocusGate().validate_package(
                    focus_gate,
                    opportunities.get("opportunities", []),
                    hypotheses.get("hypotheses", []),
                    plan,
                    shared_context=shared_context,
                    permission_map=provider._permission_map(),
                )
                errors.extend(focus_result.errors)
                narrative = rendered_plan_text
                word_count = len(narrative.split())
                if word_count < 900:
                    errors.append(
                        f"research_plan_narrative_too_short:{word_count}"
                    )
                if _CJK.search(narrative):
                    errors.append("research_plan_contains_cjk")
                if _PLACEHOLDER.search(narrative):
                    errors.append("research_plan_contains_placeholder")
                unsupported_numbers = (
                    _unsupported_narrative_quantitative_claims(
                        str(plan.get("narrative_markdown") or "")
                    )
                )
                for snippet in unsupported_numbers[:5]:
                    errors.append(
                        "unsupported_quantitative_narrative_claim:"
                        + snippet
                    )
                if not plan.get("unresolved_literature_needs"):
                    warnings.append(
                        "no_unresolved_literature_needs_declared"
                    )
                warnings.extend(audit_plan_quality_warnings(plan))
            errors.extend(
                _audit_rendered_plan_content(
                    plan,
                    rendered_plan_text,
                    provider.source_terminology_ledger,
                )
            )
            traceable_hypotheses = sum(
                bool(item.get("source_opportunity_ids"))
                for item in hypotheses.get("hypotheses", [])
                if isinstance(item, dict)
            )
            audit = {
                "schema_version": "research_harness.research_plan_audit.v1",
                "status": "passed" if not errors else "failed",
                "errors": list(dict.fromkeys(errors)),
                "warnings": list(dict.fromkeys(warnings)),
                "metrics": {
                    "opportunity_count": len(
                        opportunities.get("opportunities", [])
                    ),
                    "hypothesis_count": len(
                        hypotheses.get("hypotheses", [])
                    ),
                    "traceable_hypothesis_count": traceable_hypotheses,
                    "work_package_count": len(
                        plan.get("work_packages", [])
                    ),
                    "allowlisted_paper_count": len(provider._paper_ids),
                    "allowlisted_chunk_count": len(
                        provider._chunk_to_paper
                    ),
                    "research_problem_frame_present": bool(problem_frame),
                    "research_gap_count": int(gap_map.get("gap_count", 0) or 0),
                    "program_focus_gate_status": (
                        focus_result.status if focus_result is not None else "not_evaluated"
                    ),
                    "program_focus_gate_errors": (
                        len(focus_result.errors) if focus_result is not None else 0
                    ),
                    "program_focus_gate_metrics": (
                        focus_result.metrics if focus_result is not None else {}
                    ),
                    "shared_review_context_present": bool(shared_context),
                },
            }
            atomic_write_json(
                provider.ctx.work_dir / "RESEARCH_PLAN_AUDIT.json",
                audit,
            )
            if errors:
                message = "; ".join(list(dict.fromkeys(errors))[:12])
                if (
                    provider.ctx.plan_only_resume
                    and provider._plan_only_validation_count >= 2
                ):
                    provider._plan_only_human_stop_requested = True
                    return (
                        "VALIDATION_AWAITING_HUMAN_REVIEW: plan-only revision "
                        "limit reached; "
                        + message
                    )
                return "VALIDATION_FAILED: " + message
            return (
                "VALIDATION_PASSED: research program is traceable, "
                "falsifiable, and operationally structured."
            )

        tools = [
            FunctionTool(load_research_program_context),
            FunctionTool(read_review_section),
            FunctionTool(read_review_sections_batch),
            FunctionTool(inspect_research_evidence),
            FunctionTool(inspect_research_evidence_batch),
            FunctionTool(submit_research_opportunity_map),
            FunctionTool(submit_hypothesis_portfolio),
            FunctionTool(submit_program_focus_gate),
            FunctionTool(submit_research_plan),
            FunctionTool(validate_research_program_package),
        ]
        if provider.ctx.plan_only_resume:
            # Do not merely rely on the worker's allowlist.  The provider must
            # itself expose the smaller protocol so accidental callers cannot
            # reopen evidence archaeology during a plan-only resume.
            return [
                FunctionTool(load_research_program_context),
                FunctionTool(submit_research_plan),
                FunctionTool(validate_research_program_package),
            ]
        if provider.ctx.discovery_stage == "opportunity":
            # Initial discovery is deliberately a four-step bounded protocol:
            # load, at most one section batch, at most one evidence batch, then
            # persist the opportunity map.  Hypothesis/focus tools are not even
            # present in the toolkit.
            return [
                FunctionTool(load_research_program_context),
                FunctionTool(read_review_sections_batch),
                FunctionTool(inspect_research_evidence_batch),
                FunctionTool(submit_research_opportunity_map),
            ]
        if provider.ctx.discovery_stage == "hypothesis":
            return [
                FunctionTool(load_research_program_context),
                FunctionTool(submit_hypothesis_portfolio),
                FunctionTool(submit_program_focus_gate),
            ]
        if provider.ctx.discovery_stage == "focus":
            return [
                FunctionTool(load_research_program_context),
                FunctionTool(submit_program_focus_gate),
            ]
        return tools

    def _validate_opportunities(
        self, opportunities: List[Dict[str, Any]]
    ) -> List[str]:
        errors: List[str] = []
        if not 3 <= len(opportunities) <= 8:
            errors.append("opportunity_count_must_be_3_to_8")
        ids = []
        for item in opportunities:
            if not isinstance(item, dict):
                errors.append("opportunity_not_object")
                continue
            opportunity_id = str(item.get("opportunity_id") or "")
            ids.append(opportunity_id)
            if not _ID_PATTERNS["opportunity"].fullmatch(opportunity_id):
                errors.append(f"invalid_opportunity_id:{opportunity_id}")
            if any(
                section_id not in self._section_ids
                for section_id in item.get("source_section_ids", [])
            ):
                errors.append(
                    f"unknown_section_in_opportunity:{opportunity_id}"
                )
            unknown_papers = set(item.get("supporting_paper_ids", [])) - self._paper_ids
            unknown_chunks = set(item.get("supporting_chunk_ids", [])) - set(
                self._chunk_to_paper
            )
            if unknown_papers:
                errors.append(f"unknown_paper_in_opportunity:{opportunity_id}")
            if unknown_chunks:
                errors.append(f"unknown_chunk_in_opportunity:{opportunity_id}")
            owners = {
                self._chunk_to_paper[chunk_id]
                for chunk_id in item.get("supporting_chunk_ids", [])
                if chunk_id in self._chunk_to_paper
            }
            if owners and not owners.issubset(
                set(item.get("supporting_paper_ids", []))
            ):
                errors.append(
                    f"chunk_paper_mismatch_in_opportunity:{opportunity_id}"
                )
            if (
                item.get("evidence_status") != "open_gap"
                and not (
                    item.get("supporting_paper_ids")
                    or item.get("supporting_chunk_ids")
                )
            ):
                errors.append(
                    f"supported_opportunity_has_no_evidence:{opportunity_id}"
                )
            if len(str(item.get("author_inference") or "")) < 20:
                errors.append(
                    f"missing_author_inference:{opportunity_id}"
                )
            if _CJK.search(json.dumps(item, ensure_ascii=False)):
                errors.append(f"cjk_in_opportunity:{opportunity_id}")
        if len(ids) != len(set(ids)):
            errors.append("duplicate_opportunity_ids")
        return errors

    def _validate_hypotheses(
        self,
        hypotheses: List[Dict[str, Any]],
        valid_opportunities: set[str],
    ) -> List[str]:
        errors: List[str] = []
        if not 2 <= len(hypotheses) <= 6:
            errors.append("hypothesis_count_must_be_2_to_6")
        ids = []
        for item in hypotheses:
            if not isinstance(item, dict):
                errors.append("hypothesis_not_object")
                continue
            hypothesis_id = str(item.get("hypothesis_id") or "")
            ids.append(hypothesis_id)
            if not _ID_PATTERNS["hypothesis"].fullmatch(hypothesis_id):
                errors.append(f"invalid_hypothesis_id:{hypothesis_id}")
            if not item.get("source_opportunity_ids") or any(
                value not in valid_opportunities
                for value in item.get("source_opportunity_ids", [])
            ):
                errors.append(
                    f"invalid_hypothesis_opportunity_link:{hypothesis_id}"
                )
            if set(item.get("supporting_paper_ids", [])) - self._paper_ids:
                errors.append(f"unknown_hypothesis_paper:{hypothesis_id}")
            if set(item.get("supporting_chunk_ids", [])) - set(
                self._chunk_to_paper
            ):
                errors.append(f"unknown_hypothesis_chunk:{hypothesis_id}")
            owners = {
                self._chunk_to_paper[chunk_id]
                for chunk_id in item.get("supporting_chunk_ids", [])
                if chunk_id in self._chunk_to_paper
            }
            if owners and not owners.issubset(
                set(item.get("supporting_paper_ids", []))
            ):
                errors.append(
                    f"chunk_paper_mismatch_in_hypothesis:{hypothesis_id}"
                )
            for key in (
                "inference_chain",
                "assumptions",
                "alternative_explanations",
                "falsification_conditions",
            ):
                if not item.get(key):
                    errors.append(f"{hypothesis_id}_missing_{key}")
            if _CJK.search(json.dumps(item, ensure_ascii=False)):
                errors.append(f"cjk_in_hypothesis:{hypothesis_id}")
        if len(ids) != len(set(ids)):
            errors.append("duplicate_hypothesis_ids")
        return errors

    @staticmethod
    def _validate_plan(
        plan: Dict[str, Any],
        valid_opportunities: set[str],
        valid_hypotheses: set[str],
        hypothesis_readiness: Optional[Dict[str, str]] = None,
    ) -> List[str]:
        errors: List[str] = []
        packages = plan.get("work_packages", [])
        if len(packages) < 3:
            errors.append("research_plan_requires_at_least_3_work_packages")
        package_ids = {
            str(item.get("work_package_id"))
            for item in packages
            if isinstance(item, dict)
        }
        for item in packages:
            if not isinstance(item, dict):
                errors.append("work_package_not_object")
                continue
            package_id = str(item.get("work_package_id") or "")
            if not _ID_PATTERNS["work_package"].fullmatch(package_id):
                errors.append(f"invalid_work_package_id:{package_id}")
            if set(item.get("hypothesis_ids", [])) - valid_hypotheses:
                errors.append(f"unknown_hypothesis_in:{package_id}")
            if set(item.get("opportunity_ids", [])) - valid_opportunities:
                errors.append(f"unknown_opportunity_in:{package_id}")
            if not (
                item.get("hypothesis_ids")
                or item.get("opportunity_ids")
            ):
                errors.append(f"unlinked_work_package:{package_id}")
            if hypothesis_readiness:
                linked_readiness = [
                    hypothesis_readiness[hypothesis_id]
                    for hypothesis_id in item.get("hypothesis_ids", [])
                    if hypothesis_id in hypothesis_readiness
                ]
                if linked_readiness:
                    required = max(
                        linked_readiness,
                        key=lambda value: _READINESS_RANK.get(value, 1),
                    )
                    current = str(
                        item.get("readiness") or "needs_more_literature"
                    )
                    if (
                        _READINESS_RANK.get(current, 1)
                        < _READINESS_RANK.get(required, 1)
                    ):
                        errors.append(
                            f"readiness_outpaces_hypothesis:{package_id}"
                        )
            for key in (
                "methods",
                "expected_outputs",
                "controls_or_baselines",
                "evaluation_metrics",
                "risks",
                "stop_or_pivot_criteria",
            ):
                if not item.get(key):
                    errors.append(f"{package_id}_missing_{key}")
            if item.get("verification_status") != "verification_deferred":
                errors.append(
                    f"{package_id}_verification_status_must_be_deferred"
                )
            if not str(item.get("verification_rationale") or "").strip():
                errors.append(f"{package_id}_missing_verification_rationale")
            if any(
                dependency not in package_ids
                for dependency in item.get("dependencies", [])
            ):
                errors.append(f"unknown_dependency_in:{package_id}")
        dependencies = {
            str(item.get("work_package_id") or ""): list(
                item.get("dependencies", [])
            )
            for item in packages
            if isinstance(item, dict)
        }
        visiting: set[str] = set()
        visited: set[str] = set()

        def has_cycle(node: str) -> bool:
            if node in visiting:
                return True
            if node in visited:
                return False
            visiting.add(node)
            for dependency in dependencies.get(node, []):
                if has_cycle(str(dependency)):
                    return True
            visiting.remove(node)
            visited.add(node)
            return False

        if any(has_cycle(node) for node in dependencies):
            errors.append("work_package_dependency_cycle")
        narrative = str(plan.get("narrative_markdown") or "")
        if _CJK.search(json.dumps(plan, ensure_ascii=False)):
            errors.append("research_plan_json_contains_cjk")
        if _PLACEHOLDER.search(narrative):
            errors.append("research_plan_contains_placeholder")
        for key in (
            "paper_abstract",
            "problem_statement",
            "rationale",
            "technical_details",
            "dataset_source",
            "dataset_target",
            "methods_summary",
            "experiments",
            "expected_results",
            "verification_deferred",
        ):
            if not plan.get(key):
                errors.append(f"research_plan_missing_{key}")
        if plan.get("results_status") != "verification_deferred":
            errors.append("research_plan_results_status_must_be_deferred")
        decision_points = plan.get("human_decision_points") or []
        if not decision_points:
            errors.append("human_decision_points_missing_or_insubstantial")
        else:
            for index, point in enumerate(decision_points):
                if not _decision_point_is_substantive(point):
                    errors.append(f"human_decision_point_{index}_insubstantial")
        summary = plan.get("readiness_summary")
        if not isinstance(summary, dict) or summary.get("scope") != "current_mainline":
            errors.append("readiness_summary_scope_not_declared_as_current_mainline")
        return errors
